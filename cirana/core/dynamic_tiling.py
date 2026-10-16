"""Dynamic tiling: windows re-tile themselves whenever the set of windows changes.

Two scopes, never both at once:

* **All windows** (the Windows page, setting ``global_tiling``): every application window on the desktop is tiled,
  one layout per monitor, in the order the windows appeared. Programs can be left *floating* (setting
  ``tiling_exclusions``, or a window rule); windows without a sizing border (fixed-size dialogs, borderless full-screen
  games and videos), minimized and maximized windows float by themselves. While this is on it is in charge of every
  window, including workspace windows: a workspace still opens, starts and raises its applications, but the layout is
  this one.
* **One workspace** (its *Dynamic tiling* switch): only while that workspace is the active one, only its bound
  windows (reliably matched to an entry) and the windows a window rule assigns to it, in its own tiled layout.

A re-tile happens when a window of the tile set opens (also a re-created one), closes, is minimized, maximized or
restored, loses or regains its sizing border, when the layout settings or the order change, when the monitors
change (resolution, unplugged, DPI), and when the user gives a keyboard command (swap, resize, monocle, stack).

Tiles are *units*: one window, or a **stack** of several windows sharing one tile (one of them shown). Layouts are
side by side, main + side panel, grids, **BSP** (``cirana.wm.bsp``: a new window splits the focused tile) and
**automatic** (the layout follows the number of windows). **Monocle** shows one unit over the whole work area
without changing the layout behind it: switching it off puts every window back exactly where it was.

How it stays calm and safe:

* **No feedback loop.** A re-tile runs only when the *signature* of the tile set changes (which windows, in which
  units and groups, with which layout settings, tree, stacks and monocle), never because a window moved. CIRANA's own
  moves only change positions, so they cannot start another round. A rate limit is a second safeguard against
  applications that react to being moved (keyboard commands are the user's and are not limited).
* **Coalesced.** Bursts of events (several windows opening at once, a re-created window reported as removed + added)
  produce one re-tile. Windows that already are where they belong are not touched again.
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
* **Stacks never lose a window.** The windows of a stack stay independent top-level windows (no re-parenting). By
  default the hidden ones simply lie under the shown one, at the same place; with ``stack_hide = "minimize"`` they are
  minimized instead (their taskbar buttons stay). Windows CIRANA minimized for a stack are written down
  (``stack_hidden.json``) and shown again when they leave the stack, when the scope stops being managed, when CIRANA
  closes, and after a crash at the next start.
* **Session based.** Every re-tile is a cancellable worker job with a generation; a newer re-tile, a scope change, a
  drag or CIRANA quitting drop it and its late result. Nothing is undone when CIRANA closes: the windows simply stay.

Only ``SetWindowPos`` / ``ShowWindowAsync`` / ``SetForegroundWindow`` (through cirana.wm.controller) are used: no
re-parenting, no style changes, nothing injected into other processes; taskbar buttons, Alt+Tab and focus behave as
without CIRANA.
"""
from __future__ import annotations

import json
import logging
import math
import time
import uuid
from collections import deque
from dataclasses import dataclass, field

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QGuiApplication

from cirana.core.background import Job, run_async
from cirana.core.workspace_controller import EntryState, WorkspaceController
from cirana.native import win32_api as w32
from cirana.storage.json_store import JsonStore
from cirana.wm import bsp
from cirana.wm.controller import TOLERANCE, Placement, Result, Status
from cirana.wm.identity import Rect, TrackedWindow
from cirana.wm.layout import (BSP, GRID, MAIN_SIDE, SIDE_BY_SIDE, LayoutSpec, Size, clamp_into, fixed_kind,
                              from_fraction, grow, keep_visible, overflowing, tile, to_fraction)
from cirana.wm.monitors import Monitor, MonitorRef, monitor_for_rect, resolve
from cirana.wm.navigation import adjacent_monitor, neighbor
from cirana.wm.rules import FLOAT, IGNORE, WORKSPACE, WindowRule, first_match, read_rules

log = logging.getLogger(__name__)

DEBOUNCE_MS = 150  # events within this time produce one re-tile
DISPLAY_DEBOUNCE_MS = 600  # Windows reports a display change in several steps
RATE_LIMIT = 8  # re-tiles ...
RATE_WINDOW = 4.0  # ... within this many seconds; beyond that, wait
RATE_BACKOFF_MS = 2000
GLOBAL = "*"  # scope id of all-window tiling (workspace scopes use the workspace id)
RULE_SLOT = "win:"  # slot id prefix of a window a window rule assigns to a workspace (it has no entry)
RESIZE_STEP = 0.05  # one resize command: 5 % of the split / of the whole width or height
BEHIND, MINIMIZE = "behind", "minimize"  # how a stack's hidden windows are kept out of sight


def _center_in(rect: Rect, point: tuple[int, int]) -> bool:
    return rect[0] <= point[0] < rect[2] and rect[1] <= point[1] < rect[3]


