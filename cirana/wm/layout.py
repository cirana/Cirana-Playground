"""Layout geometry: where each window of a workspace goes. Pure functions (no Win32, no Qt), shared by the window
controller and the preview in the editor.

Rectangles are ``(left, top, right, bottom)`` and describe the *visible* window frame; the controller compensates
for the invisible resize borders Windows adds around it.
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


def _split(start: int, end: int, weights: list[float], gap: int) -> list[tuple[int, int]]:
    """Divide [start, end) into len(weights) spans separated by ``gap``, proportional to the weights."""
    count = len(weights)
    usable = max(0, end - start - gap * (count - 1))
    total = sum(weights) or 1.0
    spans, position, used = [], start, 0.0
    for index, weight in enumerate(weights):
        used += weight
        stop = end if index == count - 1 else start + round(usable * used / total) + gap * index
        spans.append((position, stop))
        position = stop + gap
    return spans


def tile(spec: LayoutSpec, work: Rect, count: int, scale: float = 1.0) -> list[Rect]:
    """Rectangles for ``count`` windows of a tiled layout inside the work area ``work``."""
    if count <= 0:
        return []
    gap = round(spec.gap * scale)
    left, top, right, bottom = work[0] + gap, work[1] + gap, work[2] - gap, work[3] - gap
    if right <= left or bottom <= top:
        return [work] * count
    if count == 1:
        return [(left, top, right, bottom)]
    kind = spec.kind if spec.kind != FREE else GRID
    if kind == SIDE_BY_SIDE:
        weights = spec.weights[:count] + [1.0] * (count - len(spec.weights[:count]))
        return [(a, top, b, bottom) for a, b in _split(left, right, weights, gap)]
    if kind == MAIN_SIDE:
        (main_l, main_r), (side_l, side_r) = _split(left, right, [spec.ratio, 1.0 - spec.ratio], gap)
        side = [(side_l, a, side_r, b) for a, b in _split(top, bottom, [1.0] * (count - 1), gap)]
        return [(main_l, top, main_r, bottom)] + side
    columns = spec.columns or math.ceil(math.sqrt(count))
    columns = max(1, min(columns, count))
    rows = math.ceil(count / columns)
    result: list[Rect] = []
    for row, (row_top, row_bottom) in enumerate(_split(top, bottom, [1.0] * rows, gap)):
        in_row = min(columns, count - row * columns)  # the last row stretches its windows over the full width
        result += [(a, row_top, b, row_bottom) for a, b in _split(left, right, [1.0] * in_row, gap)]
    return result


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
