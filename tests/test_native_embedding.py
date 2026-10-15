import os
import subprocess
import sys
import unittest

from cirana.native import win32_api as w
from cirana.native.window_discovery import WindowInfo, own_pid
from cirana.native.window_embedder import EmbedStatus, check_compatibility

CHARMAP = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32", "charmap.exe")


def info(**kw):
    base = dict(hwnd=0, pid=4, tid=1, title="", class_name="Whatever", exe="")
    base.update(kw)
    return WindowInfo(**base)


class CompatibilityTests(unittest.TestCase):
    def test_dead_handle_is_blocked(self):
        self.assertEqual(check_compatibility(info(hwnd=0)).status, EmbedStatus.BLOCKED)

    def test_own_process_is_blocked(self):
        hwnd = int(w.user32.GetDesktopWindow())
        result = check_compatibility(info(hwnd=hwnd, pid=own_pid()))
        self.assertEqual(result.status, EmbedStatus.BLOCKED)

    def test_blocked_classes_and_packaged_apps(self):
        desktop = int(w.user32.GetDesktopWindow())
        frame = check_compatibility(info(hwnd=desktop, class_name="ApplicationFrameWindow"))
        self.assertEqual(frame.status, EmbedStatus.BLOCKED)
        packaged = check_compatibility(
            info(hwnd=desktop, exe=r"c:\program files\windowsapps\microsoft.windowsnotepad_1\notepad.exe")
        )
        self.assertEqual(packaged.status, EmbedStatus.BLOCKED)
        self.assertIn("crash", packaged.reason)


@unittest.skipUnless(os.path.exists(CHARMAP), "charmap.exe not available")
class LiveEmbeddingTests(unittest.TestCase):
    """Real embedding of a classic Win32 application. Needs an interactive desktop session."""

    def run_selftest(self, target):
        # An inherited QT_QPA_PLATFORM=offscreen (set by the UI smoke tests) would give the host a fake HWND.
        env = {k: v for k, v in os.environ.items() if k != "QT_QPA_PLATFORM"}
        proc = subprocess.run(
            [sys.executable, "-m", "cirana.native.poc_embed", "--selftest", target],
            capture_output=True, text=True, timeout=90, encoding="utf-8", errors="replace", env=env,
        )
        return proc.returncode, proc.stdout

    def test_character_map_roundtrip(self):
        code, out = self.run_selftest("charmap")
        self.assertEqual(code, 0, out)
        self.assertIn("ALL PASSED", out)

    def test_packaged_notepad_is_refused_not_crashed(self):
        code, out = self.run_selftest("notepad")
        self.assertEqual(code, 0, out)
        self.assertIn("refused before any change", out)


if __name__ == "__main__":
    unittest.main()
