"""Pure layout functions behind mouse resizing and the minimum-size fallback: no Win32, no Qt.

``resize_tile`` maps the edge the user dragged to the divider it belongs to (BSP split, side-by-side weights, main
ratio / side heights, grid columns / rows), keeps neighbours from overlapping or leaving the work area, honours known
minimum sizes and says why an edge cannot move. ``fit_layout`` finds another layout before a window has to float.
"""
import unittest

from cirana.wm import bsp
from cirana.wm.layout import (AUTO, BSP, GRID, MAIN_SIDE, SIDE_BY_SIDE, LayoutSpec, Resize, fit_layout, moved_edges,
                              overflowing, resize_tile, tile)

WORK = (0, 0, 2000, 1000)


def overlap(a, b):
    return min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1])


def clean(test, rects, work=WORK):
    for i, a in enumerate(rects):
        test.assertTrue(work[0] <= a[0] < a[2] <= work[2] and work[1] <= a[1] < a[3] <= work[3], a)
        for b in rects[i + 1:]:
            test.assertFalse(overlap(a, b), (a, b))


def grown(rect, left=0, top=0, right=0, bottom=0):
    return rect[0] + left, rect[1] + top, rect[2] + right, rect[3] + bottom


class MovedEdgesTests(unittest.TestCase):
    def test_a_resize_anchors_the_opposite_edge(self):
        before = (100, 100, 500, 400)
        self.assertEqual(moved_edges(before, (100, 100, 560, 400)), {"h": "right"})
        self.assertEqual(moved_edges(before, (40, 100, 500, 400)), {"h": "left"})
        self.assertEqual(moved_edges(before, (100, 100, 500, 460)), {"v": "bottom"})
        self.assertEqual(moved_edges(before, (100, 60, 500, 400)), {"v": "top"})
        self.assertEqual(moved_edges(before, (100, 100, 560, 460)), {"h": "right", "v": "bottom"})  # a corner

    def test_a_move_or_a_snap_is_not_a_resize(self):
        before = (100, 100, 500, 400)
        self.assertIsNone(moved_edges(before, before))
        self.assertIsNone(moved_edges(before, (140, 120, 540, 420)))  # moved: both edges of both axes shifted
        self.assertIsNone(moved_edges(before, (0, 0, 1000, 960)))  # snapped to half the screen
        self.assertIsNone(moved_edges(before, (101, 99, 502, 401)))  # rounding noise is nothing at all


