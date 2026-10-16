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
  do not fit on the monitor together, another layout is tried first; only a window no layout can hold floats, moved
  (never shrunk) so it is completely on the screen, and the reason is shown. Nothing is ever asked of the application itself (no message
  is sent to it), and what it draws inside its own window is its own business.
* **Stacks never lose a window.** The windows of a stack stay independent top-level windows (no re-parenting). By
  default the hidden ones simply lie under the shown one, at the same place; with ``stack_hide = "minimize"`` they are
  minimized instead (their taskbar buttons stay). Windows CIRANA minimized for a stack are written down
  (``stack_hidden.json``) and shown again when they leave the stack, when the scope stops being managed, when CIRANA
  closes, and after a crash at the next start.
* **Session based.** Every re-tile is a cancellable worker job with a generation; a newer re-tile, a scope change, a
  drag or CIRANA quitting drop it and its late result. Nothing is undone when CIRANA closes: the windows simply stay.

Where things are (``DynamicTiler`` below is the coordinator; its parts are mixins that share its state):

* ``tiling_model``        constants, ``Unit`` (a tile), ``Group`` (tiles laid out together), small geometry helpers;
* ``tiling_plan``         PlanMixin: the tile set, groups per monitor / workspace, the adaptive engine and minimum-size
                          fallback, learned minimum sizes, where each tile goes, what a window's status is;
* ``tiling_interactions`` InteractionMixin: mouse drags and resizes, previews, drop / swap, keyboard commands;
* ``tiling_stacks``       StackMixin: stacks, monocle, windows hidden for them (and their crash recovery);
* ``tiling_reflow``       ReflowMixin: when a re-tile runs (coalescing, rate limit, generations) and its result;
                          ``run_reflow`` is the worker-thread function that places the windows;
* ``cirana.wm.animation`` the glide before the placement (knows rectangles, not layouts); ``cirana.wm.resize`` the
                          geometry of a mouse resize; ``cirana.wm.controller`` the only code that moves windows.

