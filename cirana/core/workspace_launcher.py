"""Starting a workspace's missing applications and finding the windows they open.

Several applications start at once. Each launch is remembered with the windows that already existed, the process
CIRANA started and that process's descendants. A new window is attributed, in this order, to

1. the launch whose process tree shows it (launchers and stubs often hand over to a child and exit),
2. the launch whose process tree it is a *host* for (a terminal owning the console window of the shell it shows:
   PowerShell or CMD inside Windows Terminal),
3. the launch whose application rule matches it (packaged apps started through the shell have no process to follow;
   single-instance programs open the window in their already running process).

When several launches could take the same window, the earliest one gets it (two copies of one program started
together are interchangeable). Every window is given to one launch only. A launch that sees no new window but whose
process handed over to a running copy ends as "handed over": the workspace controller then looks among the existing
windows (and asks the user when several fit).

A workspace activated again while its applications are still starting keeps waiting for them (nothing is started
twice). Cancelling (closing the workspace) bumps its generation: results of the old launches (late windows, late
launch results) are then dropped.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable

import psutil
from PySide6.QtCore import QObject, QTimer, Signal

from cirana.core import application_launcher
from cirana.core.application_registry import ApplicationRegistry
from cirana.core.background import run_async
from cirana.core.models import AppEntry, LaunchResult
from cirana.wm.identity import MatchRule, TrackedWindow
from cirana.wm.tracker import WindowTracker

log = logging.getLogger(__name__)

WINDOW_TIMEOUT = 30.0  # no window after this long: give up (CIRANA never touches the process)
ALIVE_TIMEOUT = 60.0  # ... unless the started process still runs (slow first starts)
HANDOVER_AFTER = 4.0  # started process exited without a window: it handed the request to a running copy
TICK_MS = 400


@dataclass
class PendingLaunch:
    workspace_id: str
    entry_id: str
    name: str
    rule: MatchRule
    generation: int
    before: frozenset[str]  # window keys that existed when it was started
    started: float = field(default_factory=time.monotonic)
    pid: int = 0
    tree: set[int] = field(default_factory=set)
    hosts: set[int] = field(default_factory=set)  # windows of other programs showing a process of the tree
    alive: bool = True
    exited_at: float | None = None
    launched: bool = False  # the launch call returned (the process exists)


@dataclass
class LaunchOutcome:
    workspace_id: str
    entry_id: str
    name: str
    kind: str  # "window" | "handed_over" | "failed"
    window: TrackedWindow | None = None
    message: str = ""


def grow_tree(tree: set[int]) -> bool:
    """Add the current children of every process in ``tree``; True while any of them still runs."""
    alive = False
    for pid in list(tree):
        try:
            proc = psutil.Process(pid)
            tree.update(child.pid for child in proc.children(recursive=True))
            alive = alive or proc.is_running()
        except psutil.Error:
            continue
    return alive


class WorkspaceLauncher(QObject):
    outcome = Signal(object)  # LaunchOutcome, one per started entry
    finished = Signal(str)  # workspace id: none of its launches is pending any more

    def __init__(self, registry: ApplicationRegistry, tracker: WindowTracker,
                 launcher: Callable[[AppEntry], LaunchResult] = application_launcher.launch,
                 process_tree: Callable[[set[int]], bool] = grow_tree,
                 hosts: Callable[[set[int]], set[int]] | None = None) -> None:
        super().__init__()
        self._registry = registry
        self._tracker = tracker
        self._launch = launcher
        self._grow = process_tree
        self._hosts = hosts or tracker.backend.hosts_of
        self._pending: list[PendingLaunch] = []
        self._generation: dict[str, int] = {}
        self._claimed: set[str] = set()  # window keys given to a launch (never given twice)
        self._timer = QTimer(self, interval=TICK_MS)
        self._timer.timeout.connect(self._tick)
        tracker.window_added.connect(self._on_window)
        tracker.window_changed.connect(self._on_window)  # a window created hidden becomes visible / gets a title

    # -- queries -----------------------------------------------------------------------------------------------
    def pending(self, workspace_id: str) -> set[str]:
        return {p.entry_id for p in self._pending if p.workspace_id == workspace_id}

    def busy(self, workspace_id: str) -> bool:
        return any(p.workspace_id == workspace_id for p in self._pending)

    # -- starting ----------------------------------------------------------------------------------------------
    def start(self, workspace_id: str, items: list[tuple[str, AppEntry, MatchRule]]) -> int:
        """Start ``(entry id, application, rule)`` items for a workspace. Entries still starting are skipped (never
        started twice); launches already pending keep waiting for their windows."""
        generation = self._generation.setdefault(workspace_id, 1)
        waiting = self.pending(workspace_id)
        items = [item for item in items if item[0] not in waiting]
        before = frozenset(win.key for win in self._tracker.windows())
        for entry_id, app, rule in items:
            pending = PendingLaunch(workspace_id, entry_id, app.name, rule, generation, before)
            self._pending.append(pending)
            run_async(lambda _cancel, a=app: self._launch(a),
                      lambda result, p=pending, a=app: self._launched(p, a, result),
                      lambda error, p=pending: self._fail(p, f"Could not start {p.name}: {error}"))
        if items:
            self._timer.start()
        return generation

    def cancel(self, workspace_id: str) -> int:
        """Forget the workspace's pending launches (the started programs keep running). Returns the new generation."""
        generation = self._generation.get(workspace_id, 0) + 1
        self._generation[workspace_id] = generation
        dropped = [p for p in self._pending if p.workspace_id == workspace_id]
        self._pending = [p for p in self._pending if p.workspace_id != workspace_id]
        if dropped:
            self.finished.emit(workspace_id)
        return generation

    def _current(self, pending: PendingLaunch) -> bool:
        return pending in self._pending and self._generation.get(pending.workspace_id) == pending.generation

    def _launched(self, pending: PendingLaunch, app: AppEntry, result: LaunchResult) -> None:
        if not self._current(pending):
            return  # cancelled or superseded meanwhile: the late result changes nothing
        if not result.ok:
            self._fail(pending, result.message)
            return
        self._registry.record_launch(app.id)
        pending.launched = True
        pending.started = time.monotonic()
        pending.pid = result.pid or 0
        pending.tree = {pending.pid} if pending.pid else set()
        self._tracker.refresh()
        self._scan()

    # -- attributing windows -----------------------------------------------------------------------------------
    def _on_window(self, window: TrackedWindow) -> None:
        if self._pending:
            self._assign(window)

    def _assign(self, window: TrackedWindow) -> bool:
        if window.key in self._claimed:
            return False
        fresh = [p for p in self._pending if p.launched and window.key not in p.before]
        if not fresh:
            return False
        chosen = (next((p for p in fresh if window.pid in p.tree), None)
                  or next((p for p in fresh if window.hwnd in p.hosts), None)
                  or next((p for p in sorted(fresh, key=lambda x: x.started) if p.rule.matches(window)), None))
        if chosen is None:
            return False
        self._claimed.add(window.key)
        self._done(chosen, LaunchOutcome(chosen.workspace_id, chosen.entry_id, chosen.name, "window", window))
        return True

    def _scan(self) -> None:
        for pending in self._pending:
            if pending.tree:
                pending.alive = self._grow(pending.tree)
                if not pending.alive and pending.exited_at is None:
                    pending.exited_at = time.monotonic()
                pending.hosts = self._hosts(pending.tree)
        for window in self._tracker.windows():
            if not self._pending:
                break
            self._assign(window)

    def _tick(self) -> None:
        if not self._pending:
            self._timer.stop()
            return
        self._scan()
        now = time.monotonic()
        for pending in list(self._pending):
            if not pending.launched:
                continue
            if pending.exited_at is not None and now - pending.exited_at >= HANDOVER_AFTER or \
                    not pending.pid and now - pending.started >= HANDOVER_AFTER * 2:
                # The launcher exited (or there was no process to follow: a shell / packaged launch) and no new
                # window came: the request went to a copy that was already running.
                if any(self._fits_existing(pending, win) for win in self._tracker.windows()):
                    self._done(pending, LaunchOutcome(pending.workspace_id, pending.entry_id, pending.name,
                                                      "handed_over"))
                    continue
            limit = ALIVE_TIMEOUT if pending.alive and pending.pid else WINDOW_TIMEOUT
            if now - pending.started >= limit:
                self._fail(pending, f"{pending.name} was started, but no window of it appeared within {int(limit)} "
                                    "seconds. CIRANA left it alone.")

    @staticmethod
    def _fits_existing(pending: PendingLaunch, window: TrackedWindow) -> bool:
        return window.key in pending.before and pending.rule.matches(window)

    # -- endings -----------------------------------------------------------------------------------------------
    def _fail(self, pending: PendingLaunch, message: str) -> None:
        if self._current(pending):
            self._done(pending, LaunchOutcome(pending.workspace_id, pending.entry_id, pending.name, "failed",
                                              message=message))

    def _done(self, pending: PendingLaunch, outcome: LaunchOutcome) -> None:
        self._pending.remove(pending)
        self.outcome.emit(outcome)
        if not self.busy(pending.workspace_id):
            self.finished.emit(pending.workspace_id)
        if not self._pending:
            self._timer.stop()
            self._claimed.clear()
