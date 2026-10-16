"""Binary space partitioning layout: a tree that splits the space in two, again and again, one split per window.

Pure functions (no Win32, no Qt), shared by dynamic tiling, *Apply layout* and the layout preview.

A tree is a leaf (one tile, named by an id: a workspace entry id, a window key or a stack id) or a split of a
rectangle into ``a`` and ``b``: side by side (``vertical``: the dividing line is vertical) or one above the other,
with ``ratio`` the share of ``a``. A new window splits the focused tile (else the last one) along its longer side,
so the others keep their places; when a window leaves, its sibling takes the whole split and nothing else moves.

On disk (workspaces) the tree is a small dict: ``{"leaf": id}`` or ``{"split": "v"|"h", "ratio": r, "a": …, "b": …}``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Union

from cirana.wm.identity import Rect

MIN_RATIO, MAX_RATIO = 0.1, 0.9
MAX_DEPTH = 32  # a damaged file can never make the recursion run away


@dataclass
class Leaf:
    id: str


@dataclass
class Split:
    vertical: bool  # True: a | b (left / right); False: a above b
    ratio: float
    a: "Node"
    b: "Node"


Node = Union[Leaf, Split]
Size = tuple[int, int]


def leaves(node: Node | None) -> list[str]:
    if node is None:
        return []
    if isinstance(node, Leaf):
        return [node.id]
    return leaves(node.a) + leaves(node.b)


def copy(node: Node | None) -> Node | None:
    if node is None or isinstance(node, Leaf):
        return None if node is None else Leaf(node.id)
    return Split(node.vertical, node.ratio, copy(node.a), copy(node.b))


# -- geometry ----------------------------------------------------------------------------------------------------
def _minimum(node: Node, minimums: dict[str, Size], gap: int) -> Size:
    """The smallest area this subtree needs so every window gets its minimum."""
    if isinstance(node, Leaf):
        return minimums.get(node.id, (0, 0))
    a, b = _minimum(node.a, minimums, gap), _minimum(node.b, minimums, gap)
    if node.vertical:
        return a[0] + gap + b[0], max(a[1], b[1])
    return max(a[0], b[0]), a[1] + gap + b[1]


def _cut(start: int, end: int, ratio: float, gap: int, need_a: int, need_b: int) -> int:
    """Where ``a`` ends: by ratio, moved so both sides get their minimum when the space allows it."""
    usable = max(0, end - start - gap)
    size = round(usable * ratio)
    if need_a + need_b <= usable:
        size = max(need_a, min(size, usable - need_b))
    elif need_a or need_b:
        size = min(usable, need_a)  # does not fit: the caller sees the overflow (layout.fits)
    return start + size


def rects(node: Node | None, area: Rect, gap: int = 0, minimums: dict[str, Size] | None = None) -> dict[str, Rect]:
    """The rectangle of every leaf inside ``area`` (gaps between tiles only; the caller insets the outer edge)."""
    result: dict[str, Rect] = {}
    minimums = minimums or {}

    def walk(item: Node, box: Rect) -> None:
        if isinstance(item, Leaf):
            result[item.id] = box
            return
        left, top, right, bottom = box
        need_a, need_b = _minimum(item.a, minimums, gap), _minimum(item.b, minimums, gap)
        if item.vertical:
            cut = _cut(left, right, item.ratio, gap, need_a[0], need_b[0])
            walk(item.a, (left, top, cut, bottom))
            walk(item.b, (min(right, cut + gap), top, right, bottom))
        else:
            cut = _cut(top, bottom, item.ratio, gap, need_a[1], need_b[1])
            walk(item.a, (left, top, right, cut))
            walk(item.b, (left, min(bottom, cut + gap), right, bottom))

    if node is not None:
        walk(node, area)
    return result


# -- changes (each returns the new tree; the input is never modified) ---------------------------------------------
def insert(node: Node | None, new_id: str, area: Rect, target: str | None = None, gap: int = 0) -> Node:
    """Add ``new_id`` by splitting ``target`` (default: the last leaf) along its longer side; the new window takes
    the right / lower half."""
    if node is None:
        return Leaf(new_id)
    ids = leaves(node)
    if new_id in ids:
        return copy(node)
    target = target if target in ids else ids[-1]
    box = rects(node, area, gap)[target]
    vertical = box[2] - box[0] >= box[3] - box[1]

    def walk(item: Node) -> Node:
        if isinstance(item, Leaf):
            return Split(vertical, 0.5, Leaf(item.id), Leaf(new_id)) if item.id == target else Leaf(item.id)
        return Split(item.vertical, item.ratio, walk(item.a), walk(item.b))

    return walk(node)


def remove(node: Node | None, old_id: str) -> Node | None:
    """Take a leaf out: its sibling takes the whole split (only that part of the layout changes)."""
    if node is None:
        return None
    if isinstance(node, Leaf):
        return None if node.id == old_id else Leaf(node.id)
    a, b = remove(node.a, old_id), remove(node.b, old_id)
    if a is None or b is None:
        return a if b is None else b
    return Split(node.vertical, node.ratio, a, b)


def sync(node: Node | None, ids: list[str], area: Rect, focus: str | None = None, gap: int = 0) -> Node | None:
    """The tree for exactly ``ids``: leaves that left are removed, new ids are inserted in order (each next to the
    focused tile when it is in the tree). Deterministic, and applying it twice changes nothing."""
    wanted = set(ids)
    for old in [leaf for leaf in leaves(node) if leaf not in wanted]:
        node = remove(node, old)
    seen = set(leaves(node))
    for new in ids:
        if new not in seen:
            node = insert(node, new, area, focus if focus in seen else None, gap)
            seen.add(new)
    return node


def build(ids: list[str], area: Rect, gap: int = 0) -> Node | None:
    """The default tree: each window splits the previous one (a "dwindle" spiral)."""
    return sync(None, ids, area, gap=gap)


def rename(node: Node | None, old_id: str, new_id: str) -> Node | None:
    if node is None:
        return None
    if isinstance(node, Leaf):
        return Leaf(new_id if node.id == old_id else node.id)
    return Split(node.vertical, node.ratio, rename(node.a, old_id, new_id), rename(node.b, old_id, new_id))


def swap(node: Node | None, first: str, second: str) -> Node | None:
    """Exchange two leaves' places (both must be in the tree; otherwise nothing changes)."""
    ids = leaves(node)
    if first not in ids or second not in ids or first == second:
        return copy(node)
    placeholder = "\0swap"
    return rename(rename(rename(node, first, placeholder), second, first), placeholder, second)


