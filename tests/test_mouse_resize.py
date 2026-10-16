"""Resizing a tiled window with the mouse (the user's drag, simulated on the fake desktop) keeps the new size.

The drag is told apart from a move by the edges: a resize anchors the opposite edge. The new size becomes the
layout's proportions (BSP split ratio, side-by-side weights, main ratio and side heights, grid columns and rows),
the neighbours follow without overlapping or leaving the work area, minimum sizes hold, the grey preview shows the
real result, and the window does not jump back when the button is released.
"""
import unittest

from cirana.wm.layout import AUTO, BSP, GRID, MAIN_SIDE, SIDE_BY_SIDE, LayoutSpec
from cirana.wm.monitors import Monitor
from tests.test_minimum_sizes import inside, overlap
from tests.test_tiling_overlay import OverlayEnv
from tests.test_workspace_controller import LEFT, RIGHT

KINDS = (SIDE_BY_SIDE, MAIN_SIDE, GRID, BSP, AUTO)


class ResizeEnv(OverlayEnv):
    def turn_on(self, kind=SIDE_BY_SIDE, **spec):
        self.tiler.set_global_layout(LayoutSpec(kind, gap=0, **spec))
        self.tiler.set_global(True)

    def fresh(self):
        """Another round of a loop in one test: new windows, and no proportions left from the last round."""
        self.desktop.windows.clear()
        self.sync()
        self.settle()
        self.tiler.set_global(False)
        self.settings.set("global_adjust", {})
        self.tiler._adjust = {}
        self.tiler._trees.clear()
        self.tiler._order.clear()
        self.tiler._home.clear()
        self.tiler._history.clear()

    def open_many(self, count, **kwargs):
        windows = [self.open(f"App{i}", **kwargs) for i in range(count)]
        self.flush()
        return windows

    def resize(self, window, left=0, top=0, right=0, bottom=0, release=True):
        """Drag edges of the window: the frame as it is once the mouse is released (or held, ``release=False``)."""
        self.drag(window, True)
        x1, y1, x2, y2 = window.rect
        self.move(window, (x1 + left, y1 + top, x2 + right, y2 + bottom))
        if release:
            self.drag(window, False)
            self.flush()

    def assert_clean(self, windows, work=LEFT.work):
        for i, a in enumerate(windows):
            self.assertTrue(inside(a.rect, work), (a.title, a.rect))
            for b in windows[i + 1:]:
                self.assertFalse(overlap(a.rect, b.rect), (a.title, a.rect, b.title, b.rect))

    def neighbors(self):
        return sorted(o.rect_px for o in self.overlays.neighbors if o.isVisible() and o.rect_px)


