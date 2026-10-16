"""Stacks, monocle and the windows hidden for them: creating, dissolving and cycling stacks, monocle on / off, and
the bookkeeping that shows hidden windows again (also after a crash).
"""
from __future__ import annotations

import logging
import uuid

from cirana.core.tiling_model import GLOBAL, Group, MINIMIZE, RULE_SLOT, Unit
from cirana.native import win32_api as w32
from cirana.wm import bsp
from cirana.wm.identity import TrackedWindow
from cirana.wm.monitors import resolve
from cirana.wm.navigation import neighbor

log = logging.getLogger(__name__)


class StackMixin:
    """Stacks, monocle, hidden windows. Methods use the state ``DynamicTiler.__init__`` sets up."""

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
        self._hold(group.id)
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
        if group.bsp_active:
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
        for known in self._last_groups:
            self._hold(known.id)
        stack["members"].remove(slot)
        if stack["active"] == slot and stack["members"]:
            stack["active"] = stack["members"][0]
        group = group or next((g for g in self._last_groups if any(u.id == stack["id"] for u in g.units)), None)
        tree = bsp.from_dict(group.spec.bsp) if group is not None else None
        tree = self._dissolve(stacks, tree)
        if beside and group is not None and group.bsp_active:
            monitor = resolve(group.spec.monitor, self._monitors())
            if monitor is not None:
                area, gap = self._inner(group.spec, monitor)
                near = stack["id"] if len(stack["members"]) >= 2 else stack["members"][0]
                tree = bsp.insert(tree, slot, area, near, gap)
        self._save_stacks(scope, stacks)
        if group is not None and group.bsp_active:
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