def resize(node: Node | None, leaf_id: str, vertical: bool, delta: float) -> Node | None:
    """Make ``leaf_id`` larger (``delta`` > 0) or smaller across a vertical split (width) or a horizontal one
    (height): the nearest such split above it moves. None when there is no such split (nothing to resize)."""
    if node is None or leaf_id not in leaves(node):
        return None
    path: list[tuple[Split, bool]] = []  # (split, the leaf is in its ``a`` side)

    def find(item: Node) -> bool:
        if isinstance(item, Leaf):
            return item.id == leaf_id
        for side, child in ((True, item.a), (False, item.b)):
            path.append((item, side))
            if find(child):
                return True
            path.pop()
        return False

    result = copy(node)
    find(result)
    for split, in_a in reversed(path):
        if split.vertical == vertical:
            ratio = split.ratio + (delta if in_a else -delta)
            ratio = min(MAX_RATIO, max(MIN_RATIO, ratio))
            if abs(ratio - split.ratio) < 1e-9:
                return None  # already as far as it goes
            split.ratio = ratio
            return result
    return None


# -- storage ------------------------------------------------------------------------------------------------------
def to_dict(node: Node | None) -> dict[str, Any]:
    if node is None:
        return {}
    if isinstance(node, Leaf):
        return {"leaf": node.id}
    return {"split": "v" if node.vertical else "h", "ratio": round(node.ratio, 4), "a": to_dict(node.a),
            "b": to_dict(node.b)}


def from_dict(raw: Any) -> Node | None:
    """A stored tree; damaged parts are dropped (a split missing a side becomes its other side), duplicate leaves
    keep their first place."""
    seen: set[str] = set()

    def read(item: Any, depth: int) -> Node | None:
        if not isinstance(item, dict) or depth > MAX_DEPTH:
            return None
        leaf = item.get("leaf")
        if isinstance(leaf, str) and leaf:
            if leaf in seen:
                return None
            seen.add(leaf)
            return Leaf(leaf)
        ratio = item.get("ratio")
        if item.get("split") not in ("v", "h"):
            return None
        a, b = read(item.get("a"), depth + 1), read(item.get("b"), depth + 1)
        if a is None or b is None:
            return a if b is None else b
        valid = isinstance(ratio, (int, float)) and not isinstance(ratio, bool) and MIN_RATIO <= ratio <= MAX_RATIO
        return Split(item["split"] == "v", float(ratio) if valid else 0.5, a, b)

    return read(raw, 0)
