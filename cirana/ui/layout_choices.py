"""The layouts a user can pick, shared by the Workspaces page (one workspace's layout) and the Windows page (the layout
of all-window tiling) so both offer the same names in the same order."""
from __future__ import annotations

from cirana.wm.layout import ADAPTIVE, AUTO, BSP, FREE, GRID, MAIN_SIDE, SIDE_BY_SIDE, LayoutSpec

# (label, kind, grid columns): "Grid 2×2" and "Dynamic grid" are the same layout with fixed / automatic columns.
LAYOUT_CHOICES = [
    ("Side by side", SIDE_BY_SIDE, 0),
    ("Main + side panel", MAIN_SIDE, 0),
    ("Grid 2×2", GRID, 2),
    ("Dynamic grid", GRID, 0),
    ("Saved positions", FREE, 0),
    ("BSP (split per window)", BSP, 0),
    ("By window count (fixed steps)", AUTO, 0),
    ("Automatic (adaptive)", ADAPTIVE, 0),
]
# All-window tiling has no saved positions: the same list without them.
TILING_CHOICES = [choice for choice in LAYOUT_CHOICES if choice[1] != FREE]


def choice_index(choices: list[tuple[str, str, int]], spec: LayoutSpec, default: int) -> int:
    """The entry of ``choices`` that shows ``spec`` (``default`` when none does)."""
    return next((i for i, (_text, kind, columns) in enumerate(choices)
                 if kind == spec.kind and (kind != GRID or columns == (2 if spec.columns == 2 else 0))), default)