class GlobalResizeTests(ResizeEnv):
    def test_the_size_survives_the_release_for_every_layout_and_window_count(self):
        for kind in KINDS:
            for count in (2, 3, 4, 5):
                with self.subTest(kind=kind, count=count):
                    self.fresh()
                    self.turn_on(kind)
                    windows = self.open_many(count)
                    first = windows[0]
                    before = [w.rect for w in windows]
                    self.resize(first, right=-80)
                    self.assertEqual(first.rect[2], before[0][2] - 80, "the window must not jump back")
                    self.assertEqual(first.rect[:2], before[0][:2])
                    self.assert_clean(windows)
                    self.assertEqual(self.tiler.target_of(self.desktop.tracked(first.hwnd).key), first.rect)
                    stable = [w.rect for w in windows]
                    for _ in range(2):  # nothing re-tiles it back later, and nothing keeps moving
                        self.tiler.schedule(force=True)
                        self.flush()
                    self.assertEqual([w.rect for w in windows], stable)

    def test_side_by_side_weights_and_neighbours(self):
        self.turn_on(SIDE_BY_SIDE)
        a, b, c = self.open_many(3)
        third = c.rect
        self.resize(b, right=-100)  # the middle one gets narrower: only the one on its right gives or takes
        self.assertEqual((b.rect[2], c.rect[2]), (1233, 2000))
        self.assertEqual(a.rect, (0, 0, 667, 960))
        self.assertEqual(c.rect[0], b.rect[2])
        self.assertNotEqual(c.rect, third)
        weights = self.settings.get("global_adjust")[LEFT.device]["weights"]
        self.assertAlmostEqual(sum(weights), 1.0, places=2)
        self.assert_clean([a, b, c])

    def test_main_side_ratio_and_heights(self):
        self.turn_on(MAIN_SIDE, ratio=0.6)
        main, top, bottom = self.open_many(3)
        self.resize(main, right=-200)
        self.assertEqual((main.rect[2], top.rect[0], bottom.rect[0]), (1000, 1000, 1000))
        self.assertEqual(self.settings.get("global_adjust")[LEFT.device]["ratio"], 0.5)
        self.resize(top, bottom=120)  # the side windows exchange height
        self.assertEqual((top.rect[3], bottom.rect[1]), (600, 600))
        self.assertEqual(main.rect, (0, 0, 1000, 960))
        self.assert_clean([main, top, bottom])

    def test_grid_columns_and_rows(self):
        self.turn_on(GRID)
        a, b, c, d = self.open_many(4)
        self.resize(a, right=200)
        self.assertEqual((a.rect[2], c.rect[2], b.rect[0], d.rect[0]), (1200, 1200, 1200, 1200))
        self.resize(b, bottom=-100)
        self.assertEqual((a.rect[3], b.rect[3], c.rect[1], d.rect[1]), (380, 380, 380, 380))
        self.assert_clean([a, b, c, d])

    def test_grid_with_a_stretched_last_row(self):
        self.turn_on(GRID)
        a, b, c = self.open_many(3)
        self.resize(c, right=-300)  # the stretched tile has no divider of its own: back in its tile, with a reason
        self.assertEqual(c.rect, (0, 480, 2000, 960))
        self.assertTrue(any("last row" in m for m in self.messages))
        self.resize(c, top=-100)  # but the rows can move
        self.assertEqual((a.rect[3], b.rect[3], c.rect[1]), (380, 380, 380))

    def test_bsp_split_ratio(self):
        self.turn_on(BSP)
        a, b, c = self.open_many(3)
        self.resize(a, right=300)
        self.assertEqual((a.rect[2], b.rect[0]), (1300, 1300))
        self.assertEqual(c.rect[2], 2000)
        self.assert_clean([a, b, c])
        self.resize(c, left=-150)
        self.assertEqual(c.rect[0], b.rect[2])
        self.assert_clean([a, b, c])

    def test_a_corner_resizes_both_directions(self):
        self.turn_on(BSP)
        a, b, c, d = self.open_many(4)  # a | b | (c over d)
        self.resize(c, left=-100, bottom=80)
        self.assertEqual((c.rect[0], c.rect[3]), (b.rect[2], d.rect[1]))
        self.assertEqual(c.rect[0], 1400)
        self.assert_clean([a, b, c, d])

    def test_dragging_far_into_the_neighbour_resizes_instead_of_swapping(self):
        self.turn_on(SIDE_BY_SIDE)
        a, b, c = self.open_many(3)
        self.resize(a, right=500)  # its centre ends up inside the middle tile, which a move would swap with
        self.assertEqual(a.rect, (0, 0, 1167, 960))
        self.assertEqual([a.rect[0], b.rect[0], c.rect[0]], sorted([a.rect[0], b.rect[0], c.rect[0]]))
        self.assert_clean([a, b, c])
        key = self.desktop.tracked(a.hwnd).key
        self.assertEqual(self.tiler._order[0], key)  # the order is untouched

    def test_a_move_still_swaps(self):
        self.turn_on(SIDE_BY_SIDE)
        a, b = self.open_many(2)
        self.drag(a, True)
        self.move(a, (1100, 100, 1500, 500))
        self.drag(a, False)
        self.flush()
        self.assertEqual((a.rect, b.rect), ((1000, 0, 2000, 960), (0, 0, 1000, 960)))
        self.assertEqual(self.settings.get("global_adjust"), {})  # a move never stores proportions

    def test_known_minimum_sizes_hold(self):
        self.turn_on(SIDE_BY_SIDE)
        a = self.open("A")
        b = self.open("B", min_size=(700, 0))
        c = self.open("C")
        self.flush()
        self.resize(a, right=1500)  # tries to take everything
        self.assertGreaterEqual(b.rect[2] - b.rect[0], 700)
        self.assert_clean([a, b, c])
        self.assertEqual(c.rect[2], 2000)

    def test_the_layout_has_every_tile_even_when_a_resize_is_extreme(self):
        for kind in (SIDE_BY_SIDE, MAIN_SIDE, GRID, BSP):
            with self.subTest(kind=kind):
                self.fresh()
                self.turn_on(kind)
                windows = self.open_many(4)
                self.resize(windows[0], right=5000)
                self.resize(windows[0], left=-5000)
                self.assert_clean(windows)
                for w in windows:
                    self.assertGreater(w.rect[2] - w.rect[0], 40)
                    self.assertGreater(w.rect[3] - w.rect[1], 40)

    def test_an_impossible_edge_goes_back_and_says_why(self):
        self.turn_on(SIDE_BY_SIDE)
        a, b = self.open_many(2)
        self.resize(a, left=-60)  # the outer edge
        self.assertEqual(a.rect, (0, 0, 1000, 960))
        self.assertTrue(any("border of the screen" in m for m in self.messages))

    def test_the_proportions_survive_other_windows_coming_and_going(self):
        self.turn_on(BSP)
        a, b = self.open_many(2)
        self.resize(a, right=-400)
        self.assertEqual(a.rect[2], 600)
        c = self.open("C")
        self.flush()
        self.assertEqual(a.rect[2], 600)  # the new window splits one tile; the other proportion stays
        self.desktop.close(c.hwnd)
        self.sync()
        self.flush()
        self.assertEqual((a.rect[2], b.rect[0]), (600, 600))

    def test_gap_changes_keep_the_proportions_but_a_new_layout_resets_them(self):
        self.turn_on(SIDE_BY_SIDE)
        a, b = self.open_many(2)
        self.resize(a, right=-200)
        self.tiler.set_global_layout(LayoutSpec(SIDE_BY_SIDE, gap=10))
        self.flush()
        self.assertLess(a.rect[2] - a.rect[0], b.rect[2] - b.rect[0])
        self.tiler.set_global_layout(LayoutSpec(MAIN_SIDE, gap=10))
        self.flush()
        self.assertEqual(self.settings.get("global_adjust"), {})
        self.assertGreater(a.rect[2] - a.rect[0], b.rect[2] - b.rect[0])

    def test_keyboard_resize_still_works_on_top_of_a_mouse_resize(self):
        self.turn_on(SIDE_BY_SIDE)
        a, b = self.open_many(2)
        self.resize(a, right=-200)
        self.assertEqual(a.rect[2], 800)
        self.focus(a)
        self.desktop.foreground = a.hwnd
        self.assertTrue(self.tiler.resize(True, True))
        self.flush()
        self.assertEqual(a.rect[2], 900)  # 5 % of the width on top of the dragged proportion
        self.assert_clean([a, b])


