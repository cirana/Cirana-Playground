"""Playground sessions: which applications CIRANA hosts in tabs, and how a window gets from "launched" to "embedded".

This module is UI-free. The Playground view owns the actual ``NativeHost`` widgets and registers an *attacher*
callback; the manager decides what to launch, finds the new window on a worker thread, and hands it to the
attacher on the GUI thread (``SetParent`` must never run anywhere else).

Safety rules enforced here:
* applications that cannot work (packaged/UWP, unknown executable, console programs) are opened as normal windows
  *without ever touching them*;
* a window that appears in time but cannot be embedded stays a normal window;
* no process is ever terminated.
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

import psutil
from PySide6.QtCore import QObject, QTimer, Signal

from cirana.core import application_launcher
from cirana.core.application_registry import ApplicationRegistry
from cirana.core.background import Job, run_async
from cirana.core.models import AppEntry, LaunchResult, LaunchType
from cirana.native import win32_api as w
from cirana.native import window_embedder as embedder
from cirana.native.window_discovery import (
    WindowInfo, child_windows, describe_window, list_top_level_windows, snapshot_handles, windows_of_executable,
)
from cirana.storage.settings_manager import SettingsManager

log = logging.getLogger(__name__)

SETTLE_SECONDS = 0.5  # a window must survive this long before it is touched (apps often rebuild it while starting)
WINDOW_TIMEOUT = 15.0
EXISTING_WINDOW_AFTER = 4.0  # single-instance packaged apps (Settings...) show their existing window instead
UNKNOWN_WINDOW_REASON = "CIRANA cannot tell which window belongs to this application (its executable is unknown)."
# Start-menu entries whose AppUserModelID names a classic program that CIRANA can host.
_AUMID_PROGRAMS = {"microsoft.windows.remotedesktop": "mstsc.exe"}
# Accessibility tools run with UIAccess / special window bands and must stay where Windows puts them.
_SPECIAL_PROGRAMS = {"osk.exe", "magnify.exe", "narrator.exe", "voiceaccess.exe", "livecaptions.exe"}


class SessionState(str, Enum):
    LAUNCHING = "launching"  # waiting for the application's window
    EMBEDDED = "embedded"  # hosted in a CIRANA tab
    DETACHED = "detached"  # released by the user; running as a normal window, can be re-attached
    CLOSED = "closed"  # the application closed its window
    EXTERNAL = "external"  # could not be (or no longer is) hosted; running as a normal window
    FAILED = "failed"  # nothing usable appeared


LIVE_STATES = (SessionState.LAUNCHING, SessionState.EMBEDDED, SessionState.DETACHED)


@dataclass
class PlaygroundSession:
    id: str
    app_id: str  # registry id; "" for a window the user picked from the running-window list
    title: str
    exe: str  # normalized executable path ("" if unknown)
    state: SessionState = SessionState.LAUNCHING
    workspace_id: str = ""
    pid: int = 0
    hwnd: int = 0
    launched_by_cirana: bool = False
    message: str = ""
    warnings: list[str] = field(default_factory=list)
    icon_app: AppEntry | None = None
    docked: bool = False  # kept over the tab as its own window instead of re-parented (see window_embedder)

    @property
    def live(self) -> bool:
        return self.state in LIVE_STATES


@dataclass
class OpenOutcome:
    kind: str  # "started" | "activated" | "external" | "failed"
    message: str = ""
    session_id: str = ""


Attacher = Callable[[PlaygroundSession, WindowInfo], embedder.EmbedResult]


def norm_exe(path: str) -> str:
    return os.path.normcase(os.path.normpath(path)) if path else ""


def _aumid_program(app: AppEntry) -> str:
    """The executable behind an AppUserModelID launch, when there is one.

    Get-StartApps reports classic programs without an explicit AUMID by their full path (``C:\\WINDOWS\\System32\\
    charmap.exe``); those are ordinary Win32 programs, not packaged apps."""
    target = app.target.strip()
    if os.path.isabs(target) and target.lower().endswith(".exe"):
        return target
    program = _AUMID_PROGRAMS.get(target.lower())
    return os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32", program) if program else ""


def app_exe(app: AppEntry) -> str:
    if app.exe_path:
        return norm_exe(app.exe_path)
    if app.launch_type == LaunchType.EXECUTABLE.value:
        return norm_exe(app.target)
    if app.launch_type == LaunchType.AUMID.value:
        return norm_exe(_aumid_program(app))
    return ""


def aumid_package(app: AppEntry) -> tuple[str, str] | None:
    """(package name, publisher id) of a packaged app's AUMID ``Name_PublisherId!AppId``, else None."""
    if app.launch_type != LaunchType.AUMID.value or "!" not in app.target:
        return None
    family = app.target.split("!", 1)[0]
    if "_" not in family:
        return None
    name, publisher = family.rsplit("_", 1)
    return (name.lower(), publisher.lower()) if name and publisher else None


