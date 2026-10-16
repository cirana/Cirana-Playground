"""Layout geometry: where each window of a workspace goes. Pure functions (no Win32, no Qt), shared by the window
controller and the preview in the editor.

Rectangles are ``(left, top, right, bottom)`` and describe the *visible* window frame; the controller compensates
for the invisible resize borders Windows adds around it.

Minimum sizes: many applications refuse to become smaller than a minimum (WM_GETMINMAXINFO); Windows then keeps the
left / top edge CIRANA asked for and lets the window grow to the right and down, over its neighbours or off the screen.
Tiled layouts can take known minimums (``minimums``) into account: every tile gets at least its window's minimum and
the rest of the space is shared as usual. :func:`overflowing` says which windows cannot be tiled at all.

BSP layouts (``cirana.wm.bsp``) split the space window by window; *automatic* picks a layout from the number of
windows (``auto``: e.g. two side by side, three main + side panel, four or more a grid or BSP).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from cirana.wm import bsp
from cirana.wm.identity import Rect
from cirana.wm.monitors import MonitorRef

SIDE_BY_SIDE = "side_by_side"
MAIN_SIDE = "main_side"
GRID = "grid"
FREE = "free"
BSP = "bsp"
AUTO = "auto"
ADAPTIVE = "adaptive"  # the engine picks the best layout itself (cirana.wm.adaptive)
KINDS = (SIDE_BY_SIDE, MAIN_SIDE, GRID, FREE, BSP, AUTO, ADAPTIVE)
KIND_NAMES = {SIDE_BY_SIDE: "Side by side", MAIN_SIDE: "Main + side panel", GRID: "Grid", FREE: "Saved positions",
              BSP: "BSP (split per window)", AUTO: "By window count (fixed steps)", ADAPTIVE: "Automatic (adaptive)"}
AUTO_KINDS = (SIDE_BY_SIDE, MAIN_SIDE, GRID, BSP)  # what automatic may pick
AUTO_STEPS = ("2", "3", "4")  # automatic: the layout for 2, 3, and 4 or more windows (1 window fills the area)
DEFAULT_AUTO = {"2": SIDE_BY_SIDE, "3": MAIN_SIDE, "4": GRID}
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
    heights: list[float] = field(default_factory=list)  # main + side: relative heights of the side windows
    grid_cols: list[float] = field(default_factory=list)  # grid: relative column widths (a resized grid)
    grid_rows: list[float] = field(default_factory=list)  # grid: relative row heights (a resized grid)
    bsp: dict[str, Any] = field(default_factory=dict)  # BSP: the stored tree (cirana.wm.bsp.to_dict); {} = default
    auto: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_AUTO))  # automatic: window count -> kind
    # Window stacks (several windows in one tile): [{"id", "members": [entry ids], "active": entry id}]
    stacks: list[dict[str, Any]] = field(default_factory=list)

    @property
    def tiled(self) -> bool:
        return self.kind != FREE

    def kind_for(self, count: int) -> str:
        """The concrete layout used for ``count`` tiles (automatic resolves by count; saved positions tile as a
        grid where a tiled layout is needed)."""
        if self.kind in (AUTO, ADAPTIVE):  # (adaptive: the static answer for code that has no engine context)
            if count <= 1:
                return SIDE_BY_SIDE
            step = str(min(count, 4))
            return self.auto.get(step, DEFAULT_AUTO[step])
        return GRID if self.kind == FREE else self.kind

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "gap": self.gap, "ratio": self.ratio, "columns": self.columns,
                "weights": list(self.weights), "monitor": self.monitor.to_dict() if self.monitor else None,
                "free": {key: slot.to_dict() for key, slot in self.free.items()}, "dynamic": self.dynamic,
                "heights": list(self.heights), "bsp": dict(self.bsp), "auto": dict(self.auto),
                "grid_cols": list(self.grid_cols), "grid_rows": list(self.grid_rows),
                "stacks": [dict(stack, members=list(stack["members"])) for stack in self.stacks]}

    @classmethod
    def from_dict(cls, raw: Any) -> "LayoutSpec":
        if not isinstance(raw, dict):
            return cls()

        def number(key: str, default: float, low: float, high: float) -> float:
            value = raw.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                return default
            return min(high, max(low, float(value)))

        def positive(values: Any) -> list[float]:
            return [float(v) for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)
                    and math.isfinite(v) and v > 0] if isinstance(values, list) else []

        kind = raw.get("kind") if raw.get("kind") in KINDS else GRID
        auto_raw = raw.get("auto") if isinstance(raw.get("auto"), dict) else {}
        auto = {step: auto_raw[step] if auto_raw.get(step) in AUTO_KINDS else DEFAULT_AUTO[step]
                for step in AUTO_STEPS}
        free_raw = raw.get("free")
        free = {}
        for key, value in (free_raw.items() if isinstance(free_raw, dict) else ()):
            slot = FreeSlot.from_dict(value)
            if slot is not None and isinstance(key, str):
                free[key] = slot
        return cls(
            kind=kind, gap=int(number("gap", 8, 0, MAX_GAP)), ratio=number("ratio", 0.6, 0.2, 0.8),
            columns=int(number("columns", 0, 0, 8)),
            weights=positive(raw.get("weights")),
            monitor=MonitorRef.from_dict(raw.get("monitor")), free=free, dynamic=raw.get("dynamic") is True,
            heights=positive(raw.get("heights")), bsp=bsp.to_dict(bsp.from_dict(raw.get("bsp"))), auto=auto,
            stacks=read_stacks(raw.get("stacks")), grid_cols=positive(raw.get("grid_cols")),
            grid_rows=positive(raw.get("grid_rows")),
        )


def read_stacks(raw: Any) -> list[dict[str, Any]]:
    """Stored stacks; damaged ones (fewer than two members, a member in two stacks) are dropped."""
    stacks, used = [], set()
    for item in raw if isinstance(raw, list) else ():
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
            continue
        members = item.get("members")
        members = [m for m in dict.fromkeys(members) if isinstance(m, str) and m and m not in used] \
            if isinstance(members, list) else []
        if len(members) < 2:
            continue
        used.update(members)
        active = item.get("active") if item.get("active") in members else members[0]
        stacks.append({"id": item["id"], "members": members, "active": active})
    return stacks


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
         minimums: list[Size] | None = None, ids: list[str] | None = None) -> list[Rect]:
    """Rectangles for ``count`` windows of a tiled layout inside the work area ``work``. ``minimums``: per window,
    the smallest frame it accepts (physical pixels); its tile is made at least that large where the space allows.
    ``ids``: the tiles' names, in order (BSP: the leaves of ``spec.bsp``; names missing from it are added the way
    new windows are, leaves that are not named are left out)."""
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
    kind = spec.kind_for(count)
    if kind == BSP:
        names = list(ids[:count]) if ids and len(ids) >= count else [str(i) for i in range(count)]
        area = (left, top, right, bottom)
        wanted = dict(zip(names, mins)) if mins else None
        tree = bsp.sync(bsp.from_dict(spec.bsp), names, area, gap=gap, minimums=wanted)
        boxes = bsp.rects(tree, area, gap, wanted)
        return [boxes[name] for name in names]
    if kind == SIDE_BY_SIDE:
        return [(a, top, b, bottom) for a, b in _split(left, right, padded(spec.weights, count), gap, widths)]
    if kind == MAIN_SIDE:
        columns = [widths[0], max(widths[1:])] if widths else None
        (main_l, main_r), (side_l, side_r) = _split(left, right, [spec.ratio, 1.0 - spec.ratio], gap, columns)
        heights = [m[1] for m in mins[1:]] if mins else None
        side = [(side_l, a, side_r, b) for a, b in _split(top, bottom, padded(spec.heights, count - 1), gap, heights)]
        return [(main_l, top, main_r, bottom)] + side
    columns = spec.columns or math.ceil(math.sqrt(count))
    columns = max(1, min(columns, count))
    rows = math.ceil(count / columns)
    row_heights = [max(m[1] for m in mins[r * columns:(r + 1) * columns]) for r in range(rows)] if mins else None
    column_weights = spec.grid_cols if len(spec.grid_cols) == columns else [1.0] * columns  # (a resized grid)
    row_weights = spec.grid_rows if len(spec.grid_rows) == rows else [1.0] * rows
    result: list[Rect] = []
    for row, (row_top, row_bottom) in enumerate(_split(top, bottom, row_weights, gap, row_heights)):
        in_row = min(columns, count - row * columns)  # the last row stretches its windows over the full width
        row_widths = widths[row * columns:row * columns + in_row] if widths else None
        weights = column_weights if in_row == columns else [1.0] * in_row
        result += [(a, row_top, b, row_bottom) for a, b in _split(left, right, weights, gap, row_widths)]
    return result


def fits(rects: list[Rect], minimums: list[Size], area: Rect | None = None) -> bool:
    """Every tile is at least as large as its window's minimum (so no window grows over its neighbours), not empty,
    and inside ``area`` (when given)."""
    return all(r[2] - r[0] >= max(1, m[0]) and r[3] - r[1] >= max(1, m[1]) for r, m in zip(rects, minimums)) and (
        area is None or all(area[0] <= r[0] and area[1] <= r[1] and r[2] <= area[2] and r[3] <= area[3]
                            for r in rects))


def overflowing(spec: LayoutSpec, work: Rect, minimums: list[Size], scale: float = 1.0,
                ids: list[str] | None = None) -> list[int]:
    """Indices of the windows that cannot be tiled with the others because of their minimum sizes: they float.
    First every window that does not fit even alone, then the newest (last) window with a minimum, until the rest
    fit. Windows without a minimum never cause an overflow (they can always shrink). An automatic layout is
    resolved once, for all the windows, so a floating window cannot flip the layout back and forth."""
    spec = fixed_kind(spec, len(minimums))
    alone = tile(spec, work, 1, scale)[0]
    out = [i for i, m in enumerate(minimums) if not fits([alone], [m])]
    while True:
        rest = [i for i in range(len(minimums)) if i not in out]
        mins = [minimums[i] for i in rest]
        names = [ids[i] for i in rest] if ids else None
        if not rest or fits(tile(spec, work, len(rest), scale, mins, names), mins, alone):
            return sorted(out)
        out.append(next(i for i in reversed(rest) if minimums[i] != (0, 0)))


def fixed_kind(spec: LayoutSpec, count: int) -> LayoutSpec:
    """``spec`` with an automatic layout resolved for ``count`` windows (other layouts are returned as they are)."""
    if spec.kind not in (AUTO, ADAPTIVE):
        return spec
    fixed = LayoutSpec.from_dict(spec.to_dict())
    fixed.kind = spec.kind_for(count)
    return fixed


def padded(weights: list[float], count: int) -> list[float]:
    """``count`` relative sizes from the stored ones: missing ones (a window came) get the average of the stored
    ones, so a newcomer takes an average share instead of upsetting the proportions the user chose."""
    kept = weights[:count]
    if len(kept) >= count:
        return kept
    return kept + [(sum(kept) / len(kept)) if kept else 1.0] * (count - len(kept))


def grow(weights: list[float], count: int, index: int, delta: float) -> list[float] | None:
    """Relative sizes after tile ``index`` (of ``count``) grew by ``delta`` (a fraction of the whole; negative:
    shrank); the others give or take the space in proportion. None when it cannot change (any further)."""
    if count < 2 or not 0 <= index < count:
        return None
    values = padded(weights, count)
    total = sum(values)
    shares = [v / total for v in values]
    new = min(0.8, max(0.1, shares[index] + delta))
    if abs(new - shares[index]) < 1e-6:
        return None
    rest = 1.0 - shares[index]
    result = [new if i == index else (s / rest * (1.0 - new) if rest > 0 else (1.0 - new) / (count - 1))
              for i, s in enumerate(shares)]
    return [round(v, 4) for v in result]


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


# -- minimum sizes: another layout instead of a floating window -----------------------------------------------------
def alternatives(spec: LayoutSpec, count: int) -> list[LayoutSpec]:
    """Other tiled layouts to try, in the order they are preferred, when ``spec`` (a concrete kind) cannot give every
    window its minimum size: main + side panel, a grid, side by side, columns of the grid one by one, a fresh BSP
    tree. The proportions the user set for the layout they chose are not carried over."""
    def variant(kind: str, columns: int = 0) -> LayoutSpec:
        other = LayoutSpec.from_dict(spec.to_dict())
        other.kind, other.columns = kind, columns
        other.weights, other.heights, other.grid_cols, other.grid_rows = [], [], [], []
        other.bsp = {}
        return other

    wanted = [(MAIN_SIDE, 0), (GRID, 0), (SIDE_BY_SIDE, 0)] + [(GRID, c) for c in range(1, count + 1)] + [(BSP, 0)]
    current = (spec.kind, spec.columns if spec.kind == GRID else 0)
    seen, result = {current}, []
    for kind, columns in wanted:
        key = (kind, columns)
        if key in seen:
            continue
        seen.add(key)
        result.append(variant(kind, columns))
    return result


def fit_layout(spec: LayoutSpec, work: Rect, minimums: list[Size], scale: float = 1.0,
               ids: list[str] | None = None) -> tuple[LayoutSpec, list[int]]:
    """The layout to use for windows with these minimum sizes, and the indices of those that still float. The
    chosen layout is used while every window fits; when it cannot be, the other layouts are tried and the one
    that floats the fewest windows wins (the first of ``alternatives`` on a tie, so the answer depends on
    the sizes only and never flips back and forth)."""
    spec = fixed_kind(spec, len(minimums))
    best, out = spec, overflowing(spec, work, minimums, scale, ids)
    if not out:
        return best, out
    gap = round(spec.gap * scale)
    area = (work[0] + gap, work[1] + gap, work[2] - gap, work[3] - gap)
    for other in alternatives(spec, len(minimums)):
        if other.kind == BSP and ids:
            other.bsp = bsp.to_dict(bsp.sync(None, ids, area, gap=gap, minimums=dict(zip(ids, minimums))))
        floating = overflowing(other, work, minimums, scale, ids)
        if len(floating) < len(out):
            best, out = other, floating
            if not out:
                break
    return best, out


# -- resizing a tile with the mouse ---------------------------------------------------------------------------------
RESIZE_SLOP = 3  # pixels an edge may differ before it counts as moved
MIN_SHARE = 0.05  # a tile never becomes narrower / lower than this share of the whole (when it has no minimum)


@dataclass
class Resize:
    """The outcome of a resize: the fields of the layout that change (``weights``, ``ratio``, ``heights``,
    ``grid_cols``, ``grid_rows`` or ``bsp``), the rectangles of all tiles afterwards, and what could not be done."""

    changes: dict[str, Any]
    rects: list[Rect]
    notes: list[str] = field(default_factory=list)


def moved_edges(before: Rect, after: Rect) -> dict[str, str] | None:
    """Which edges the user dragged: ``{"h": "left" | "right", "v": "top" | "bottom"}`` (an axis is missing when
    its size did not change). None when the frame was *moved* (or snapped): a resize anchors the opposite edge,
    a move shifts both edges of an axis."""
    edges: dict[str, str] = {}
    for axis, (low, high, names) in {"h": (0, 2, ("left", "right")), "v": (1, 3, ("top", "bottom"))}.items():
        low_moved = abs(after[low] - before[low]) > RESIZE_SLOP
        high_moved = abs(after[high] - before[high]) > RESIZE_SLOP
        if low_moved and high_moved:
            return None
        if low_moved or high_moved:
            edges[axis] = names[0] if low_moved else names[1]
    return edges or None


def _normalized(sizes: list[int]) -> list[float]:
    total = sum(sizes) or 1
    return [round(size / total, 4) for size in sizes]


def _divider(spans: list[tuple[int, int]], k: int, cut: int, mins: list[int]) -> list[float] | None:
    """Relative sizes after the divider between spans ``k`` and ``k + 1`` moved so that span ``k`` ends at ``cut``;
    both neighbours keep their minimum (or a small share of the whole). None when nothing changes."""
    sizes = [b - a for a, b in spans]
    floor = max(1, round(sum(sizes) * MIN_SHARE))
    low = max(mins[k], floor) - sizes[k]  # the most span k may shrink (negative) / the least it must grow
    high = sizes[k + 1] - max(mins[k + 1], floor)  # the most span k may grow
    if low > high:
        return None
    delta = min(high, max(low, cut - spans[k][1]))
    if delta == 0:
        return None
    sizes[k] += delta
    sizes[k + 1] -= delta
    return _normalized(sizes)


def resize_tile(spec: LayoutSpec, work: Rect, count: int, scale: float, minimums: list[Size], ids: list[str],
                index: int, frame: Rect, edges: dict[str, str]) -> Resize | str:
    """What a layout looks like after the user dragged ``edges`` of tile ``index`` so that its window now has the
    visible frame ``frame``: the divider that edge belongs to moves there, the neighbours give or take the space
    (they never overlap and stay inside the work area), and every window keeps its known minimum. A string says why
    this cannot be done (an edge on the border of the screen, a tile that has no divider of its own in this
    layout). Pure: the same function serves the preview while the mouse is held and the commit when it is released.
    """
    spec = fixed_kind(spec, count)
    kind = spec.kind_for(count)
    if count < 2 or not 0 <= index < count:
        return "This is the only tile: there is nothing to resize against."
    rects = tile(spec, work, count, scale, minimums, ids)
    mins = list(minimums[:count]) + [(0, 0)] * (count - len(minimums[:count]))
    gap = round(spec.gap * scale)
    left, top, right, bottom = work[0] + gap, work[1] + gap, work[2] - gap, work[3] - gap
    changes: dict[str, Any] = {}
    notes: list[str] = []
    horizontal, vertical = edges.get("h"), edges.get("v")

    if kind == BSP:
        area = (left, top, right, bottom)
        names = list(ids[:count]) if ids and len(ids) >= count else [str(i) for i in range(count)]
        tree = bsp.sync(bsp.from_dict(spec.bsp), names, area, gap=gap)
        for edge, position in ((horizontal, frame[0] if horizontal == "left" else frame[2]),
                               (vertical, frame[1] if vertical == "top" else frame[3])):
            if edge is None:
                continue
            tree_after = bsp.resize_edge(tree, names[index], edge, position, area, gap, dict(zip(names, mins)))
            if tree_after is None:
                notes.append(f"The {edge} edge of this tile has no divider that can move (screen border, or as far "
                             "as it goes).")
            else:
                tree = tree_after
                changes["bsp"] = bsp.to_dict(tree)
    elif kind == SIDE_BY_SIDE:
        if vertical is not None:
            notes.append("Side by side: every tile has the full height; use BSP (or another layout) to change "
                         "heights.")
        if horizontal is not None:
            k = index if horizontal == "right" else index - 1
            if not 0 <= k < count - 1:
                notes.append(f"The {horizontal} edge of this tile is on the border of the screen.")
            else:
                cut = frame[2] if horizontal == "right" else frame[0] - gap
                weights = _divider([(r[0], r[2]) for r in rects], k, cut, [m[0] for m in mins])
                if weights is None:
                    notes.append("The width of these tiles cannot change any further.")
                else:
                    changes["weights"] = weights
    elif kind == MAIN_SIDE:
        if horizontal is not None:
            if (index == 0) != (horizontal == "right"):
                notes.append(f"The {horizontal} edge of this tile is on the border of the screen.")
            else:
                main_right = frame[2] if index == 0 else frame[0] - gap
                usable = right - left - gap
                low, high = round(usable * 0.2), round(usable * 0.8)
                side_min = max((m[0] for m in mins[1:]), default=0)
                if mins[0][0] + side_min <= usable:
                    low, high = max(low, mins[0][0]), min(high, usable - side_min)
                ratio = round(min(high, max(low, main_right - left)) / usable, 4) if usable > 0 and low <= high \
                    else spec.ratio
                if abs(ratio - spec.ratio) < 1e-3:
                    notes.append("The width of the main window cannot change any further.")
                else:
                    changes["ratio"] = ratio
        if vertical is not None:
            if index == 0:
                notes.append("The main window has the full height; resize the side windows instead.")
            else:
                j = index - 1  # among the side windows
                k = j if vertical == "bottom" else j - 1
                if not 0 <= k < count - 2:
                    notes.append(f"The {vertical} edge of this tile is on the border of the screen (or it is the "
                                 "only side window).")
                else:
                    cut = frame[3] if vertical == "bottom" else frame[1] - gap
                    heights = _divider([(r[1], r[3]) for r in rects[1:]], k, cut, [m[1] for m in mins[1:]])
                    if heights is None:
                        notes.append("The height of these tiles cannot change any further.")
                    else:
                        changes["heights"] = heights
    else:  # grid
        columns = max(1, min(spec.columns or math.ceil(math.sqrt(count)), count))
        rows = math.ceil(count / columns)
        row, column = divmod(index, columns)
        if horizontal is not None:
            if min(columns, count - row * columns) != columns:
                notes.append("The last row of a grid is stretched over the full width: its tiles have no vertical "
                             "divider of their own to move.")
            else:
                k = column if horizontal == "right" else column - 1
                if not 0 <= k < columns - 1:
                    notes.append(f"The {horizontal} edge of this tile is on the border of the screen.")
                else:
                    cut = frame[2] if horizontal == "right" else frame[0] - gap
                    members = range(row * columns, row * columns + columns)
                    weights = _divider([(rects[i][0], rects[i][2]) for i in members], k, cut,
                                       [mins[i][0] for i in members])
                    if weights is None:
                        notes.append("The width of these tiles cannot change any further.")
                    else:
                        changes["grid_cols"] = weights
        if vertical is not None:
            k = row if vertical == "bottom" else row - 1
            if not 0 <= k < rows - 1:
                notes.append(f"The {vertical} edge of this tile is on the border of the screen.")
            else:
                cut = frame[3] if vertical == "bottom" else frame[1] - gap
                firsts = [r * columns for r in range(rows)]
                row_mins = [max(m[1] for m in mins[i:i + columns]) for i in firsts]
                weights = _divider([(rects[i][1], rects[i][3]) for i in firsts], k, cut, row_mins)
                if weights is None:
                    notes.append("The height of these rows cannot change any further.")
                else:
                    changes["grid_rows"] = weights
    if not changes:
        return " ".join(notes) or "Nothing to resize."
    resized = LayoutSpec.from_dict(spec.to_dict())
    for key, value in changes.items():
        setattr(resized, key, value)
    return Resize(changes, tile(resized, work, count, scale, minimums, ids), notes)
