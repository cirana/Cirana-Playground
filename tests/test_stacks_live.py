"""Opt-in (set CIRANA_LIVE=1): stacks and monocle with REAL windows in other processes (tests/hang_window_helper.py).

Every other program is left floating, so none of the user's windows is touched. Checks that a stacked window is
minimized for real (ShowWindowAsync, never hidden: it keeps its taskbar button), that stopping CIRANA shows it again,
and that monocle fills the work area and gives the layout back exactly."""
import ctypes
import os
import subprocess
import sys
import tempfile
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

LIVE = os.environ.get("CIRANA_LIVE") == "1"


@unittest.skipUnless(LIVE, "live desktop test: set CIRANA_LIVE=1")
class LiveStackTests(unittest.TestCase):
    def wait(self, condition, timeout=8.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            QApplication.processEvents()
            if condition():
                return True
            time.sleep(0.02)
        return False

    def test_stack_minimizes_for_real_and_quitting_shows_it_again(self):
        from pathlib import Path

        from cirana.core.application_registry import ApplicationRegistry
        from cirana.core.dynamic_tiling import MINIMIZE, DynamicTiler
        from cirana.core.workspace_controller import WorkspaceController
        from cirana.core.workspace_launcher import WorkspaceLauncher
        from cirana.core.workspaces import WorkspaceManager
        from cirana.native import win32_api as w
        from cirana.storage.settings_manager import SettingsManager
        from cirana.wm.controller import WindowController
        from cirana.wm.layout import SIDE_BY_SIDE, LayoutSpec, tile
        from cirana.wm.tracker import WindowTracker

        # Like CIRANA itself (Qt makes it so): per-monitor DPI aware, so monitors and frames are both physical pixels
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        QApplication.instance() or QApplication([])
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        home = tempfile.mkdtemp(prefix="cirana-live-stack-")
        settings = SettingsManager(Path(home))
        registry = ApplicationRegistry(settings)
        tracker = WindowTracker(hooks=True, interval=60)
        tracker.start()
        self.addCleanup(tracker.stop)
        self.assertTrue(self.wait(lambda: tracker.ready))
        helper_name = os.path.basename(sys.executable).lower()
        others = {win.exe_name.lower() for win in tracker.windows()} - {helper_name, ""}
        settings.set("tiling_exclusions", sorted(others))  # never arrange the user's windows
        settings.set("stack_hide", MINIMIZE)
        helpers = []
        for _ in range(2):
            proc = subprocess.Popen([sys.executable, "-m", "tests.hang_window_helper", os.path.join(home, "x"), "1"],
                                    stdout=subprocess.PIPE, text=True, cwd=root)
            helpers.append((proc, int(proc.stdout.readline().split()[1])))
        for proc, hwnd in helpers:
            self.addCleanup(proc.stdout.close)
            self.addCleanup(proc.wait, 10)
            self.addCleanup(w.PostMessageW, hwnd, w.WM_CLOSE, 0, 0)  # politely, our own helpers only
        self.assertTrue(self.wait(lambda: all(tracker.inventory.by_hwnd(h) for _p, h in helpers)))
        if any(tracker.inventory.by_hwnd(h).exe_name.lower() != helper_name for _p, h in helpers):
            self.skipTest("the helper runs under another interpreter image than expected")
        unrelated = [win for win in tracker.windows() if win.exe_name.lower() != helper_name]
        if unrelated and any(not win.exe_name for win in unrelated):
            self.skipTest("a window whose program cannot be read could not be kept out of the test")
        manager = WorkspaceManager(settings, registry, monitors=tracker.backend.monitors)
        wm = WorkspaceController(manager, registry, tracker, WindowController(tracker.backend),
                                 WorkspaceLauncher(registry, tracker))
        tiler = DynamicTiler(wm, settings)
        stopped = []
        self.addCleanup(lambda: stopped or tiler.stop())
        tiler.set_global_layout(LayoutSpec(SIDE_BY_SIDE, gap=10))
        tiler.set_global(True)
        (_p1, first), (_p2, second) = helpers
        self.assertTrue(self.wait(lambda: all(tiler.target_of(tracker.inventory.by_hwnd(h).key) for _p, h in helpers)
                                  and tiler._job is None))
        if len({tiler._home.get(tracker.inventory.by_hwnd(h).key) for _p, h in helpers}) != 1:
            self.skipTest("the helpers opened on different monitors")

        # monocle: the work area, then exactly the layout again
        before = tracker.backend.frame_rect(first)
        monitor = next(m for m in tracker.backend.monitors() if m.device == tiler._home[
            tracker.inventory.by_hwnd(first).key])
        full = tile(LayoutSpec(SIDE_BY_SIDE, gap=10), monitor.work, 1, monitor.scale)[0]
        tiler.toggle_monocle(tracker.inventory.by_hwnd(first))
        self.assertTrue(self.wait(lambda: max(abs(a - b) for a, b in zip(tracker.backend.frame_rect(first), full))
                                  <= 2), "monocle did not fill the work area")
        tiler.toggle_monocle(tracker.inventory.by_hwnd(first))
        self.assertTrue(self.wait(lambda: max(abs(a - b) for a, b in zip(tracker.backend.frame_rect(first), before))
                                  <= 2), "the layout did not come back")

        # stack: the other window is minimized (not hidden) and shown again when CIRANA stops
        self.assertTrue(self.wait(lambda: tiler._job is None))
        self.assertTrue(tiler.stack_with(tracker.inventory.by_hwnd(second), tracker.inventory.by_hwnd(first)))
        self.assertTrue(self.wait(lambda: w.IsIconic(first)), "the stacked window was not minimized")
        self.assertTrue(w.IsWindowVisible(first))  # minimized, never hidden: its taskbar button stays
        self.assertFalse(w.IsIconic(second))
        tiler.stop()
        stopped.append(True)
        self.assertTrue(self.wait(lambda: not w.IsIconic(first)), "stopping did not show the window again")


if __name__ == "__main__":
    unittest.main()
