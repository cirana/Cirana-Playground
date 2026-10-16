"""Layout planning for DynamicTiler: which windows take part, how they are grouped into tiles (units) per monitor or
workspace, which layout each group gets (the adaptive engine, the minimum-size fallback), where every tile goes,
and what the window lists say about a window. It reads the tiler's state and changes only what it has to
remember (learned minimum sizes, the adaptive engine's choices).
"""
from __future__ import annotations

import json
import math

from cirana.core.tiling_model import FLOATING, GLOBAL, Group, IGNORED, RULE_SLOT, STACKED, TILED, Unit
from cirana.core.workspace_controller import EntryState
from cirana.wm import adaptive, bsp
from cirana.wm.controller import TOLERANCE
from cirana.wm.identity import Rect, TrackedWindow
from cirana.wm.layout import ADAPTIVE, BSP, GRID, KIND_NAMES, LayoutSpec, Size, fit_layout, fixed_kind, tile
from cirana.wm.monitors import Monitor, MonitorRef, monitor_for_rect, resolve
from cirana.wm.rules import IGNORE


class PlanMixin:
    """What is laid out and where. Methods use the state ``DynamicTiler.__init__`` sets up."""

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
        smart = spec.kind == ADAPTIVE  # the engine picks the layout below, once the minimum sizes are known
        if not smart:
            spec = fixed_kind(spec, len(units))  # automatic: resolved once, before minimum sizes are considered
        if scope == GLOBAL:
            for key, value in self._adjust.get(group_id, {}).items():
                setattr(spec, key, value)
        monitor = resolve(spec.monitor, monitors)
        if monitor is None:
            return Group(group_id, fixed_kind(spec, len(units)), units, scope=scope)
        ids = [unit.id for unit in units]
        sizes = [self._unit_minimum(unit, monitor) for unit in units]
        wanted = {unit_id: size for unit_id, size in zip(ids, sizes) if any(size)}
        out, fallback, key = set(), "", ""
        if smart or spec.kind_for(len(units)) == BSP:
            tree = self._tree_of(group_id, spec, scope)
            if swap is not None:
                tree = self._swap_tree(tree, *swap)
            area, gap = self._inner(spec, monitor)
            spec.bsp = bsp.to_dict(bsp.sync(tree, ids, area, self._focus_hint.get(group_id), gap, wanted))
        if smart:
            lock = self._lock.get(group_id, False)
            last = {unit_id: rect for unit_id, (group, rect) in self._targets.items() if group == group_id}
            choice = adaptive.choose(spec, monitor.work, monitor.scale, sizes, ids, self._adaptive.get(group_id, ""),
                                     last, hold=lock is None or lock == frozenset(ids))
            spec, out, key = choice.spec, set(choice.floating), choice.key
        elif any(any(m) for m in sizes):
            # The chosen layout is kept while every window fits. When it cannot give every window its minimum size,
            # another layout is used (a pure function of the sizes, so it cannot flip back and forth) before any
            # window is left floating.
            fitted, floated = fit_layout(spec, monitor.work, sizes, monitor.scale, ids)
            out = set(floated)
            if fitted is not spec:
                fallback = (f"{KIND_NAMES[spec.kind_for(len(units))]} → {KIND_NAMES[fitted.kind]}"
                            f"{f' ({fitted.columns} column)' if fitted.kind == GRID and fitted.columns == 1 else ''}: "
                            "the minimum sizes of the windows do not fit the chosen layout")
                spec = fitted
        tiled = [unit for i, unit in enumerate(units) if i not in out]
        floating = [(unit, self._overflow_note(sizes[i], monitor)) for i, unit in enumerate(units) if i in out]
        minimums = {unit.id: sizes[i] for i, unit in enumerate(units) if i not in out and any(sizes[i])}
        monocle = self._monocle.get(group_id)
        shown = monocle[0] if monocle is not None and any(u.id == monocle[0] for u in tiled) else ""
        return Group(group_id, spec, tiled, floating, minimums, shown, scope, fallback, key)

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
        return f"floating: it needs at least {need} and does not fit beside the other tiled windows in any layout"

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

    def status(self, window: TrackedWindow) -> tuple[str, str]:
        """What dynamic tiling does with this window right now: (``tiled`` | ``stacked`` | ``floating`` |
        ``ignored``, detail) - ("", "") when it is not in charge of the window (tiling is off, or the window is not
        part of the scope that is managed)."""
        if not self.in_charge():
            return "", ""
        rule = self.rule_for(window)
        if rule is not None and rule.action == IGNORE:
            return IGNORED, f"window rule “{rule.label}”"
        found = self._unit_of(window.key)
        if found is not None:
            group, unit = found
            parts = ["monocle"] if unit.id == group.monocle else []
            if unit.stacked:
                index = [s for s, _w in unit.members].index(next(s for s, w in unit.members if w.key == window.key))
                parts.append(f"{index + 1}/{len(unit.members)}, {'shown' if unit.window.key == window.key else 'hidden'}")
            if group.fallback:
                parts.append(f"layout adjusted: {group.fallback}")
            return (STACKED if unit.stacked else TILED), "; ".join(parts)
        if not self._in_scope(window):
            return "", ""
        reason = self.float_reason(window)
        if not reason:
            return "", ""
        for prefix in ("floating (", "floating: "):  # (the state itself is the first word)
            if reason.startswith(prefix):
                reason = reason[len(prefix):].removesuffix(")")
        return FLOATING, reason

    def _in_scope(self, window: TrackedWindow) -> bool:
        """The window is one the managed scope is in charge of (all windows; or a bound / assigned workspace window)."""
        if self.global_on:
            return True
        workspace_id = self._scope()
        if not workspace_id:
            return False
        if any(s.window is not None and s.window.key == window.key for s in self.wm.statuses(workspace_id)):
            return True
        return any(w.key == window.key for w in self.rule_windows(workspace_id))

    def monocle_on(self) -> bool:
        return any(group.monocle for group in self._last_groups)

    def mode_notes(self) -> str:
        """What is special about the layout right now, for the state lines of the pages (" · 2 stacks · monocle")."""
        stacks = self.stack_count()
        notes = f" · {stacks} stack{'s' if stacks != 1 else ''}" if stacks else ""
        return notes + (" · monocle" if self.monocle_on() else "")

    def monitor_count(self) -> int:
        """On how many monitors all-window tiling has windows right now."""
        return len({self._home.get(w.key) for w in self.tiled_windows()})

    def stack_count(self) -> int:
        return sum(1 for group in self._last_groups for unit in group.units if unit.stacked)

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

    def _remember_choices(self, groups: list[Group]) -> None:
        """What the adaptive engine decided is what it keeps (hysteresis); a hold the user asked for is tied to the
        windows laid out now."""
        for group in groups:
            if not group.adaptive_key:
                continue
            self._adaptive[group.id] = group.adaptive_key
            ids = frozenset(unit.id for unit in group.units)
            if group.id in self._lock:
                if self._lock[group.id] is None:
                    self._lock[group.id] = ids
                elif self._lock[group.id] != ids:
                    del self._lock[group.id]  # other windows: the engine decides again

    def _hold(self, group_id: str) -> None:
        """The user shaped this layout (resize, swap, stack): the adaptive engine leaves it alone for these windows."""
        self._lock[group_id] = None

    def adaptive_layouts(self) -> list[str]:
        """Names of the layouts the adaptive engine uses right now (one per monitor / workspace)."""
        return [adaptive.name_of(g.adaptive_key) for g in self._last_groups if g.adaptive_key]