class PreviewAndBorderTests(ResizeEnv):
    def test_the_preview_shows_the_real_target_sizes_while_the_mouse_is_held(self):
        for kind in (SIDE_BY_SIDE, MAIN_SIDE, GRID, BSP):
            with self.subTest(kind=kind):
                self.fresh()
                self.turn_on(kind)
                windows = self.open_many(3)
                first = windows[0]
                self.resize(first, right=-120, release=False)
                own, neighbours = self.preview(), self.neighbors()
                self.assertIsNotNone(own)
                self.assertEqual(own[2], first.rect[2])  # the tile whose edge the user holds
                self.drag(first, False)
                self.flush()
                self.assertEqual(first.rect, own)  # released: exactly what the preview showed
                self.assertEqual(sorted(w.rect for w in windows[1:]), neighbours)
                self.assertIsNone(self.preview())
                self.assertEqual(self.neighbors(), [])  # and the grey boxes are gone

    def test_the_preview_of_an_impossible_resize_stays_on_the_old_tile(self):
        self.turn_on(SIDE_BY_SIDE)
        a, b = self.open_many(2)
        self.resize(a, left=-50, release=False)
        self.assertEqual(self.preview(), (0, 0, 1000, 960))
        self.assertEqual(self.neighbors(), [])
        self.drag(a, False)

    def test_borders_follow_the_new_sizes(self):
        self.turn_on(SIDE_BY_SIDE)
        a, b, c = self.open_many(3)
        self.focus(b)
        self.resize(b, right=100)
        for window in (a, b, c):
            border = self.border(window)
            t = border.thickness
            self.assertEqual(border.rect_px, (window.rect[0] - t, window.rect[1] - t, window.rect[2] + t,
                                              window.rect[3] + t))

    def test_few_reflows(self):
        self.turn_on(SIDE_BY_SIDE)
        a, b, c = self.open_many(3)
        count = self.tiler.count
        self.resize(a, right=80)
        self.assertEqual(self.tiler.count, count + 1)  # one re-tile for the whole resize: no loop, no flicker


