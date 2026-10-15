"""Dynamic tiling: windows re-tile themselves whenever the set of windows changes.

Two scopes, never both at once:

* **All windows** (the Windows page, setting ``global_tiling``): every application window on the desktop is tiled,
  one layout per monitor, in the order the windows appeared. Programs can be left *floating* (setting
  ``tiling_exclusions``); windows without a sizing border (fixed-size dialogs, borderless full-screen games and
  videos), minimized and maximized windows float by themselves. While this is on it is in charge of every window,
  including workspace windows: a workspace still opens, starts and raises its applications, but the layout is this
  one.
* **One workspace** (its *Dynamic tiling* switch): only while that workspace is the active one, only its bound
  windows (reliably matched to an entry), in its own tiled layout.

A re-tile happens when a window of the tile set opens (also a re-created one), closes, is minimized, maximized or
restored, loses or regains its sizing border, when the layout settings or the order change, and when the monitors
change (resolution, unplugged, DPI).

How it stays calm and safe:

* **No feedback loop.** A re-tile runs only when the *signature* of the tile set changes (which windows, in which
  groups, with which layout settings), never because a window moved. CIRANA's own moves only change positions, so they
  cannot start another round. A rate limit is a second safeguard against applications that react to being moved.
* **Coalesced.** Bursts of events (several windows opening at once, a re-created window reported as removed + added)
  produce one re-tile.
* **The user's drag wins.** While the user drags or resizes a tiled window (the system move loop,
  ``EVENT_SYSTEM_MOVESIZESTART`` / ``END``) nothing is re-tiled and a running re-tile is cancelled. When the drag
  ends, a window dropped onto another tile swaps places with it; with all-window tiling, a window dropped onto another
  monitor moves to that monitor's layout; otherwise it goes back to its tile.
* **Refusals do not spread.** Windows Windows will not let CIRANA move (higher rights) are left out so the others fill
  the space; windows that are not responding are skipped for that round. Problems are reported once.
* **Minimum sizes.** An application that refuses to become as small as its tile (WM_GETMINMAXINFO) keeps the left /
  top edge it was given and grows to the right and down: over its neighbours (which hide that part of it) or off the
  screen. The controller sees this when it reads the window back (``Status.CONSTRAINED``); the minimum is remembered
  for that window (per DPI) and the layout is computed again with every tile at least that large. When the minimums
  do not fit on the monitor together, the newest window with a minimum floats instead, moved (never shrunk) so it
  is completely on the screen, and the reason is shown. Nothing is ever asked of the application itself (no message
  is sent to it), and what it draws inside its own window is its own business.
* **Session based.** Every re-tile is a cancellable worker job with a generation; a newer re-tile, a scope change, a
  drag or CIRANA quitting drop it and its late result. Nothing is undone when CIRANA closes: the windows simply stay.

Only ``SetWindowPos`` / ``ShowWindowAsync`` (through cirana.wm.controller) are used: no re-parenting, no style
changes, nothing injected into other processes; taskbar buttons, Alt+Tab and focus behave as without CIRANA.
"""
from __future__ import annotations

import json
import logging
import time
from collections import deque
import math
from dataclasses import dataclass, field

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QGuiApplication

from cirana.core.background import Job, run_async
from cirana.core.workspace_controller import EntryState, WorkspaceController, plan_layout
from cirana.wm.controller import TOLERANCE, Placement, Result, Status
from cirana.wm.identity import Rect, TrackedWindow
from cirana.wm.layout import MAIN_SIDE, LayoutSpec, Size, keep_visible, overflowing, tile
from cirana.wm.monitors import Monitor, MonitorRef, monitor_for_rect, resolve

log = logging.getLogger(__name__)