class SideBySideResizeTests(unittest.TestCase):
    def setUp(self):
        self.spec = LayoutSpec(SIDE_BY_SIDE, gap=0)
        self.ids = ["a", "b", "c"]
        self.rects = tile(self.spec, WORK, 3, 1.0, ids=self.ids)

    def resize(self, index, frame, edges, minimums=None, spec=None, count=3):
        return resize_tile(spec or self.spec, WORK, count, 1.0, minimums or [(0, 0)] * count, self.ids[:count],
                           index, frame, edges)

    def test_right_edge_moves_the_divider_and_only_the_neighbour_gives(self):
        outcome = self.resize(0, grown(self.rects[0], right=100), {"h": "right"})
        self.assertIsInstance(outcome, Resize)
        a, b, c = outcome.rects
        self.assertEqual(a[2], self.rects[0][2] + 100)
        self.assertEqual(c, self.rects[2])  # the third column does not move
        self.assertEqual((a[2], b[0], b[2]), (b[0], a[2], c[0]))  # still no gap, no overlap
        clean(self, outcome.rects)

    def test_left_edge_of_the_middle_tile(self):
        outcome = self.resize(1, grown(self.rects[1], left=-150), {"h": "left"})
        a, b, c = outcome.rects
        self.assertEqual(b[0], self.rects[1][0] - 150)
        self.assertEqual((a[0], c), (0, self.rects[2]))
        clean(self, outcome.rects)

    def test_the_committed_weights_give_the_previewed_rects(self):
        outcome = self.resize(1, grown(self.rects[1], right=-120), {"h": "right"})
        stored = LayoutSpec(SIDE_BY_SIDE, gap=0, weights=outcome.changes["weights"])
        self.assertEqual(tile(stored, WORK, 3, 1.0, ids=self.ids), outcome.rects)

    def test_edges_on_the_border_and_heights_are_refused_with_a_reason(self):
        self.assertIn("border of the screen", self.resize(0, grown(self.rects[0], left=-50), {"h": "left"}))
        self.assertIn("border of the screen", self.resize(2, grown(self.rects[2], right=50), {"h": "right"}))
        self.assertIn("full height", self.resize(1, grown(self.rects[1], bottom=-100), {"v": "bottom"}))

    def test_a_tile_never_becomes_smaller_than_its_neighbours_minimum_or_a_sliver(self):
        outcome = self.resize(0, grown(self.rects[0], right=2000), {"h": "right"},
                              minimums=[(0, 0), (500, 0), (0, 0)])
        self.assertGreaterEqual(outcome.rects[1][2] - outcome.rects[1][0], 500)
        clean(self, outcome.rects)
        outcome = self.resize(0, grown(self.rects[0], right=-2000), {"h": "right"})  # shrink to nothing
        self.assertGreater(outcome.rects[0][2] - outcome.rects[0][0], 50)
        clean(self, outcome.rects)
        outcome = self.resize(0, grown(self.rects[0], right=-2000), {"h": "right"},
                              minimums=[(450, 0), (0, 0), (0, 0)])
        self.assertGreaterEqual(outcome.rects[0][2] - outcome.rects[0][0], 450)

    def test_a_resize_that_changes_nothing_says_so(self):
        far = self.resize(0, grown(self.rects[0], right=2000), {"h": "right"})
        again = LayoutSpec(SIDE_BY_SIDE, gap=0, weights=far.changes["weights"])
        text = self.resize(0, grown(far.rects[0], right=2000), {"h": "right"}, spec=again)
        self.assertIn("cannot change any further", text)

    def test_with_gaps_and_a_scaled_monitor(self):
        spec = LayoutSpec(SIDE_BY_SIDE, gap=8)
        rects = tile(spec, WORK, 3, 1.5, ids=self.ids)
        outcome = resize_tile(spec, WORK, 3, 1.5, [(0, 0)] * 3, self.ids, 0, grown(rects[0], right=90), {"h": "right"})
        self.assertEqual(outcome.rects[0][2], rects[0][2] + 90)
        self.assertEqual(outcome.rects[1][0] - outcome.rects[0][2], 12)  # the gap is still 8 * 1.5
        self.assertEqual((outcome.rects[0][0], outcome.rects[2][2]), (12, 1988))  # the outer gap too
        clean(self, outcome.rects)


class StoredProportionsTests(unittest.TestCase):
    def test_a_new_window_takes_an_average_share_not_half_the_screen(self):
        spec = LayoutSpec(SIDE_BY_SIDE, gap=0, weights=[0.7, 0.3])
        rects = tile(spec, WORK, 3, 1.0)
        widths = [r[2] - r[0] for r in rects]
        self.assertEqual(widths[0] > widths[1], True)  # the user's proportion between the first two is kept
        self.assertAlmostEqual(widths[1] / widths[0], 0.3 / 0.7, places=1)
        self.assertLess(widths[2], 0.45 * 2000)
        main = LayoutSpec(MAIN_SIDE, gap=0, heights=[0.8, 0.2])
        sides = tile(main, WORK, 4, 1.0)[1:]
        self.assertLess(sides[2][3] - sides[2][1], 0.5 * 1000)
        self.assertEqual(tile(LayoutSpec(SIDE_BY_SIDE, gap=0), WORK, 3, 1.0)[2], (1333, 0, 2000, 1000))