class WorkspaceResizeTests(ResizeEnv):
    def test_workspace_proportions_are_stored_and_survive_switching_and_restart(self):
        ws = self.tiled_workspace("Code", "Notes", "Brave", kind=MAIN_SIDE, ratio=0.6)
        code, notes, brave = self.open("Code"), self.open("Notes"), self.open("Brave")
        self.flush()
        self.resize(code, right=-200)
        self.assertEqual(self.manager.get(ws.id).layout.ratio, 0.5)
        self.assertEqual((code.rect[2], notes.rect[0]), (1000, 1000))
        self.resize(notes, bottom=100)
        heights = self.manager.get(ws.id).layout.heights
        self.assertEqual(len(heights), 2)
        other = self.manager.create("Other", [])
        self.manager.set_layout(other.id, LayoutSpec(SIDE_BY_SIDE, gap=0, dynamic=True))
        self.manager.set_active(other.id)
        self.settle()
        self.manager.set_active(ws.id)
        self.flush()
        self.assertEqual((code.rect[2], notes.rect[0]), (1000, 1000))  # kept
        self.build()  # a restart
        self.assertEqual(self.manager.get(ws.id).layout.ratio, 0.5)
        self.assertEqual(self.manager.get(ws.id).layout.heights, heights)

    def test_workspace_bsp_tree_is_stored(self):
        ws = self.tiled_workspace("Code", "Notes", kind=BSP)
        code, notes = self.open("Code"), self.open("Notes")
        self.flush()
        self.resize(code, right=300)
        self.assertEqual((code.rect[2], notes.rect[0]), (1300, 1300))
        self.assertAlmostEqual(self.manager.get(ws.id).layout.bsp["ratio"], 0.65, places=2)

    def test_workspace_grid(self):
        ws = self.tiled_workspace("Code", "Notes", "Brave", "Shell", kind=GRID)
        windows = [self.open(n) for n in ("Code", "Notes", "Brave")]
        windows.append(self.desktop.add(r"c:\windows\system32\powershell.exe", "Shell"))
        self.sync()
        self.flush()
        self.resize(windows[0], right=100)
        self.assertEqual(len(self.manager.get(ws.id).layout.grid_cols), 2)
        self.assertEqual(windows[2].rect[2], windows[0].rect[2])