def in_package(exe: str, package: tuple[str, str] | None) -> bool:
    """True if ``exe`` lives in the package's install folder ``...\\<Name>_<version>_<arch>__<PublisherId>\\``."""
    if not package or not exe:
        return False
    name, publisher = package
    return any(part.startswith(name + "_") and part.endswith("__" + publisher) for part in exe.lower().split("\\"))


def embed_precheck(app: AppEntry) -> str | None:
    """Why ``app`` cannot be hosted in a tab, judged from what is known *before* launching. None = worth trying."""
    exe = app_exe(app)
    if not exe:
        return None if aumid_package(app) else UNKNOWN_WINDOW_REASON  # packaged apps are found by package
    if not exe.endswith(".exe"):
        return "Only .exe programs can be hosted (scripts and batch files run in a console window)."
    name = os.path.basename(exe)
    if name in _SPECIAL_PROGRAMS or name.startswith("unins"):
        return "Accessibility tools and uninstallers run with special rights or as a one-off; they open on their own."
    return None


def hosting_kind(app: AppEntry) -> str:
    """'tab' (re-parented), 'docked' (kept over the tab as its own window) or 'external' (own window). A classic
    program can still turn out to be docked once its window is seen (e.g. a launcher stub for a Store app)."""
    if embed_precheck(app):
        return "external"
    exe = app_exe(app)
    docked = not exe or "\\windowsapps\\" in exe or os.path.basename(exe) in embedder.DOCKED_EXECUTABLES
    return "docked" if docked else "tab"


def _frame_content_exes(info: WindowInfo) -> list[str]:
    """Images of the app inside a UWP frame (ApplicationFrameWindow belongs to ApplicationFrameHost.exe; the app's
    own CoreWindow is a child of it). EnumChildWindows and the process lookups send no messages."""
    if info.class_name != "ApplicationFrameWindow":
        return []
    exes = []
    for child in child_windows(info.hwnd):
        if w.class_name(child) == "Windows.UI.Core.CoreWindow":
            exes.append(w.process_image_path(w.window_pid(child)[0]).lower())
    return exes


def window_matches(info: WindowInfo, exe: str, pid: int = 0, package: tuple[str, str] | None = None,
                   title: str = "") -> bool:
    """Does ``info`` belong to the application? Empty criteria never match (an unknown exe must not match the many
    windows whose process cannot be inspected)."""
    # Matching the file name (not just the full path) also catches launcher stubs: on Windows 11 System32\notepad.exe
    # starts the packaged app whose real image lives under WindowsApps.
    if exe and (info.exe == exe or (info.exe_name and info.exe_name == os.path.basename(exe))):
        return True
    if pid and info.pid == pid:
        return True
    if package and (in_package(info.exe, package) or any(in_package(e, package) for e in _frame_content_exes(info))):
        return True
    if title:  # last resort (names are localized): "Hesap Makinesi", "Başlıksız - Not Defteri"
        shown, wanted = info.title.strip().casefold(), title.strip().casefold()
        return bool(wanted) and (shown == wanted or shown.endswith(" - " + wanted))
    return False


def wait_for_app_window(
    before: set[int], exe: str, pid: int, cancel: threading.Event,
    timeout: float = WINDOW_TIMEOUT, settle: float = SETTLE_SECONDS, poll: float = 0.1,
    package: tuple[str, str] | None = None, title: str = "", existing_after: float | None = None,
    exclude: set[int] | None = None,
) -> WindowInfo | None:
    """Worker-thread loop: the first new, titled window of the application that stays stable for ``settle`` seconds.

    ``existing_after``: from then on a window that already existed before the launch is accepted too (single-instance
    apps bring their existing window forward instead of opening a new one). ``exclude``: windows already in tabs.
    """
    started = time.monotonic()
    deadline = started + timeout
    seen: dict[int, float] = {}
    exclude = exclude or set()
    while not cancel.is_set() and time.monotonic() < deadline:
        now = time.monotonic()
        allow_existing = existing_after is not None and now - started >= existing_after
        for info in list_top_level_windows():
            if info.hwnd in exclude or (info.hwnd in before and not allow_existing) or not info.title.strip():
                continue
            if not window_matches(info, exe, pid, package, title):
                continue
            first = seen.setdefault(info.hwnd, now)
            if (now - first >= settle or info.hwnd in before) and w.IsWindow(info.hwnd) \
                    and not w.IsHungAppWindow(info.hwnd):
                return describe_window(info.hwnd)
        time.sleep(poll)
    return None