Only ``SetWindowPos`` / ``ShowWindowAsync`` / ``SetForegroundWindow`` (through cirana.wm.controller) are used: no
re-parenting, no style changes, nothing injected into other processes; taskbar buttons, Alt+Tab and focus behave as
without CIRANA.
"""
from __future__ import annotations

import logging
from collections import deque

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QGuiApplication

from cirana.core.background import Job
from cirana.core.tiling_interactions import InteractionMixin
from cirana.core.tiling_model import BEHIND, DEBOUNCE_MS, DISPLAY_DEBOUNCE_MS, GLOBAL, Group, MINIMIZE, STARTUP_MS
from cirana.core.tiling_plan import PlanMixin
from cirana.core.tiling_reflow import ReflowMixin
from cirana.core.tiling_stacks import StackMixin
from cirana.core.workspace_controller import WorkspaceController
from cirana.storage.json_store import JsonStore
from cirana.wm import animation, bsp
from cirana.wm.identity import Rect, TrackedWindow
from cirana.wm.layout import ADAPTIVE, BSP, MAIN_SIDE, LayoutSpec
from cirana.wm.monitors import Monitor
from cirana.wm.rules import FLOAT, IGNORE, WORKSPACE, WindowRule, first_match, read_rules
from cirana.core.tiling_model import (FLOATING, IGNORED, RATE_BACKOFF_MS, RATE_LIMIT, RATE_WINDOW,  # noqa: F401
                                      RESIZE_STEP, RULE_SLOT, STACKED, TILED)  # (this module's API, used elsewhere)

log = logging.getLogger(__name__)


class DynamicTiler(PlanMixin, InteractionMixin, StackMixin, ReflowMixin, QObject):
    """The coordinator: owns the state, the settings (all-window tiling, its layout, floating programs, window rules),
    the signals and the reaction to window / display / workspace events. The work is in its parts, which share this
    state (they are mixins, not objects to delegate to): ``PlanMixin`` decides what is laid out and where,
    ``InteractionMixin`` is the user's mouse and keyboard, ``StackMixin`` stacks / monocle / hidden windows and
    ``ReflowMixin`` when and how the windows are actually placed."""

    reflowed = Signal(str)  # scope: workspace id, or GLOBAL
    state_changed = Signal(str)  # scope: paused / resumed (a drag), all-window tiling switched, floats changed
    # While the user drags a tiled window: where it will land if dropped now (visible frame, physical pixels), or
    # None (no valid target, or the drag is over). Computed with the same rules the drop itself uses.
    drop_preview = Signal(object)
    # While the user resizes a tiled window: where the other tiles of its layout will be (list of rects) or None.
    # ``drop_preview`` then carries the resized tile itself, so both together are the real layout after the release.
    resize_preview = Signal(object)

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
        self._drag_start: dict[str, Rect] = {}  # window key -> its visible frame when the drag started
        self._denied: set[str] = set()  # windows Windows refuses to let CIRANA move: left out of the layout
        # hwnd -> (window, outer rectangle at its target) of windows an animation has moved and the placement has not
        # confirmed yet: placed exactly (no tolerance) and snapped there if CIRANA stops meanwhile
        self._animated: dict[int, tuple[TrackedWindow, tuple]] = {}
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
        self._adaptive: dict[str, str] = {}  # group id -> layout the adaptive engine uses now (hysteresis)
        # group id -> windows the user shaped the layout for (resize, swap, stack): the engine keeps its choice while
        # they are the same ones; None = for the set that is laid out next
        self._lock: dict[str, frozenset | None] = {}
        self._user_float: set[str] = set()  # windows the user took out of tiling with the keyboard
        self._stack_hidden: set[str] = set()  # keys of windows CIRANA minimized for a stack (``stack_hide``)
        self._scope_seen = ""  # the scope of the last re-tile (stack windows are shown again when it changes)
        self._on_top: dict[str, tuple] = {}  # unit id -> what was raised last (stacks, monocle): raised on change
        self._history: deque[float] = deque()
        self._last_groups: list[Group] = []
        self._preview: Rect | None = None
        self._neighbors: list[Rect] | None = None
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
        tracker.inventory_ready.connect(self._on_inventory_ready)
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
        # (nothing stored = the user never chose: the adaptive engine; a stored choice is never replaced)
        spec = LayoutSpec.from_dict(raw) if isinstance(raw, dict) and raw else LayoutSpec(ADAPTIVE, gap=8)
        if not spec.tiled:
            spec.kind = MAIN_SIDE
        return spec

    def set_global_layout(self, spec: LayoutSpec) -> None:
        if self.settings is not None:
            old = self.global_layout()
            data = spec.to_dict()
            for key in ("free", "monitor", "bsp", "stacks"):
                data.pop(key, None)  # every monitor has its own layout of the same kind (trees: runtime state)
            if (spec.kind, spec.columns, spec.auto) != (old.kind, old.columns, old.auto):
                self._set_adjust({})  # a new layout starts from its own proportions on every monitor
                self._trees.clear()
            elif spec.ratio != old.ratio:  # the main width was typed in: it replaces the dragged one
                self._set_adjust({device: {k: v for k, v in kept.items() if k != "ratio"}
                                  for device, kept in self._adjust.items()})
            # (a different gap keeps the proportions the user dragged)
            self.settings.set("global_layout", data)
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
        reason = self._excluded(window, programs=self.global_on)
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

    def _excluded(self, window: TrackedWindow, programs: bool = True) -> str:
        """Why a window cannot take part in tiling at all ("" when it can). ``programs``: the programs chosen to float
        count (all-window tiling only)."""
        if programs and window.exe_name.lower() in self.exclusions():
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

    def _scope(self) -> str:
        if self.global_on:
            return GLOBAL
        return self.wm.workspaces.active_id if self.manages(self.wm.workspaces.active_id) else ""

    def _monitors(self) -> list[Monitor]:
        try:
            return self.wm.tracker.backend.monitors()
        except Exception:  # noqa: BLE001
            log.debug("Monitors unavailable", exc_info=True)
            return []

    def _on_inventory_ready(self) -> None:
        """The window tracker has seen every window once: only now can anything be arranged (a layout computed from a
        half-known desktop would be redone, and flicker)."""
        self._signature = None
        self._force = True
        if self.in_charge():
            self._timer.start(STARTUP_MS)

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
        self._drag_start.pop(window.key, None)
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
        animation.snap_to_targets(self.wm.windows, self._animated)  # nobody stays half way in a scope no longer managed
        self._signature = None
        self._dragging.clear()
        self._drag_start.clear()
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
