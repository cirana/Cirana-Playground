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
from cirana.native import taskbar
from cirana.native import win32_api as w
from cirana.native import window_embedder as embedder
from cirana.native.window_discovery import (
    WindowInfo, child_windows, describe_window, is_main_window_candidate, list_top_level_windows, snapshot_handles,
    windows_of_executable,
)
from cirana.storage.settings_manager import SettingsManager

log = logging.getLogger(__name__)

SETTLE_SECONDS = 0.5  # a window must survive this long before it is touched (apps often rebuild it while starting)
WINDOW_TIMEOUT = 15.0
EXISTING_WINDOW_AFTER = 4.0  # single-instance packaged apps (Settings...) show their existing window instead
MAX_WINDOW_TIMEOUT = 45.0  # a slow application keeps getting time while the process CIRANA started is still running
EXITED_GRACE = 5.0  # after the started process exited, a window may still come from a process it handed over to
TREE_REFRESH = 0.5  # seconds between process-tree / window-owner scans
RECONNECT_WAIT = 3.0  # how long a window that moved itself out of the host is waited for (to become top-level)
# A re-created window appears at once (often before the old one is destroyed). Looking only briefly keeps a window
# the user opens a little later (e.g. a new Explorer window) from being taken for it.
REPLACE_WAIT = 1.0
MAX_RECONNECTS = 2  # reconnections in a row that are lost again right away; then CIRANA stops (no endless loop)
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
    safe_mode: bool = False  # child mode proved unstable for this window: it is only docked from now on
    reconnects: int = 0  # reconnections in a row after window losses (see PlaygroundManager.window_lost)
    # Who the hosted window belongs to, recorded when it was hosted: a replacement window must match all of it.
    process_created: float = 0.0  # creation time of process ``pid`` (0: unknown; a reused pid differs)
    process_exe: str = ""  # its image path, lower-case ("" unknown: only the same process can then replace it)
    parent_pid: int = 0  # the process that started it, with its creation time and image path (0 / "": unknown)
    parent_created: float = 0.0
    parent_exe: str = ""
    window_class: str = ""
    known_windows: set[int] = field(default_factory=set)  # top-level windows that already existed then

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
    return _family_package(app.target.split("!", 1)[0])


def _family_package(family: str) -> tuple[str, str] | None:
    """(name, publisher id) of a package family name ``Name_PublisherId``."""
    if "_" not in family:
        return None
    name, publisher = family.rsplit("_", 1)
    return (name.lower(), publisher.lower()) if name and publisher else None


def alias_package(exe: str) -> tuple[str, str] | None:
    """The package behind an app execution alias (``...\\Microsoft\\WindowsApps\\wt.exe``). The alias only hands the
    request over: the window then belongs to another image of the package (WindowsTerminal.exe), often in a process
    that was already running, so neither the file name nor the process tree leads to it."""
    if not exe or "\\microsoft\\windowsapps\\" not in exe.lower():
        return None
    fields = w.read_app_alias(exe)
    return _family_package(fields[0]) if fields and fields[0] else None


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


@dataclass(frozen=True)
class NoWindow:
    """Why no window was picked. Falsy, so code that expects ``WindowInfo | None`` keeps working."""

    reason: str
    exited: bool = False  # the application's process is gone

    def __bool__(self) -> bool:
        return False


def _grow_process_tree(tree: set[int]) -> bool:
    """Add the current children of every process in ``tree``; True while any of them is still running.

    Launchers and stubs often start the real program as a child (or a grandchild) and exit; the union of every pid
    ever seen is kept, so a window opened by a child that outlived its parent still counts."""
    alive = False
    for pid in list(tree):
        try:
            proc = psutil.Process(pid)
            tree.update(child.pid for child in proc.children(recursive=True))
            alive = alive or proc.is_running()
        except psutil.Error:
            continue
    return alive


def _hosts_of(tree: set[int]) -> dict[int, set[int]]:
    """Windows of *other* processes that own a window of the started process tree, mapped to every process whose
    windows they own. A terminal owns the console window of each shell it shows (Windows Terminal hosts PowerShell
    and CMD this way), so this links a shell to the terminal window showing it without knowing any terminal by name.
    Only GetWindow / GetWindowThreadProcessId: no process is opened, no message is sent."""
    owned_by: dict[int, set[int]] = {}
    for hwnd in snapshot_handles():
        owner = int(w.GetWindow(hwnd, w.GW_OWNER) or 0)
        if owner:
            owned_by.setdefault(owner, set()).add(w.window_pid(hwnd)[0])
    return {owner: pids for owner, pids in owned_by.items() if pids & tree and w.window_pid(owner)[0] not in tree}


