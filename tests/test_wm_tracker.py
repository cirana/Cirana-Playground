"""Phase 1: the window inventory, and the tracker's threads feeding it without blocking the GUI thread."""
import os
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from cirana.native import win32_api as w  # noqa: E402
from cirana.wm.identity import TrackedWindow  # noqa: E402
from cirana.wm.tracker import WindowInventory, WindowTracker  # noqa: E402
from tests.fake_desktop import FakeDesktop  # noqa: E402

app = QApplication.instance() or QApplication([])


def tw(hwnd, pid=10, created=1.0, title="A"):
    return TrackedWindow(hwnd, pid, created, "c:\\a.exe", "", title, "C")


class InventoryTests(unittest.TestCase):
    def test_added_changed_removed(self):
        inv = WindowInventory()
        self.assertEqual(inv.apply(1, tw(1)), [("added", tw(1))])
        self.assertEqual(inv.apply(1, tw(1)), [])  # nothing changed
        self.assertEqual(inv.apply(1, tw(1, title="B")), [("changed", tw(1, title="B"))])
        self.assertEqual(inv.apply(1, None), [("removed", tw(1, title="B"))])
        self.assertEqual(inv.apply(1, None), [])

    def test_recycled_handle_is_a_different_window(self):
        inv = WindowInventory()
        inv.apply(1, tw(1, pid=10))
        changes = inv.apply(1, tw(1, pid=99))
        self.assertEqual([kind for kind, _w in changes], ["removed", "added"])
        self.assertIsNone(inv.get(tw(1, pid=10).key))
        self.assertIsNotNone(inv.get(tw(1, pid=99).key))
        changes = inv.apply(1, tw(1, pid=99, created=2.0))  # same pid, but a new process
        self.assertEqual([kind for kind, _w in changes], ["removed", "added"])

    def test_reconcile_catches_missed_events_and_keeps_z_order(self):
        inv = WindowInventory()
        inv.apply(1, tw(1))
        inv.apply(2, tw(2))
        changes = inv.reconcile([3, 2, 4], {3: tw(3), 2: tw(2), 4: None})
        self.assertIn(("removed", tw(1)), changes)  # its destroy event was missed
        self.assertIn(("added", tw(3)), changes)
        self.assertEqual([x.hwnd for x in inv.windows()], [3, 2])

    def test_get_validates_the_whole_key(self):
        inv = WindowInventory()
        inv.apply(0x1A, tw(0x1A))
        self.assertIsNotNone(inv.get(tw(0x1A).key))
        self.assertIsNone(inv.get("1a:10:5"))
        self.assertIsNone(inv.get(""))


class TrackerThreadTests(unittest.TestCase):
    def wait_for(self, condition, timeout=3.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            app.processEvents()
            if condition():
                return True
            time.sleep(0.01)
        return False

    def test_probe_thread_builds_the_inventory_and_follows_events(self):
        desktop = FakeDesktop()
        first = desktop.add(title="One")
        tracker = WindowTracker(desktop, hooks=False, interval=60)
        added, removed, ready = [], [], []
        tracker.window_added.connect(added.append)
        tracker.window_removed.connect(removed.append)
        tracker.inventory_ready.connect(lambda: ready.append(True))
        tracker.start()
        self.addCleanup(tracker.stop)
        self.assertTrue(self.wait_for(lambda: ready))
        self.assertEqual([x.hwnd for x in added], [first.hwnd])

        second = desktop.add(title="Two")
        tracker._on_event(w.EVENT_OBJECT_SHOW, second.hwnd)  # what the hook thread does
        self.assertTrue(self.wait_for(lambda: len(added) == 2))
        desktop.close(first.hwnd)
        tracker._on_event(w.EVENT_OBJECT_DESTROY, first.hwnd)
        self.assertTrue(self.wait_for(lambda: removed))
        self.assertEqual(removed[0].hwnd, first.hwnd)
        self.assertEqual([x.hwnd for x in tracker.windows()], [second.hwnd])

    def test_foreground_events_name_the_window(self):
        desktop = FakeDesktop()
        one = desktop.add()
        tracker = WindowTracker(desktop, hooks=False, interval=60)
        keys = []
        tracker.foreground_changed.connect(keys.append)
        tracker.start()
        self.addCleanup(tracker.stop)
        self.assertTrue(self.wait_for(lambda: tracker.ready))
        tracker._on_event(w.EVENT_SYSTEM_FOREGROUND, one.hwnd)
        self.assertTrue(self.wait_for(lambda: keys))
        self.assertEqual(keys[0], desktop.tracked(one.hwnd).key)

    def test_missed_destroy_is_caught_by_the_reconcile_pass(self):
        desktop = FakeDesktop()
        one = desktop.add()
        tracker = WindowTracker(desktop, hooks=False, interval=0.05)
        removed = []
        tracker.window_removed.connect(removed.append)
        tracker.start()
        self.addCleanup(tracker.stop)
        self.assertTrue(self.wait_for(lambda: tracker.ready))
        desktop.close(one.hwnd)  # no event at all
        self.assertTrue(self.wait_for(lambda: removed))

    def test_a_slow_describe_never_blocks_the_gui_thread(self):
        desktop = FakeDesktop()
        desktop.add()
        original = desktop.describe

        def slow(hwnd):
            time.sleep(0.3)
            return original(hwnd)

        desktop.describe = slow
        tracker = WindowTracker(desktop, hooks=False, interval=60)
        started = time.monotonic()
        tracker.start()
        self.addCleanup(tracker.stop)
        self.assertLess(time.monotonic() - started, 0.1)
        self.assertTrue(self.wait_for(lambda: tracker.ready))

    def test_real_hooks_start_and_stop_cleanly(self):
        tracker = WindowTracker(hooks=True, interval=60)
        tracker.start()
        self.assertTrue(self.wait_for(lambda: tracker.ready, timeout=5))
        started = time.monotonic()
        tracker.stop()
        self.assertLess(time.monotonic() - started, 2.5)


if __name__ == "__main__":
    unittest.main()
