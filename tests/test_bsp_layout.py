"""Pure geometry of the new layouts: the BSP tree, automatic layouts, keyboard resizing helpers and the direction
neighbour search. No desktop, no Qt."""
import unittest

from cirana.wm import bsp
from cirana.wm.layout import (AUTO, BSP, GRID, MAIN_SIDE, SIDE_BY_SIDE, LayoutSpec, fits, fixed_kind, grow,
                              overflowing, read_stacks, tile)
from cirana.wm.monitors import Monitor
from cirana.wm.navigation import DOWN, LEFT, RIGHT, UP, adjacent_monitor, neighbor

AREA = (0, 0, 1600, 900)


def area_of(rect):
    return (rect[2] - rect[0]) * (rect[3] - rect[1])


class BspTreeTests(unittest.TestCase):
    def test_each_new_window_splits_the_last_tile_along_its_longer_side(self):
        tree = bsp.build(["a", "b", "c"], AREA)
        rects = bsp.rects(tree, AREA)
        self.assertEqual(rects["a"], (0, 0, 800, 900))  # first split: vertical (the area is wide)
        self.assertEqual(rects["b"], (800, 0, 1600, 450))  # b's tile (800 x 900) is tall: split horizontally
        self.assertEqual(rects["c"], (800, 450, 1600, 900))
        self.assertEqual(sum(area_of(r) for r in rects.values()), area_of(AREA))  # no gaps, no overlaps

    def test_insert_next_to_the_focused_tile_moves_only_that_tile(self):
        tree = bsp.build(["a", "b", "c"], AREA)
        before = bsp.rects(tree, AREA)
        tree = bsp.insert(tree, "d", AREA, target="a")
        after = bsp.rects(tree, AREA)
        self.assertEqual((after["b"], after["c"]), (before["b"], before["c"]))
        self.assertEqual(after["a"][:2], before["a"][:2])
        self.assertEqual(area_of(after["a"]) + area_of(after["d"]), area_of(before["a"]))

    def test_removing_a_window_gives_its_space_to_the_sibling_only(self):
        tree = bsp.build(["a", "b", "c"], AREA)
        before = bsp.rects(tree, AREA)
        after = bsp.rects(bsp.remove(tree, "c"), AREA)
        self.assertEqual(after["a"], before["a"])  # untouched
        self.assertEqual(after["b"], (800, 0, 1600, 900))  # took c's half
        self.assertIsNone(bsp.remove(bsp.Leaf("a"), "a"))

    def test_sync_is_deterministic_and_idempotent(self):
        tree = bsp.sync(None, ["a", "b", "c", "d"], AREA, focus="b")
        again = bsp.sync(tree, ["a", "b", "c", "d"], AREA, focus="c")
        self.assertEqual(bsp.to_dict(tree), bsp.to_dict(again))
        self.assertEqual(bsp.to_dict(bsp.sync(None, ["a", "b"], AREA)), bsp.to_dict(bsp.sync(None, ["a", "b"], AREA)))
        smaller = bsp.sync(tree, ["a", "c"], AREA)
        self.assertEqual(sorted(bsp.leaves(smaller)), ["a", "c"])

    def test_gaps_separate_tiles(self):
        rects = bsp.rects(bsp.build(["a", "b"], AREA), AREA, gap=10)
        self.assertEqual(rects["b"][0] - rects["a"][2], 10)

    def test_minimum_sizes_move_the_split(self):
        tree = bsp.build(["a", "b"], AREA)
        rects = bsp.rects(tree, AREA, minimums={"b": (1200, 0)})
        self.assertEqual(rects["b"][2] - rects["b"][0], 1200)
        self.assertEqual(rects["a"], (0, 0, 400, 900))

    def test_swap_rename_and_resize(self):
        tree = bsp.build(["a", "b", "c"], AREA)
        swapped = bsp.rects(bsp.swap(tree, "a", "c"), AREA)
        original = bsp.rects(tree, AREA)
        self.assertEqual((swapped["a"], swapped["c"]), (original["c"], original["a"]))
        self.assertEqual(bsp.leaves(bsp.rename(tree, "b", "s")), ["a", "s", "c"])
        wider = bsp.resize(tree, "a", True, 0.1)
        self.assertEqual(bsp.rects(wider, AREA)["a"], (0, 0, 960, 900))
        taller = bsp.resize(tree, "c", False, 0.1)  # c is in the lower half of the right column
        self.assertEqual(bsp.rects(taller, AREA)["c"][1], 360)
        self.assertIsNone(bsp.resize(tree, "a", False, 0.1))  # no horizontal split above a
        far = tree
        for _ in range(20):
            far = bsp.resize(far, "a", True, 0.1) or far
        self.assertIsNone(bsp.resize(far, "a", True, 0.1))  # as far as it goes (90 %)

    def test_storage_round_trip_and_damaged_trees(self):
        tree = bsp.resize(bsp.build(["a", "b", "c"], AREA), "a", True, 0.1)
        self.assertEqual(bsp.to_dict(bsp.from_dict(bsp.to_dict(tree))), bsp.to_dict(tree))
        self.assertIsNone(bsp.from_dict("nonsense"))
        damaged = {"split": "v", "ratio": 7, "a": {"leaf": "x"}, "b": {"split": "h", "a": {"leaf": "x"}, "b": {}}}
        self.assertEqual(bsp.to_dict(bsp.from_dict(damaged)), {"leaf": "x"})  # duplicates and empty sides dropped
        deep = {"leaf": "z"}
        for _ in range(100):
            deep = {"split": "v", "ratio": 0.5, "a": deep, "b": {"leaf": f"n{id(deep)}"}}
        self.assertIsNotNone(bsp.from_dict(deep))  # far too deep: cut off, never a crash


