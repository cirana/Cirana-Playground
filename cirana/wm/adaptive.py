"""Adaptive layout engine: picks the best of BSP, main + side panel, grid, a single column and side by side for the
windows that are tiled together on one monitor. Pure functions (no Win32, no Qt).

Every candidate is laid out for real (``layout.tile``, with the learned minimum sizes and the proportions the user
set) and scored; the lowest score wins:

* a window that cannot get its minimum size in the layout floats: that costs ``FLOAT_COST`` each, far more than
  anything else, so a layout that holds every window always beats one that does not;
* tiles with awkward proportions (much wider or taller than a window should be, which also reflects the monitor's
  aspect ratio and orientation), slivers (a small share of the monitor's width or height) and tiles that are tiny
  compared with the average one cost a little each;
* the windows that are already tiled must move as little as possible (``MOVE_WEIGHT``).

Stability: the layout in use is kept unless another one is better by more than ``HYSTERESIS`` (and holds at least as
many windows); a user's resize, swap or stack *holds* it while the set of windows stays the same. Ties go to the
first candidate in ``CANDIDATES`` so the answer is deterministic. At most five layouts are laid out per decision.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from cirana.wm.identity import Rect
from cirana.wm.layout import BSP, GRID, MAIN_SIDE, SIDE_BY_SIDE, LayoutSpec, Size, overflowing, tile

# (kind, grid columns); GRID with one column is the stack of rows that suits portrait monitors
CANDIDATES = ((BSP, 0), (MAIN_SIDE, 0), (GRID, 0), (GRID, 1), (SIDE_BY_SIDE, 0))
NAMES = {(BSP, 0): "BSP", (MAIN_SIDE, 0): "Main + side panel", (GRID, 0): "Grid", (GRID, 1): "Column",
         (SIDE_BY_SIDE, 0): "Side by side"}
FLOAT_COST = 100.0
HYSTERESIS = 0.12
MOVE_WEIGHT = 0.8
ASPECT_LOW, ASPECT_HIGH = 0.4, 2.8
SLIVER = 0.15
IDEAL_ASPECT, ASPECT_WEIGHT = 1.4, 0.12  # a window likes a landscape tile; a gentle pull towards it
BALANCE_WEIGHT = 0.15
SMALL_SHARE = 0.4  # a tile smaller than this share of the average tile is "tiny"


def key_of(spec: LayoutSpec) -> str:
    return f"{spec.kind}:{spec.columns if spec.kind == GRID else 0}"


def name_of(key: str) -> str:
    kind, _, columns = key.partition(":")
    return NAMES.get((kind, int(columns or 0)), kind)


@dataclass
class Choice:
    spec: LayoutSpec  # a concrete layout (never ADAPTIVE)
    floating: list[int]  # indices of the windows that fit no matter what
    score: float
    key: str


def candidate(base: LayoutSpec, kind: str, columns: int) -> LayoutSpec:
    spec = LayoutSpec.from_dict(base.to_dict())
    spec.kind, spec.columns = kind, columns
    return spec


def _movement(rects: list[Rect], ids: list[str], last: dict[str, Rect], work: Rect) -> float:
    span = max(1, (work[2] - work[0]) + (work[3] - work[1]))
    moved = [sum(abs(a - b) for a, b in zip(rect, last[i])) / (2 * span) for rect, i in zip(rects, ids) if i in last]
    return sum(moved) / len(moved) if moved else 0.0


def _quality(rects: list[Rect], work: Rect) -> float:
    width, height = max(1, work[2] - work[0]), max(1, work[3] - work[1])
    areas = [max(1, (r[2] - r[0]) * (r[3] - r[1])) for r in rects]
    mean = sum(areas) / len(areas)
    cost = 0.0
    for rect, area in zip(rects, areas):
        w, h = max(1, rect[2] - rect[0]), max(1, rect[3] - rect[1])
        aspect = w / h
        cost += max(0.0, math.log(ASPECT_LOW / aspect), math.log(aspect / ASPECT_HIGH))
        cost += max(0.0, SLIVER - min(w / width, h / height)) * 8.0
        cost += max(0.0, SMALL_SHARE - area / mean) * 2.0
        cost += abs(math.log(aspect / IDEAL_ASPECT)) * ASPECT_WEIGHT
    spread = math.sqrt(sum((a / mean - 1.0) ** 2 for a in areas) / len(areas))  # unequal tiles: a main window is fine
    return cost / len(rects) + spread * BALANCE_WEIGHT


def evaluate(spec: LayoutSpec, work: Rect, scale: float, sizes: list[Size], ids: list[str],
             last: dict[str, Rect]) -> Choice:
    """Score one concrete layout for these windows."""
    out = overflowing(spec, work, sizes, scale, ids)
    rest = [i for i in range(len(ids)) if i not in out]
    if not rest:
        return Choice(spec, out, FLOAT_COST * len(out), key_of(spec))
    names = [ids[i] for i in rest]
    rects = tile(spec, work, len(rest), scale, [sizes[i] for i in rest], names)
    score = FLOAT_COST * len(out) + _quality(rects, work) + MOVE_WEIGHT * _movement(rects, names, last, work)
    return Choice(spec, out, score, key_of(spec))


def choose(base: LayoutSpec, work: Rect, scale: float, sizes: list[Size], ids: list[str],
           previous: str = "", last: dict[str, Rect] | None = None, hold: bool = False) -> Choice:
    """The layout for these windows. ``previous``: key of the layout in use (kept unless clearly worse);
    ``hold``: keep it no matter what (the user just shaped it); ``last``: where the windows are now."""
    last = last or {}
    options = [evaluate(candidate(base, kind, columns), work, scale, sizes, ids, last) for kind, columns in CANDIDATES]
    current = next((o for o in options if o.key == previous), None)
    if current is not None and hold:
        return current
    best = min(options, key=lambda o: (o.score, options.index(o)))
    if current is not None and len(current.floating) <= len(best.floating) \
            and current.score <= best.score + HYSTERESIS:
        return current
    return best
