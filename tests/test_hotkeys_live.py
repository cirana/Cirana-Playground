"""Opt-in (set CIRANA_LIVE=1): global shortcuts on the real system. A shortcut nobody uses (Ctrl+Alt+Win+F24) is
registered by CIRANA's hotkey thread, is then really taken (a second registration fails with error 1409), a taken
shortcut is reported instead of breaking the others, and stopping releases everything."""
import os
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from cirana.core.hotkeys import DEFAULTS, MOD_ALT, MOD_CONTROL, MOD_WIN, HotkeyManager  # noqa: E402

LIVE = os.environ.get("CIRANA_LIVE") == "1"
F24 = 0x87


class Settings(dict):
    def get(self, key):
        return dict.get(self, key)

    def set(self, key, value):
        self[key] = value


@unittest.skipUnless(LIVE, "live desktop test: set CIRANA_LIVE=1")
class LiveHotkeyTests(unittest.TestCase):
    def test_register_report_and_release(self):
        from cirana.native import win32_api as w

        app = QApplication.instance() or QApplication([])
        bindings = {action: "" for action in DEFAULTS}
        bindings["focus_left"] = "Ctrl+Alt+Win+F24"
        bindings["focus_right"] = "Ctrl+Alt+Win+F23"
        mods = MOD_CONTROL | MOD_ALT | MOD_WIN
        self.assertTrue(w.RegisterHotKey(None, 77, mods, F24 - 1))  # "another program" holds Ctrl+Alt+Win+F23
        self.addCleanup(w.UnregisterHotKey, None, 77)
        manager = HotkeyManager(Settings(hotkeys=bindings))
        manager.start()
        end = time.monotonic() + 2
        while time.monotonic() < end and "focus_right" not in manager.problems:
            app.processEvents()
            time.sleep(0.01)
        self.assertIn("already uses it", manager.problems.get("focus_right", ""))
        self.assertNotIn("focus_left", manager.problems)
        self.assertFalse(w.RegisterHotKey(None, 78, mods, F24))  # CIRANA really holds it
        self.assertEqual(w.last_error(), 1409)
        manager.stop()
        self.assertTrue(w.RegisterHotKey(None, 79, mods, F24))  # released
        w.UnregisterHotKey(None, 79)


if __name__ == "__main__":
    unittest.main()
