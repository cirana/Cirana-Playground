"""Phase 2: moving real windows safely (against the simulated desktop): verification, refusals, multi-monitor DPI."""
import threading
import unittest

from cirana.native import win32_api as w
from cirana.wm.controller import Placement, Status, WindowController
from cirana.wm.monitors import Monitor
from tests.fake_desktop import FakeDesktop

LEFT = Monitor("\\\\.\\DISPLAY1", (0, 0, 1920, 1080), (0, 0, 1920, 1040), True, 96)
RIGHT = Monitor("\\\\.\\DISPLAY2", (1920, 0, 4480, 1440), (1920, 0, 4480, 1400), False, 144)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.desktop = FakeDesktop(monitors_=[LEFT, RIGHT])
        self.controller = WindowController(self.desktop, settle=0.0)

    def place(self, *pairs, maximized=False):
        placements = [Placement(self.desktop.tracked(win.hwnd), rect, maximized) for win, rect in pairs]
        return self.controller.place_all(placements)

    def test_visible_frame_lands_exactly_where_asked(self):
        win = self.desktop.add()
        [result] = self.place((win, (10, 10, 910, 510)))
        self.assertEqual(result.status, Status.PLACED)
        self.assertEqual(win.rect, (10, 10, 910, 510))
        # the request itself included the invisible borders
        self.assertIn(("pos", win.hwnd, (3, 10, 917, 517)), self.desktop.calls)

    def test_styles_parent_and_process_are_never_touched(self):
        win = self.desktop.add()
        self.place((win, (0, 0, 500, 500)))
        self.assertEqual({call[0] for call in self.desktop.calls}, {"pos"})

    def test_minimum_size_is_reported_not_fought(self):
        win = self.desktop.add(min_size=(800, 600))
        [result] = self.place((win, (0, 0, 400, 300)))
        self.assertEqual(result.status, Status.CONSTRAINED)
        self.assertTrue(result.ok)
        self.assertIn("minimum size of 800×600", result.message)

    def test_fixed_size_window_is_moved_only(self):
        win = self.desktop.add(fixed_size=True, rect=(0, 0, 300, 200))
        [result] = self.place((win, (100, 100, 900, 900)))
        self.assertEqual(result.status, Status.CONSTRAINED)
        self.assertEqual(win.rect, (100, 100, 400, 300))

    def test_denied_window_is_reported_and_the_others_still_move(self):
        admin = self.desktop.add(title="Armoury Crate", elevated=True)
        normal = self.desktop.add(title="Brave")
        results = self.place((admin, (0, 0, 500, 500)), (normal, (500, 0, 1000, 500)))
        self.assertEqual(results[0].status, Status.DENIED)
        self.assertIn("higher rights", results[0].message)
        self.assertEqual(results[1].status, Status.PLACED)
        self.assertEqual(normal.rect, (500, 0, 1000, 500))
        self.assertNotIn(("pos", admin.hwnd), [c[:2] for c in self.desktop.calls])  # not even attempted

    def test_access_denied_from_windows_without_known_rights(self):
        win = self.desktop.add()
        self.desktop.higher_rights = lambda pid: None  # its token cannot be read
        original = self.desktop.set_pos
        self.desktop.set_pos = lambda hwnd, rect: (False, w.ERROR_ACCESS_DENIED)
        [result] = self.place((win, (0, 0, 500, 500)))
        self.desktop.set_pos = original
        self.assertEqual(result.status, Status.DENIED)
        self.assertIn("Win32 error 5", result.message)

    def test_hung_window_is_skipped(self):
        win = self.desktop.add(hung=True)
        [result] = self.place((win, (0, 0, 500, 500)))
        self.assertEqual(result.status, Status.HUNG)
        self.assertEqual(self.desktop.calls, [])

    def test_closed_or_recycled_handle_is_never_moved(self):
        win = self.desktop.add()
        snapshot = self.desktop.tracked(win.hwnd)
        self.desktop.close(win.hwnd)
        other = self.desktop.add(hwnd=win.hwnd, pid=999, title="Someone else")  # same handle, another process
        [result] = self.controller.place_all([Placement(snapshot, (0, 0, 500, 500))])
        self.assertEqual(result.status, Status.GONE)
        self.assertEqual(other.rect, (100, 100, 900, 700))

    def test_minimized_and_maximized_windows_are_restored_first(self):
        mini = self.desktop.add(minimized=True)
        maxi = self.desktop.add(maximized=True, rect=LEFT.work)
        was_max_mini = self.desktop.add(minimized=True, was_maximized=True, maximized=True)
        results = self.place((mini, (0, 0, 600, 500)), (maxi, (600, 0, 1200, 500)), (was_max_mini, (1200, 0, 1900, 500)))
        self.assertEqual([r.status for r in results], [Status.PLACED] * 3)
        for win in (mini, maxi, was_max_mini):
            self.assertFalse(win.minimized or win.maximized)
        self.assertEqual(was_max_mini.rect, (1200, 0, 1900, 500))

    def test_maximize_on_the_target_monitor(self):
        win = self.desktop.add()
        [result] = self.place((win, (2000, 100, 2800, 900)), maximized=True)
        self.assertEqual(result.status, Status.PLACED)
        self.assertTrue(win.maximized)
        self.assertEqual(win.rect, RIGHT.work)

    def test_moving_to_a_monitor_with_another_scale_survives_the_dpi_resize(self):
        win = self.desktop.add(rect=(100, 100, 700, 500))
        target = (2000, 100, 3000, 900)
        [result] = self.place((win, target))
        self.assertEqual(result.status, Status.PLACED)
        self.assertEqual(win.rect, target)

    def test_cancel_stops_waiting(self):
        controller = WindowController(self.desktop, settle=5.0)
        win = self.desktop.add()
        cancel = threading.Event()
        cancel.set()
        [result] = controller.place_all([Placement(self.desktop.tracked(win.hwnd), (0, 0, 500, 500))], cancel)
        self.assertEqual(result.status, Status.FAILED)

    def test_bring_to_front_restores_and_raises_in_order(self):
        a = self.desktop.add(minimized=True)
        b = self.desktop.add()
        hung = self.desktop.add(hung=True)
        results = self.controller.bring_to_front([self.desktop.tracked(x.hwnd) for x in (a, b, hung)])
        self.assertFalse(a.minimized)
        raises = [c[1] for c in self.desktop.calls if c[0] == "raise"]
        self.assertEqual(raises, [b.hwnd, a.hwnd])  # the first window ends on top
        self.assertEqual(self.desktop.foreground, a.hwnd)
        self.assertIn(Status.HUNG, [r.status for r in results])

    def test_minimize_maximize_restore(self):
        win = self.desktop.add()
        tracked = self.desktop.tracked(win.hwnd)
        self.assertTrue(self.controller.minimize(tracked).ok)
        self.assertTrue(win.minimized)
        self.assertTrue(self.controller.restore(tracked).ok)
        self.assertFalse(win.minimized)
        self.assertTrue(self.controller.maximize(tracked).ok)
        self.assertTrue(win.maximized)
        win.elevated = True
        self.assertEqual(self.controller.restore(tracked).status, Status.DENIED)


if __name__ == "__main__":
    unittest.main()
