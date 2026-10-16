"""Applications with a minimum size under dynamic tiling.

Windows keeps the left / top edge CIRANA asks for and lets a window that refuses to be as small as its tile grow to
the right and down - over its neighbours (they hide that part of it) or off the screen. The tiler learns the minimum
from what it reads back, makes room for it, floats a window that cannot fit (kept completely on the screen) and says
why; the drop preview keeps showing exactly where a window lands.
"""
import unittest

from cirana.core.dynamic_tiling import RATE_LIMIT
from cirana.wm.layout import GRID, MAIN_SIDE, SIDE_BY_SIDE, LayoutSpec, keep_visible, overflowing, tile
from cirana.wm.monitors import Monitor
from tests.test_tiling_overlay import OverlayEnv
from tests.test_workspace_controller import LEFT, RIGHT


def overlap(a, b):
    return min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1])


def inside(rect, area):
    return area[0] <= rect[0] and area[1] <= rect[1] and rect[2] <= area[2] and rect[3] <= area[3]


class LayoutMinimumTests(unittest.TestCase):
    WORK = (0, 0, 2000, 1000)

    def test_without_minimums_nothing_changes(self):
        for kind in (SIDE_BY_SIDE, MAIN_SIDE, GRID):
            for count in range(1, 7):
                spec = LayoutSpec(kind, gap=8)
                self.assertEqual(tile(spec, self.WORK, count, 1.25), tile(spec, self.WORK, count, 1.25, None))
                self.assertEqual(tile(spec, self.WORK, count, 1.25), tile(spec, self.WORK, count, 1.25,
                                                                           [(0, 0)] * count))

    def test_every_tile_gets_at_least_its_minimum(self):
        cases = [
            (SIDE_BY_SIDE, [(0, 0), (1100, 0), (0, 0)]),
            (MAIN_SIDE, [(0, 0), (900, 0), (0, 700)]),  # the side column widens, its rows follow the heights
            (GRID, [(0, 0), (1500, 0), (0, 0), (0, 800)]),
        ]
        for kind, mins in cases:
            rects = tile(LayoutSpec(kind, gap=10), self.WORK, len(mins), 1.0, mins)
            for rect, (width, height) in zip(rects, mins):
                self.assertGreaterEqual(rect[2] - rect[0], width, kind)
                self.assertGreaterEqual(rect[3] - rect[1], height, kind)
                self.assertTrue(inside(rect, (10, 10, 1990, 990)), (kind, rect))
            for i, a in enumerate(rects):
                for b in rects[i + 1:]:
                    self.assertFalse(overlap(a, b), (kind, a, b))
        # the others share what is left by their weights
        rects = tile(LayoutSpec(SIDE_BY_SIDE, gap=0), self.WORK, 3, 1.0, [(1000, 0), (0, 0), (0, 0)])
        self.assertEqual(rects, [(0, 0, 1000, 1000), (1000, 0, 1500, 1000), (1500, 0, 2000, 1000)])

    def test_overflowing_floats_the_newest_window_with_a_minimum(self):
        spec = LayoutSpec(SIDE_BY_SIDE, gap=0)
        self.assertEqual(overflowing(spec, self.WORK, [(1200, 0), (0, 0), (1200, 0), (0, 0)]), [2])
        self.assertEqual(overflowing(spec, self.WORK, [(800, 0), (800, 0), (0, 0)]), [])
        self.assertEqual(overflowing(spec, self.WORK, [(0, 0), (2500, 0)]), [1])  # larger than the monitor
        self.assertEqual(overflowing(spec, self.WORK, [(0, 0), (0, 1200)]), [1])
        self.assertEqual(overflowing(spec, self.WORK, [(2000, 0), (0, 0)]), [0])  # nothing would be left for the other

    def test_keep_visible_moves_but_never_shrinks(self):
        self.assertEqual(keep_visible(self.WORK, (1500, 100, 2600, 600)), (900, 100, 2000, 600))
        self.assertEqual(keep_visible(self.WORK, (100, 700, 600, 1300)), (100, 400, 600, 1000))
        self.assertEqual(keep_visible(self.WORK, (300, 50, 2800, 400)), (0, 50, 2500, 400))  # top-left stays inside
        self.assertEqual(keep_visible(self.WORK, (10, 10, 500, 500)), (10, 10, 500, 500))


