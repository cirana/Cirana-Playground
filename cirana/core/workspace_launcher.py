"""Opening a workspace in one click, and capturing its session.

Opening is sequential on purpose (one application at a time, each waiting until its window settled), which keeps
window-to-application attribution reliable and the UI responsive. Applications that are already running are never
started a second time: a hosted tab is activated, a running window is adopted into a tab or brought to the front.
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field

from PySide6.QtCore import QObject, QTimer, Signal

from cirana.core import application_launcher
from cirana.core.application_registry import ApplicationRegistry
from cirana.core.background import run_async
from cirana.core.models import AppEntry, LaunchResult
from cirana.core.playground import PlaygroundManager, SessionState, app_exe, embed_precheck, wait_for_app_window
from cirana.core.workspaces import MODE_EMBEDDED, SavedLayout, Workspace, WorkspaceManager
from cirana.native import window_embedder as embedder
from cirana.native.window_discovery import WindowInfo, list_top_level_windows, snapshot_handles
from cirana.native.window_restorer import capture_state, place_window

log = logging.getLogger(__name__)

SETTLE_WAIT = 22.0  # never wait longer than this for one application before moving on
STEP_DELAY_MS = 150


@dataclass
class LaunchSummary:
    workspace: str
    in_tabs: list[str] = field(default_factory=list)
    opened: list[str] = field(default_factory=list)  # started as normal windows
    already_open: list[str] = field(default_factory=list)  # not started again
    missing: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    own_window: list[tuple[str, str]] = field(default_factory=list)  # wanted a tab, opened as a normal window

    @property
    def started_tabs(self) -> bool:
        return bool(self.in_tabs)

    def message(self) -> str:
        parts = []
        if self.in_tabs:
            parts.append(f"{len(self.in_tabs)} in tabs")
        if self.opened or self.own_window:
            parts.append(f"{len(self.opened) + len(self.own_window)} in their own window")
        if self.already_open:
            parts.append(f"{len(self.already_open)} already open")
        if self.missing:
            parts.append(f"{len(self.missing)} not found ({', '.join(self.missing)})")
        if self.failed:
            parts.append(f"{len(self.failed)} failed ({', '.join(name for name, _ in self.failed)})")
        return f"{self.workspace}: " + (", ".join(parts) if parts else "nothing to open")


def _windows_of(windows: list[WindowInfo], exe: str) -> list[WindowInfo]:
    if not exe:
        return []
    name = os.path.basename(exe)
    return [w for w in windows if w.exe == exe or w.exe_name == name]


class WorkspaceLauncher(QObject):
    progress = Signal(str)
    finished = Signal(object)  # LaunchSummary

    def __init__(
        self,
        registry: ApplicationRegistry,
        workspaces: WorkspaceManager,
        playground: PlaygroundManager,
        launcher=application_launcher.launch,
    ) -> None:
        super().__init__()
        self._registry = registry
        self._workspaces = workspaces
        self._playground = playground
        self._launch = launcher
        self._busy = False
        self._workspace: Workspace | None = None
        self._queue: list = []
        self._windows: list[WindowInfo] = []
        self._summary = LaunchSummary("")
        self._waiting = ""
        self._wait_names: dict[str, str] = {}
        self._wait_timer = QTimer(self, singleShot=True, interval=int(SETTLE_WAIT * 1000))
        self._wait_timer.timeout.connect(self._wait_timed_out)
        playground.session_settled.connect(self._on_settled)

    @property
    def busy(self) -> bool:
        return self._busy

    # -- opening -------------------------------------------------------------------------------
    def open(self, workspace_id: str) -> bool:
        """Make the workspace active and open its applications. False when another open is still running."""
        workspace = self._workspaces.get(workspace_id)
        if self._busy or workspace is None:
            return False
        self._busy = True
        self._workspace = workspace
        self._summary = LaunchSummary(workspace.name)
        self._workspaces.set_active(workspace.id)  # new tabs belong to this workspace
        layout = workspace.layout
        order = {app_id: i for i, app_id in enumerate(layout.tab_order)} if layout else {}
        # Hosted applications come first, in their saved tab order; the rest keep the list order.
        self._queue = sorted(
            enumerate(workspace.apps),
            key=lambda pair: (order.get(pair[1].app_id, len(order) + pair[0]),),
        )
        self._queue = [item for _i, item in self._queue]
        self.progress.emit(f"Opening {workspace.name}…")
        run_async(lambda _cancel: list_top_level_windows(), self._begin, lambda _err: self._begin([]))
        return True

    def _begin(self, windows: list[WindowInfo]) -> None:
        self._windows = windows or []
        self._next()

    def _next(self) -> None:
        if not self._queue:
            self._finish()
            return
        item = self._queue.pop(0)
        try:
            waiting = self._open_item(item)
        except Exception as exc:  # noqa: BLE001 - one broken application must not stop the rest
            log.exception("Opening %s failed", item.name)
            self._summary.failed.append((item.name, str(exc)))
            waiting = None
        if waiting:
            self._waiting = waiting
            self._wait_names[waiting] = self._workspaces.display_name(item)
            self._wait_timer.start()
        else:
            QTimer.singleShot(STEP_DELAY_MS, self._next)

    def _open_item(self, item) -> str | None:
        """Open one application. Returns a session id to wait for, or None when the step is already complete."""
        summary, workspace = self._summary, self._workspace
        app = self._registry.get(item.app_id)
        if app is None:
            summary.missing.append(item.name)
            return None
        exe = app_exe(app)
        running = _windows_of(self._windows, exe)
        hosted_reason = embed_precheck(app)
        wants_tab = item.mode == MODE_EMBEDDED

        if wants_tab and hosted_reason is None:
            outcome = self._playground.open_app(
                app.id, workspace.id, reuse_existing=True, known_windows=running,
            )
            if outcome.kind == "started":
                return outcome.session_id
            if outcome.kind == "activated":
                summary.already_open.append(app.name)
            elif outcome.kind == "failed":
                summary.failed.append((app.name, outcome.message))
            return None

        # Normal window: do not start a second copy of something that is already open.
        if running:
            embedder.bring_to_front(running[0].hwnd)
            summary.already_open.append(app.name)
            return None
        before = snapshot_handles()
        result: LaunchResult = self._launch(app)
        if not result.ok:
            summary.failed.append((app.name, result.message))
            return None
        self._registry.record_launch(app.id)
        if wants_tab:
            summary.own_window.append((app.name, hosted_reason or ""))
        else:
            summary.opened.append(app.name)
        self._restore_placement(app, exe, before, result.pid or 0)
        return None

    def _restore_placement(self, app: AppEntry, exe: str, before: set[int], pid: int) -> None:
        layout = self._workspace.layout if self._workspace else None
        placement = layout.windows.get(app.id) if layout else None
        if not placement or not exe:
            return

        def job(cancel: threading.Event) -> bool:
            info = wait_for_app_window(before, exe, pid, cancel, timeout=12.0, settle=0.3)
            return bool(info and place_window(info.hwnd, tuple(placement["rect"]), bool(placement.get("maximized"))))

        run_async(job)  # fire and forget: a window that never appears simply stays where Windows put it

    # -- progress -------------------------------------------------------------------------------------
    def _on_settled(self, session_id: str) -> None:
        if session_id != self._waiting:
            return
        self._wait_timer.stop()
        self._waiting = ""
        session = self._playground.get(session_id)
        name = self._wait_names.pop(session_id, session.title if session else "")
        if session is not None:
            if session.state is SessionState.EMBEDDED:
                self._summary.in_tabs.append(name)
            else:
                self._summary.own_window.append((name, session.message))
        QTimer.singleShot(STEP_DELAY_MS, self._next)

    def _wait_timed_out(self) -> None:
        if self._waiting:
            self._summary.failed.append((self._wait_names.pop(self._waiting, "an application"), "no window appeared in time"))
            self._waiting = ""
            self._next()

    def _finish(self) -> None:
        workspace, summary = self._workspace, self._summary
        layout = workspace.layout if workspace else None
        if workspace is not None and layout is not None:
            wanted = [self._playground.session_for_app(app_id) for app_id in layout.tab_order]
            ids = [s.id for s in wanted if s is not None and s.workspace_id == workspace.id]
            if ids:
                self._playground.request_order(ids)
            active = self._playground.session_for_app(layout.active) if layout.active else None
            if active is not None and active.workspace_id == workspace.id:
                self._playground.activate_requested.emit(active.id)
        self._busy = False
        self._workspace = None
        self.finished.emit(summary)


# -- session capture ------------------------------------------------------------------------------------------
def external_placements(apps: list[AppEntry], cancel: threading.Event | None = None) -> dict[str, dict]:
    """Worker-thread: where the open normal windows of ``apps`` are right now (only windows that can be found)."""
    windows = list_top_level_windows()
    found: dict[str, dict] = {}
    for app in apps:
        if cancel is not None and cancel.is_set():
            break
        matches = _windows_of(windows, app_exe(app))
        if matches:
            state = capture_state(matches[0].hwnd)
            found[app.id] = {"rect": list(state.rect), "maximized": state.maximized}
    return found


def build_layout(
    workspace: Workspace, playground: PlaygroundManager, registry: ApplicationRegistry, placements: dict[str, dict],
) -> tuple[SavedLayout, list[tuple[str, str]]]:
    """Snapshot of this workspace: hosted tab order + active tab, window placements, newly seen applications."""
    sessions = [s for s in playground.sessions() if s.live and s.workspace_id == workspace.id and s.app_id]
    tab_order = [s.app_id for s in sessions]
    current = playground.get(playground.current_id) if playground.current_id else None
    active = current.app_id if current and current.app_id in tab_order else ""
    new_apps = [(s.app_id, MODE_EMBEDDED) for s in sessions if not workspace.has(s.app_id) and registry.get(s.app_id)]
    hosted = set(tab_order)
    windows = {app_id: placement for app_id, placement in placements.items() if app_id not in hosted}
    return SavedLayout(tab_order=tab_order, active=active, windows=windows), new_apps
