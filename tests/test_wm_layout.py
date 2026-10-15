"""Phase 2: layout geometry and monitor resolution (pure functions)."""
import unittest

from cirana.wm import layout as L
from cirana.wm.monitors import Monitor, MonitorRef, monitor_for_rect, resolve

WORK = (0, 0, 1000, 600)


def width(r):
    return r[2] - r[0]


def height(r):
    return r[3] - r[1]


class LayoutTests(unittest.TestCase):
    def test_single_window_fills_the_work_area_minus_the_gap(self):
        self.assertEqual(L.tile(L.LayoutSpec(gap=10), WORK, 1), [(10, 10, 990, 590)])
        self.assertEqual(L.tile(L.LayoutSpec(gap=0), WORK, 1), [WORK])

    def test_side_by_side_equal_and_weighted(self):
        rects = L.tile(L.LayoutSpec(L.SIDE_BY_SIDE, gap=0), WORK, 2)
        self.assertEqual(rects, [(0, 0, 500, 600), (500, 0, 1000, 600)])
        rects = L.tile(L.LayoutSpec(L.SIDE_BY_SIDE, gap=10, weights=[2, 1]), WORK, 2)
        self.assertEqual(rects[0][0], 10)
        self.assertEqual(rects[1][2], 990)
        self.assertEqual(rects[1][0] - rects[0][2], 10)  # the gap between them
        self.assertAlmostEqual(width(rects[0]) / width(rects[1]), 2, delta=0.05)

    def test_main_and_side_panel(self):
        rects = L.tile(L.LayoutSpec(L.MAIN_SIDE, gap=0, ratio=0.7), WORK, 3)
        self.assertEqual(rects[0], (0, 0, 700, 600))
        self.assertEqual(rects[1], (700, 0, 1000, 300))
        self.assertEqual(rects[2], (700, 300, 1000, 600))

    def test_grid_two_by_two_and_dynamic(self):
        rects = L.tile(L.LayoutSpec(L.GRID, gap=0, columns=2), WORK, 4)
        self.assertEqual(rects, [(0, 0, 500, 300), (500, 0, 1000, 300), (0, 300, 500, 600), (500, 300, 1000, 600)])
        five = L.tile(L.LayoutSpec(L.GRID, gap=0), WORK, 5)  # 3 columns, last row stretched
        self.assertEqual(len(five), 5)
        self.assertEqual(width(five[3]), 500)
        self.assertEqual(five[4][2], 1000)
        for count in range(1, 10):  # never overlapping, always inside the work area
            rects = L.tile(L.LayoutSpec(L.GRID, gap=6), WORK, count)
            for i, a in enumerate(rects):
                self.assertTrue(a[0] >= 0 and a[1] >= 0 and a[2] <= 1000 and a[3] <= 600)
                for b in rects[i + 1:]:
                    overlap = min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1])
                    self.assertFalse(overlap)

    def test_gap_scales_with_dpi(self):
        self.assertEqual(L.tile(L.LayoutSpec(gap=8), WORK, 1, scale=1.5), [(12, 12, 988, 588)])

    def test_work_area_offset_for_a_second_monitor_with_taskbar_on_the_left(self):
        rects = L.tile(L.LayoutSpec(L.SIDE_BY_SIDE, gap=0), (1980, 40, 3900, 1080), 2)
        self.assertEqual(rects[0], (1980, 40, 2940, 1080))

    def test_fractions_survive_a_resolution_change(self):
        fraction = L.to_fraction((100, 60, 600, 360), WORK)
        self.assertEqual(L.from_fraction(fraction, WORK), (100, 60, 600, 360))
        self.assertEqual(L.from_fraction(fraction, (0, 0, 2000, 1200)), (200, 120, 1200, 720))

    def test_clamp_keeps_windows_on_screen(self):
        self.assertEqual(L.clamp_into(WORK, (900, 500, 1300, 800)), (600, 300, 1000, 600))
        self.assertEqual(L.clamp_into(WORK, (-50, 0, 2000, 100)), (0, 0, 1000, 100))

    def test_spec_round_trip_and_damage(self):
        spec = L.LayoutSpec(L.MAIN_SIDE, gap=12, ratio=0.65, monitor=MonitorRef("\\\\.\\DISPLAY2", (1920, 0, 3840, 1080)),
                            free={"e-1": L.FreeSlot((0.1, 0.1, 0.5, 0.5), None, True)})
        self.assertEqual(L.LayoutSpec.from_dict(spec.to_dict()), spec)
        damaged = L.LayoutSpec.from_dict({"kind": "spiral", "gap": "x", "ratio": 5, "free": {"a": {"fraction": [1]}}})
        self.assertEqual((damaged.kind, damaged.gap, damaged.ratio, damaged.free), (L.GRID, 8, 0.8, {}))


class MonitorTests(unittest.TestCase):
    PRIMARY = Monitor("\\\\.\\DISPLAY1", (0, 0, 1920, 1080), (0, 0, 1920, 1040), True, 96)
    SECOND = Monitor("\\\\.\\DISPLAY2", (1920, 0, 4480, 1440), (1920, 0, 4480, 1400), False, 144)

    def test_saved_monitor_is_found_again(self):
        monitors = [self.PRIMARY, self.SECOND]
        self.assertIs(resolve(self.SECOND.ref(), monitors), self.SECOND)
        moved = Monitor(self.SECOND.device, (-2560, 0, 0, 1440), (-2560, 0, 0, 1400), False, 144)
        self.assertIs(resolve(self.SECOND.ref(), [self.PRIMARY, moved]), moved)  # rearranged: same output
        self.assertIs(resolve(self.SECOND.ref(), [self.PRIMARY]), self.PRIMARY)  # unplugged: primary
        self.assertIs(resolve(None, monitors), self.PRIMARY)
        self.assertIsNone(resolve(self.SECOND.ref(), []))

    def test_monitor_for_rect(self):
        monitors = [self.PRIMARY, self.SECOND]
        self.assertIs(monitor_for_rect((1800, 0, 2400, 500), monitors), self.SECOND)  # mostly on the second
        self.assertIsNone(monitor_for_rect((-32000, -32000, -31800, -31900), monitors))


if __name__ == "__main__":
    unittest.main()