class LayoutKindTests(unittest.TestCase):
    WORK = (0, 0, 2000, 1000)

    def test_bsp_and_auto_survive_storage(self):
        spec = LayoutSpec(BSP, gap=4, bsp=bsp.to_dict(bsp.build(["e1", "e2"], self.WORK)), heights=[2.0, 1.0],
                          stacks=[{"id": "s", "members": ["e1", "e2"], "active": "e2"}])
        again = LayoutSpec.from_dict(spec.to_dict())
        self.assertEqual(again.to_dict(), spec.to_dict())
        self.assertEqual(LayoutSpec.from_dict({"kind": AUTO}).auto, {"2": SIDE_BY_SIDE, "3": MAIN_SIDE, "4": GRID})
        self.assertEqual(LayoutSpec.from_dict({"kind": AUTO, "auto": {"4": BSP, "2": "nonsense"}}).auto["4"], BSP)
        self.assertEqual(LayoutSpec.from_dict({"kind": "unknown"}).kind, GRID)

    def test_automatic_layout_follows_the_window_count(self):
        spec = LayoutSpec(AUTO, gap=0)
        self.assertEqual([spec.kind_for(n) for n in (1, 2, 3, 4, 9)],
                         [SIDE_BY_SIDE, SIDE_BY_SIDE, MAIN_SIDE, GRID, GRID])
        self.assertEqual(tile(spec, self.WORK, 3), tile(LayoutSpec(MAIN_SIDE, gap=0), self.WORK, 3))
        self.assertEqual(tile(spec, self.WORK, 4), tile(LayoutSpec(GRID, gap=0), self.WORK, 4))
        spec.auto["4"] = BSP
        self.assertEqual(tile(spec, self.WORK, 5), tile(LayoutSpec(BSP, gap=0), self.WORK, 5))

    def test_automatic_layout_is_resolved_once_for_minimum_sizes(self):
        spec = LayoutSpec(AUTO, gap=0)
        self.assertEqual(fixed_kind(spec, 3).kind, MAIN_SIDE)
        self.assertIs(fixed_kind(LayoutSpec(GRID), 3).kind, GRID)
        # four windows -> a grid; two too wide to share a row: the newest floats, the rest stay a grid
        out = overflowing(spec, self.WORK, [(0, 0), (0, 0), (1100, 0), (1100, 0)])
        self.assertEqual(out, [3])

    def test_bsp_tiles_honour_stored_trees_and_minimums(self):
        tree = bsp.resize(bsp.build(["x", "y"], (0, 0, 2000, 1000)), "x", True, 0.2)
        spec = LayoutSpec(BSP, gap=0, bsp=bsp.to_dict(tree))
        self.assertEqual(tile(spec, self.WORK, 2, ids=["x", "y"])[0], (0, 0, 1400, 1000))
        rects = tile(spec, self.WORK, 2, minimums=[(0, 0), (900, 0)], ids=["x", "y"])
        self.assertTrue(fits(rects, [(0, 0), (900, 0)]))
        self.assertEqual(tile(spec, self.WORK, 1, ids=["y"]), [self.WORK])  # x left: y takes everything

    def test_main_side_heights_and_weights(self):
        spec = LayoutSpec(MAIN_SIDE, gap=0, ratio=0.5, heights=[3.0, 1.0])
        rects = tile(spec, self.WORK, 3)
        self.assertEqual(rects[1], (1000, 0, 2000, 750))
        self.assertEqual(grow([], 2, 0, 0.1), [0.6, 0.4])
        self.assertEqual(grow([1, 1, 2], 3, 2, -0.1), [0.3, 0.3, 0.4])
        self.assertIsNone(grow([4.0, 1.0], 2, 0, 0.1))  # already 80 %
        self.assertIsNone(grow([], 1, 0, 0.1))

    def test_damaged_stacks_are_dropped(self):
        stacks = read_stacks([{"id": "a", "members": ["e1", "e2", "e1"], "active": "zz"},
                              {"id": "b", "members": ["e2", "e3"]}, {"id": "", "members": ["x", "y"]}, "junk"])
        self.assertEqual(stacks, [{"id": "a", "members": ["e1", "e2"], "active": "e1"}])  # b: e2 is taken, 1 left


