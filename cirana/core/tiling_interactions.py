"""The user's hands on DynamicTiler: mouse drags (a move becomes a swap or a change of monitor, a resize becomes
the layout's proportions), the grey previews, and the keyboard commands (focus, swap, move to monitor, float,
resize).
"""
from __future__ import annotations

from cirana.core.background import run_async
from cirana.core.tiling_model import GLOBAL, Group, RESIZE_STEP, Unit, center_in, center_of
from cirana.wm import bsp
from cirana.wm.controller import Placement, Result
from cirana.wm.identity import Rect, TrackedWindow
from cirana.wm.layout import BSP, GRID, MAIN_SIDE, SIDE_BY_SIDE, clamp_into, from_fraction, grow, to_fraction
from cirana.wm.monitors import monitor_for_rect, resolve
from cirana.wm.navigation import adjacent_monitor, neighbor
from cirana.wm.resize import Resize, moved_edges, resize_tile


class InteractionMixin:
    """Mouse and keyboard. Methods use the state ``DynamicTiler.__init__`` sets up."""

    def _frame(self, window: TrackedWindow) -> Rect | None:
        """The visible frame of a window as Windows has it now (the tracker's copy can lag behind a drag)."""
        try:
            return self.wm.tracker.backend.frame_rect(window.hwnd)
        except Exception:  # noqa: BLE001 - the window closed meanwhile
            return None

    def _on_move_size(self, window: TrackedWindow, started: bool) -> None:
        if not self.in_charge():
            return
        tiled = {w.key: (group, unit) for group in self._groups() for unit in group.units
                 for _s, w in [(unit.active, unit.window)]}
        if window.key not in tiled and window.key not in self._dragging:
            return
        if started:
            self._dragging.add(window.key)
            self._drag_start[window.key] = self._frame(window) or window.rect
            self._cancel()  # never move a window while the user holds it (or one of its neighbours)
            group = tiled[window.key][0] if window.key in tiled else None
            if group is not None and group.monocle and self._monocle.pop(group.id, None) is not None:
                self._signature = None  # dragging ends monocle: the window goes back into the layout
            self.state_changed.emit(self._scope())
            self._update_preview(window)
            return
        self._dragging.discard(window.key)
        start = self._drag_start.pop(window.key, None)
        self._set_preview(None)
        resized = False
        if window.key in tiled:
            group, unit = tiled[window.key]
            resized = self._resized(group, unit, window, start)  # a resize is never a drop (no swap, no move)
            if not resized:
                self._dropped(unit.id, window)
        self.state_changed.emit(self._scope())
        self.schedule(force=True, user=resized)  # the drag is over: back into the layout (the new one, if resized)

    def _resize_plan(self, group: Group, unit: Unit, frame: Rect, edges: dict[str, str]) -> Resize | str:
        """The layout after the user dragged ``edges`` of ``unit``'s window to ``frame`` (the one function behind the
        preview and the commit), or why it cannot be done."""
        if group.fallback:
            return (f"The layout is adjusted to the minimum sizes of the windows ({group.fallback}); "
                    "it cannot be resized until they fit again.")
        monitor = resolve(group.spec.monitor, self._monitors())
        if monitor is None:
            return "The monitor of this layout is not available."
        ids = [u.id for u in group.units]
        sizes = [group.minimums.get(u, (0, 0)) for u in ids]
        return resize_tile(group.spec, monitor.work, len(ids), monitor.scale, sizes, ids, ids.index(unit.id), frame,
                           edges)

    def _resized(self, group: Group, unit: Unit, window: TrackedWindow, start: Rect | None) -> bool:
        """The drag that just ended resized the window (one edge per axis moved, the opposite one stayed): the new
        size becomes the layout's proportions and the neighbours follow. False when it was a move."""
        frame = self._frame(window)
        edges = moved_edges(start, frame) if start is not None and frame is not None else None
        if edges is None:
            return False
        outcome = self._resize_plan(group, unit, frame, edges)
        if isinstance(outcome, str):
            self._say(outcome)  # the window goes back to its tile; the user is told why
            return True
        self._store_proportions(group, outcome.changes)
        for note in outcome.notes:
            self._say(note)
        return True

    def _store_proportions(self, group: Group, changes: dict) -> None:
        """Keep the proportions of a resized layout where the keyboard commands keep theirs: the BSP tree through
        ``_set_tree``; ratios and weights per monitor (all-window tiling) or in the workspace."""
        self._hold(group.id)
        if "bsp" in changes:
            self._set_tree(group, bsp.from_dict(changes["bsp"]))
        rest = {key: value for key, value in changes.items() if key != "bsp"}
        if not rest:
            return
        if group.scope == GLOBAL:
            adjust = dict(self._adjust)
            adjust[group.id] = {**adjust.get(group.id, {}), **rest}
            self._set_adjust(adjust)
        else:
            self.wm.workspaces.update_tiling(group.scope, **rest)

    def _set_preview(self, rect: Rect | None) -> None:
        if rect != self._preview:
            self._preview = rect
            self.drop_preview.emit(rect)
        if rect is None and self._neighbors is not None:
            self._neighbors = None
            self.resize_preview.emit(None)

    def _set_neighbors(self, rects: list[Rect] | None) -> None:
        if rects != self._neighbors:
            self._neighbors = rects
            self.resize_preview.emit(rects)

    def _update_preview(self, window: TrackedWindow) -> None:
        if not self.in_charge() or window.minimized or window.maximized:
            self._set_preview(None)
            return
        found = self._unit_of(window.key)
        if found is None:
            self._set_preview(None)
            return
        group, unit = found
        start, frame = self._drag_start.get(window.key), self._frame(window)
        edges = moved_edges(start, frame) if start is not None and frame is not None else None
        if edges is not None:  # being resized: the layout as it will be after the release
            outcome = self._resize_plan(group, unit, frame, edges)
            own = self._targets.get(unit.id)
            if isinstance(outcome, str):
                self._set_neighbors(None)
                self._set_preview(own[1] if own is not None else None)
            else:
                index = [u.id for u in group.units].index(unit.id)
                self._set_neighbors([rect for i, rect in enumerate(outcome.rects) if i != index])
                self._set_preview(outcome.rects[index])
            return
        self._set_neighbors(None)
        self._set_preview(self.predict_drop(unit.id, window.rect))

    def predict_drop(self, unit: str, frame: Rect) -> Rect | None:
        """Where the tile ``unit`` lands if its window is dropped with its visible frame at ``frame``: exactly what
        _dropped + the following re-tile do (swap with the tile under its centre, join another monitor's layout,
        or back to its own tile). None when there is no valid target."""
        own = self._targets.get(unit)
        center = center_of(frame)
        other = next((other for other, (_g, rect) in self._targets.items()
                      if other != unit and center_in(rect, center)), None)
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
        center = center_of(frame)
        target = next((other for other, (_g, rect) in self._targets.items()
                       if other != unit and center_in(rect, center)), None)
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
            if group.bsp_active:
                tree = bsp.from_dict(group.spec.bsp)
                swapped = self._swap_tree(tree, first, second)
                if bsp.to_dict(swapped) != bsp.to_dict(tree):
                    self._set_tree(group, swapped)
        first_members, second_members = self._members(first), self._members(second)
        for group in self._last_groups:
            self._hold(group.id)
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
        if group.fallback:
            self._say(f"The layout is adjusted to the minimum sizes of the windows ({group.fallback}); "
                      "it cannot be resized until they fit again.")
            return False
        self._hold(group.id)
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