class PlaygroundManager(QObject):
    session_added = Signal(str)
    session_updated = Signal(str)
    session_removed = Signal(str)
    activate_requested = Signal(str)  # an existing tab should come to the front
    session_settled = Signal(str)  # a session left LAUNCHING (embedded, external or failed)
    order_requested = Signal(list)  # tab order (session ids) a restored workspace session asks the view to apply
    message = Signal(str)

    def __init__(
        self,
        registry: ApplicationRegistry,
        settings: SettingsManager,
        launcher: Callable[[AppEntry], LaunchResult] = application_launcher.launch,
    ) -> None:
        super().__init__()
        self._registry = registry
        self._settings = settings
        self._launch = launcher
        self._sessions: dict[str, PlaygroundSession] = {}
        self._order: list[str] = []
        self._jobs: dict[str, Job] = {}
        self._attacher: Attacher | None = None
        self._owner = self._process_identity(os.getpid())
        # Sessions whose (hung) window is still parented to a host although the session was detached or its tab
        # closed. Their recovery record stays until the view has verified that the window left the host.
        self._unreleased: dict[str, PlaygroundSession] = {}
        self.current_id = ""  # session whose tab is selected (kept up to date by the view)

    # -- registration / queries -----------------------------------------------------
    def set_attacher(self, attacher: Attacher) -> None:
        self._attacher = attacher

    def sessions(self) -> list[PlaygroundSession]:
        return [self._sessions[i] for i in self._order if i in self._sessions]

    def get(self, session_id: str) -> PlaygroundSession | None:
        return self._sessions.get(session_id)

    def session_for_app(self, app_id: str) -> PlaygroundSession | None:
        for session in self.sessions():
            if session.app_id != app_id or not session.live:
                continue
            if session.state is SessionState.DETACHED and not w.IsWindow(session.hwnd):
                self.update_state(session.id, SessionState.CLOSED, "The application closed its window.")
                continue
            return session
        return None

    def hosted_handles(self) -> set[int]:
        return {s.hwnd for s in self._sessions.values() if s.hwnd and s.state is SessionState.EMBEDDED}

    def set_order(self, session_ids: list[str]) -> None:
        known = [i for i in session_ids if i in self._sessions]
        self._order = known + [i for i in self._order if i not in known]

    def request_order(self, session_ids: list[str]) -> None:
        """Ask the Playground view to arrange its tabs in this order."""
        self.set_order(session_ids)
        self.order_requested.emit(list(session_ids))

    # -- opening ------------------------------------------------------------------------
    def open_app(
        self, app_id: str, workspace_id: str = "", reuse_existing: bool = False, allow_duplicate: bool = False,
        known_windows: list[WindowInfo] | None = None,
    ) -> OpenOutcome:
        """Open an application in a Playground tab (or, when that is impossible, as a normal window).

        ``reuse_existing`` hosts a window of the application that is already running instead of starting a second
        copy; ``known_windows`` is an optional pre-collected window list so the caller can enumerate off-thread.
        """
        app = self._registry.get(app_id)
        if app is None:
            return OpenOutcome("failed", "That application is no longer in the list. Refresh the application list.")

        existing = None if allow_duplicate else self.session_for_app(app_id)
        if existing is not None:
            self.activate_requested.emit(existing.id)
            return OpenOutcome("activated", f"{app.name} is already open in a tab.", existing.id)

        reason = embed_precheck(app)
        if reason:
            return self._open_external(app, reason)

        exe = app_exe(app)
        package = aumid_package(app)
        if reuse_existing and exe:  # without an executable "the running window" cannot be identified
            hosted = self.hosted_handles()
            candidates = known_windows if known_windows is not None else windows_of_executable(exe)
            for info in candidates:
                if not window_matches(info, exe):
                    continue
                if info.hwnd not in hosted:
                    session = self._new_session(app, workspace_id, launched_by_cirana=False)
                    QTimer.singleShot(0, lambda sid=session.id, i=info: self._on_window_found(sid, i))
                    return OpenOutcome("started", f"Using the running {app.name} window.", session.id)

        before = snapshot_handles()
        result = self._launch(app)
        if not result.ok:
            return OpenOutcome("failed", result.message)
        self._registry.record_launch(app_id)
        session = self._new_session(app, workspace_id, launched_by_cirana=True)
        pid = result.pid or 0
        # Packaged apps start through the shell (no pid, no executable path): find them by package, then by name.
        extra = {} if exe else {"package": package, "title": app.name, "existing_after": EXISTING_WINDOW_AFTER}
        exclude = self.hosted_handles()  # docked windows are top-level: never pick one that is already in a tab
        self._jobs[session.id] = run_async(
            lambda cancel: wait_for_app_window(before, exe, pid, cancel, exclude=exclude, **extra),
            lambda info, sid=session.id: self._on_window_found(sid, info),
            lambda err, sid=session.id: self._settle(sid, SessionState.FAILED, f"Waiting for the window failed: {err}"),
        )
        return OpenOutcome("started", f"Starting {app.name}…", session.id)

    def adopt_window(self, info: WindowInfo, workspace_id: str = "") -> OpenOutcome:
        """Host a window that is already running (chosen from the running-window list)."""
        if info.hwnd in self.hosted_handles():
            return OpenOutcome("failed", "That window is already hosted in a tab.")
        known = next((a for a in self._registry.all_apps() if info.exe and app_exe(a) == info.exe), None)
        session = self._new_session(known, workspace_id, launched_by_cirana=False, title=info.title or info.exe_name,
                                    exe=info.exe)
        QTimer.singleShot(0, lambda: self._on_window_found(session.id, info))
        return OpenOutcome("started", f"Hosting {info.title or info.exe_name}…", session.id)

    def reattach(self, session_id: str) -> embedder.EmbedResult | None:
        """Put a detached window back into its tab."""
        session = self._sessions.get(session_id)
        if session is None or self._attacher is None:
            return None
        if not w.IsWindow(session.hwnd):
            self.update_state(session_id, SessionState.CLOSED, "The application closed its window.")
            return None
        result = self._attacher(session, describe_window(session.hwnd))
        self._apply_embed_result(session, result)
        return result

    def _open_external(self, app: AppEntry, reason: str) -> OpenOutcome:
        result = self._launch(app)
        if not result.ok:
            return OpenOutcome("failed", result.message)
        self._registry.record_launch(app.id)
        return OpenOutcome("external", f"{app.name} opened in its own window: {reason}")

    # -- session bookkeeping ----------------------------------------------------------
    def _new_session(
        self, app: AppEntry | None, workspace_id: str, launched_by_cirana: bool, title: str = "", exe: str = "",
    ) -> PlaygroundSession:
        session = PlaygroundSession(
            id=uuid.uuid4().hex[:8],
            app_id=app.id if app else "",
            title=app.name if app else title,
            exe=exe or (app_exe(app) if app else ""),
            workspace_id=workspace_id,
            launched_by_cirana=launched_by_cirana,
            icon_app=app or AppEntry(
                id="window-" + hashlib.sha1(norm_exe(exe).encode("utf-8")).hexdigest()[:12], name=title, launch_type=LaunchType.EXECUTABLE.value, target=exe,
                source="user", exe_path=exe,
            ),
        )
        self._sessions[session.id] = session
        self._order.append(session.id)
        self.session_added.emit(session.id)
        return session

    def update_state(self, session_id: str, state: SessionState, message: str = "", hwnd: int | None = None) -> None:
        session = self._sessions.get(session_id)
        if session is None:
            return
        session.state = state
        session.message = message
        if hwnd is not None:
            session.hwnd = hwnd
        self._persist()
        self.session_updated.emit(session_id)

    def remove_session(self, session_id: str) -> None:
        """Forget a session (its tab was closed). The application itself is left running."""
        job = self._jobs.pop(session_id, None)
        if job:
            job.cancel()
        if self._sessions.pop(session_id, None) is not None:
            self._order = [i for i in self._order if i != session_id]
            self._persist()
            self.session_removed.emit(session_id)

    def _settle(self, session_id: str, state: SessionState, message: str) -> None:
        if session_id not in self._sessions:
            return
        self._jobs.pop(session_id, None)
        self.update_state(session_id, state, message)
        self.session_settled.emit(session_id)

    # -- window found -> attach ----------------------------------------------------------
    def _on_window_found(self, session_id: str, info: WindowInfo | None) -> None:
        session = self._sessions.get(session_id)
        if session is None:
            return
        self._jobs.pop(session_id, None)
        if info is None:
            self._settle(
                session_id, SessionState.EXTERNAL,
                f"No new window of {session.title} appeared within {int(WINDOW_TIMEOUT)} seconds. The application "
                "probably reused an existing window or is still starting; CIRANA did not touch it.",
            )
            return
        session.hwnd, session.pid = info.hwnd, info.pid
        if not session.title or session.title == session.exe:
            session.title = info.title
        if self._attacher is None:
            self._settle(session_id, SessionState.EXTERNAL, "The Playground is not available.")
            return
        try:
            result = self._attacher(session, info)
        except Exception as exc:  # noqa: BLE001 - whatever went wrong, the window must stay a normal window
            log.exception("Attaching %s failed", session.title)
            result = embedder.EmbedResult(embedder.EmbedStatus.FAILED, f"Unexpected error: {exc}")
        self._apply_embed_result(session, result)
        self.session_settled.emit(session_id)

    def _apply_embed_result(self, session: PlaygroundSession, result: embedder.EmbedResult) -> None:
        session.warnings = list(result.warnings)
        if result.ok:
            self.update_state(session.id, SessionState.EMBEDDED, "")
            return
        verb = "could not be hosted" if result.status is embedder.EmbedStatus.FAILED else "cannot be hosted"
        self.update_state(
            session.id, SessionState.EXTERNAL,
            f"{session.title} {verb}: {result.reason} It keeps running as a normal window; CIRANA left it alone.",
        )

    # -- crash recovery state ---------------------------------------------------------------
    @staticmethod
    def _process_identity(pid: int) -> dict:
        try:
            return {"pid": pid, "created": psutil.Process(pid).create_time()}
        except psutil.Error:
            return {"pid": pid, "created": 0.0}

    def hold_record(self, session_id: str) -> None:
        """Keep the session's recovery record: its window has not left the host yet (a hung application)."""
        session = self._sessions.get(session_id) or self._unreleased.get(session_id)
        if session is not None and session_id not in self._unreleased:
            self._unreleased[session_id] = session
            self._persist()

    def release_record(self, session_id: str) -> None:
        """The window was verified to have left its host (or no longer exists): the held record may go."""
        if self._unreleased.pop(session_id, None) is not None:
            self._persist()

    def unreleased_ids(self) -> list[str]:
        return list(self._unreleased)

    def _persist(self, only_unreleased: bool = False) -> None:
        """``only_unreleased``: just the held records (at shutdown, every other window was handed back)."""
        sessions = [] if only_unreleased else list(self._sessions.values())
        sessions += [s for i, s in self._unreleased.items() if only_unreleased or i not in self._sessions]
        entries = []
        for session in sessions:
            # Docked windows survive a CIRANA crash with their window intact, so they need no recovery; and for UWP
            # apps the pid is ApplicationFrameHost.exe, which hosts every UWP app: never offer to end that.
            # A held session may already say DETACHED (or be gone): its window is still in the host all the same.
            if (session.state is SessionState.EMBEDDED or session.id in self._unreleased) and session.pid \
                    and not session.docked:
                identity = self._process_identity(session.pid)
                entries.append({
                    "session": session.id, "app_id": session.app_id, "title": session.title, "exe": session.exe,
                    "pid": session.pid, "created": identity["created"], "hwnd": session.hwnd, "owner": self._owner,
                })
        # Docked windows have their taskbar button hidden while hosted: if CIRANA dies, the next start gives it back.
        docked = []
        for session in [] if only_unreleased else self._sessions.values():
            if session.state is SessionState.EMBEDDED and session.docked and session.hwnd:
                pid = w.window_pid(session.hwnd)[0]  # the window's own process (a UWP frame is not the app's pid)
                if pid:
                    docked.append({"hwnd": session.hwnd, "pid": pid, "owner": self._owner,
                                   "created": self._process_identity(pid)["created"]})
        try:
            self._settings.playground_store.save({"embedded": entries, "docked": docked})
        except (OSError, ValueError):
            log.exception("Could not write the playground session file")

    def clear_persisted_state(self) -> None:
        """Forget the crash-recovery records, except held ones: those windows are still parented to a host, so they
        stay recoverable on the next start if CIRANA ends before they are released."""
        if self._unreleased:
            self._persist(only_unreleased=True)
            return
        try:
            self._settings.playground_store.save({"embedded": []})
        except (OSError, ValueError):
            log.exception("Could not clear the playground session file")

    def shutdown(self) -> None:
        for job in self._jobs.values():
            job.cancel()
        self._jobs.clear()