class NavigationTests(unittest.TestCase):
    TILES = {"main": (0, 0, 1000, 1000), "top": (1000, 0, 2000, 500), "bottom": (1000, 500, 2000, 1000),
             "far": (2000, 0, 3000, 1000)}

    def test_directions(self):
        self.assertEqual(neighbor(self.TILES, self.TILES["main"], RIGHT, "main"), "top")  # same span, nearest top
        self.assertEqual(neighbor(self.TILES, self.TILES["bottom"], LEFT, "bottom"), "main")
        self.assertEqual(neighbor(self.TILES, self.TILES["top"], DOWN, "top"), "bottom")
        self.assertEqual(neighbor(self.TILES, self.TILES["bottom"], UP, "bottom"), "top")
        self.assertEqual(neighbor(self.TILES, self.TILES["top"], RIGHT, "top"), "far")
        self.assertIsNone(neighbor(self.TILES, self.TILES["main"], LEFT, "main"))
        self.assertIsNone(neighbor(self.TILES, self.TILES["main"], UP, "main"))
        with self.assertRaises(ValueError):
            neighbor(self.TILES, self.TILES["main"], "sideways")

    def test_gaps_and_diagonals(self):
        tiles = {"a": (8, 8, 990, 990), "b": (1006, 8, 1990, 400), "c": (1500, 1100, 1900, 1500)}
        self.assertEqual(neighbor(tiles, tiles["a"], RIGHT, "a"), "b")  # a gap between them
        self.assertEqual(neighbor(tiles, tiles["a"], DOWN, "a"), "c")  # only diagonally below: still found

    def test_adjacent_monitor(self):
        left = Monitor("L", (0, 0, 1920, 1080), (0, 0, 1920, 1040), True, 96)
        right = Monitor("R", (1920, 0, 4480, 1440), (1920, 0, 4480, 1400), False, 144)
        self.assertIs(adjacent_monitor([left, right], left, RIGHT), right)
        self.assertIs(adjacent_monitor([left, right], right, LEFT), left)
        self.assertIsNone(adjacent_monitor([left, right], left, LEFT))


if __name__ == "__main__":
    unittest.main()
