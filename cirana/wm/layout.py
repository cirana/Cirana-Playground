"""Layout geometry: where each window of a workspace goes. Pure functions (no Win32, no Qt), shared by the window
controller and the preview in the editor.

Rectangles are ``(left, top, right, bottom)`` and describe the *visible* window frame; the controller compensates
for the invisible resize borders Windows adds around it.

Minimum sizes: many applications refuse to become smaller than a minimum (WM_GETMINMAXINFO); Windows then keeps the
left / top edge CIRANA asked for and lets the window grow to the right and down, over its neighbours or off the screen.
Tiled layouts can take known minimums (``minimums``) into account: every tile gets at least its window's minimum and
the rest of the space is shared as usual. :func:`overflowing` says which windows cannot be tiled at all.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from cirana.wm.identity import Rect
from cirana.wm.monitors import MonitorRef

SIDE_BY_SIDE = "side_by_side"
MAIN_SIDE = "main_side"
GRID = "grid"
FREE = "free"
KINDS = (SIDE_BY_SIDE, MAIN_SIDE, GRID, FREE)
KIND_NAMES = {SIDE_BY_SIDE: "Side by side", MAIN_SIDE: "Main + side panel", GRID: "Grid", FREE: "Saved positions"}
MAX_GAP = 64


@dataclass
class FreeSlot:
    """One window of a saved (free) layout: a fraction of a monitor's work area, so it survives resolution changes."""

    fraction: tuple[float, float, float, float]  # x, y, width, height as fractions of the work area
    monitor: MonitorRef | None = None
    maximized: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"fraction": list(self.fraction), "monitor": self.monitor.to_dict() if self.monitor else None,
                "maximized": self.maximized}

    @classmethod
    def from_dict(cls, raw: Any) -> "FreeSlot | None":
        if not isinstance(raw, dict):
            return None
        values = raw.get("fraction")
        if not (isinstance(values, list) and len(values) == 4
                and all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in values)):
            return None
        x, y, width, height = (float(v) for v in values)
        if width <= 0 or height <= 0:
            return None
        return cls((x, y, width, height), MonitorRef.from_dict(raw.get("monitor")), bool(raw.get("maximized")))


@dataclass
class LayoutSpec:
    kind: str = GRID
    gap: int = 8  # logical pixels between windows and around the edge (scaled with the monitor's DPI)
    ratio: float = 0.6  # main + side: width of the main window; side by side: ignored when weights are given
    columns: int = 0  # grid: 0 = as square as possible
    weights: list[float] = field(default_factory=list)  # side by side: relative column widths (empty = equal)
    monitor: MonitorRef | None = None  # tiled layouts: the monitor they fill (None = primary)
    free: dict[str, FreeSlot] = field(default_factory=dict)  # free layout: entry id -> slot
    # Dynamic tiling: while the workspace is active, re-tile whenever its windows open, close, minimize or restore
    # (tiled layouts only; see cirana.core.dynamic_tiling). Off by default and for records written before it existed.
    dynamic: bool = False

    @property
    def tiled(self) -> bool:
        return self.kind != FREE

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "gap": self.gap, "ratio": self.ratio, "columns": self.columns,
                "weights": list(self.weights), "monitor": self.monitor.to_dict() if self.monitor else None,
                "free": {key: slot.to_dict() for key, slot in self.free.items()}, "dynamic": self.dynamic}

    @classmethod
    def from_dict(cls, raw: Any) -> "LayoutSpec":
        if not isinstance(raw, dict):
            return cls()

        def number(key: str, default: float, low: float, high: float) -> float:
            value = raw.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                return default
            return min(high, max(low, float(value)))

        kind = raw.get("kind") if raw.get("kind") in KINDS else GRID
        weights = raw.get("weights")
        free_raw = raw.get("free")
        free = {}
        for key, value in (free_raw.items() if isinstance(free_raw, dict) else ()):
            slot = FreeSlot.from_dict(value)
            if slot is not None and isinstance(key, str):
                free[key] = slot
        return cls(
            kind=kind, gap=int(number("gap", 8, 0, MAX_GAP)), ratio=number("ratio", 0.6, 0.2, 0.8),
            columns=int(number("columns", 0, 0, 8)),
            weights=[float(v) for v in weights if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0]
            if isinstance(weights, list) else [],
            monitor=MonitorRef.from_dict(raw.get("monitor")), free=free, dynamic=raw.get("dynamic") is True,
        )