def _has_title_bar(hwnd: int) -> bool:
    return int(w.GetWindowLongPtrW(hwnd, w.GWL_STYLE)) & w.WS_CAPTION == w.WS_CAPTION


def wait_for_app_window(
    before: set[int], exe: str, pid: int, cancel: threading.Event,
    timeout: float = WINDOW_TIMEOUT, settle: float = SETTLE_SECONDS, poll: float = 0.1,
    package: tuple[str, str] | None = None, title: str = "", existing_after: float | None = None,
    exclude: set[int] | None = None, max_timeout: float = MAX_WINDOW_TIMEOUT,
) -> WindowInfo | NoWindow | None:
    """Worker-thread loop: the first new window of the application that stays stable for ``settle`` seconds.

    A window belongs to the application when it is shown by the started process or one of its descendants, by a
    window of another process that owns one of theirs (a terminal showing a console), by the application's package,
    or by its executable (full path; the file name or the title only for titled windows). Untitled windows need a
    strong link and a title bar (an untitled splash screen is not the application's window).

    ``existing_after``: from then on a window that already existed before the launch is accepted too (single-instance
    packaged apps bring their existing window forward). A classic program's existing window is only accepted when the
    started process exited and that window came to the front. ``exclude``: windows already in tabs. Returns
    :class:`NoWindow` with the reason when it gives up, None when cancelled.
    """
    started = time.monotonic()
    seen: dict[int, float] = {}
    exclude = exclude or set()
    tree = {pid} if pid else set()
    alive, exited_at, hosts, next_tree = bool(pid), None, {}, 0.0
    name = os.path.basename(exe) if exe else (title or "The application")
    grace = timeout if package else EXITED_GRACE  # an alias / packaged app hands over to another process
    while not cancel.is_set():
        now = time.monotonic()
        if tree and now >= next_tree:
            alive = _grow_process_tree(tree)
            hosts = _hosts_of(tree)
            next_tree = now + TREE_REFRESH
            if not alive and exited_at is None:
                exited_at = now
        limit = max_timeout if alive else timeout
        if now - started >= limit:
            if alive:
                return NoWindow(f"{name} is still running but showed no window within {int(limit)} seconds. "
                                "CIRANA did not touch it.")
            return NoWindow(f"No new window of {name} appeared within {int(limit)} seconds. The application probably "
                            "reused an existing window or is still starting; CIRANA did not touch it.")
        allow_existing = existing_after is not None and now - started >= existing_after
        foreground = int(w.GetForegroundWindow() or 0)
        for info in list_top_level_windows():
            if info.hwnd in exclude:
                continue
            shown_by_host = info.hwnd in hosts
            if shown_by_host and (info.hwnd in before or hosts[info.hwnd] - tree - {info.pid}):
                return NoWindow(f"{name} opened as a tab in a terminal window that also shows other programs. Tabs "
                                "are not separate windows, so CIRANA left it alone. To host it, let the terminal open "
                                "new windows instead of tabs.")
            if info.hwnd in before and not allow_existing:
                # a single-instance program: its launcher exited and the running copy brought its window forward
                if not (exited_at is not None and exe and info.exe == exe and info.hwnd == foreground):
                    continue
            strong = shown_by_host or info.pid in tree or bool(exe and info.exe == exe) \
                or bool(package and window_matches(info, "", 0, package))
            if not strong and not window_matches(info, exe, 0, None, title):
                continue
            untitled = not info.title.strip()
            if untitled and not (strong and _has_title_bar(info.hwnd)):
                continue
            first = seen.setdefault(info.hwnd, now)
            if (now - first >= settle * (2 if untitled else 1) or info.hwnd in before) and w.IsWindow(info.hwnd) \
                    and not w.IsHungAppWindow(info.hwnd):
                return describe_window(info.hwnd)
        if exited_at is not None and now - exited_at >= grace:
            return NoWindow(f"{name} exited without opening a window of its own. It probably passed the request to a "
                            "copy that was already running, which did not bring a window to the front, so CIRANA could "
                            "not tell which window it is and left everything alone.")
        time.sleep(poll)
    return None