DEBOUNCE_MS = 150  # events within this time produce one re-tile
DISPLAY_DEBOUNCE_MS = 600  # Windows reports a display change in several steps
RATE_LIMIT = 8  # re-tiles ...
RATE_WINDOW = 4.0  # ... within this many seconds; beyond that, wait
RATE_BACKOFF_MS = 2000
GLOBAL = "*"  # scope id of all-window tiling (workspace scopes use the workspace id)


def _center_in(rect: Rect, point: tuple[int, int]) -> bool:
    return rect[0] <= point[0] < rect[2] and rect[1] <= point[1] < rect[3]


def _center(rect: Rect) -> tuple[int, int]:
    return (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2


@dataclass
class Group:
    """Windows laid out together: a workspace, or all tiled windows of one monitor."""

    id: str
    spec: LayoutSpec
    slots: list[tuple[str, TrackedWindow]]  # (slot id: entry id or window key, window), in layout order
    # windows that would take part but do not fit beside the others (minimum sizes): (slot id, window, why)
    floating: list[tuple[str, TrackedWindow, str]] = field(default_factory=list)
    minimums: dict[str, Size] = field(default_factory=dict)  # window key -> known minimum on this group's monitor


class DynamicTiler(QObject):
    reflowed = Signal(str)  # scope: workspace id, or GLOBAL
    state_changed = Signal(str)  # scope: paused / resumed (a drag), all-window tiling switched, floats changed
    # While the user drags a tiled window: where it will land if dropped now (visible frame, physical pixels), or
    # None (no valid target, or the drag is over). Computed with the same rules the drop itself uses.
    drop_preview = Signal(object)

    def __init__(self, wm: WorkspaceController, settings=None, debounce_ms: int = DEBOUNCE_MS,  # noqa: ANN001
                 display_debounce_ms: int = DISPLAY_DEBOUNCE_MS) -> None:
        super().__init__()
        self.wm = wm
        self.settings = settings  # SettingsManager (None: all-window tiling is unavailable, e.g. in some tests)
        wm.tiler = self
        self._stopped = False
        self._generation = 0
        self._job: Job | None = None
        self._signature: tuple | None = None
        self._force = False
        self._raise: list[TrackedWindow] | bool = False
        self._dragging: set[str] = set()  # keys of tiled windows the user is dragging / resizing
        self._denied: set[str] = set()  # windows Windows refuses to let CIRANA move: left out of the layout
        self._reported: set[tuple[str, str]] = set()  # (window key, status) already announced
        self._targets: dict[str, tuple[str, Rect]] = {}  # slot id -> (group id, rect of the last re-tile)
        self._order: list[str] = []  # all-window tiling: window keys in the order they joined
        self._home: dict[str, str] = {}  # all-window tiling: window key -> monitor device it is tiled on
        self._shown: dict[str, tuple] = {}  # window key -> (minimized, maximized, resizable) last seen
        self._minimums: dict[str, tuple[int, int, int]] = {}  # window key -> (width, height, at dpi) it insisted on
        self._history: deque[float] = deque()
        self._last_groups: list[Group] = []
        self._preview: Rect | None = None
        self.count = 0  # re-tiles started (diagnostics and tests)
        self._debounce = debounce_ms
        self._timer = QTimer(self, singleShot=True)
        self._timer.timeout.connect(self._evaluate)
        self._display_timer = QTimer(self, singleShot=True, interval=display_debounce_ms)
        self._display_timer.timeout.connect(lambda: self.schedule(force=True))

        tracker = wm.tracker
        tracker.window_added.connect(self._on_added)
        tracker.window_removed.connect(self._on_removed)
        tracker.window_changed.connect(self._on_changed)
        tracker.move_size.connect(self._on_move_size)
        wm.statuses_changed.connect(lambda _w: self.schedule())
        wm.workspaces.changed.connect(self.schedule)
        wm.workspaces.active_changed.connect(self._on_scope_changed)
        self._watch_displays()

    # -- all-window tiling settings ------------------------------------------------------------------------------
    @property
    def global_on(self) -> bool:
        return not self._stopped and self.settings is not None and self.settings.get("global_tiling") is True

    def set_global(self, on: bool) -> None:
        if self.settings is None:
            return
        self.settings.set("global_tiling", bool(on))
        self._on_scope_changed()
        self.state_changed.emit(GLOBAL)

    def global_layout(self) -> LayoutSpec:
        raw = self.settings.get("global_layout") if self.settings is not None else None
        spec = LayoutSpec.from_dict(raw) if isinstance(raw, dict) and raw else LayoutSpec(MAIN_SIDE, gap=8)
        if not spec.tiled:
            spec.kind = MAIN_SIDE
        return spec

    def set_global_layout(self, spec: LayoutSpec) -> None:
        if self.settings is not None:
            data = spec.to_dict()
            data.pop("free", None)
            data.pop("monitor", None)  # every monitor has its own layout of the same kind
            self.settings.set("global_layout", data)
            self.schedule(force=True)

    def exclusions(self) -> set[str]:
        raw = self.settings.get("tiling_exclusions") if self.settings is not None else []
        return {name.lower() for name in raw if isinstance(name, str)} if isinstance(raw, list) else set()

    def set_floating(self, exe_name: str, floating: bool) -> None:
        """Leave a program out of all-window tiling (or take it back in)."""
        if self.settings is None or not exe_name:
            return
        names = self.exclusions()
        names = names | {exe_name.lower()} if floating else names - {exe_name.lower()}
        self.settings.set("tiling_exclusions", sorted(names))
        self.state_changed.emit(GLOBAL)
        self.schedule()

    def float_reason(self, window: TrackedWindow) -> str:
        """Why a window is not tiled by all-window tiling ("" when it is)."""
        reason = self._excluded(window)
        if reason:
            return reason
        return next((why for group in self._last_groups for _s, w, why in group.floating if w.key == window.key), "")

    def _excluded(self, window: TrackedWindow) -> str:
        """Why a window cannot take part in all-window tiling at all ("" when it can)."""
        if window.exe_name.lower() in self.exclusions():
            return "floating (you chose so for this program)"
        if not window.resizable:
            return "floating (fixed size or full screen)"
        if window.minimized:
            return "minimized"
        if window.maximized:
            return "maximized"
        if window.key in self._denied:
            return "floating (Windows does not let CIRANA move it)"
        return ""

    # -- scope ---------------------------------------------------------------------------------------------------
    def manages(self, workspace_id: str) -> bool:
        """A workspace's own dynamic tiling is in charge of it right now (switch on, tiled layout, active, and
        all-window tiling off)."""
        workspace = self.wm.workspaces.get(workspace_id)
        return (not self._stopped and not self.global_on and bool(workspace_id)
                and workspace_id == self.wm.workspaces.active_id
                and workspace is not None and workspace.layout.dynamic and workspace.layout.tiled)

    def in_charge(self) -> bool:
        return self.global_on or self.manages(self.wm.workspaces.active_id)

    def paused(self, workspace_id: str = GLOBAL) -> bool:
        if workspace_id == GLOBAL:
            return self.global_on and bool(self._dragging)
        return self.manages(workspace_id) and bool(self._dragging)

    def tile_set(self, workspace_id: str) -> list[tuple[str, TrackedWindow]]:
        """A workspace's windows that take part: bound (reliably matched), not minimized or maximized, movable."""
        return [(s.entry.id, s.window) for s in self.wm.statuses(workspace_id)
                if s.state is EntryState.RUNNING and s.window is not None and not s.window.minimized
                and not s.window.maximized and s.window.key not in self._denied]

    def tiled_windows(self) -> list[TrackedWindow]:
        """All-window tiling: the windows that take part right now."""
        return [window for group in self._groups() for _slot, window in group.slots] if self.global_on else []

    def _groups(self) -> list[Group]:
        if self.global_on:
            return self._global_groups()
        workspace_id = self.wm.workspaces.active_id
        if self.manages(workspace_id):
            spec = LayoutSpec.from_dict(self.wm.workspaces.get(workspace_id).layout.to_dict())
            return [self._group(workspace_id, spec, self.tile_set(workspace_id), self._monitors())]
        return []

    # -- minimum sizes -------------------------------------------------------------------------------------------
    def minimum(self, window_key: str, monitor: Monitor) -> Size:
        """The smallest frame this window accepted so far, in physical pixels of ``monitor`` ((0, 0): none known)."""
        known = self._minimums.get(window_key)
        if known is None:
            return 0, 0
        width, height, dpi = known
        return math.ceil(width * monitor.dpi / dpi), math.ceil(height * monitor.dpi / dpi)

    def _learn(self, window_key: str, target: Rect, actual: Rect, monitors: list[Monitor]) -> bool:
        """A window placed at ``target`` ended up at ``actual``: when it kept the position but stayed larger, that
        is its minimum size (Windows enforces it from WM_GETMINMAXINFO). True when that is new."""
        if abs(actual[0] - target[0]) > TOLERANCE or abs(actual[1] - target[1]) > TOLERANCE:
            return False  # the application put itself somewhere else: that says nothing about its size
        width, height = actual[2] - actual[0], actual[3] - actual[1]
        width = width if width > target[2] - target[0] + TOLERANCE else 0
        height = height if height > target[3] - target[1] + TOLERANCE else 0
        if not (width or height):
            return False
        monitor = monitor_for_rect(target, monitors)
        if monitor is None:
            return False
        old = self.minimum(window_key, monitor)
        new = (max(width, old[0]), max(height, old[1]))
        if new == old:
            return False
        self._minimums[window_key] = (*new, monitor.dpi)
        return True

    def _group(self, group_id: str, spec: LayoutSpec, candidates: list[tuple[str, TrackedWindow]],
               monitors: list[Monitor]) -> Group:
        """A group of windows laid out together; those whose minimum sizes do not fit beside the others float."""
        monitor = resolve(spec.monitor, monitors)
        if monitor is None:
            return Group(group_id, spec, candidates)
        sizes = [self.minimum(window.key, monitor) for _s, window in candidates]
        out = set(overflowing(spec, monitor.work, sizes, monitor.scale)) if any(any(m) for m in sizes) else set()
        slots = [c for i, c in enumerate(candidates) if i not in out]
        floating = [(slot, window, self._overflow_note(sizes[i], monitor))
                    for i, (slot, window) in enumerate(candidates) if i in out]
        minimums = {window.key: sizes[i] for i, (_s, window) in enumerate(candidates) if i not in out and any(sizes[i])}
        return Group(group_id, spec, slots, floating, minimums)

    @staticmethod
    def _overflow_note(size: Size, monitor: Monitor) -> str:
        need = " × ".join(f"{v} px {what}" for v, what in zip(size, ("wide", "high")) if v)
        work_w, work_h = monitor.work[2] - monitor.work[0], monitor.work[3] - monitor.work[1]
        if size[0] > work_w or size[1] > work_h:
            return (f"floating: it needs at least {need}, more than this monitor's free area ({work_w} × {work_h}), "
                    "so it cannot be shown completely")
        return f"floating: it needs at least {need} and does not fit beside the other tiled windows"

    def managed_windows(self) -> list[TrackedWindow]:
        """The windows dynamic tiling is in charge of right now (either scope); [] when it is off."""
        if not self.in_charge():
            return []
        return [window for group in self._groups() for _slot, window in group.slots]

    def target_of(self, window_key: str) -> Rect | None:
        """Where the last re-tile put this window (None: not tiled by it)."""
        slot = next((s for group in self._last_groups for s, w in group.slots if w.key == window_key), None)
        target = self._targets.get(slot) if slot is not None else None
        return target[1] if target is not None else None

    def _monitors(self) -> list[Monitor]:
        try:
            return self.wm.tracker.backend.monitors()
        except Exception:  # noqa: BLE001
            log.debug("Monitors unavailable", exc_info=True)
            return []

    def _global_groups(self) -> list[Group]:
        monitors = self._monitors()
        if not monitors:
            return []
        eligible = [w for w in self.wm.tracker.windows() if not self._excluded(w)]
        keys = {w.key for w in eligible}
        self._order = [key for key in self._order if key in keys]
        for window in eligible:
            if window.key not in self._order:
                self._order.append(window.key)  # a new window joins at the end
            if window.key not in self._home:
                monitor = monitor_for_rect(window.rect, monitors) or resolve(None, monitors)
                self._home[window.key] = monitor.device
        return self._layout_global(self._order, self._home, {w.key: w for w in eligible}, monitors)

    def _layout_global(self, order: list[str], home: dict[str, str], by_key: dict[str, TrackedWindow],
                       monitors: list[Monitor]) -> list[Group]:
        base = self.global_layout()
        groups = []
        for monitor in monitors:
            # a window whose monitor was unplugged is tiled on the primary one
            slots = [(key, by_key[key]) for key in order if key in by_key
                     and resolve(MonitorRef(home.get(key, "")), monitors).device == monitor.device]
            if slots:
                spec = LayoutSpec.from_dict(base.to_dict())
                spec.monitor = monitor.ref()
                groups.append(self._group(monitor.device, spec, slots, monitors))
        return groups

    def _signature_of(self, groups: list[Group]) -> tuple:
        parts = []
        for group in groups:
            layout = group.spec.to_dict()
            layout.pop("free", None)
            parts.append((group.id, json.dumps(layout, sort_keys=True), tuple((s, w.key) for s, w in group.slots),
                          tuple(w.key for _s, w, _why in group.floating), tuple(sorted(group.minimums.items()))))
        return GLOBAL if self.global_on else self.wm.workspaces.active_id, tuple(parts)

    # -- triggers ------------------------------------------------------------------------------------------------
    def schedule(self, force: bool = False) -> None:
        if self._stopped:
            return
        self._force = self._force or force
        if self.in_charge() and not (self._timer.isActive() and self._timer.interval() == RATE_BACKOFF_MS):
            self._timer.start(self._debounce)

    def reflow(self, workspace_id: str = GLOBAL, raise_group: bool | list[TrackedWindow] = False) -> bool:
        """Re-tile now (Apply layout, activation), even if nothing changed. ``raise_group``: raise the tiled windows
        afterwards (True) or these windows (a workspace being activated under all-window tiling). False when no
        dynamic tiling is in charge of it."""
        if not (self.global_on or self.manages(workspace_id)):
            return False
        if raise_group:
            self._raise = raise_group if isinstance(raise_group, list) else True
        self.schedule(force=True)
        return True

    def _on_added(self, window: TrackedWindow) -> None:
        self._shown[window.key] = (window.minimized, window.maximized, window.resizable)
        self.schedule()

    def _on_removed(self, window: TrackedWindow) -> None:
        self._shown.pop(window.key, None)
        self._home.pop(window.key, None)
        self._minimums.pop(window.key, None)
        if window.key in self._dragging:
            self._dragging.discard(window.key)
            self._set_preview(None)
        self.schedule()

    def _on_changed(self, window: TrackedWindow) -> None:
        if window.key in self._dragging:
            self._update_preview(window)
        # Moves (CIRANA's own included) are never a trigger; a change of state is (for all-window tiling: any
        # window; for a workspace the controller reports its bound windows' changes through statuses_changed).
        state = (window.minimized, window.maximized, window.resizable)
        if self._shown.get(window.key) != state:
            known = window.key in self._shown
            self._shown[window.key] = state
            if known and self.global_on:
                self.schedule()

    def _on_scope_changed(self, _workspace_id: str = "") -> None:
        self._cancel()
        self._signature = None
        self._dragging.clear()
        self._set_preview(None)
        self._targets.clear()
        self._last_groups = []
        self.schedule()

    def _watch_displays(self) -> None:
        app = QGuiApplication.instance()
        if app is None:
            return

        def watch(screen) -> None:  # noqa: ANN001
            screen.geometryChanged.connect(lambda _r: self._display_timer.start())
            screen.availableGeometryChanged.connect(lambda _r: self._display_timer.start())
            screen.logicalDotsPerInchChanged.connect(lambda _d: self._display_timer.start())

        for screen in app.screens():
            watch(screen)
        app.screenAdded.connect(lambda screen: (watch(screen), self._display_timer.start()))
        app.screenRemoved.connect(lambda _screen: self._display_timer.start())

    def display_changed(self) -> None:
        """Monitors were added, removed or changed resolution / scale: lay the windows out again."""
        self._display_timer.start()

    # -- the user's drags ----------------------------------------------------------------------------------------
    def _scope(self) -> str:
        return GLOBAL if self.global_on else self.wm.workspaces.active_id

    def _on_move_size(self, window: TrackedWindow, started: bool) -> None:
        if not self.in_charge():
            return
        tiled = {w.key: slot for group in self._groups() for slot, w in group.slots}
        if window.key not in tiled and window.key not in self._dragging:
            return
        if started:
            self._dragging.add(window.key)
            self._cancel()  # never move a window while the user holds it (or one of its neighbours)
            self.state_changed.emit(self._scope())
            self._update_preview(window)
            return
        self._dragging.discard(window.key)
        self._set_preview(None)
        slot = tiled.get(window.key)
        if slot is not None:
            self._dropped(slot, window)
        self.state_changed.emit(self._scope())
        self.schedule(force=True)  # the drag is over: back into the layout

    # -- drop preview --------------------------------------------------------------------------------------------
    def _set_preview(self, rect: Rect | None) -> None:
        if rect != self._preview:
            self._preview = rect
            self.drop_preview.emit(rect)

    def _update_preview(self, window: TrackedWindow) -> None:
        if not self.in_charge() or window.minimized or window.maximized:
            self._set_preview(None)
            return
        slot = next((s for group in self._last_groups for s, w in group.slots if w.key == window.key), None)
        self._set_preview(self.predict_drop(slot, window.rect) if slot is not None else None)

    def predict_drop(self, slot: str, frame: Rect) -> Rect | None:
        """Where the window of ``slot`` lands if it is dropped with its visible frame at ``frame``: exactly what
        _dropped + the following re-tile do (swap with the tile under its centre, join another monitor's layout,
        or back to its own tile). None when there is no valid target."""
        own = self._targets.get(slot)
        center = _center(frame)
        other = next((other for other, (_g, rect) in self._targets.items()
                      if other != slot and _center_in(rect, center)), None)
        if other is not None:
            return self._simulate(slot, swap=other)  # a swap: the layout with the two exchanged
        if self.global_on:
            monitors = self._monitors()
            monitor = monitor_for_rect(frame, monitors)
            if monitor is None:
                return None  # off every monitor: nothing to show
            if monitor.device != self._home.get(slot):
                return self._simulate(slot, home=monitor.device)  # it joins that monitor's layout
        return own[1] if own is not None else None

    def _simulate(self, slot: str, swap: str | None = None, home: str | None = None) -> Rect | None:
        """The tile ``slot`` gets in the layout as it will be after the drop (the same planning as the re-tile,
        minimum sizes included). None: it would float."""
        monitors = self._monitors()
        if self.global_on:
            order, homes = list(self._order), dict(self._home)
            if swap is not None and slot in order and swap in order:
                i, j = order.index(slot), order.index(swap)
                order[i], order[j] = order[j], order[i]
                homes[slot], homes[swap] = homes.get(swap, ""), homes.get(slot, "")
            if home is not None:
                homes[slot] = home
            windows = {w.key: w for g in self._last_groups for w in [w for _s, w in g.slots]
                       + [w for _s, w, _why in g.floating]}
            groups = self._layout_global(order, homes, windows, monitors)
        else:
            group = next(iter(self._last_groups), None)
            if group is None:
                return None
            candidates = list(self.tile_set(group.id))
            ids = [s for s, _w in candidates]
            if swap is not None and slot in ids and swap in ids:
                i, j = ids.index(slot), ids.index(swap)
                candidates[i], candidates[j] = candidates[j], candidates[i]
            groups = [self._group(group.id, group.spec, candidates, monitors)]
        for group in groups:
            ids = [s for s, _w in group.slots]
            if slot in ids:
                monitor = resolve(group.spec.monitor, monitors)
                sizes = [group.minimums.get(w.key, (0, 0)) for _s, w in group.slots]
                return tile(group.spec, monitor.work, len(ids), monitor.scale, sizes)[ids.index(slot)]
        return None

    def _dropped(self, slot: str, window: TrackedWindow) -> None:
        try:
            frame = self.wm.tracker.backend.frame_rect(window.hwnd)
        except Exception:  # noqa: BLE001 - the window closed while it was dropped
            return
        own = self._targets.get(slot)
        if own is not None and frame == own[1]:
            return  # not moved at all
        center = _center(frame)
        target = next((other for other, (_g, rect) in self._targets.items()
                       if other != slot and _center_in(rect, center)), None)
        if target is not None:
            self._swap(slot, target)
            return
        if self.global_on:  # dropped on another monitor (outside its tiles): it joins that monitor's layout
            monitor = monitor_for_rect(frame, self._monitors())
            if monitor is not None and monitor.device != self._home.get(window.key):
                self._home[window.key] = monitor.device

    def _swap(self, first: str, second: str) -> None:
        if not self.global_on:
            self.wm.workspaces.swap_entries(self.wm.workspaces.active_id, first, second)
            return
        if first in self._order and second in self._order:
            i, j = self._order.index(first), self._order.index(second)
            self._order[i], self._order[j] = self._order[j], self._order[i]
            self._home[first], self._home[second] = self._home.get(second, ""), self._home.get(first, "")

    # -- re-tiling -----------------------------------------------------------------------------------------------
    def _cancel(self) -> None:
        self._generation += 1
        if self._job is not None:
            self._job.cancel()
            self._job = None

    def _rate_limited(self) -> bool:
        now = time.monotonic()
        while self._history and now - self._history[0] > RATE_WINDOW:
            self._history.popleft()
        if len(self._history) >= RATE_LIMIT:
            log.warning("Dynamic tiling: %d re-tiles within %.0f s; waiting before the next one", RATE_LIMIT,
                        RATE_WINDOW)
            self._timer.start(RATE_BACKOFF_MS)
            return True
        self._history.append(now)
        return False

    def _evaluate(self) -> None:
        if not self.in_charge():
            self._force, self._raise = False, False
            return
        if self._dragging:
            return  # the end of the drag schedules the re-tile
        groups = self._groups()
        signature = self._signature_of(groups)
        if signature == self._signature and not self._force:
            return
        if self._rate_limited():
            return
        raise_group, self._force, self._raise = self._raise, False, False
        self._signature = signature
        self._last_groups = groups
        self._start(signature[0], groups, raise_group)

    def _start(self, scope: str, groups: list[Group], raise_group: bool | list[TrackedWindow]) -> None:
        self._cancel()
        generation = self._generation
        backend, controller = self.wm.tracker.backend, self.wm.windows
        self.count += 1

        def job(cancel):  # noqa: ANN001 - worker thread
            monitors = backend.monitors()
            planned, refused, usable_all, floats = [], [], [], []
            for group in groups:
                usable = []
                for slot, window in group.slots:
                    problem = controller.check(window)
                    if problem is None:
                        usable.append((slot, window))
                    else:
                        refused.append((slot, problem))
                usable_all += [window for _s, window in usable]
                planned += [(group.id, slot, p) for slot, p in
                            plan_layout(group.spec, usable, monitors, minimums=group.minimums)]
                monitor = resolve(group.spec.monitor, monitors)
                for slot, window, _why in group.floating:  # too large to tile: only made completely visible
                    if monitor is None or controller.check(window) is not None or backend.is_minimized(window.hwnd):
                        continue
                    frame = backend.frame_rect(window.hwnd)
                    if keep_visible(monitor.work, frame) != frame:
                        floats.append((slot, Placement(window, keep_visible(monitor.work, frame))))
            results = controller.place_all([p for _g, _s, p in planned] + [p for _s, p in floats], cancel)
            if raise_group and not cancel.is_set():
                front = raise_group if isinstance(raise_group, list) else usable_all
                if front:
                    controller.bring_to_front(front)
            targets = [(slot, (group_id, p.rect)) for group_id, slot, p in planned]
            tiled = list(zip([slot for _g, slot, _p in planned], results)) + refused
            return targets, tiled, list(zip([slot for slot, _p in floats], results[len(planned):]))

        self._job = run_async(job, lambda outcome: self._done(scope, generation, outcome),
                              lambda error: self._failed(generation, error))

    def _failed(self, generation: int, error: str) -> None:
        if generation == self._generation:
            self._job = None
            log.warning("Dynamic tiling failed: %s", error)

    def _done(self, scope: str, generation: int,
              outcome: tuple[list[tuple[str, tuple[str, Rect]]], list[tuple[str, Result]],
                             list[tuple[str, Result]]]) -> None:
        if generation != self._generation or not self.in_charge() or scope != self._scope():
            return  # superseded, cancelled, or the scope changed meanwhile
        self._job = None
        targets, results, floated = outcome
        self._targets = dict(targets)
        changed, problems, learned = False, [], False
        monitors = self._monitors()
        for slot, result in results:
            if result.status is Status.DENIED and result.key not in self._denied:
                self._denied.add(result.key)  # left out from now on: the others take its space
                self.schedule(force=True)
            if result.status is Status.CONSTRAINED and result.actual is not None and slot in self._targets:
                # larger than its tile: its minimum size. The next layout makes room for it (or floats it).
                learned = self._learn(result.key, self._targets[slot][1], result.actual, monitors) or learned
            if scope != GLOBAL:
                message = "" if result.status is Status.PLACED else result.message
                changed = self.wm.set_entry_message(scope, slot, message) or changed
            if result.status not in (Status.PLACED, Status.GONE) and (result.key, result.status.value) \
                    not in self._reported:
                self._reported.add((result.key, result.status.value))
                if not result.ok:
                    problems.append(result.message)
        for group in self._last_groups:
            for slot, window, why in group.floating:
                if scope != GLOBAL:
                    changed = self.wm.set_entry_message(scope, slot, f"{window.display_title}: {why}.") or changed
                if (window.key, why) not in self._reported:
                    self._reported.add((window.key, why))
                    problems.append(f"{window.display_title}: {why}.")
        for _slot, result in floated:
            if not result.ok and (result.key, result.status.value) not in self._reported:
                self._reported.add((result.key, result.status.value))
                problems.append(result.message)
        if learned:
            self.schedule()  # the minimums are part of the signature: one more round, with room for them
        if problems:
            self.wm.message.emit(" ".join(problems))
        if changed:
            self.wm.statuses_changed.emit(scope)
        self.reflowed.emit(scope)

    def stop(self) -> None:
        """CIRANA is closing: stop arranging. The windows stay exactly where they are."""
        self._stopped = True
        self._timer.stop()
        self._display_timer.stop()
        self._cancel()