Size = tuple[int, int]  # width, height in physical pixels (0: no minimum)


def _share(total: int, weights: list[float], minimums: list[int]) -> list[int]:
    """Split ``total`` pixels by weight, but never below a minimum: spans that would get less than their minimum
    get exactly it, and the others share what is left (repeated until stable). When even the minimums do not fit,
    every span gets its minimum (the caller sees the overflow with :func:`fits`)."""
    count = len(weights)
    fixed = [False] * count
    while True:
        free = [i for i in range(count) if not fixed[i]]
        rest = total - sum(minimums[i] for i in range(count) if fixed[i])
        weight = sum(weights[i] for i in free) or 1.0
        newly = [i for i in free if rest * weights[i] / weight < minimums[i]]
        if not newly:
            break
        for i in newly:
            fixed[i] = True
    sizes = [minimums[i] if fixed[i] else 0 for i in range(count)]
    free = [i for i in range(count) if not fixed[i]]
    rest = max(0, total - sum(sizes))
    weight = sum(weights[i] for i in free) or 1.0
    used, given = 0.0, 0
    for i in free:  # cumulative rounding: the free spans add up to exactly ``rest``
        used += weights[i]
        size = round(rest * used / weight) - given
        sizes[i], given = size, given + size
    return sizes


def _split(start: int, end: int, weights: list[float], gap: int,
           minimums: list[int] | None = None) -> list[tuple[int, int]]:
    """Divide [start, end) into len(weights) spans separated by ``gap``, proportional to the weights (and, when
    given, at least ``minimums`` long)."""
    count = len(weights)
    usable = max(0, end - start - gap * (count - 1))
    if minimums and any(minimums):
        spans, position = [], start
        sizes = _share(usable, weights, minimums)
        for index, size in enumerate(sizes):
            stop = end if index == count - 1 and sum(sizes) == usable else position + size
            spans.append((position, stop))
            position = stop + gap
        return spans
    total = sum(weights) or 1.0
    spans, position, used = [], start, 0.0
    for index, weight in enumerate(weights):
        used += weight
        stop = end if index == count - 1 else start + round(usable * used / total) + gap * index
        spans.append((position, stop))
        position = stop + gap
    return spans


def tile(spec: LayoutSpec, work: Rect, count: int, scale: float = 1.0,
         minimums: list[Size] | None = None) -> list[Rect]:
    """Rectangles for ``count`` windows of a tiled layout inside the work area ``work``. ``minimums``: per window,
    the smallest frame it accepts (physical pixels); its tile is made at least that large where the space allows."""
    if count <= 0:
        return []
    gap = round(spec.gap * scale)
    left, top, right, bottom = work[0] + gap, work[1] + gap, work[2] - gap, work[3] - gap
    if right <= left or bottom <= top:
        return [work] * count
    if count == 1:
        return [(left, top, right, bottom)]
    mins = list(minimums[:count]) + [(0, 0)] * (count - len(minimums[:count])) if minimums else None
    widths = [m[0] for m in mins] if mins else None
    kind = spec.kind if spec.kind != FREE else GRID
    if kind == SIDE_BY_SIDE:
        weights = spec.weights[:count] + [1.0] * (count - len(spec.weights[:count]))
        return [(a, top, b, bottom) for a, b in _split(left, right, weights, gap, widths)]
    if kind == MAIN_SIDE:
        columns = [widths[0], max(widths[1:])] if widths else None
        (main_l, main_r), (side_l, side_r) = _split(left, right, [spec.ratio, 1.0 - spec.ratio], gap, columns)
        heights = [m[1] for m in mins[1:]] if mins else None
        side = [(side_l, a, side_r, b) for a, b in _split(top, bottom, [1.0] * (count - 1), gap, heights)]
        return [(main_l, top, main_r, bottom)] + side
    columns = spec.columns or math.ceil(math.sqrt(count))
    columns = max(1, min(columns, count))
    rows = math.ceil(count / columns)
    row_heights = [max(m[1] for m in mins[r * columns:(r + 1) * columns]) for r in range(rows)] if mins else None
    result: list[Rect] = []
    for row, (row_top, row_bottom) in enumerate(_split(top, bottom, [1.0] * rows, gap, row_heights)):
        in_row = min(columns, count - row * columns)  # the last row stretches its windows over the full width
        row_widths = widths[row * columns:row * columns + in_row] if widths else None
        result += [(a, row_top, b, row_bottom) for a, b in _split(left, right, [1.0] * in_row, gap, row_widths)]
    return result