def process_started(pid: int) -> float:
    """Creation time of process ``pid`` (0.0 when it is gone or cannot be inspected). With the pid it identifies one
    process: a pid that Windows hands out again belongs to a process with another creation time."""
    try:
        return psutil.Process(pid).create_time() if pid else 0.0
    except psutil.Error:
        return 0.0


@dataclass(frozen=True)
class ProcessFacts:
    created: float
    exe: str  # lower-case image path ("" when it cannot be read)
    parent: int  # the pid recorded as its parent when it was started (that process may be gone by now)


def process_facts(pid: int) -> ProcessFacts | None:
    """Creation time, image path and parent pid of a process (None when it is gone or cannot be inspected)."""
    try:
        proc = psutil.Process(pid)
        with proc.oneshot():
            created, parent = proc.create_time(), proc.ppid()
    except psutil.Error:
        return None
    return ProcessFacts(created, w.process_image_path(pid).lower(), parent)


def _still_or_was(pid: int, created: float, child_created: float) -> bool:
    """``pid`` (recorded as the parent of a process started at ``child_created``) is the process created at
    ``created``: it still runs with that creation time, or it is gone and the child started after it (a pid that
    named a newer process would have a later creation time while it runs)."""
    now = process_started(pid)
    return now == created if now else bool(created) and child_created >= created


TREE_DEPTH = 6  # how far up a candidate's parent chain is followed


def process_relation(session: PlaygroundSession, pid: int, exe: str) -> str:
    """How process ``pid`` (image ``exe``) belongs to the hosted window's application; "" when not reliably.

    Some programs open their new window in another process (Electron / Chromium-style apps, programs that restart
    themselves). Another process only counts when it runs the *same executable* and is tied to the hosted process by
    the verified process tree (each parent's creation time checked, so a reused pid breaks the chain): its child or
    grandchild, its parent, or a sibling started by a parent that is the same program too. A sibling started by a
    shell or by CIRANA (any program's parent) is never enough, nor is the executable alone."""
    if pid == session.pid:
        return "the same process" if process_started(pid) == session.process_created else ""
    if not pid or pid == os.getpid() or not session.process_exe or exe != session.process_exe:
        return ""
    facts = process_facts(pid)
    if facts is None or facts.exe != session.process_exe:
        return ""
    if session.parent_pid and pid == session.parent_pid and facts.created == session.parent_created:
        return "its parent process"
    if session.parent_pid and facts.parent == session.parent_pid and session.parent_exe == session.process_exe \
            and _still_or_was(session.parent_pid, session.parent_created, facts.created):
        return "a sibling process of the same program"
    child_created, current = facts.created, facts.parent
    for _ in range(TREE_DEPTH):
        if not current or current == os.getpid():
            break
        if current == session.pid:
            return "a child process" if _still_or_was(session.pid, session.process_created, child_created) else ""
        parent = process_facts(current)
        if parent is None or parent.created > child_created:
            break  # gone, or the pid names a newer process now: the chain cannot be followed reliably
        child_created, current = parent.created, parent.parent
    return ""