class MainSideResizeTests(unittest.TestCase):
    def setUp(self):
        self.spec = LayoutSpec(MAIN_SIDE, gap=0, ratio=0.6)
        self.ids = ["m", "s1", "s2", "s3"]
        self.rects = tile(self.spec, WORK, 4, 1.0, ids=self.ids)

    def resize(self, index, frame, edges, minimums=None, count=4, spec=None):
        return resize_tile(spec or self.spec, WORK, count, 1.0, minimums or [(0, 0)] * count, self.ids[:count],
                           index, frame, edges)

    def test_main_width_from_either_side_of_the_divider(self):
        for index, frame in ((0, grown(self.rects[0], right=-200)), (1, grown(self.rects[1], left=-200))):
            outcome = self.resize(index, frame, {"h": "right" if index == 0 else "left"})
            self.assertAlmostEqual(outcome.changes["ratio"], 0.5, places=3)
            self.assertEqual(outcome.rects[0][2], 1000)
            self.assertEqual(outcome.rects[1][0], 1000)
            clean(self, outcome.rects)

    def test_ratio_stays_in_its_bounds(self):
        outcome = self.resize(0, grown(self.rects[0], right=2000), {"h": "right"})
        self.assertEqual(outcome.changes["ratio"], 0.8)

    def test_side_heights_exchange_between_neighbours_only(self):
        outcome = self.resize(1, grown(self.rects[1], bottom=60), {"v": "bottom"})
        self.assertEqual(outcome.rects[1][3], self.rects[1][3] + 60)
        self.assertEqual(outcome.rects[3], self.rects[3])
        self.assertEqual(outcome.rects[0], self.rects[0])
        clean(self, outcome.rects)
        top = self.resize(3, grown(self.rects[3], top=-60), {"v": "top"})
        self.assertEqual(top.rects[3][1], self.rects[3][1] - 60)
        clean(self, top.rects)

    def test_the_main_window_and_the_outer_side_edges_have_no_height_divider(self):
        self.assertIn("full height", self.resize(0, grown(self.rects[0], bottom=-50), {"v": "bottom"}))
        self.assertIn("border", self.resize(1, grown(self.rects[1], top=-50), {"v": "top"}))
        self.assertIn("border", self.resize(3, grown(self.rects[3], bottom=50), {"v": "bottom"}))
        self.assertIn("border", self.resize(0, grown(self.rects[0], left=-50), {"h": "left"}))

    def test_a_corner_changes_ratio_and_heights_together(self):
        outcome = self.resize(1, grown(self.rects[1], left=-100, bottom=40), {"h": "left", "v": "bottom"})
        self.assertEqual(set(outcome.changes), {"ratio", "heights"})
        clean(self, outcome.rects)

    def test_minimums_hold(self):
        outcome = self.resize(0, grown(self.rects[0], right=2000), {"h": "right"},
                              minimums=[(0, 0), (900, 0), (0, 0), (0, 0)])
        self.assertGreaterEqual(outcome.rects[1][2] - outcome.rects[1][0], 900)
        heights = self.resize(1, grown(self.rects[1], bottom=900), {"v": "bottom"},
                              minimums=[(0, 0), (0, 0), (0, 200), (0, 0)])
        self.assertGreaterEqual(heights.rects[2][3] - heights.rects[2][1], 200)


class GridResizeTests(unittest.TestCase):
    def setUp(self):
        self.spec = LayoutSpec(GRID, gap=0)
        self.ids = list("abcde")

    def rects(self, count):
        return tile(self.spec, WORK, count, 1.0, ids=self.ids[:count])

    def resize(self, count, index, frame, edges, spec=None, minimums=None):
        return resize_tile(spec or self.spec, WORK, count, 1.0, minimums or [(0, 0)] * count, self.ids[:count],
                           index, frame, edges)

    def test_columns_and_rows_of_a_full_grid(self):
        rects = self.rects(4)
        wide = self.resize(4, 0, grown(rects[0], right=200), {"h": "right"})
        self.assertEqual(set(wide.changes), {"grid_cols"})
        self.assertEqual((wide.rects[0][2], wide.rects[2][2]), (1200, 1200))  # the whole column moved
        clean(self, wide.rects)
        tall = self.resize(4, 1, grown(rects[1], bottom=100), {"v": "bottom"})
        self.assertEqual(set(tall.changes), {"grid_rows"})
        self.assertEqual((tall.rects[1][3], tall.rects[3][1]), (600, 600))
        clean(self, tall.rects)

    def test_a_stretched_last_row_has_no_vertical_divider_but_rows_still_resize(self):
        rects = self.rects(3)  # two on top, one stretched below
        self.assertIn("last row", self.resize(3, 2, grown(rects[2], right=-100), {"h": "right"}))
        outcome = self.resize(3, 2, grown(rects[2], top=-100), {"v": "top"})
        self.assertEqual(outcome.rects[2][1], rects[2][1] - 100)
        clean(self, outcome.rects)

    def test_stored_columns_apply_to_full_rows_only(self):
        spec = LayoutSpec(GRID, gap=0, grid_cols=[0.7, 0.3])
        rects = tile(spec, WORK, 3, 1.0)
        self.assertEqual((rects[0][2], rects[2]), (1400, (0, 500, 2000, 1000)))

    def test_proportions_from_another_shape_are_not_used(self):
        spec = LayoutSpec(GRID, gap=0, grid_cols=[0.7, 0.3], grid_rows=[1, 2, 3])
        self.assertEqual(tile(spec, WORK, 4, 1.0)[:2], [(0, 0, 1400, 500), (1400, 0, 2000, 500)])  # rows: ignored
        self.assertEqual(tile(spec, WORK, 9, 1.0)[0][2], 667)  # 3 columns now: the two stored widths do not fit

    def test_round_trip_through_the_stored_dict(self):
        spec = LayoutSpec(GRID, gap=0, grid_cols=[0.7, 0.3], grid_rows=[0.4, 0.6])
        self.assertEqual(LayoutSpec.from_dict(spec.to_dict()).grid_cols, [0.7, 0.3])
        self.assertEqual(LayoutSpec.from_dict(spec.to_dict()).grid_rows, [0.4, 0.6])
        self.assertEqual(LayoutSpec.from_dict({"kind": GRID, "grid_cols": [0, "x", -1]}).grid_cols, [])


