"""Which tile lies to the left / right / above / below another one: keyboard focus, swaps and moves between monitors.

Pure geometry over rectangles (physical pixels of the virtual screen), so it works across monitors with different
scales and never asks a window anything.
"""
from __future__ import annotations

from cirana.wm.identity import Rect

LEFT, RIGHT, UP, DOWN = "left", "right", "up", "down"
DIRECTIONS = (LEFT, RIGHT, UP, DOWN)
SLACK = 4  # pixels two tiles may overlap and still count as neighbours (rounding, borders)


def _overlap(a0: int, a1: int, b0: int, b1: int) -> int:
    return max(0, min(a1, b1) - max(a0, b0))


def neighbor(rects: dict[str, Rect], current: Rect, direction: str, exclude: str | None = None) -> str | None:
    """The id of the rectangle next to ``current`` in ``direction``: it must lie on that side (its near edge at or
    beyond ``current``'s edge), and among those the one that shares the most of ``current``'s span wins, then the
    nearest, then the one closest to ``current``'s top / left edge. None when nothing lies that way."""
    left, top, right, bottom = current
    best, best_key = None, None
    for key, rect in rects.items():
        if key == exclude or rect == current:
            continue
        if direction == LEFT:
            ok, distance, shared = rect[2] <= left + SLACK, left - rect[2], _overlap(top, bottom, rect[1], rect[3])
        elif direction == RIGHT:
            ok, distance, shared = rect[0] >= right - SLACK, rect[0] - right, _overlap(top, bottom, rect[1], rect[3])
        elif direction == UP:
            ok, distance, shared = rect[3] <= top + SLACK, top - rect[3], _overlap(left, right, rect[0], rect[2])
        elif direction == DOWN:
            ok, distance, shared = rect[1] >= bottom - SLACK, rect[1] - bottom, _overlap(left, right, rect[0], rect[2])
        else:
            raise ValueError(f"Unknown direction {direction!r}")
        if not ok:
            continue
        # Tiles that share no span (diagonal) only count when nothing straight ahead exists.
        across = abs(rect[1] - top) if direction in (LEFT, RIGHT) else abs(rect[0] - left)
        rank = (shared <= 0, max(0, distance), -shared, across)
        if best_key is None or rank < best_key:
            best, best_key = key, rank
    return best


def adjacent_monitor(monitors: list, current, direction: str):  # noqa: ANN001 - cirana.wm.monitors.Monitor
    """The monitor next to ``current`` in ``direction`` (None: there is none)."""
    key = neighbor({m.device: m.rect for m in monitors}, current.rect, direction, exclude=current.device)
    return next((m for m in monitors if m.device == key), None)