def find_replacement_window(
    session: PlaygroundSession, old_hwnd: int, escaped: bool, exclude: set[int], cancel: threading.Event,
    timeout: float = RECONNECT_WAIT, poll: float = 0.2,
) -> WindowInfo | NoWindow | None:
    """Worker-thread: the window that took the place of a lost hosted window, or why there is none.

    There is no creation time for a window in Win32, so a replacement must match everything else that is known: a
    process of the same application (the same process, pid *and* creation time, or one tied to it by the verified
    process tree and the same executable, see process_relation), the same window class, a real main window, not
    hosted already, and new (absent from the top-level windows that existed when the lost one was hosted, which rules
    out the application's other windows). Exactly one such window must be there on two polls in a row; with more than
    one, none is taken. ``escaped``: the lost window itself left the host and is the candidate, once it is top-level.
    The hosted process may have ended (a program that restarted itself); then only related processes can match."""
    deadline = time.monotonic() + timeout
    pid, seen = session.pid, 0
    while not cancel.is_set():
        gone = not session.process_created or process_started(pid) != session.process_created
        if escaped:
            if gone:
                return NoWindow("Its process has ended.", exited=True)
            if w.IsWindow(old_hwnd) and w.window_pid(old_hwnd)[0] == pid and embedder.is_top_level(old_hwnd) \
                    and is_main_window_candidate(old_hwnd) and old_hwnd not in exclude:
                return describe_window(old_hwnd)
        else:
            relation: dict[int, str] = {}  # per process, worked out once per poll
            found = [i for i in list_top_level_windows() if i.class_name == session.window_class
                     and i.hwnd != old_hwnd and i.hwnd not in session.known_windows and i.hwnd not in exclude]
            for info in found:
                if info.pid not in relation:
                    relation[info.pid] = process_relation(session, info.pid, info.exe)
            found = [i for i in found if relation[i.pid]]
            if len(found) > 1:
                return NoWindow(f"It opened {len(found)} new windows of the same kind; CIRANA cannot tell which one "
                                "replaces the hosted one, so it took none.")
            if found and found[0].hwnd == seen:
                log.info("Replacement window %#x of %s found in %s (pid %d)", found[0].hwnd, session.title,
                         relation[found[0].pid], found[0].pid)
                return found[0]
            seen = found[0].hwnd if found else 0
        if time.monotonic() >= deadline:
            return NoWindow("", exited=gone)
        cancel.wait(poll)
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
        # Programs whose window did not stay put in child mode during this run: their next windows are docked.
        self._unstable_child: set[str] = set()

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
        package = aumid_package(app) or alias_package(exe)
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
        # An alias's package helps find a *new* window; its already open windows (the user's own Terminal) never count.
        extra = {"package": package} if exe else {"package": package, "title": app.name,
                                                  "existing_after": EXISTING_WINDOW_AFTER}
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

    def dock_only(self, session: PlaygroundSession) -> bool:
        """Safe mode: child mode already failed for this window, or for this program earlier in this run."""
        return session.safe_mode or bool(session.exe and session.exe in self._unstable_child)

    def window_lost(self, session_id: str, reason: str, unstable: bool) -> None:
        """The hosted window closed ("closed") or left its host on its own ("escaped"); ``unstable``: within the
        observation time right after it was hosted, which counts as a failure of the hosting mode.

        While the same process still runs, its replacement is looked for briefly and reconnected automatically: the
        window itself when it escaped, a re-created one otherwise (see find_replacement_window for what must match).
        After an unstable loss the window is only docked from then on (safe mode). Losses in a row are capped by
        MAX_RECONNECTS; then, and whenever no replacement is certain, the session ends as a normal window."""
        session = self._sessions.get(session_id)
        if session is None:
            return
        escaped = reason == "escaped" and not session.docked  # a docked handle naming another window: it is gone
        if unstable and not session.docked:  # child mode did not hold for this window: never try it on it again
            session.safe_mode = True
            if session.exe:
                self._unstable_child.add(session.exe)
        session.reconnects = session.reconnects + 1 if unstable else 1
        if not session.process_created:  # who the window belonged to is unknown: nothing can be verified
            self._end_lost(session, escaped, exited=False)
            return
        if process_started(session.pid) != session.process_created and (escaped or not session.process_exe):
            # Ended. A program that restarted itself is still looked for below, among the processes tied to it.
            self._end_lost(session, escaped, exited=True)
            return
        if session.reconnects > MAX_RECONNECTS:
            self._end_lost(session, escaped, exited=False, detail=(
                f"It lost its window again right after being hosted, so CIRANA stopped reconnecting it after "
                f"{MAX_RECONNECTS} attempts; it keeps running as a normal window."))
            return
        if not escaped and any(s.state is SessionState.LAUNCHING for s in self._sessions.values()):
            # Another application is being opened right now: its new window could be taken for a replacement.
            self.update_state(session_id, SessionState.CLOSED, f"{session.title} closed its window. CIRANA did not "
                                                               "look for a replacement while another application was "
                                                               "opening.")
            return
        # The lost window itself is no longer hosted (its session is still EMBEDDED until the state below changes).
        old_hwnd, exclude = session.hwnd, self.hosted_handles() - {session.hwnd}
        wait = RECONNECT_WAIT if escaped else REPLACE_WAIT
        self._jobs[session_id] = run_async(
            lambda cancel: find_replacement_window(session, old_hwnd, escaped, exclude, cancel, timeout=wait),
            lambda info, sid=session_id: self._on_replacement(sid, info, escaped),
            lambda err, sid=session_id: self._on_replacement(sid, NoWindow(f"The search failed: {err}"), escaped),
        )
        what = "moved its window out of CIRANA" if escaped else "replaced its window"
        self.update_state(session_id, SessionState.LAUNCHING, f"{session.title} {what}; reconnecting…")

    def _end_lost(self, session: PlaygroundSession, escaped: bool, exited: bool, detail: str = "") -> None:
        """No reconnection: a closed window (or ended process) is CLOSED, a window that still exists EXTERNAL."""
        if escaped and not exited:
            state = SessionState.EXTERNAL
            message = f"{session.title} moved its window out of CIRANA on its own; it is a normal window now."
        elif detail and not exited:  # the process still runs and has a window CIRANA did not (or may not) take
            state, message = SessionState.EXTERNAL, f"{session.title} replaced its window."
        else:
            state, message = SessionState.CLOSED, f"{session.title} closed its window."
            if exited and session.safe_mode and not session.docked and session.reconnects == 1:
                message += (" It ended right after it was placed in the tab, so CIRANA docks this program instead "
                            "from now on.")
        self.update_state(session.id, state, f"{message} {detail}".strip())

    def _on_replacement(self, session_id: str, info: WindowInfo | NoWindow | None, escaped: bool) -> None:
        session = self._sessions.get(session_id)
        if session is None or info is None:  # tab closed, or the search was cancelled
            return
        if not info:
            self._jobs.pop(session_id, None)
            self._end_lost(session, escaped, info.exited, "" if info.exited else info.reason)  # "": none came
            return
        self._on_window_found(session_id, info)
        if session.state is SessionState.EMBEDDED:
            how = "docked it over the tab (safe mode)" if session.docked and session.safe_mode else "hosted it again"
            what = "moved its window out of CIRANA" if escaped else "replaced its window"
            session.warnings.append(f"{session.title} {what}; CIRANA verified that the window belongs to the same "
                                    f"process and {how}.")
            self.session_updated.emit(session_id)

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

    def cancel_search(self, session_id: str, message: str) -> bool:
        """Stop looking for a session's window (the caller gave up waiting). The search is cancelled first, so a late
        result can never attach a window to this session (or to anything else); the session settles as a normal
        window. False when no search was running."""
        job = self._jobs.get(session_id)
        if job is None:
            return False
        job.cancel()
        self._settle(session_id, SessionState.EXTERNAL, message)
        return True

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
        if not info:  # None or NoWindow (which says why)
            self._settle(
                session_id, SessionState.EXTERNAL,
                info.reason if isinstance(info, NoWindow) else
                f"No new window of {session.title} appeared within {int(WINDOW_TIMEOUT)} seconds. The application "
                "probably reused an existing window or is still starting; CIRANA did not touch it.",
            )
            return
        if info.hwnd in self.hosted_handles():  # two searches can find the same window: it is never hosted twice
            self._settle(session_id, SessionState.EXTERNAL, f"The window CIRANA found for {session.title} is already "
                                                            "hosted in another tab; it was left there.")
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
            if session.hwnd:  # who the window belongs to, for recognising a replacement (find_replacement_window)
                session.process_created = process_started(session.pid)
                facts = process_facts(session.pid)
                parent = process_facts(facts.parent) if facts and facts.parent else None
                # The parent only counts while it is the process that started this one (not a newer one, same pid).
                if parent is not None and facts is not None and parent.created > facts.created:
                    parent = None
                session.process_exe = facts.exe if facts else ""
                session.parent_pid = facts.parent if facts and parent else 0
                session.parent_created, session.parent_exe = (parent.created, parent.exe) if parent else (0.0, "")
                session.window_class = w.class_name(session.hwnd)
                session.known_windows = snapshot_handles()
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
        try:
            self._settings.playground_store.save({"embedded": entries, "docked": self._taskbar_records()})
        except (OSError, ValueError):
            log.exception("Could not write the playground session file")

    def clear_persisted_state(self) -> None:
        """Forget the crash-recovery records, except held ones: those windows are still parented to a host, so they
        stay recoverable on the next start if CIRANA ends before they are released."""
        taskbar.retry_pending()  # buttons whose AddTab failed earlier get another chance before the records are written
        if self._unreleased:
            self._persist(only_unreleased=True)
            return
        try:
            self._settings.playground_store.save({"embedded": [], "docked": self._taskbar_records()})
        except (OSError, ValueError):
            log.exception("Could not clear the playground session file")

    def _taskbar_records(self) -> list[dict]:
        """Windows whose taskbar button is still missing because of CIRANA (hidden while docked, or not given back
        yet because AddTab failed). If CIRANA dies, the next start gives them back."""
        return [{**record, "owner": self._owner} for record in taskbar.records()]

    def shutdown(self) -> None:
        for job in self._jobs.values():
            job.cancel()
        self._jobs.clear()