def fits(rects: list[Rect], minimums: list[Size], area: Rect | None = None) -> bool:
    """Every tile is at least as large as its window's minimum (so no window grows over its neighbours), not empty,
    and inside ``area`` (when given)."""
    return all(r[2] - r[0] >= max(1, m[0]) and r[3] - r[1] >= max(1, m[1]) for r, m in zip(rects, minimums)) and (
        area is None or all(area[0] <= r[0] and area[1] <= r[1] and r[2] <= area[2] and r[3] <= area[3]
                            for r in rects))


def overflowing(spec: LayoutSpec, work: Rect, minimums: list[Size], scale: float = 1.0) -> list[int]:
    """Indices of the windows that cannot be tiled with the others because of their minimum sizes: they float.
    First every window that does not fit even alone, then the newest (last) window with a minimum, until the rest
    fit. Windows without a minimum never cause an overflow (they can always shrink)."""
    alone = tile(spec, work, 1, scale)[0]
    out = [i for i, m in enumerate(minimums) if not fits([alone], [m])]
    while True:
        rest = [i for i in range(len(minimums)) if i not in out]
        mins = [minimums[i] for i in rest]
        if not rest or fits(tile(spec, work, len(rest), scale, mins), mins, alone):
            return sorted(out)
        out.append(next(i for i in reversed(rest) if minimums[i] != (0, 0)))


def keep_visible(work: Rect, rect: Rect) -> Rect:
    """Shift (never shrink) ``rect`` into ``work``; a window larger than ``work`` keeps its top-left corner (title
    bar, menus) inside it."""
    width, height = rect[2] - rect[0], rect[3] - rect[1]
    left = max(work[0], min(rect[0], work[2] - width))
    top = max(work[1], min(rect[1], work[3] - height))
    return left, top, left + width, top + height


def to_fraction(rect: Rect, work: Rect) -> tuple[float, float, float, float]:
    width, height = max(1, work[2] - work[0]), max(1, work[3] - work[1])
    return ((rect[0] - work[0]) / width, (rect[1] - work[1]) / height,
            (rect[2] - rect[0]) / width, (rect[3] - rect[1]) / height)


def from_fraction(fraction: tuple[float, float, float, float], work: Rect) -> Rect:
    width, height = work[2] - work[0], work[3] - work[1]
    x, y, w, h = fraction
    left, top = work[0] + round(x * width), work[1] + round(y * height)
    return clamp_into(work, (left, top, left + max(1, round(w * width)), top + max(1, round(h * height))))


def clamp_into(work: Rect, rect: Rect) -> Rect:
    """Shift (and if needed shrink) ``rect`` so it lies inside ``work``: a window never ends up off-screen."""
    width = min(rect[2] - rect[0], work[2] - work[0])
    height = min(rect[3] - rect[1], work[3] - work[1])
    left = min(max(rect[0], work[0]), work[2] - width)
    top = min(max(rect[1], work[1]), work[3] - height)
    return left, top, left + width, top + height
