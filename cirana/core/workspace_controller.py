"""Workspaces at runtime: which open window belongs to which entry, activating a workspace, applying its layout.

Bindings (entry -> window) live only in memory and use the tracker's runtime keys (hwnd + pid + process creation
time), so a closed window or a recycled handle can never be mistaken for the bound one. After a restart they are
rebuilt from the entries' match rules: one fitting window is bound by itself, several are offered to the user (the
window whose title matches the last bound one is preferred).

What activating does - and does not do:

* windows that are already open are used as they are (nothing is restarted), missing applications are started;
* the layout is applied to the group's windows and the group is raised, once, because the user asked;
* other workspaces' windows are left alone (not hidden, not minimized, not closed), windows the user moves later are
  never pulled back, and closing a workspace ends no process.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

from PySide6.QtCore import QObject, QTimer, Signal

from cirana.core.application_registry import ApplicationRegistry
from cirana.core.background import run_async
from cirana.core.workspace_launcher import LaunchOutcome, WorkspaceLauncher
from cirana.core.workspaces import Workspace, WorkspaceApp, WorkspaceManager
from cirana.wm.controller import Placement, Result, Status, WindowController
from cirana.wm.identity import TrackedWindow
from cirana.wm.layout import FREE, FreeSlot, LayoutSpec, from_fraction, tile, to_fraction
from cirana.wm.monitors import Monitor, monitor_for_rect, resolve
from cirana.wm.tracker import WindowTracker

log = logging.getLogger(__name__)


class EntryState(str, Enum):
    RUNNING = "running"  # bound to an open window
    LAUNCHING = "launching"  # started, waiting for its window
    AMBIGUOUS = "ambiguous"  # several open windows fit: the user picks one
    MISSING = "missing"  # not open; started when the workspace is activated
    STOPPED = "stopped"  # not open; not started automatically
    UNAVAILABLE = "unavailable"  # not open, and its application is no longer in the catalog


@dataclass
class EntryStatus:
    entry: WorkspaceApp
    state: EntryState
    window: TrackedWindow | None = None
    candidates: list[TrackedWindow] = field(default_factory=list)
    message: str = ""  # the last problem (launch failed, Windows refused to move it, ...)

    @property
    def placeable(self) -> bool:
        """Takes a place in a tiled layout (it has, or is about to get, a window)."""
        return self.state in (EntryState.RUNNING, EntryState.LAUNCHING, EntryState.AMBIGUOUS, EntryState.MISSING)


def plan_layout(spec: LayoutSpec, slots: list[tuple[str, TrackedWindow | None]], monitors: list[Monitor],
                only: set[str] | None = None, minimums: dict[str, tuple[int, int]] | None = None,
                ) -> list[tuple[str, Placement]]:
    """Where each window goes. ``slots``: (entry id, window or None) in workspace order; a None window keeps its
    place free (an application still starting). Free layouts only move windows with a saved slot. ``minimums``:
    window key -> known minimum frame size (physical pixels on the layout's monitor), honoured by tiled layouts."""
    if not monitors:
        return []
    placements = []
    if spec.kind == FREE:
        for entry_id, window in slots:
            slot = spec.free.get(entry_id)
            if window is None or slot is None or (only is not None and entry_id not in only):
                continue
            monitor = resolve(slot.monitor, monitors)
            placements.append((entry_id, Placement(window, from_fraction(slot.fraction, monitor.work),
                                                   slot.maximized)))
        return placements
    monitor = resolve(spec.monitor, monitors)
    sizes = [minimums.get(window.key, (0, 0)) if window is not None else (0, 0) for _e, window in slots]         if minimums else None
    rects = tile(spec, monitor.work, len(slots), monitor.scale, sizes, [entry_id for entry_id, _w in slots])
    for (entry_id, window), rect in zip(slots, rects):
        if window is not None and (only is None or entry_id in only):
            placements.append((entry_id, Placement(window, rect)))
    return placements


def capture_slots(windows: dict[str, TrackedWindow], monitors: list[Monitor], backend) -> dict[str, FreeSlot]:  # noqa: ANN001
    """Worker thread: the bound windows' current places as monitor-relative slots (minimized ones are skipped)."""
    slots = {}
    for entry_id, window in windows.items():
        if not backend.still_same(window) or backend.is_minimized(window.hwnd):
            continue
        rect = backend.frame_rect(window.hwnd)
        maximized = backend.is_maximized(window.hwnd)
        monitor = monitor_for_rect(rect, monitors)
        if monitor is not None:
            slots[entry_id] = FreeSlot(to_fraction(rect, monitor.work), monitor.ref(), maximized)
    return slots


class WorkspaceController(QObject):
    statuses_changed = Signal(str)  # workspace id ("" = several / unknown): its entries' states changed
    choice_needed = Signal(str, str)  # workspace id, entry id: several windows fit, the user should pick
    message = Signal(str)  # a summary for the status toast
    busy_changed = Signal(str, bool)  # workspace id, an activation / arrangement is running

    def __init__(self, workspaces: WorkspaceManager, registry: ApplicationRegistry, tracker: WindowTracker,
                 controller: WindowController, launcher: WorkspaceLauncher) -> None:
        super().__init__()
        self.workspaces = workspaces
        self.registry = registry
        self.tracker = tracker
        self.windows = controller
        self.launcher = launcher
        self._bindings: dict[str, dict[str, str]] = {}  # workspace id -> entry id -> window key
        self._messages: dict[tuple[str, str], str] = {}  # (workspace id, entry id) -> last problem
        self._slot_order: dict[str, list[str]] = {}  # workspace id -> entry ids that share the tiled layout
        self._busy: dict[str, int] = {}
        self.tiler = None  # cirana.core.dynamic_tiling.DynamicTiler, when it is attached
        self._shown: dict[str, tuple] = {}  # window key -> (title, minimized, maximized) last reported
        self._last_focus: dict[str, str] = {}  # workspace id -> key of its window that had the focus last
        self._notify = QTimer(self, singleShot=True, interval=60)  # coalesces bursts of window events
        self._notify.timeout.connect(lambda: self.statuses_changed.emit(""))
        tracker.window_added.connect(lambda _w: self._notify.start())
        tracker.window_removed.connect(self._on_removed)
        tracker.window_changed.connect(self._on_changed)
        tracker.foreground_changed.connect(self._on_foreground)
        launcher.outcome.connect(self._on_launch_outcome)
        launcher.finished.connect(self._on_launches_finished)
        workspaces.changed.connect(self._prune)

    # -- status ------------------------------------------------------------------------------------------------
    def busy(self, workspace_id: str) -> bool:
        return self._busy.get(workspace_id, 0) > 0 or self.launcher.busy(workspace_id)

    def bound_window(self, workspace_id: str, entry_id: str) -> TrackedWindow | None:
        key = self._bindings.get(workspace_id, {}).get(entry_id, "")
        return self.tracker.get(key) if key else None

    def statuses(self, workspace_id: str) -> list[EntryStatus]:
        """The state of every entry, binding windows that fit unambiguously (cheap: no Win32 calls)."""
        workspace = self.workspaces.get(workspace_id)
        if workspace is None:
            return []
        bindings = self._bindings.setdefault(workspace_id, {})
        launching = self.launcher.pending(workspace_id)
        used: set[str] = set()
        result: dict[str, EntryStatus] = {}
        for entry in workspace.apps:  # windows already bound keep their entry
            window = self.bound_window(workspace_id, entry.id)
            if window is not None:
                used.add(window.key)
                result[entry.id] = EntryStatus(entry, EntryState.RUNNING, window,
                                               message=self._messages.get((workspace_id, entry.id), ""))
            else:
                bindings.pop(entry.id, None)
        windows = self.tracker.windows()
        for entry in workspace.apps:
            if entry.id in result:
                continue
            message = self._messages.get((workspace_id, entry.id), "")
            if entry.id in launching:
                result[entry.id] = EntryStatus(entry, EntryState.LAUNCHING, message=message)
                continue
            rule = self.workspaces.rule_for(entry)
            candidates = [win for win in windows if win.key not in used and rule.matches(win)
                          and not (self.tiler is not None and self.tiler.ignores(win))]  # a window rule: hands off
            hinted = [win for win in candidates if entry.hint_title and win.title == entry.hint_title]
            pick = candidates[0] if len(candidates) == 1 else hinted[0] if len(hinted) == 1 else None
            if pick is not None:
                self._bind(workspace_id, entry, pick)
                used.add(pick.key)
                result[entry.id] = EntryStatus(entry, EntryState.RUNNING, pick, message=message)
            elif candidates:
                result[entry.id] = EntryStatus(entry, EntryState.AMBIGUOUS, None, candidates, message)
            elif entry.app_id and self.workspaces.resolve(entry) is None:
                result[entry.id] = EntryStatus(entry, EntryState.UNAVAILABLE, message=message or
                                               "Not in the application list any more. Refresh the list or remove it.")
            elif entry.launch and self.workspaces.resolve(entry) is not None:
                result[entry.id] = EntryStatus(entry, EntryState.MISSING, message=message)
            else:
                result[entry.id] = EntryStatus(entry, EntryState.STOPPED, message=message)
        # Choices made for earlier entries can settle later ones: two windows, two entries, one picked.
        return [result[entry.id] for entry in workspace.apps]

    def candidates(self, workspace_id: str, entry_id: str) -> list[TrackedWindow]:
        status = next((s for s in self.statuses(workspace_id) if s.entry.id == entry_id), None)
        if status is None:
            return []
        if status.state is EntryState.AMBIGUOUS:
            return status.candidates
        rule = self.workspaces.rule_for(status.entry)
        taken = {key for other, key in self._bindings.get(workspace_id, {}).items() if other != entry_id}
        return [win for win in self.tracker.windows() if rule.matches(win) and win.key not in taken]

    # -- binding -----------------------------------------------------------------------------------------------
    def _bind(self, workspace_id: str, entry: WorkspaceApp, window: TrackedWindow) -> None:
        bindings = self._bindings.setdefault(workspace_id, {})
        for other, key in list(bindings.items()):
            if key == window.key and other != entry.id:
                del bindings[other]  # one window, one entry (per workspace)
        bindings[entry.id] = window.key
        self._messages.pop((workspace_id, entry.id), None)
        # Persisting the hint emits "changed"; never from inside a status computation.
        QTimer.singleShot(0, lambda: self.workspaces.remember_window(workspace_id, entry.id, window))

    def choose(self, workspace_id: str, entry_id: str, key: str, place: bool = True) -> bool:
        """The user picked the window for an entry (of several that fit). It is placed in its slot right away
        when the workspace was being activated."""
        workspace = self.workspaces.get(workspace_id)
        entry = workspace.entry(entry_id) if workspace else None
        window = self.tracker.get(key)
        if entry is None or window is None:
            return False
        self._bind(workspace_id, entry, window)
        self.statuses_changed.emit(workspace_id)
        if place and workspace_id in self._slot_order:
            self.apply_layout(workspace_id, only={entry_id})
        return True

    def unbind(self, workspace_id: str, entry_id: str) -> None:
        self._bindings.get(workspace_id, {}).pop(entry_id, None)
        self.statuses_changed.emit(workspace_id)

    def _on_removed(self, window: TrackedWindow) -> None:
        self._shown.pop(window.key, None)
        changed = False
        for bindings in self._bindings.values():
            for entry_id, key in list(bindings.items()):
                if key == window.key:
                    del bindings[entry_id]
                    changed = True
        if changed:
            self.statuses_changed.emit("")
        else:
            self._notify.start()

    def _on_changed(self, window: TrackedWindow) -> None:
        # Moves and resizes are not shown anywhere (and a dragged window reports dozens a second): only a change of
        # title or show state matters, and only for a window that belongs to a workspace.
        shown = (window.title, window.minimized, window.maximized)
        if self._shown.get(window.key) == shown:
            return
        self._shown[window.key] = shown
        if any(window.key in bindings.values() for bindings in self._bindings.values()):
            self._notify.start()

    def _on_foreground(self, key: str) -> None:
        workspace_id = self.workspaces.active_id
        if key and workspace_id and key in self._bindings.get(workspace_id, {}).values():
            self._last_focus[workspace_id] = key

    def group(self, workspace_id: str) -> list[TrackedWindow]:
        """The workspace's open windows to raise together: the one that had the focus last first (it gets the
        focus again), then the others in order; windows a window rule assigns to the workspace are included."""
        windows = [s.window for s in self.statuses(workspace_id) if s.window is not None]
        if self.tiler is not None:
            windows += [w for w in self.tiler.rule_windows(workspace_id) if w not in windows]
            windows = [w for w in windows if not self.tiler.out_of_sight(w.key)]  # a stack shows one window
        last = self._last_focus.get(workspace_id)
        windows.sort(key=lambda w: w.key != last)  # stable: the rest keep their order
        return windows

    def move_window(self, window: TrackedWindow, target_id: str) -> bool:
        """Move a window from the active workspace to another one: its entry there is removed (a window that was not
        in it is just added) and it becomes an entry of ``target_id``, bound to it. The window itself stays open
        where it is; it is arranged when that workspace is activated."""
        source = self.workspaces.active()
        target = self.workspaces.get(target_id)
        if target is None or (source is not None and source.id == target_id):
            return False
        if source is not None:
            for status in self.statuses(source.id):
                if status.window is not None and status.window.key == window.key:
                    self.workspaces.remove_entry(source.id, status.entry.id)
        entry = next((s.entry for s in self.statuses(target_id) if s.window is not None
                      and s.window.key == window.key), None)
        if entry is None:
            entry = self.workspaces.add_window(target_id, window)
        self.choose(target_id, entry.id, window.key, place=False)
        self.message.emit(f"Moved {window.display_title} to {target.name}")
        return True

    def _prune(self) -> None:
        for workspace_id in list(self._bindings):
            workspace = self.workspaces.get(workspace_id)
            if workspace is None:
                self._bindings.pop(workspace_id)
                self._slot_order.pop(workspace_id, None)
                self.launcher.cancel(workspace_id)
                continue
            ids = {entry.id for entry in workspace.apps}
            for entry_id in list(self._bindings[workspace_id]):
                if entry_id not in ids:
                    del self._bindings[workspace_id][entry_id]

    # -- activation --------------------------------------------------------------------------------------------
    def activate(self, workspace_id: str) -> bool:
        """Make the workspace active, start what is missing, arrange and raise its windows."""
        workspace = self.workspaces.get(workspace_id)
        if workspace is None:
            return False
        self.workspaces.set_active(workspace_id)
        statuses = self.statuses(workspace_id)
        self._slot_order[workspace_id] = [s.entry.id for s in statuses if s.placeable]
        to_start = []
        for status in statuses:
            if status.state is EntryState.MISSING:
                app = self.workspaces.resolve(status.entry)
                if app is not None:
                    self._messages.pop((workspace_id, status.entry.id), None)
                    to_start.append((status.entry.id, app, self.workspaces.rule_for(status.entry)))
        if to_start:
            self.launcher.start(workspace_id, to_start)
        running = sum(1 for s in statuses if s.state is EntryState.RUNNING)
        ambiguous = [s for s in statuses if s.state is EntryState.AMBIGUOUS]
        parts = [f"{running} already open"] if running else []
        if to_start:
            parts.append(f"starting {len(to_start)}")
        if ambiguous:
            parts.append(f"{len(ambiguous)} to choose")
        self.message.emit(f"{workspace.name}: " + (", ".join(parts) if parts else "nothing to open"))
        self.apply_layout(workspace_id, raise_group=True)
        for status in ambiguous:
            self.choice_needed.emit(workspace_id, status.entry.id)
        self.statuses_changed.emit(workspace_id)
        return True

    def close(self, workspace_id: str) -> None:
        """Stop managing the workspace's windows. Every application keeps running exactly as it is."""
        self.launcher.cancel(workspace_id)
        self._slot_order.pop(workspace_id, None)
        self._bindings.pop(workspace_id, None)
        if self.workspaces.active_id == workspace_id:
            self.workspaces.set_active("")
        self.statuses_changed.emit(workspace_id)

    def _on_launch_outcome(self, outcome: LaunchOutcome) -> None:
        workspace = self.workspaces.get(outcome.workspace_id)
        entry = workspace.entry(outcome.entry_id) if workspace else None
        if entry is None:
            return
        if outcome.kind == "window" and outcome.window is not None:
            self._bind(outcome.workspace_id, entry, outcome.window)
            self.apply_layout(outcome.workspace_id, only={entry.id}, raise_group=False)
        elif outcome.kind == "failed":
            self._messages[(outcome.workspace_id, entry.id)] = outcome.message
            self.message.emit(outcome.message)
        else:  # handed over to a running copy: an existing window may fit now
            status = next((s for s in self.statuses(outcome.workspace_id) if s.entry.id == entry.id), None)
            if status is not None and status.state is EntryState.RUNNING:
                self.apply_layout(outcome.workspace_id, only={entry.id}, raise_group=False)
            elif status is not None and status.state is EntryState.AMBIGUOUS:
                self.choice_needed.emit(outcome.workspace_id, entry.id)
        self.statuses_changed.emit(outcome.workspace_id)

    def _on_launches_finished(self, workspace_id: str) -> None:
        order = self._slot_order.get(workspace_id)
        if order is None:
            return
        statuses = {s.entry.id: s for s in self.statuses(workspace_id)}
        kept = [entry_id for entry_id in order if entry_id in statuses and statuses[entry_id].placeable]
        if kept != order:  # an application did not come: close its gap in the tiled layout
            self._slot_order[workspace_id] = kept
            workspace = self.workspaces.get(workspace_id)
            if workspace is not None and workspace.layout.kind != FREE:
                self.apply_layout(workspace_id)
        self.busy_changed.emit(workspace_id, self.busy(workspace_id))

    # -- arranging ---------------------------------------------------------------------------------------------
    def _slots(self, workspace: Workspace) -> list[tuple[str, TrackedWindow | None]]:
        statuses = {s.entry.id: s for s in self.statuses(workspace.id)}
        order = self._slot_order.get(workspace.id) or [s.entry.id for s in statuses.values() if s.placeable]
        return [(entry_id, statuses[entry_id].window) for entry_id in order if entry_id in statuses]

    def set_entry_message(self, workspace_id: str, entry_id: str, message: str) -> bool:
        """Show (or clear, with "") the last problem on an entry's row. True when it changed."""
        key = (workspace_id, entry_id)
        if self._messages.get(key, "") == message:
            return False
        if message:
            self._messages[key] = message
        else:
            self._messages.pop(key, None)
        return True

    def apply_layout(self, workspace_id: str, only: set[str] | None = None, raise_group: bool = False) -> bool:
        """Arrange the workspace's open windows (``only``: just these entries). Runs on a worker thread.

        With dynamic tiling in charge of the workspace the whole layout is re-tiled instead (over the windows that
        are actually there, so no place is kept for one that is still starting)."""
        if self.tiler is not None and self.tiler.global_on:
            # All-window tiling is in charge of every window: re-tile, and raise this workspace's windows if asked.
            group = self.group(workspace_id) if raise_group else False
            return self.tiler.reflow(raise_group=group or False)
        if self.tiler is not None and self.tiler.manages(workspace_id):
            return self.tiler.reflow(workspace_id, (self.group(workspace_id) or False) if raise_group else False)
        workspace = self.workspaces.get(workspace_id)
        if workspace is None:
            return False
        slots = [(entry_id, window if window is None or self.tiler is None or not self.tiler.ignores(window)
                  else None) for entry_id, window in self._slots(workspace)]
        spec = workspace.layout
        backend, controller = self.tracker.backend, self.windows
        group = self.group(workspace_id)

        def job(cancel):  # noqa: ANN001
            planned = plan_layout(spec, slots, backend.monitors(), only)
            results = controller.place_all([placement for _e, placement in planned], cancel)
            if raise_group and group:
                controller.bring_to_front(group)
            return [(entry_id, result) for (entry_id, _p), result in zip(planned, results)]

        self._set_busy(workspace_id, +1)
        run_async(job, lambda results: self._placed(workspace_id, results, only is None),
                  lambda error: self._placed(workspace_id, [], only is None, error))
        return True

    def _placed(self, workspace_id: str, results: list[tuple[str, Result]], whole: bool, error: str = "") -> None:
        self._set_busy(workspace_id, -1)
        if error:
            self.message.emit(f"Arranging the windows failed: {error}")
            return
        problems = []
        for entry_id, result in results:
            if result.ok and result.status is Status.PLACED:
                self._messages.pop((workspace_id, entry_id), None)
            else:
                self._messages[(workspace_id, entry_id)] = result.message
                if not result.ok:
                    problems.append(result.message)
        if problems:
            self.message.emit(" ".join(problems))
        elif whole and results:
            workspace = self.workspaces.get(workspace_id)
            self.message.emit(f"Arranged {len(results)} window{'s' if len(results) != 1 else ''}"
                              + (f" of {workspace.name}" if workspace else ""))
        self.statuses_changed.emit(workspace_id)

    def _set_busy(self, workspace_id: str, delta: int) -> None:
        self._busy[workspace_id] = max(0, self._busy.get(workspace_id, 0) + delta)
        self.busy_changed.emit(workspace_id, self.busy(workspace_id))

    def bring_to_front(self, workspace_id: str) -> None:
        group = self.group(workspace_id)
        if not group:
            self.message.emit("None of this workspace's windows is open.")
            return
        controller = self.windows
        run_async(lambda _cancel: controller.bring_to_front(group),
                  lambda results: self._report([r for r in results if not r.ok]))

    def minimize(self, workspace_id: str) -> None:
        """Minimize the workspace's windows (only on request; nothing is ever hidden behind the user's back)."""
        group = [s.window for s in self.statuses(workspace_id) if s.window is not None]
        controller = self.windows
        run_async(lambda _cancel: [controller.minimize(win) for win in group],
                  lambda results: self._report([r for r in results if not r.ok]))

    def _report(self, problems: list[Result]) -> None:
        if problems:
            self.message.emit(" ".join(r.message for r in problems))

    def save_positions(self, workspace_id: str) -> None:
        """Remember where the workspace's windows are now (a "saved positions" layout)."""
        windows = {s.entry.id: s.window for s in self.statuses(workspace_id) if s.window is not None}
        if not windows:
            self.message.emit("None of this workspace's windows is open, so there is nothing to save.")
            return
        backend = self.tracker.backend

        def done(slots: dict[str, FreeSlot]) -> None:
            if self.workspaces.get(workspace_id) is None:
                return
            self.workspaces.save_positions(workspace_id, slots)
            self.message.emit(f"Saved the positions of {len(slots)} window{'s' if len(slots) != 1 else ''}")

        run_async(lambda _cancel: capture_slots(windows, backend.monitors(), backend), done,
                  lambda error: self.message.emit(f"Could not read the window positions: {error}"))