def _center(rect: Rect) -> tuple[int, int]:
    return (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2


def _off_by(a: Rect, b: Rect) -> int:
    return max(abs(x - y) for x, y in zip(a, b))


@dataclass
class Unit:
    """One tile: a window, or a stack of windows of which one (``active``) is shown."""

    id: str  # the window's slot id, or the stack id
    members: list[tuple[str, TrackedWindow]]  # (slot id, window), in stack order; only windows that take part
    active: str  # slot id of the shown window

    @property
    def window(self) -> TrackedWindow:
        return next(window for slot, window in self.members if slot == self.active)

    @property
    def stacked(self) -> bool:
        return len(self.members) > 1

    def hidden(self) -> list[tuple[str, TrackedWindow]]:
        return [(slot, window) for slot, window in self.members if slot != self.active]


@dataclass
class Group:
    """Windows laid out together: a workspace, or all tiled windows of one monitor."""

    id: str
    spec: LayoutSpec  # automatic layouts resolved, BSP tree synced, per-monitor adjustments applied
    units: list[Unit]  # in layout order
    # units that would take part but do not fit beside the others (minimum sizes): (unit, why)
    floating: list[tuple[Unit, str]] = field(default_factory=list)
    minimums: dict[str, Size] = field(default_factory=dict)  # unit id -> known minimum on this group's monitor
    monocle: str = ""  # unit shown over the whole work area ("" = none)
    scope: str = GLOBAL

    @property
    def slots(self) -> list[tuple[str, TrackedWindow]]:
        """The windows that are shown in tiles: (slot id, window)."""
        return [(unit.active, unit.window) for unit in self.units]


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
        self._user = False  # the pending re-tile was asked for by a keyboard command (not rate limited)
        self._raise: list[TrackedWindow] | bool = False
        self._focus_after: TrackedWindow | None = None  # give this window the focus after the next re-tile
        self._dragging: set[str] = set()  # keys of tiled windows the user is dragging / resizing
        self._denied: set[str] = set()  # windows Windows refuses to let CIRANA move: left out of the layout
        self._reported: set[tuple[str, str]] = set()  # (window key, status) already announced
        self._targets: dict[str, tuple[str, Rect]] = {}  # unit id -> (group id, rect of the last re-tile)
        self._order: list[str] = []  # all-window tiling: window keys in the order they joined
        self._home: dict[str, str] = {}  # all-window tiling: window key -> monitor device it is tiled on
        self._shown: dict[str, tuple] = {}  # window key -> (minimized, maximized, resizable) last seen
        self._minimums: dict[str, tuple[int, int, int]] = {}  # window key -> (width, height, at dpi) it insisted on
        self._trees: dict[str, bsp.Node | None] = {}  # all-window tiling: monitor device -> BSP tree of unit ids
        self._global_stacks: list[dict] = []  # all-window tiling: stacks of window keys
        self._monocle: dict[str, tuple[str, frozenset]] = {}  # group id -> (unit id, the units when it started)
        self._focus_hint: dict[str, str] = {}  # group id -> unit that had the focus last (BSP: new windows split it)
        self._user_float: set[str] = set()  # windows the user took out of tiling with the keyboard
        self._stack_hidden: set[str] = set()  # keys of windows CIRANA minimized for a stack (``stack_hide``)
        self._scope_seen = ""  # the scope of the last re-tile (stack windows are shown again when it changes)
        self._on_top: dict[str, tuple] = {}  # unit id -> what was raised last (stacks, monocle): raised on change
        self._history: deque[float] = deque()
        self._last_groups: list[Group] = []
        self._preview: Rect | None = None
        self._rules: list[WindowRule] = read_rules(settings.get("window_rules")) if settings is not None else []
        raw_adjust = settings.get("global_adjust") if settings is not None else None
        self._adjust: dict[str, dict] = dict(raw_adjust) if isinstance(raw_adjust, dict) else {}
        self._hidden_store = (JsonStore(settings.root / "stack_hidden.json", lambda: {"windows": []}, 1,
                                        lambda d: isinstance(d, dict) and isinstance(d.get("windows"), list))
                              if settings is not None and getattr(settings, "root", None) else None)
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
        tracker.foreground_changed.connect(self._on_focus)
        wm.statuses_changed.connect(lambda _w: self.schedule())
        wm.workspaces.changed.connect(self.schedule)
        wm.workspaces.active_changed.connect(self._on_scope_changed)
        self._watch_displays()
        self._recover_hidden()

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
            for key in ("free", "monitor", "bsp", "stacks"):
                data.pop(key, None)  # every monitor has its own layout of the same kind (trees: runtime state)
            self.settings.set("global_layout", data)
            self._set_adjust({})  # a new layout starts from its own proportions on every monitor
            self.schedule(force=True)

    def _set_adjust(self, adjust: dict[str, dict]) -> None:
        self._adjust = adjust
        if self.settings is not None:
            self.settings.set("global_adjust", adjust)

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

    @property
    def stack_mode(self) -> str:
        mode = self.settings.get("stack_hide") if self.settings is not None else BEHIND
        return MINIMIZE if mode == MINIMIZE else BEHIND

    def set_stack_mode(self, mode: str) -> None:
        if self.settings is not None and mode in (BEHIND, MINIMIZE):
            self.settings.set("stack_hide", mode)
            self.state_changed.emit(self._scope())
            self.schedule(force=True, user=True)

    # -- window rules --------------------------------------------------------------------------------------------
    def rules(self) -> list[WindowRule]:
        return list(self._rules)

    def set_rules(self, rules: list[WindowRule]) -> None:
        self._rules = [rule for rule in rules if not rule.problem()]
        if self.settings is not None:
            self.settings.set("window_rules", [rule.to_dict() for rule in self._rules])
        self.state_changed.emit(self._scope())
        self.wm.statuses_changed.emit("")  # ignored windows are no longer bound, assigned ones join
        self.schedule(force=True)

    def rule_for(self, window: TrackedWindow) -> WindowRule | None:
        return first_match(self._rules, window)

    def ignores(self, window: TrackedWindow) -> bool:
        """A window rule says CIRANA must not manage this window at all."""
        rule = self.rule_for(window)
        return rule is not None and rule.action == IGNORE

    def rule_windows(self, workspace_id: str) -> list[TrackedWindow]:
        """Open windows a window rule assigns to this workspace."""
        if not any(rule.action == WORKSPACE and rule.workspace_id == workspace_id for rule in self._rules):
            return []
        result = []
        for window in self.wm.tracker.windows():
            rule = self.rule_for(window)
            if rule is not None and rule.action == WORKSPACE and rule.workspace_id == workspace_id:
                result.append(window)
        return result

    def float_reason(self, window: TrackedWindow) -> str:
        """Why a window is not tiled by all-window tiling ("" when it is)."""
        reason = self._excluded(window)
        if reason:
            return reason
        return next((why for group in self._last_groups for unit, why in group.floating
                     if any(w.key == window.key for _s, w in unit.members)), "")

    def _rule_reason(self, window: TrackedWindow) -> str:
        rule = self.rule_for(window)
        if rule is not None and rule.action == IGNORE:
            return f"ignored (window rule “{rule.label}”)"
        if rule is not None and rule.action == FLOAT:
            return f"floating (window rule “{rule.label}”)"
        if window.key in self._user_float:
            return "floating (you chose so for this window)"
        return ""

    def _excluded(self, window: TrackedWindow) -> str:
        """Why a window cannot take part in all-window tiling at all ("" when it can)."""
        if window.exe_name.lower() in self.exclusions():
            return "floating (you chose so for this program)"
        reason = self._rule_reason(window)
        if reason:
            return reason
        if not window.resizable:
            return "floating (fixed size or full screen)"
        if window.minimized and window.key not in self._stack_hidden:
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
        """A workspace's windows that take part: bound (reliably matched) or assigned by a window rule, not
        minimized (unless CIRANA hid them in a stack) or maximized, movable, not floated by a rule or the user."""
        result, bound = [], set()
        for status in self.wm.statuses(workspace_id):
            window = status.window
            if status.state is not EntryState.RUNNING or window is None:
                continue
            bound.add(window.key)
            if (window.minimized and window.key not in self._stack_hidden) or window.maximized \
                    or window.key in self._denied or self._rule_reason(window):
                continue
            result.append((status.entry.id, window))
        for window in self.rule_windows(workspace_id):
            if window.key in bound or (window.minimized and window.key not in self._stack_hidden) \
                    or window.maximized or window.key in self._denied or window.key in self._user_float \
                    or not window.resizable:
                continue
            result.append((RULE_SLOT + window.key, window))
        return result

    def tiled_windows(self) -> list[TrackedWindow]:
        """All-window tiling: the windows that take part right now (every member of a stack)."""
        if not self.global_on:
            return []
        return [window for group in self._groups() for unit in group.units for _s, window in unit.members]

    def _groups(self, swap: tuple[str, str] | None = None,
                rehome: tuple[list[str], str] | None = None) -> list[Group]:
        """The groups as they are now (``swap`` / ``rehome``: as they would be after exchanging two units, or after
        moving these windows to another monitor; for the drop preview, nothing is changed)."""
        if self.global_on:
            return self._global_groups(swap, rehome)
        workspace_id = self.wm.workspaces.active_id
        if self.manages(workspace_id):
            spec = LayoutSpec.from_dict(self.wm.workspaces.get(workspace_id).layout.to_dict())
            candidates = self.tile_set(workspace_id)
            if swap is not None:
                candidates = self._swapped_order(candidates, swap)
            return [self._group(workspace_id, spec, candidates, self._monitors(), workspace_id, swap)]
        return []

    def _swapped_order(self, candidates: list[tuple[str, TrackedWindow]], swap: tuple[str, str]) -> list:
        """The candidates with the first windows (in order) of two units exchanged."""
        ids = [slot for slot, _w in candidates]
        leads = [next((s for s in ids if s in self._members(unit)), None) for unit in swap]
        if None in leads:
            return candidates
        result = list(candidates)
        i, j = ids.index(leads[0]), ids.index(leads[1])
        result[i], result[j] = result[j], result[i]
        return result

    # -- stacks --------------------------------------------------------------------------------------------------
    def _stack_list(self, scope: str) -> list[dict]:
        if scope == GLOBAL:
            return self._global_stacks
        workspace = self.wm.workspaces.get(scope)
        return workspace.layout.stacks if workspace is not None else []

    def _save_stacks(self, scope: str, stacks: list[dict]) -> None:
        stacks = [s for s in stacks if len(s["members"]) >= 2]
        if scope == GLOBAL:
            self._global_stacks = stacks
        elif self.wm.workspaces.get(scope) is not None:
            self.wm.workspaces.update_tiling(scope, stacks=stacks)

    def _units(self, scope: str, candidates: list[tuple[str, TrackedWindow]]) -> list[Unit]:
        stacks = self._stack_list(scope)
        present = dict(candidates)
        member_of = {member: stack for stack in stacks for member in stack["members"]}
        units, done = [], set()
        for slot, window in candidates:
            stack = member_of.get(slot)
            if stack is None:
                units.append(Unit(slot, [(slot, window)], slot))
            elif stack["id"] not in done:
                done.add(stack["id"])
                members = [(m, present[m]) for m in stack["members"] if m in present]
                active = stack["active"] if stack["active"] in present else members[0][0]
                units.append(Unit(stack["id"], members, active))
        return units

    def _members(self, unit_id: str) -> list[str]:
        """The slot ids of a unit of the last re-tile (a window's own slot id names a unit of one)."""
        for group in self._last_groups:
            for unit in group.units + [u for u, _why in group.floating]:
                if unit.id == unit_id:
                    return [slot for slot, _w in unit.members]
        return [unit_id]

    def shown_groups(self) -> list[Group]:
        """The groups as the last re-tile laid them out."""
        return list(self._last_groups)

    def tile_of(self, window_key: str) -> tuple[Group, Unit] | None:
        """The group and tile (unit) a window is shown in by the last re-tile (None: it is not tiled)."""
        return self._unit_of(window_key)

    def _unit_of(self, window_key: str) -> tuple[Group, Unit] | None:
        for group in self._last_groups:
            for unit in group.units:
                if any(window.key == window_key for _s, window in unit.members):
                    return group, unit
        return None

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

    def _tree_of(self, group_id: str, spec: LayoutSpec, scope: str) -> bsp.Node | None:
        return self._trees.get(group_id) if scope == GLOBAL else bsp.from_dict(spec.bsp)

    def _set_tree(self, group: Group, tree: bsp.Node | None) -> None:
        group.spec.bsp = bsp.to_dict(tree)
        if group.scope == GLOBAL:
            self._trees[group.id] = tree
        elif self.wm.workspaces.get(group.scope) is not None:
            self.wm.workspaces.update_tiling(group.scope, bsp=bsp.to_dict(tree))

    @staticmethod
    def _inner(spec: LayoutSpec, monitor: Monitor) -> tuple[Rect, int]:
        """The area tiles fill (the work area minus the outer gap) and the gap, as tile() computes them."""
        gap = round(spec.gap * monitor.scale)
        work = monitor.work
        return (work[0] + gap, work[1] + gap, work[2] - gap, work[3] - gap), gap

    def _group(self, group_id: str, spec: LayoutSpec, candidates: list[tuple[str, TrackedWindow]],
               monitors: list[Monitor], scope: str, swap: tuple[str, str] | None = None) -> Group:
        """A group of windows laid out together; units whose minimum sizes do not fit beside the others float."""
        units = self._units(scope, candidates)
        spec = fixed_kind(spec, len(units))  # automatic: resolved once, before minimum sizes are considered
        if scope == GLOBAL:
            for key, value in self._adjust.get(group_id, {}).items():
                setattr(spec, key, value)
        monitor = resolve(spec.monitor, monitors)
        if monitor is None:
            return Group(group_id, spec, units, scope=scope)
        ids = [unit.id for unit in units]
        if spec.kind_for(len(units)) == BSP:
            tree = self._tree_of(group_id, spec, scope)
            if swap is not None:
                tree = self._swap_tree(tree, *swap)
            area, gap = self._inner(spec, monitor)
            spec.bsp = bsp.to_dict(bsp.sync(tree, ids, area, self._focus_hint.get(group_id), gap))
        sizes = [self._unit_minimum(unit, monitor) for unit in units]
        out = set(overflowing(spec, monitor.work, sizes, monitor.scale, ids)) if any(any(m) for m in sizes) else set()
        tiled = [unit for i, unit in enumerate(units) if i not in out]
        floating = [(unit, self._overflow_note(sizes[i], monitor)) for i, unit in enumerate(units) if i in out]
        minimums = {unit.id: sizes[i] for i, unit in enumerate(units) if i not in out and any(sizes[i])}
        monocle = self._monocle.get(group_id)
        shown = monocle[0] if monocle is not None and any(u.id == monocle[0] for u in tiled) else ""
        return Group(group_id, spec, tiled, floating, minimums, shown, scope)

    def _unit_minimum(self, unit: Unit, monitor: Monitor) -> Size:
        sizes = [self.minimum(window.key, monitor) for _s, window in unit.members]
        return max(s[0] for s in sizes), max(s[1] for s in sizes)

    @staticmethod
    def _overflow_note(size: Size, monitor: Monitor) -> str:
        need = " × ".join(f"{v} px {what}" for v, what in zip(size, ("wide", "high")) if v)
        work_w, work_h = monitor.work[2] - monitor.work[0], monitor.work[3] - monitor.work[1]
        if size[0] > work_w or size[1] > work_h:
            return (f"floating: it needs at least {need}, more than this monitor's free area ({work_w} × {work_h}), "
                    "so it cannot be shown completely")
        return f"floating: it needs at least {need} and does not fit beside the other tiled windows"

    @staticmethod
    def plan(group: Group, units: list[Unit], monitors: list[Monitor]) -> list[tuple[Unit, Rect]]:
        """Where each of ``units`` (the group's units that can be placed now) goes."""
        monitor = resolve(group.spec.monitor, monitors)
        if monitor is None or not units:
            return []
        ids = [unit.id for unit in units]
        sizes = [group.minimums.get(unit.id, (0, 0)) for unit in units]
        rects = tile(group.spec, monitor.work, len(units), monitor.scale, sizes, ids)
        if group.monocle in ids:  # shown over the whole work area; the others keep their tiles behind it
            rects[ids.index(group.monocle)] = tile(group.spec, monitor.work, 1, monitor.scale)[0]
        return list(zip(units, rects))

    def managed_windows(self) -> list[TrackedWindow]:
        """The windows dynamic tiling shows in tiles right now (either scope); [] when it is off."""
        if not self.in_charge():
            return []
        return [window for group in self._groups() for _slot, window in group.slots]

    def target_of(self, window_key: str) -> Rect | None:
        """Where the last re-tile put this window (None: not tiled by it)."""
        found = self._unit_of(window_key)
        target = self._targets.get(found[1].id) if found is not None else None
        return target[1] if target is not None else None

    def out_of_sight(self, window_key: str) -> bool:
        """A stacked window that is not the shown one (raising it would cover the stack's shown window)."""
        found = self._unit_of(window_key)
        return found is not None and found[1].window.key != window_key

    def describe(self, window: TrackedWindow) -> str:
        """How dynamic tiling treats this window right now, for the window lists ("" when it does not)."""
        found = self._unit_of(window.key)
        if found is None:
            return ""
        group, unit = found
        parts = ["monocle" if unit.id == group.monocle else "tiled"]
        if unit.stacked:
            index = [s for s, _w in unit.members].index(next(s for s, w in unit.members if w.key == window.key))
            shown = "shown" if unit.window.key == window.key else "hidden"
            parts.append(f"stack {index + 1}/{len(unit.members)} ({shown})")
        return ", ".join(parts)

    def monocle_on(self) -> bool:
        return any(group.monocle for group in self._last_groups)

    def stack_count(self) -> int:
        return sum(1 for group in self._last_groups for unit in group.units if unit.stacked)

    def _monitors(self) -> list[Monitor]:
        try:
            return self.wm.tracker.backend.monitors()
        except Exception:  # noqa: BLE001
            log.debug("Monitors unavailable", exc_info=True)
            return []

    def _global_groups(self, swap: tuple[str, str] | None = None,
                       rehome: tuple[list[str], str] | None = None) -> list[Group]:
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
        order, home = list(self._order), dict(self._home)
        if swap is not None:
            first, second = (self._members(unit) for unit in swap)
            leads = [next((k for k in order if k in members), None) for members in (first, second)]
            if None not in leads:
                i, j = order.index(leads[0]), order.index(leads[1])
                order[i], order[j] = order[j], order[i]
                first_home, second_home = home.get(leads[0], ""), home.get(leads[1], "")
                home.update({key: second_home for key in first})
                home.update({key: first_home for key in second})
        if rehome is not None:
            home.update({key: rehome[1] for key in rehome[0]})
        return self._layout_global(order, home, {w.key: w for w in eligible}, monitors, swap)

    def _layout_global(self, order: list[str], home: dict[str, str], by_key: dict[str, TrackedWindow],
                       monitors: list[Monitor], swap: tuple[str, str] | None = None) -> list[Group]:
        base = self.global_layout()
        groups = []
        for monitor in monitors:
            # a window whose monitor was unplugged is tiled on the primary one
            slots = [(key, by_key[key]) for key in order if key in by_key
                     and resolve(MonitorRef(home.get(key, "")), monitors).device == monitor.device]
            if slots:
                spec = LayoutSpec.from_dict(base.to_dict())
                spec.monitor = monitor.ref()
                groups.append(self._group(monitor.device, spec, slots, monitors, GLOBAL, swap))
        return groups

    def _signature_of(self, groups: list[Group]) -> tuple:
        parts = []
        for group in groups:
            layout = group.spec.to_dict()
            layout.pop("free", None)
            layout.pop("stacks", None)  # stacks are in the units below
            parts.append((group.id, json.dumps(layout, sort_keys=True),
                          tuple((u.id, tuple((s, w.key) for s, w in u.members), u.active) for u in group.units),
                          tuple(w.key for u, _why in group.floating for _s, w in u.members),
                          tuple(sorted(group.minimums.items())), group.monocle))
        return GLOBAL if self.global_on else self.wm.workspaces.active_id, self.stack_mode, tuple(parts)

    # -- triggers ------------------------------------------------------------------------------------------------
    def schedule(self, force: bool = False, user: bool = False) -> None:
        if self._stopped:
            return
        self._force = self._force or force
        self._user = self._user or user
        if not self.in_charge():
            if self._stack_hidden or self._scope_seen:  # e.g. the workspace's tiling was switched off: nothing
                self._release_hidden()  # manages its stacks any more, so their hidden windows are shown again
                self._scope_seen, self._signature = "", None
            return
        if user:
            self._timer.start(0)  # the user is waiting for it: no debounce, no back-off
        elif not (self._timer.isActive() and self._timer.interval() in (RATE_BACKOFF_MS, 0)):
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
        self._user_float.discard(window.key)
        if window.key in self._stack_hidden:
            self._stack_hidden.discard(window.key)
            self._save_hidden()
        if any(window.key in stack["members"] for stack in self._global_stacks):
            self._leave_stack(GLOBAL, window.key)  # a closed window leaves its stack (the next one is shown)
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
        old = self._shown.get(window.key)
        if old != state:
            self._shown[window.key] = state
            if old is not None and old[0] and not window.minimized:
                self._restored(window)  # a stacked window shown again by the user (taskbar): it is the shown one
            if old is not None and self.global_on:
                self.schedule()

    def _restored(self, window: TrackedWindow) -> None:
        hidden = window.key in self._stack_hidden
        if hidden:
            self._stack_hidden.discard(window.key)
            self._save_hidden()
        found = self._stack_containing(window.key)
        if found is not None:
            scope, stack, slot = found
            if stack["active"] != slot:
                stack["active"] = slot
                self._save_stacks(scope, self._stack_list(scope))
            self.schedule(force=True, user=True)
        elif hidden:
            self.schedule(force=True)

    def _stack_containing(self, window_key: str) -> tuple[str, dict, str] | None:
        """(scope, stack, slot id) of the stack this window is a member of, in the scope managed now."""
        scope = self._scope()
        if not scope:
            return None
        if scope == GLOBAL:
            slots = {window_key}
        else:
            slots = {RULE_SLOT + window_key} | {s.entry.id for s in self.wm.statuses(scope)
                                                 if s.window is not None and s.window.key == window_key}
        for stack in self._stack_list(scope):
            slot = next((m for m in stack["members"] if m in slots), None)
            if slot is not None:
                return scope, stack, slot
        return None

    def _on_focus(self, key: str) -> None:
        found = self._unit_of(key) if key else None
        if found is None:
            return
        group, unit = found
        if unit.id in bsp.leaves(bsp.from_dict(group.spec.bsp)) or group.spec.kind_for(len(group.units)) != BSP:
            self._focus_hint[group.id] = unit.id  # BSP: the next window splits this tile
        if unit.stacked and unit.window.key != key:  # a hidden stacked window was brought up (Alt+Tab)
            slot = next(s for s, w in unit.members if w.key == key)
            stack = next((s for s in self._stack_list(group.scope) if s["id"] == unit.id), None)
            if stack is not None:
                stack["active"] = slot
                self._save_stacks(group.scope, self._stack_list(group.scope))
                self.schedule(force=True, user=True)

    def _on_scope_changed(self, _workspace_id: str = "") -> None:
        self._cancel()
        self._signature = None
        self._dragging.clear()
        self._set_preview(None)
        self._targets.clear()
        self._last_groups = []
        if self._scope() != self._scope_seen:
            self._release_hidden()  # the stacks of the scope that is no longer managed show all their windows
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
        if self.global_on:
            return GLOBAL
        return self.wm.workspaces.active_id if self.manages(self.wm.workspaces.active_id) else ""

    def _on_move_size(self, window: TrackedWindow, started: bool) -> None:
        if not self.in_charge():
            return
        tiled = {w.key: (group, unit) for group in self._groups() for unit in group.units
                 for _s, w in [(unit.active, unit.window)]}
        if window.key not in tiled and window.key not in self._dragging:
            return
        if started:
            self._dragging.add(window.key)
            self._cancel()  # never move a window while the user holds it (or one of its neighbours)
            group = tiled[window.key][0] if window.key in tiled else None
            if group is not None and group.monocle and self._monocle.pop(group.id, None) is not None:
                self._signature = None  # dragging ends monocle: the window goes back into the layout
            self.state_changed.emit(self._scope())
            self._update_preview(window)
            return
        self._dragging.discard(window.key)
        self._set_preview(None)
        if window.key in tiled:
            self._dropped(tiled[window.key][1].id, window)
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
        found = self._unit_of(window.key)
        self._set_preview(self.predict_drop(found[1].id, window.rect) if found is not None else None)

    def predict_drop(self, unit: str, frame: Rect) -> Rect | None:
        """Where the tile ``unit`` lands if its window is dropped with its visible frame at ``frame``: exactly what
        _dropped + the following re-tile do (swap with the tile under its centre, join another monitor's layout,
        or back to its own tile). None when there is no valid target."""
        own = self._targets.get(unit)
        center = _center(frame)
        other = next((other for other, (_g, rect) in self._targets.items()
                      if other != unit and _center_in(rect, center)), None)
        if other is not None:
            return self._simulate(unit, swap=other)  # a swap: the layout with the two exchanged
        if self.global_on:
            monitors = self._monitors()
            monitor = monitor_for_rect(frame, monitors)
            if monitor is None:
                return None  # off every monitor: nothing to show
            group = next((g for g in self._last_groups if any(u.id == unit for u in g.units)), None)
            if group is not None and monitor.device != group.id:
                return self._simulate(unit, home=monitor.device)  # it joins that monitor's layout
        return own[1] if own is not None else None

    def _simulate(self, unit: str, swap: str | None = None, home: str | None = None) -> Rect | None:
        """The tile ``unit`` gets in the layout as it will be after the drop (the same planning as the re-tile,
        minimum sizes included). None: it would float."""
        monitors = self._monitors()
        groups = self._groups(swap=(unit, swap) if swap is not None else None,
                              rehome=(self._members(unit), home) if home is not None else None)
        for group in groups:
            for planned, rect in self.plan(group, group.units, monitors):
                if planned.id == unit:
                    return rect
        return None

    def _dropped(self, unit: str, window: TrackedWindow) -> None:
        try:
            frame = self.wm.tracker.backend.frame_rect(window.hwnd)
        except Exception:  # noqa: BLE001 - the window closed while it was dropped
            return
        own = self._targets.get(unit)
        if own is not None and frame == own[1]:
            return  # not moved at all
        center = _center(frame)
        target = next((other for other, (_g, rect) in self._targets.items()
                       if other != unit and _center_in(rect, center)), None)
        if target is not None:
            self._swap(unit, target)
            return
        if self.global_on:  # dropped on another monitor (outside its tiles): it joins that monitor's layout
            monitor = monitor_for_rect(frame, self._monitors())
            if monitor is not None and monitor.device != self._home.get(window.key):
                for key in self._members(unit):
                    self._home[key] = monitor.device

    @staticmethod
    def _swap_tree(tree: bsp.Node | None, first: str, second: str) -> bsp.Node | None:
        """A tree after two units exchanged places: swapped when both are in it, renamed when one of them came
        from another monitor's tree."""
        leaves = bsp.leaves(tree)
        if first in leaves and second in leaves:
            return bsp.swap(tree, first, second)
        if first in leaves:
            return bsp.rename(tree, first, second)
        if second in leaves:
            return bsp.rename(tree, second, first)
        return tree

    def _swap(self, first: str, second: str) -> None:
        """Two units exchange places (the order, their monitors, and BSP trees)."""
        for group in self._last_groups:
            if group.spec.kind_for(len(group.units)) == BSP:
                tree = bsp.from_dict(group.spec.bsp)
                swapped = self._swap_tree(tree, first, second)
                if bsp.to_dict(swapped) != bsp.to_dict(tree):
                    self._set_tree(group, swapped)
        first_members, second_members = self._members(first), self._members(second)
        if not self.global_on:
            entries = {entry.id for entry in self.wm.workspaces.active().apps} if self.wm.workspaces.active() else set()
            order = [s.entry.id for s in self.wm.statuses(self.wm.workspaces.active_id)]
            leads = [next((e for e in order if e in members), None) for members in (first_members, second_members)]
            if None not in leads and set(leads) <= entries:
                self.wm.workspaces.swap_entries(self.wm.workspaces.active_id, leads[0], leads[1])
            return
        leads = [next((k for k in self._order if k in members), None) for members in (first_members, second_members)]
        if None in leads:
            return
        i, j = self._order.index(leads[0]), self._order.index(leads[1])
        self._order[i], self._order[j] = self._order[j], self._order[i]
        first_home, second_home = self._home.get(leads[0], ""), self._home.get(leads[1], "")
        self._home.update({key: second_home for key in first_members})
        self._home.update({key: first_home for key in second_members})

    # -- keyboard commands (GUI thread; each says what it did through WorkspaceController.message) ---------------
    def focused_window(self) -> TrackedWindow | None:
        """The application window with the keyboard focus: the foreground window when it is one (read back from
        Windows, the tracker's last report can lag behind), else the last one the tracker saw (the user clicked
        CIRANA's own window)."""
        tracker = self.wm.tracker
        try:
            hwnd = tracker.backend.foreground_window()
        except Exception:  # noqa: BLE001 - a backend without it (tests)
            hwnd = 0
        window = tracker.inventory.by_hwnd(hwnd) if hwnd else None
        if window is not None:
            return window
        return tracker.get(tracker.foreground_key) if tracker.foreground_key else None

    def _say(self, text: str) -> None:
        self.wm.message.emit(text)

    def _focused_unit(self, quiet: bool = False, window: TrackedWindow | None = None,
                      ) -> tuple[TrackedWindow, Group, Unit] | None:
        """The focused window (or ``window``), its group and its tile; None (and why) when it is not tiled."""
        window = window or self.focused_window()
        if window is None:
            if not quiet:
                self._say("No application window has the focus.")
            return None
        if not self.in_charge():
            if not quiet:
                self._say("Dynamic tiling is off: turn on Tile all windows, or a workspace's Dynamic tiling.")
            return None
        found = self._unit_of(window.key)
        if found is None:
            if not quiet:
                reason = self._excluded(window) if self.global_on else self._rule_reason(window)
                self._say(f"{window.display_title} is not tiled" + (f" ({reason})." if reason else "."))
            return None
        return window, found[0], found[1]

    def focus(self, window: TrackedWindow) -> None:
        controller = self.wm.windows

        def done(result: Result) -> None:
            if not result.ok:
                self._say(result.message)

        run_async(lambda _cancel: controller.focus(window), done)

    def focus_direction(self, direction: str) -> bool:
        """Give the focus to the window next to the focused one (tiles of every monitor when tiling is on; any
        visible window otherwise)."""
        window = self.focused_window()
        if window is None:
            self._say("No application window has the focus.")
            return False
        rects: dict[str, Rect] = {}
        windows: dict[str, TrackedWindow] = {}
        found = self._unit_of(window.key) if self.in_charge() else None
        if found is not None:
            current, skip = self._targets.get(found[1].id, ("", window.rect))[1], found[1].id
            for group in self._last_groups:
                for unit in group.units:
                    if unit.id in self._targets:
                        rects[unit.id], windows[unit.id] = self._targets[unit.id][1], unit.window
        else:
            current, skip = window.rect, window.key
            for other in self.wm.tracker.windows():
                if not other.minimized and not self.ignores(other):
                    rects[other.key], windows[other.key] = other.rect, other
        target = neighbor(rects, current, direction, exclude=skip)
        if target is None:
            return False
        self.focus(windows[target])
        return True

    def swap_direction(self, direction: str) -> bool:
        """Move the focused window one tile that way: it swaps with the tile there; at the edge of a monitor it
        moves to the next monitor (all-window tiling: into that monitor's layout; otherwise it is moved there
        as it is and floats)."""
        window = self.focused_window()
        if window is None:
            self._say("No application window has the focus.")
            return False
        found = self._unit_of(window.key) if self.in_charge() else None
        if found is not None:
            group, unit = found
            rects = {u.id: self._targets[u.id][1] for g in self._last_groups for u in g.units
                     if u.id in self._targets and (self.global_on or g.id == group.id)}
            current = self._targets.get(unit.id, ("", window.rect))[1]
            other = neighbor({k: r for k, r in rects.items() if monitor_for_rect(r, self._monitors())
                              == monitor_for_rect(current, self._monitors())}, current, direction, exclude=unit.id)
            if other is not None:
                self._swap(unit.id, other)
                self._monocle.pop(group.id, None)
                self._focus_after = window
                self.schedule(force=True, user=True)
                return True
            if self.global_on:
                monitors = self._monitors()
                here = monitor_for_rect(current, monitors)
                there = adjacent_monitor(monitors, here, direction) if here is not None else None
                if there is None:
                    return False
                for key in self._members(unit.id):
                    self._home[key] = there.device
                self._focus_after = window
                self.schedule(force=True, user=True)
                return True
        return self.move_to_monitor(window, direction)

    def move_to_monitor(self, window: TrackedWindow, direction: str) -> bool:
        """Move a window to the monitor next to its own, at the same relative place and size (made to fit). A
        workspace-tiled window floats from then on (a workspace's layout fills one monitor)."""
        monitors = self._monitors()
        here = monitor_for_rect(window.rect, monitors)
        there = adjacent_monitor(monitors, here, direction) if here is not None else None
        if there is None:
            self._say("There is no monitor in that direction.")
            return False
        if window.minimized or window.maximized:
            self._say(f"{window.display_title} is minimized or maximized; restore it first.")
            return False

        rect = clamp_into(there.work, from_fraction(to_fraction(window.rect, here.work), there.work))
        if self._unit_of(window.key) is not None:
            self._user_float.add(window.key)
            self.schedule(force=True, user=True)
        controller = self.wm.windows
        run_async(lambda cancel: controller.place_all([Placement(window, rect)], cancel),
                  lambda results: [self._say(r.message) for r in results if not r.ok])
        return True

    def toggle_float(self, window: TrackedWindow | None = None) -> bool:
        window = window or self.focused_window()
        if window is None:
            self._say("No application window has the focus.")
            return False
        if window.key in self._user_float:
            self._user_float.discard(window.key)
            self._say(f"{window.display_title} is tiled again.")
        else:
            self._user_float.add(window.key)
            self._say(f"{window.display_title} floats: tiling leaves it where it is.")
        self.state_changed.emit(self._scope())
        self.schedule(force=True, user=True)
        return True

    def toggle_monocle(self, window: TrackedWindow | None = None) -> bool:
        """Show the focused tile over the whole work area, or go back to the layout (exactly as it was)."""
        found = self._focused_unit(window=window)
        if found is None:
            return False
        window, group, unit = found
        if group.monocle:
            self._monocle.pop(group.id, None)
            self._say("Monocle off: the layout is back.")
        else:
            self._monocle[group.id] = (unit.id, frozenset(u.id for u in group.units))
            self._focus_after = window
            self._say(f"Monocle: {window.display_title} fills the screen (the layout behind it is kept).")
        self.state_changed.emit(self._scope())
        self.schedule(force=True, user=True)
        return True

    def resize(self, horizontal: bool, larger: bool) -> bool:
        """Make the focused tile wider / narrower (``horizontal``) or taller / lower."""
        found = self._focused_unit()
        if found is None:
            return False
        window, group, unit = found
        count = len(group.units)
        index = [u.id for u in group.units].index(unit.id)
        kind = group.spec.kind_for(count)
        delta = RESIZE_STEP if larger else -RESIZE_STEP
        what = "width" if horizontal else "height"
        changes: dict = {}
        if count < 2:
            self._say(f"{window.display_title} is the only tile: there is nothing to resize against.")
            return False
        if kind == BSP:
            tree = bsp.resize(bsp.from_dict(group.spec.bsp), unit.id, horizontal, delta)
            if tree is None:
                self._say(f"The {what} of this tile cannot change (any further).")
                return False
            self._set_tree(group, tree)
        elif kind == SIDE_BY_SIDE and horizontal:
            weights = grow(group.spec.weights, count, index, delta)
            changes = {"weights": weights} if weights else {}
        elif kind == MAIN_SIDE and horizontal:
            ratio = min(0.8, max(0.2, round(group.spec.ratio + (delta if index == 0 else -delta), 4)))
            changes = {"ratio": ratio} if ratio != group.spec.ratio else {}
        elif kind == MAIN_SIDE and index > 0:
            heights = grow(group.spec.heights, count - 1, index - 1, delta)
            changes = {"heights": heights} if heights else {}
        else:
            layout = {SIDE_BY_SIDE: "side by side", MAIN_SIDE: "the main window of main + side panel",
                      GRID: "a grid"}.get(kind, "this layout")
            self._say(f"The {what} of a tile in {layout} cannot be changed on its own; BSP can resize every tile.")
            return False
        if kind != BSP:
            if not changes:
                self._say(f"The {what} of this tile cannot change any further.")
                return False
            if group.scope == GLOBAL:
                adjust = dict(self._adjust)
                adjust[group.id] = {**adjust.get(group.id, {}), **changes}
                self._set_adjust(adjust)
            else:
                self.wm.workspaces.update_tiling(group.scope, **changes)
        self.schedule(force=True, user=True)
        return True

    # -- stacks: commands ----------------------------------------------------------------------------------------
    def stack_direction(self, direction: str) -> bool:
        """Put the focused window into the tile next to it (it becomes a stack, or joins the one there)."""
        found = self._focused_unit()
        if found is None:
            return False
        window, group, unit = found
        rects = {u.id: self._targets[u.id][1] for u in group.units if u.id in self._targets}
        other = neighbor(rects, self._targets.get(unit.id, ("", window.rect))[1], direction, exclude=unit.id)
        if other is None:
            self._say("There is no tile in that direction to stack with.")
            return False
        return self.stack_with(window, next(u for u in group.units if u.id == other).window)

    def stack_with(self, window: TrackedWindow, target: TrackedWindow) -> bool:
        """``window`` joins ``target``'s tile as a stack, and is the one shown."""
        mine, theirs = self._unit_of(window.key), self._unit_of(target.key)
        if mine is None or theirs is None or mine[0].id != theirs[0].id:
            self._say("Only two windows tiled in the same layout can be stacked.")
            return False
        group, unit, other = mine[0], mine[1], theirs[1]
        if unit.id == other.id:
            return False
        scope = group.scope
        slot = next(s for s, w in unit.members if w.key == window.key)
        stacks = [dict(s, members=list(s["members"])) for s in self._stack_list(scope)]
        for stack in stacks:  # leaving its own stack first
            if slot in stack["members"]:
                stack["members"].remove(slot)
                if stack["active"] == slot and stack["members"]:
                    stack["active"] = stack["members"][0]
        target_stack = next((s for s in stacks if s["id"] == other.id), None)
        tree = bsp.from_dict(group.spec.bsp)
        if target_stack is None:  # a new stack takes the other tile's place
            target_stack = {"id": f"stack-{uuid.uuid4().hex[:6]}", "members": [other.active], "active": other.active}
            stacks.append(target_stack)
            tree = bsp.rename(tree, other.id, target_stack["id"])
        target_stack["members"].append(slot)
        target_stack["active"] = slot
        tree = self._dissolve(stacks, tree)
        if not unit.stacked:
            tree = bsp.remove(tree, unit.id)
        self._save_stacks(scope, stacks)
        if group.spec.kind_for(len(group.units)) == BSP:
            self._set_tree(group, tree)
        self._focus_after = window
        self._say(f"{window.display_title} is stacked with {target.display_title}.")
        self.state_changed.emit(scope)
        self.schedule(force=True, user=True)
        return True

    @staticmethod
    def _dissolve(stacks: list[dict], tree: bsp.Node | None) -> bsp.Node | None:
        """Stacks left with one window end: that window takes the stack's tile."""
        for stack in [s for s in stacks if len(s["members"]) < 2]:
            stacks.remove(stack)
            tree = bsp.rename(tree, stack["id"], stack["members"][0]) if stack["members"] \
                else bsp.remove(tree, stack["id"])
        return tree

    def unstack(self, window: TrackedWindow | None = None) -> bool:
        """The focused window leaves its stack and gets a tile of its own next to it."""
        found = self._focused_unit(window=window)
        if found is None:
            return False
        window, group, unit = found
        if not unit.stacked:
            self._say(f"{window.display_title} is not in a stack.")
            return False
        slot = next(s for s, w in unit.members if w.key == window.key)
        self._leave_stack(group.scope, slot, group, beside=True)
        self._focus_after = window
        self._say(f"{window.display_title} left the stack.")
        return True

    def _leave_stack(self, scope: str, slot: str, group: Group | None = None, beside: bool = False) -> None:
        stacks = [dict(s, members=list(s["members"])) for s in self._stack_list(scope)]
        stack = next((s for s in stacks if slot in s["members"]), None)
        if stack is None:
            return
        stack["members"].remove(slot)
        if stack["active"] == slot and stack["members"]:
            stack["active"] = stack["members"][0]
        group = group or next((g for g in self._last_groups if any(u.id == stack["id"] for u in g.units)), None)
        tree = bsp.from_dict(group.spec.bsp) if group is not None else None
        tree = self._dissolve(stacks, tree)
        if beside and group is not None and group.spec.kind_for(len(group.units)) == BSP:
            monitor = resolve(group.spec.monitor, self._monitors())
            if monitor is not None:
                area, gap = self._inner(group.spec, monitor)
                near = stack["id"] if len(stack["members"]) >= 2 else stack["members"][0]
                tree = bsp.insert(tree, slot, area, near, gap)
        self._save_stacks(scope, stacks)
        if group is not None and group.spec.kind_for(len(group.units)) == BSP:
            self._set_tree(group, tree)
        self.state_changed.emit(scope)
        self.schedule(force=True, user=True)

    def stack_cycle(self, step: int) -> bool:
        """Show the next (``step`` 1) or previous (-1) window of the focused stack."""
        found = self._focused_unit()
        if found is None:
            return False
        _window, group, unit = found
        if not unit.stacked:
            self._say("The focused window is not in a stack.")
            return False
        slots = [s for s, _w in unit.members]
        slot = slots[(slots.index(unit.active) + step) % len(slots)]
        self._show_in_stack(group, unit, slot)
        return True

    def _show_in_stack(self, group: Group, unit: Unit, slot: str) -> None:
        stacks = self._stack_list(group.scope)
        stack = next((s for s in stacks if s["id"] == unit.id), None)
        if stack is None:
            return
        stack["active"] = slot
        self._save_stacks(group.scope, stacks)
        window = dict(unit.members)[slot]
        if window.key in self._stack_hidden:
            self._stack_hidden.discard(window.key)  # the re-tile shows it (no "user restored it" event then)
            self._save_hidden()
        self._focus_after = window
        self.schedule(force=True, user=True)

    def show_stacked(self, window: TrackedWindow) -> bool:
        """Show this window of its stack (the window list's button)."""
        found = self._unit_of(window.key)
        if found is None or not found[1].stacked:
            return False
        slot = next(s for s, w in found[1].members if w.key == window.key)
        self._show_in_stack(found[0], found[1], slot)
        return True

    # -- stacks: hidden windows ----------------------------------------------------------------------------------
    def _wanted_hidden(self, groups: list[Group]) -> dict[str, TrackedWindow]:
        if self.stack_mode != MINIMIZE:
            return {}
        return {window.key: window for group in groups for unit in group.units + [u for u, _ in group.floating]
                for _slot, window in unit.hidden()}

    def _save_hidden(self) -> None:
        if self._hidden_store is None:
            return
        windows = [self.wm.tracker.get(key) for key in sorted(self._stack_hidden)]
        try:
            self._hidden_store.save({"windows": [{"hwnd": w.hwnd, "pid": w.pid, "created": w.process_created}
                                                 for w in windows if w is not None]})
        except (OSError, ValueError):
            log.warning("Could not write the list of stacked windows CIRANA minimized", exc_info=True)

    def _release_hidden(self) -> None:
        """Show every window CIRANA minimized for a stack again (without taking the focus)."""
        if not self._stack_hidden:
            return
        backend = self.wm.tracker.backend
        for key in list(self._stack_hidden):
            window = self.wm.tracker.get(key)
            try:
                if window is not None and backend.still_same(window) and backend.is_minimized(window.hwnd):
                    backend.show_async(window.hwnd, w32.SW_SHOWNOACTIVATE)
            except Exception:  # noqa: BLE001 - one window's problem never keeps the others hidden
                log.warning("Could not show a stacked window again", exc_info=True)
        self._stack_hidden.clear()
        self._save_hidden()

    def _recover_hidden(self) -> None:
        """After a crash: show the windows an earlier CIRANA minimized for a stack (if they are still the same
        windows and still minimized)."""
        if self._hidden_store is None:
            return
        try:
            records = self._hidden_store.load().get("windows", [])
        except Exception:  # noqa: BLE001
            return
        if not records:
            return
        backend = self.wm.tracker.backend
        for record in records:
            try:
                window = TrackedWindow(int(record["hwnd"]), int(record["pid"]), float(record["created"]))
                if backend.still_same(window) and backend.is_minimized(window.hwnd):
                    backend.show_async(window.hwnd, w32.SW_SHOWNOACTIVATE)
                    log.info("Showed window %#x again (minimized for a stack by an earlier CIRANA)", window.hwnd)
            except (KeyError, TypeError, ValueError):
                continue
            except Exception:  # noqa: BLE001
                log.warning("Could not show a stacked window again", exc_info=True)
        self._hidden_store.save({"windows": []})

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
            self._force, self._raise, self._user, self._focus_after = False, False, False, None
            return
        if self._dragging:
            return  # the end of the drag schedules the re-tile
        groups = self._groups()
        for group in groups:  # monocle ends when its window leaves, or another one joins the layout
            monocle = self._monocle.get(group.id)
            if monocle is not None and (not group.monocle or not {u.id for u in group.units} <= monocle[1]):
                del self._monocle[group.id]
                group.monocle = ""
        for group_id in [g for g in self._monocle if g not in {group.id for group in groups}]:
            del self._monocle[group_id]
        signature = self._signature_of(groups)
        if signature == self._signature and not self._force:
            self._user = False
            return
        if not self._user and self._rate_limited():
            return
        raise_group, self._force, self._raise, self._user = self._raise, False, False, False
        focus_after, self._focus_after = self._focus_after, None
        for group in groups:  # BSP trees with the windows that came and went: kept for the next round
            if group.spec.kind_for(len(group.units)) == BSP and group.spec.bsp != (
                    bsp.to_dict(self._trees.get(group.id)) if group.scope == GLOBAL
                    else self.wm.workspaces.get(group.scope).layout.bsp):
                self._set_tree(group, bsp.from_dict(group.spec.bsp))
        self._signature = signature
        self._last_groups = groups
        self._scope_seen = signature[0]
        wanted = self._wanted_hidden(groups)
        to_hide = [window for key, window in wanted.items() if key not in self._stack_hidden]
        to_show = [window for window in (self.wm.tracker.get(k) for k in self._stack_hidden - set(wanted))
                   if window is not None]
        if to_hide or to_show:
            self._stack_hidden = set(wanted)
            self._save_hidden()
        on_top, raised = [], {}
        for group in groups:  # a stack's shown window / the monocle window goes on top when that changes
            for unit in group.units:
                if unit.stacked or unit.id == group.monocle:
                    raised[unit.id] = (unit.active, unit.id == group.monocle, tuple(s for s, _w in unit.members))
                    if self._on_top.get(unit.id) != raised[unit.id]:
                        on_top.append(unit.window)
        self._on_top = raised
        self._start(signature[0], groups, raise_group, focus_after, to_hide, to_show, on_top)

    def _start(self, scope: str, groups: list[Group], raise_group: bool | list[TrackedWindow],
               focus_after: TrackedWindow | None = None, to_hide: list[TrackedWindow] | None = None,
               to_show: list[TrackedWindow] | None = None, on_top: list[TrackedWindow] | None = None) -> None:
        self._cancel()
        generation = self._generation
        backend, controller = self.wm.tracker.backend, self.wm.windows
        behind = self.stack_mode == BEHIND
        plan = self.plan
        out_of_sight = {window.key for group in groups for unit in group.units + [u for u, _ in group.floating]
                        for _slot, window in unit.hidden()}
        self.count += 1

        def already(placement: Placement) -> Result | None:
            """The window is exactly where it belongs: it is not touched again (fewer moves, no flicker)."""
            hwnd = placement.window.hwnd
            if backend.is_minimized(hwnd) or backend.is_maximized(hwnd):
                return None
            frame = backend.frame_rect(hwnd)
            if _off_by(frame, placement.rect) > TOLERANCE:
                return None
            return Result(placement.window.key, placement.window.display_title, Status.PLACED, actual=frame)

        def job(cancel):  # noqa: ANN001 - worker thread
            monitors = backend.monitors()
            planned, refused, usable_all, floats = [], [], [], []
            for group in groups:
                usable = []
                for unit in group.units:
                    problem = controller.check(unit.window)
                    if problem is None:
                        usable.append(unit)
                    else:
                        refused.append((unit.active, problem))
                for unit, rect in plan(group, usable, monitors):
                    planned.append((group.id, unit.id, unit.active, Placement(unit.window, rect), True))
                    usable_all.append(unit.window)
                    if behind:  # the hidden windows of a stack lie under the shown one, at the same place
                        for slot, window in unit.hidden():
                            if controller.check(window) is None:
                                planned.append((group.id, unit.id, slot, Placement(window, rect), False))
                monitor = resolve(group.spec.monitor, monitors)
                for unit, _why in group.floating:  # too large to tile: only made completely visible
                    window = unit.window
                    if monitor is None or controller.check(window) is not None or backend.is_minimized(window.hwnd):
                        continue
                    frame = backend.frame_rect(window.hwnd)
                    if keep_visible(monitor.work, frame) != frame:
                        floats.append((unit.active, Placement(window, keep_visible(monitor.work, frame))))
            placements = [p for *_ids, p, _primary in planned] + [p for _s, p in floats]
            results: list[Result | None] = []
            for placement in placements:
                results.append(None if controller.check(placement.window) else already(placement))
            moving = [p for p, r in zip(placements, results) if r is None]
            moved = iter(controller.place_all(moving, cancel) if moving else [])
            results = [r if r is not None else next(moved) for r in results]
            if not cancel.is_set():
                planned_keys = {p.window.key for p in placements}
                for window in to_show or []:
                    if window.key not in planned_keys:
                        controller.show(window)  # it left its stack: shown again, without the focus
                for window in to_hide or []:
                    controller.minimize(window, quiet=True)  # a hidden window of a stack (stack_hide = minimize)
                if raise_group:  # (a stack's hidden windows stay hidden: only its shown one is raised)
                    front = raise_group if isinstance(raise_group, list) else usable_all
                    front = [window for window in front if window.key not in out_of_sight]
                    if front:
                        controller.bring_to_front(front)
                for window in on_top or []:  # a stack's shown window and the monocle window on top of their tile
                    if window in usable_all:
                        backend.raise_window(window.hwnd)
                if focus_after is not None:
                    controller.focus(focus_after)
            targets = [(unit_id, (group_id, p.rect)) for group_id, unit_id, _s, p, primary in planned if primary]
            tiled = [(slot, result) for (_g, _u, slot, _p, _primary), result in zip(planned, results)] + refused
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
        rect_of = {}  # window key -> where it was meant to go
        for group in self._last_groups:
            for unit in group.units:
                if unit.id in self._targets:
                    for _slot, window in unit.members:
                        rect_of[window.key] = self._targets[unit.id][1]
        changed, problems, learned = False, [], False
        monitors = self._monitors()
        for slot, result in results:
            if result.status is Status.DENIED and result.key not in self._denied:
                self._denied.add(result.key)  # left out from now on: the others take its space
                self.schedule(force=True)
            if result.status is Status.CONSTRAINED and result.actual is not None and result.key in rect_of:
                # larger than its tile: its minimum size. The next layout makes room for it (or floats it).
                learned = self._learn(result.key, rect_of[result.key], result.actual, monitors) or learned
            if scope != GLOBAL and not slot.startswith(RULE_SLOT):
                message = "" if result.status is Status.PLACED else result.message
                changed = self.wm.set_entry_message(scope, slot, message) or changed
            if result.status not in (Status.PLACED, Status.GONE) and (result.key, result.status.value) \
                    not in self._reported:
                self._reported.add((result.key, result.status.value))
                if not result.ok:
                    problems.append(result.message)
        for group in self._last_groups:
            for unit, why in group.floating:
                window = unit.window
                if scope != GLOBAL and not unit.active.startswith(RULE_SLOT):
                    changed = self.wm.set_entry_message(scope, unit.active, f"{window.display_title}: {why}.") \
                        or changed
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
        """CIRANA is closing: stop arranging. The windows stay exactly where they are; windows hidden in a stack
        are shown again."""
        self._stopped = True
        self._timer.stop()
        self._display_timer.stop()
        self._cancel()
        self._release_hidden()