class TilerMinimumTests(OverlayEnv):
    def build(self):
        super().build()
        self.messages = []
        self.wm.message.connect(self.messages.append)

    def assert_clean(self, *windows, work=LEFT.work):
        for i, a in enumerate(windows):
            self.assertTrue(inside(a.rect, work), (a.title, a.rect))
            for b in windows[i + 1:]:
                self.assertFalse(overlap(a.rect, b.rect), (a.title, a.rect, b.title, b.rect))

    def test_room_is_made_for_a_minimum_size(self):
        self.global_on()
        code, wide, mail = self.open("Code"), self.open("Wide", min_size=(1100, 0)), self.open("Mail")
        self.flush()
        self.assertEqual(wide.rect[2] - wide.rect[0], 1100)
        self.assert_clean(code, wide, mail)  # nothing sticks out under a neighbour or off the screen
        self.assertEqual(self.tiler.float_reason(self.desktop.tracked(wide.hwnd)), "")
        self.assertEqual(self.messages, [])  # handled: nothing to complain about
        self.assertEqual(self.tiler.count, 2)  # the first round, and one with room for it
        self.assertEqual(self.border(wide).rect_px, (wide.rect[0] - 2, wide.rect[1] - 2, wide.rect[2] + 2,
                                                     wide.rect[3] + 2))

    def test_no_loop(self):
        """A cascade: making room for one minimum squeezes the others below theirs, round after round - and then it
        stops."""
        self.global_on()
        # four tiles of 500 px: only A is too wide; with room for A, B and D no longer fit their share
        windows = [self.open("A", min_size=(900, 0)), self.open("B", min_size=(450, 0)), self.open("C"),
                   self.open("D", min_size=(400, 0))]
        self.flush()
        count = self.tiler.count
        self.assertEqual(count, 3)  # A learned in round 1, B and D (squeezed by the room made for A) in round 2
        self.assertLess(count, RATE_LIMIT)  # never near the rate limit (its back-off did not fire)
        for _ in range(3):
            self.tiler.schedule()
            self.flush()
        self.assertEqual(self.tiler.count, count)
        self.assert_clean(*windows)
        self.assertEqual([x.rect[2] - x.rect[0] for x in windows], [900, 450, 250, 400])

    def test_a_window_that_does_not_fit_floats_completely_visible(self):
        self.global_on()
        first, second = self.open("First", min_size=(1200, 600)), self.open("Second", min_size=(1200, 600))
        self.flush()
        self.assertEqual(first.rect, LEFT.work)  # tiled alone
        self.assertTrue(inside(second.rect, LEFT.work), second.rect)  # floating, moved (not shrunk) on the screen
        self.assertGreaterEqual(second.rect[2] - second.rect[0], 1200)  # never shrunk below its minimum
        reason = self.tiler.float_reason(self.desktop.tracked(second.hwnd))
        self.assertIn("needs at least 1200 px wide × 600 px high", reason)
        self.assertEqual(len(self.messages), 1)
        self.assertIn("Second", self.messages[0])
        self.assertIsNone(self.border(second))  # not tiled: no tile border
        self.assertIsNotNone(self.border(first))
        self.desktop.close(first.hwnd)  # room again: it is tiled
        self.sync()
        self.flush()
        self.assertEqual(second.rect, LEFT.work)
        self.assertEqual(self.tiler.float_reason(self.desktop.tracked(second.hwnd)), "")
        self.assertEqual(len(self.messages), 1)  # said once

    def test_a_window_larger_than_the_monitor(self):
        self.global_on()
        code, huge = self.open("Code"), self.open("Huge", min_size=(2600, 0))
        self.flush()
        self.assertEqual(code.rect, LEFT.work)
        self.assertEqual(huge.rect[:2], LEFT.work[:2])  # its top-left corner (title bar) stays on the screen
        self.assertIn("cannot be shown completely", self.tiler.float_reason(self.desktop.tracked(huge.hwnd)))

    def test_workspace_tiling_makes_room_too(self):
        self.tiled_workspace("Code", "Notes", "Brave", kind=MAIN_SIDE)
        code, notes, brave = self.open("Code"), self.open("Notes", min_size=(0, 700)), self.open("Brave")
        self.flush()
        self.assertGreaterEqual(notes.rect[3] - notes.rect[1], 700)
        self.assert_clean(code, notes, brave)
        self.assertTrue(all(s.message == "" for s in self.wm.statuses(self.manager.active_id)))

    def test_workspace_window_that_does_not_fit_shows_why(self):
        self.tiled_workspace("Code", "Notes")
        code, notes = self.open("Code", min_size=(1500, 600)), self.open("Notes", min_size=(1000, 600))
        self.flush()
        self.assertEqual(code.rect, LEFT.work)
        self.assertTrue(inside(notes.rect, LEFT.work))
        message = next(s.message for s in self.wm.statuses(self.manager.active_id) if s.window
                       and s.window.hwnd == notes.hwnd)
        self.assertIn("does not fit beside the other tiled windows", message)

    def test_preview_matches_the_landing_with_a_minimum_size(self):
        self.global_on()
        wide, code, mail = self.open("Wide", min_size=(1100, 0)), self.open("Code"), self.open("Mail")
        self.flush()
        self.assert_clean(wide, code, mail)
        self.drag(mail, True)
        self.move(mail, (100, 100, 500, 500))  # over Wide's tile: the two swap, Wide's tile keeps its width
        predicted = self.preview()
        self.assertNotEqual(predicted, self.tiler.target_of(self.desktop.tracked(wide.hwnd).key))
        self.drag(mail, False)
        self.flush()
        self.assertEqual(mail.rect, predicted)
        self.assert_clean(wide, code, mail)

    def test_minimum_follows_the_dpi_of_the_monitor(self):
        right = Monitor(RIGHT.device, RIGHT.rect, RIGHT.work, False, 144)
        self.desktop.monitors_ = [LEFT, right]
        self.global_on()
        self.open("Code")
        wide = self.open("Wide", min_size=(1200, 0))
        self.flush()
        key = self.desktop.tracked(wide.hwnd).key
        self.assertEqual(self.tiler.minimum(key, LEFT), (1200, 0))
        self.assertEqual(self.tiler.minimum(key, right), (1800, 0))  # the same window at 150 %
        self.desktop.close(wide.hwnd)
        self.sync()
        self.flush()
        self.assertEqual(self.tiler.minimum(key, LEFT), (0, 0))  # forgotten with the window


if __name__ == "__main__":
    unittest.main()