class BspResizeTests(unittest.TestCase):
    def setUp(self):
        self.spec = LayoutSpec(BSP, gap=0)
        self.ids = ["a", "b", "c", "d"]
        self.rects = tile(self.spec, WORK, 4, 1.0, ids=self.ids)  # a | b | (c over d)

    def resize(self, index, frame, edges, minimums=None, spec=None):
        return resize_tile(spec or self.spec, WORK, 4, 1.0, minimums or [(0, 0)] * 4, self.ids, index, frame, edges)

    def test_every_edge_moves_its_own_split(self):
        outcome = self.resize(1, grown(self.rects[1], right=100), {"h": "right"})  # b | (c, d): the second split
        self.assertEqual((outcome.rects[1][2], outcome.rects[2][0], outcome.rects[3][0]), (1600, 1600, 1600))
        self.assertEqual(outcome.rects[0], self.rects[0])
        clean(self, outcome.rects)
        outcome = self.resize(2, grown(self.rects[2], bottom=-100), {"v": "bottom"})  # c over d
        self.assertEqual((outcome.rects[2][3], outcome.rects[3][1]), (400, 400))
        self.assertEqual(outcome.rects[:2], self.rects[:2])
        clean(self, outcome.rects)

    def test_the_stored_tree_gives_the_previewed_rects(self):
        outcome = self.resize(0, grown(self.rects[0], right=-300), {"h": "right"})
        stored = LayoutSpec(BSP, gap=0, bsp=outcome.changes["bsp"])
        self.assertEqual(tile(stored, WORK, 4, 1.0, ids=self.ids), outcome.rects)

    def test_edges_without_a_divider(self):
        self.assertIn("no divider", self.resize(0, grown(self.rects[0], left=-30), {"h": "left"}))
        self.assertIn("no divider", self.resize(0, grown(self.rects[0], bottom=-30), {"v": "bottom"}))
        self.assertIn("no divider", self.resize(3, grown(self.rects[3], bottom=30), {"v": "bottom"}))

    def test_ratio_bounds_and_minimums(self):
        outcome = self.resize(0, grown(self.rects[0], right=5000), {"h": "right"})
        self.assertLessEqual(outcome.rects[0][2], 1800)  # MAX_RATIO of the split
        outcome = self.resize(0, grown(self.rects[0], right=5000), {"h": "right"},
                              minimums=[(0, 0), (700, 0), (0, 0), (0, 0)])
        self.assertGreaterEqual(outcome.rects[1][2] - outcome.rects[1][0], 700)
        clean(self, outcome.rects)

    def test_edge_helper_on_a_plain_tree(self):
        tree = bsp.build(["a", "b"], WORK)
        moved = bsp.resize_edge(tree, "a", "right", 600, WORK)
        self.assertAlmostEqual(moved.ratio, 0.3, places=3)
        self.assertEqual(bsp.rects(tree, WORK)["a"][2], 1000)  # the input is never modified
        self.assertIsNone(bsp.resize_edge(tree, "a", "left", 50, WORK))
        self.assertIsNone(bsp.resize_edge(tree, "zzz", "right", 600, WORK))


