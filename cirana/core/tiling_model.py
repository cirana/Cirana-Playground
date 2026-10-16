"""The vocabulary of dynamic tiling: its constants, what a tile (``Unit``) and a set of tiles laid out together
(``Group``) are, and a few geometry helpers. No behaviour of its own: ``DynamicTiler`` and its parts build on it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from cirana.wm.identity import Rect, TrackedWindow
from cirana.wm.layout import BSP, LayoutSpec, Size


DEBOUNCE_MS = 150  # events within this time produce one re-tile
STARTUP_MS = 400  # the first re-tile after the first window inventory: the burst of start-up events is one
DISPLAY_DEBOUNCE_MS = 600  # Windows reports a display change in several steps
RATE_LIMIT = 8  # re-tiles ...
RATE_WINDOW = 4.0  # ... within this many seconds; beyond that, wait
RATE_BACKOFF_MS = 2000
GLOBAL = "*"  # scope id of all-window tiling (workspace scopes use the workspace id)
RULE_SLOT = "win:"  # slot id prefix of a window a window rule assigns to a workspace (it has no entry)
RESIZE_STEP = 0.05  # one resize command: 5 % of the split / of the whole width or height
BEHIND, MINIMIZE = "behind", "minimize"  # how a stack's hidden windows are kept out of sight
TILED, STACKED, FLOATING, IGNORED = "tiled", "stacked", "floating", "ignored"  # what is done with a window


def center_in(rect: Rect, point: tuple[int, int]) -> bool:
    return rect[0] <= point[0] < rect[2] and rect[1] <= point[1] < rect[3]


def center_of(rect: Rect) -> tuple[int, int]:
    return (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2


def off_by(a: Rect, b: Rect) -> int:
    return max(abs(x - y) for x, y in zip(a, b))


@dataclass
class Unit:
    """One tile: a window, or a stack of windows of which one (``active``) is shown."""

    id: str  # the window's slot id, or the stack id
    members: list[tuple[str, TrackedWindow]]  # (slot id, window), in stack order; only windows that take part
    active: str  # slot id of the shown window

    @property
    def window(self) -> TrackedWindow:
        return next(window for slot, window in self.members if slot == self.active)

    @property
    def stacked(self) -> bool:
        return len(self.members) > 1

    def hidden(self) -> list[tuple[str, TrackedWindow]]:
        return [(slot, window) for slot, window in self.members if slot != self.active]


@dataclass
class Group:
    """Windows laid out together: a workspace, or all tiled windows of one monitor."""

    id: str
    spec: LayoutSpec  # automatic layouts resolved, BSP tree synced, per-monitor adjustments applied
    units: list[Unit]  # in layout order
    # units that would take part but do not fit beside the others (minimum sizes): (unit, why)
    floating: list[tuple[Unit, str]] = field(default_factory=list)
    minimums: dict[str, Size] = field(default_factory=dict)  # unit id -> known minimum on this group's monitor
    monocle: str = ""  # unit shown over the whole work area ("" = none)
    scope: str = GLOBAL
    # Set when the layout the user chose cannot give every window its minimum size and another one is used instead
    # (``spec`` is that other layout; what is stored for the chosen one is never touched): why, for the user.
    fallback: str = ""
    adaptive_key: str = ""  # the adaptive engine chose this layout (adaptive.key_of); "" = the user's own choice

    @property
    def bsp_active(self) -> bool:
        """The group is laid out by its own BSP tree (the one that is stored), not by a substitute layout."""
        return not self.fallback and self.spec.kind_for(len(self.units)) == BSP

    @property
    def slots(self) -> list[tuple[str, TrackedWindow]]:
        """The windows that are shown in tiles: (slot id, window)."""
        return [(unit.active, unit.window) for unit in self.units]
