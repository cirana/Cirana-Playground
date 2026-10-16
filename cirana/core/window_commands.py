"""The window manager's commands by name: what a global shortcut (cirana.core.hotkeys) or a button does.

Every command works on the window that has the keyboard focus and says what it did (or why it did nothing)
through the status toast; none of them blocks the GUI thread.
"""
from __future__ import annotations

from typing import Callable

from cirana.core.dynamic_tiling import DynamicTiler
from cirana.wm.navigation import DOWN, LEFT, RIGHT, UP


class WindowCommands:
    def __init__(self, tiler: DynamicTiler) -> None:
        self.tiler = tiler
        self.wm = tiler.wm
        t = tiler
        self.handlers: dict[str, Callable[[], object]] = {
            **{f"focus_{d}": (lambda d=d: t.focus_direction(d)) for d in (LEFT, RIGHT, UP, DOWN)},
            **{f"swap_{d}": (lambda d=d: t.swap_direction(d)) for d in (LEFT, RIGHT, UP, DOWN)},
            **{f"stack_{d}": (lambda d=d: t.stack_direction(d)) for d in (LEFT, RIGHT, UP, DOWN)},
            "grow_width": lambda: t.resize(True, True), "shrink_width": lambda: t.resize(True, False),
            "grow_height": lambda: t.resize(False, True), "shrink_height": lambda: t.resize(False, False),
            "monocle": t.toggle_monocle, "toggle_float": t.toggle_float, "unstack": t.unstack,
            "stack_next": lambda: t.stack_cycle(1), "stack_previous": lambda: t.stack_cycle(-1),
            "move_workspace_next": lambda: self.move_to_workspace(1),
            "move_workspace_previous": lambda: self.move_to_workspace(-1),
            "workspace_next": lambda: self.activate_workspace(1),
            "workspace_previous": lambda: self.activate_workspace(-1),
        }

    def run(self, action: str) -> bool:
        handler = self.handlers.get(action)
        return bool(handler()) if handler is not None else False

    def _neighbour_workspace(self, step: int) -> str:
        workspaces = self.wm.workspaces.all()
        if not workspaces:
            return ""
        ids = [w.id for w in workspaces]
        active = self.wm.workspaces.active_id
        index = ids.index(active) if active in ids else (-1 if step > 0 else 0)
        return ids[(index + step) % len(ids)]

    def activate_workspace(self, step: int) -> bool:
        target = self._neighbour_workspace(step)
        if not target or target == self.wm.workspaces.active_id:
            self.wm.message.emit("There is no other workspace.")
            return False
        return self.wm.activate(target)

    def move_to_workspace(self, step: int) -> bool:
        window = self.tiler.focused_window()
        if window is None:
            self.wm.message.emit("No application window has the focus.")
            return False
        target = self._neighbour_workspace(step)
        if not target or target == self.wm.workspaces.active_id:
            self.wm.message.emit("Create another workspace to move windows to.")
            return False
        return self.wm.move_window(window, target)