class OtherMonitorTests(ResizeEnv):
    def test_a_scaled_monitor_and_per_monitor_proportions(self):
        right = Monitor(RIGHT.device, RIGHT.rect, RIGHT.work, False, 144)
        self.desktop.monitors_ = [LEFT, right]
        self.turn_on(SIDE_BY_SIDE)
        l1, l2 = self.open("L1"), self.open("L2")
        r1 = self.open("R1", rect=(2100, 100, 2600, 600))
        r2 = self.open("R2", rect=(2200, 200, 2700, 700))
        self.flush()
        gap = round(0 * right.scale)
        self.resize(r1, right=-150)
        self.assertEqual(r1.rect[2], 3000 - 150)
        self.assertEqual(r2.rect[0], r1.rect[2] + gap)
        self.assertEqual((l1.rect, l2.rect), ((0, 0, 1000, 960), (1000, 0, 2000, 960)))  # the other monitor is untouched
        adjust = self.settings.get("global_adjust")
        self.assertIn(right.device, adjust)
        self.assertNotIn(LEFT.device, adjust)
        self.assert_clean([r1, r2], work=right.work)

    def test_proportions_stay_when_a_monitor_is_unplugged(self):
        self.turn_on(SIDE_BY_SIDE)
        a, b = self.open_many(2)
        self.resize(a, right=-300)
        self.desktop.monitors_ = [LEFT]
        self.tiler.display_changed()
        self.flush()
        self.assertEqual((a.rect[2], b.rect[0]), (700, 700))

    def test_a_scaled_monitor_with_gaps(self):
        right = Monitor(RIGHT.device, RIGHT.rect, RIGHT.work, False, 144)
        self.desktop.monitors_ = [LEFT, right]
        self.tiler.set_global_layout(LayoutSpec(SIDE_BY_SIDE, gap=8))
        self.tiler.set_global(True)
        r1 = self.open("R1", rect=(2100, 100, 2600, 600))
        r2 = self.open("R2", rect=(2200, 200, 2700, 700))
        self.flush()
        self.assertEqual(r2.rect[0] - r1.rect[2], 12)  # 8 px at 150 %
        self.resize(r1, right=100)
        self.assertEqual(r2.rect[0] - r1.rect[2], 12)
        self.assertEqual((r1.rect[0], r2.rect[2]), (2012, 3988))
        self.assert_clean([r1, r2], work=right.work)


class StackAndFloatTests(ResizeEnv):
    def test_a_stack_resizes_as_one_tile(self):
        self.turn_on(SIDE_BY_SIDE)
        a, b, c = self.open_many(3)
        self.assertTrue(self.tiler.stack_with(self.desktop.tracked(b.hwnd), self.desktop.tracked(a.hwnd)))
        self.flush()
        self.assertEqual(a.rect, b.rect)  # (the hidden window lies under the shown one)
        self.resize(b, right=100)
        self.assertEqual(b.rect[2], 1100)
        self.assertEqual(a.rect, b.rect)  # the stack's other window follows its tile
        self.assertEqual(c.rect[0], 1100)

    def test_a_floating_window_is_not_a_tile_and_a_drag_of_it_changes_nothing(self):
        self.turn_on(SIDE_BY_SIDE)
        a, b, c = self.open_many(3)
        self.tiler.toggle_float(self.desktop.tracked(c.hwnd))
        self.flush()
        placed = c.rect
        self.resize(c, right=-100)  # not tiled: left exactly where the user put it
        self.assertEqual(self.settings.get("global_adjust"), {})
        self.assertEqual(c.rect, (placed[0], placed[1], placed[2] - 100, placed[3]))
        self.assertEqual(a.rect, (0, 0, 1000, 960))

    def test_no_resize_on_a_layout_substituted_for_minimum_sizes(self):
        self.turn_on(SIDE_BY_SIDE)
        windows = [self.open(f"W{i}", min_size=(800, 300)) for i in range(3)]
        self.flush()
        group = self.tiler.shown_groups()[0]
        self.assertTrue(group.fallback)
        before = [w.rect for w in windows]
        self.resize(windows[0], right=-100)
        self.assertEqual([w.rect for w in windows], before)
        self.assertTrue(any("minimum sizes" in m for m in self.messages))
        self.assertEqual(self.settings.get("global_adjust"), {})


if __name__ == "__main__":
    unittest.main()
