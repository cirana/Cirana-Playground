"""Resizing a tile with the mouse, as pure geometry: which edges the user dragged, which divider of the layout they
belong to, and what the layout looks like afterwards (``resize_tile``). The same function serves the preview while the
mouse is held and the commit when it is released. No Win32, no Qt.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from cirana.wm import bsp
from cirana.wm.identity import Rect
from cirana.wm.layout import BSP, MAIN_SIDE, SIDE_BY_SIDE, LayoutSpec, Size, fixed_kind, tile

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