class AutomaticResizeTests(unittest.TestCase):
    def test_automatic_resizes_the_layout_it_resolved_to(self):
        spec = LayoutSpec(AUTO, gap=0)
        ids = ["a", "b", "c"]
        rects = tile(spec, WORK, 3, 1.0, ids=ids)  # 3 windows: main + side
        outcome = resize_tile(spec, WORK, 3, 1.0, [(0, 0)] * 3, ids, 0, grown(rects[0], right=-100), {"h": "right"})
        self.assertEqual(set(outcome.changes), {"ratio"})
        self.assertEqual(outcome.rects[0][2], rects[0][2] - 100)

    def test_a_lone_tile_has_nothing_to_resize_against(self):
        self.assertIn("only tile", resize_tile(LayoutSpec(BSP, gap=0), WORK, 1, 1.0, [(0, 0)], ["a"], 0, WORK,
                                               {"h": "right"}))


class MinimumFallbackTests(unittest.TestCase):
    def test_nothing_changes_while_everything_fits(self):
        spec = LayoutSpec(SIDE_BY_SIDE, gap=0)
        fitted, out = fit_layout(spec, WORK, [(600, 0)] * 3, ids=list("abc"))
        self.assertIs(fitted, spec)
        self.assertEqual(out, [])

    def test_another_layout_before_a_window_floats(self):
        spec = LayoutSpec(SIDE_BY_SIDE, gap=0)
        minimums = [(800, 300)] * 3
        self.assertEqual(overflowing(spec, WORK, minimums, 1.0, list("abc")), [2])  # side by side: the third floats
        fitted, out = fit_layout(spec, WORK, minimums, 1.0, list("abc"))
        self.assertEqual(out, [])
        self.assertNotEqual(fitted.kind, SIDE_BY_SIDE)
        rects = tile(fitted, WORK, 3, 1.0, minimums, list("abc"))
        clean(self, rects)
        for rect, (width, height) in zip(rects, minimums):
            self.assertGreaterEqual(rect[2] - rect[0], width)
            self.assertGreaterEqual(rect[3] - rect[1], height)
        self.assertEqual(spec.kind, SIDE_BY_SIDE)  # the chosen layout object is untouched

    def test_rows_when_only_a_column_fits(self):
        spec = LayoutSpec(SIDE_BY_SIDE, gap=0)
        minimums = [(1300, 200)] * 3  # no two side by side; three rows of 333 px fit
        fitted, out = fit_layout(spec, WORK, minimums, 1.0, list("abc"))
        self.assertEqual(out, [])
        self.assertEqual((fitted.kind, fitted.columns), (GRID, 1))
        clean(self, tile(fitted, WORK, 3, 1.0, minimums, list("abc")))

    def test_the_fewest_floats_win_and_the_answer_is_stable(self):
        spec = LayoutSpec(SIDE_BY_SIDE, gap=0)
        minimums = [(1200, 700)] * 3  # neither three rows nor two columns: one window fits beside none
        first = fit_layout(spec, WORK, minimums, 1.0, list("abc"))
        for _ in range(3):
            again = fit_layout(spec, WORK, minimums, 1.0, list("abc"))
            self.assertEqual(again[0].to_dict(), first[0].to_dict())
            self.assertEqual(again[1], first[1])
        self.assertEqual(len(first[1]), 2)

    def test_a_bsp_choice_holds_the_windows_with_its_own_smart_tree(self):
        spec = LayoutSpec(BSP, gap=0)
        fitted, out = fit_layout(spec, WORK, [(800, 300)] * 3, 1.0, list("abc"))
        self.assertEqual(out, [])  # (BSP places new windows where their minimums fit, so it holds them itself)
        rects = tile(fitted, WORK, 3, 1.0, [(800, 300)] * 3, list("abc"))
        clean(self, rects)

    def test_automatic_is_resolved_for_all_the_windows_first(self):
        spec = LayoutSpec(AUTO, gap=0)
        fitted, out = fit_layout(spec, WORK, [(0, 0)] * 3, 1.0, list("abc"))
        self.assertEqual((fitted.kind, out), (MAIN_SIDE, []))


if __name__ == "__main__":
    unittest.main()
