"""Live checks against a REAL Win32 window in another process (tests/hang_window_helper.py).

Opt-in, because it opens windows on the desktop for a few seconds:  set CIRANA_LIVE=1. Proves on the real system that
the tracker sees a new window, that the controller moves and sizes it with its parent, owner and styles untouched,
and that a window that stops responding is reported without freezing CIRANA.
"""
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
class LiveWindowManagerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:  # physical pixels everywhere, like the Qt application (per-monitor aware v2)
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except (AttributeError, OSError):
            pass
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from cirana.wm.backend import Win32Backend

        self.trigger = os.path.join(tempfile.mkdtemp(prefix="cirana-live-"), "hang")
        self.proc = subprocess.Popen([sys.executable, "-m", "tests.hang_window_helper", self.trigger, "6"],
                                     stdout=subprocess.PIPE, text=True,
                                     cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        line = self.proc.stdout.readline().strip()
        self.assertTrue(line.startswith("READY"), line)
        self.hwnd = int(line.split()[1])
        self.backend = Win32Backend()
        self.addCleanup(self._close)

    def _close(self):
        from cirana.native import win32_api as w

        w.PostMessageW(self.hwnd, w.WM_CLOSE, 0, 0)
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:  # only our own helper, never anything else
            self.proc.kill()
        self.proc.stdout.close()

    def wait_for(self, condition, timeout=5.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.app.processEvents()
            if condition():
                return True
            time.sleep(0.02)
        return False

    def test_tracker_controller_and_hung_window(self):
        from cirana.native import win32_api as w
        from cirana.wm.controller import Placement, Status, WindowController
        from cirana.wm.tracker import WindowTracker

        tracker = WindowTracker(self.backend, hooks=True, interval=60)
        tracker.start()
        self.addCleanup(tracker.stop)
        self.assertTrue(self.wait_for(lambda: tracker.ready))
        window = next((x for x in tracker.windows() if x.hwnd == self.hwnd), None)
        self.assertIsNotNone(window, "the tracker did not list the helper window")
        import psutil  # the venv's python.exe is a launcher: the window belongs to it or to its child

        self.assertIn(window.pid, {self.proc.pid} | {c.pid for c in psutil.Process(self.proc.pid).children()})

        style = int(w.GetWindowLongPtrW(self.hwnd, w.GWL_STYLE))
        ex_style = int(w.GetWindowLongPtrW(self.hwnd, w.GWL_EXSTYLE))
        parent = int(w.GetAncestor(self.hwnd, w.GA_PARENT) or 0)
        work = self.backend.monitors()[0].work
        target = (work[0] + 40, work[1] + 40, work[0] + 640, work[1] + 440)
        controller = WindowController(self.backend)
        [result] = controller.place_all([Placement(window, target)])
        self.assertEqual(result.status, Status.PLACED, result.message)
        self.assertLessEqual(max(abs(a - b) for a, b in zip(self.backend.frame_rect(self.hwnd), target)), 2)
        self.assertEqual(int(w.GetWindowLongPtrW(self.hwnd, w.GWL_STYLE)), style)
        self.assertEqual(int(w.GetWindowLongPtrW(self.hwnd, w.GWL_EXSTYLE)), ex_style)
        self.assertEqual(int(w.GetAncestor(self.hwnd, w.GA_PARENT) or 0), parent)

        # The tracker followed the move (location events).
        self.assertTrue(self.wait_for(lambda: tracker.get(window.key) is not None
                                      and tracker.get(window.key).rect == self.backend.frame_rect(self.hwnd)))

        # Hang the helper: Windows declares it not responding after ~5 s. Placing it must neither block nor lie.
        open(self.trigger, "w").close()
        self.assertTrue(self.wait_for(lambda: self.backend.is_hung(self.hwnd), timeout=8))
        started = time.monotonic()
        [result] = controller.place_all([Placement(tracker.get(window.key), target)])
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(result.status, Status.HUNG)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(LIVE, "live desktop test: set CIRANA_LIVE=1")
class LiveWorkspaceTests(unittest.TestCase):
    """End to end on the real desktop: a workspace starts a real program, finds its window through the process tree
    (the venv's pythonw.exe is a launcher that starts the real interpreter as a child), arranges and raises it."""

    @classmethod
    def setUpClass(cls):
        LiveWindowManagerTests.setUpClass()
        cls.app = QApplication.instance()

    def test_activation_launches_finds_and_arranges_a_real_window(self):
        self.run_workspace("executable")

    def test_a_start_menu_style_shortcut_is_followed_to_its_window(self):
        # Shortcuts start through ShellExecuteEx on a worker thread; the shortcut's process (a launcher here) is
        # followed to the child that owns the window, like Discord's Update.exe -> Discord.exe.
        self.run_workspace("shortcut")

    def run_workspace(self, launch_type):
        from cirana.core.application_registry import ApplicationRegistry
        from cirana.core.models import AppEntry
        from cirana.core.workspace_controller import EntryState, WorkspaceController
        from cirana.core.workspace_launcher import WorkspaceLauncher
        from cirana.core.workspaces import WorkspaceManager
        from cirana.native import win32_api as w
        from cirana.storage.settings_manager import SettingsManager
        from cirana.wm.controller import WindowController
        from cirana.wm.layout import LayoutSpec, tile
        from cirana.wm.tracker import WindowTracker

        home = tempfile.mkdtemp(prefix="cirana-live-home-")
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        trigger = os.path.join(home, "never")
        settings = SettingsManager(__import__("pathlib").Path(home))
        registry = ApplicationRegistry(settings)
        arguments = f'-m tests.hang_window_helper "{trigger}" 1'
        target = pythonw
        if launch_type == "shortcut":
            import win32com.client

            target = os.path.join(home, "Live helper.lnk")
            link = win32com.client.Dispatch("WScript.Shell").CreateShortcut(target)
            link.TargetPath, link.Arguments, link.WorkingDirectory = pythonw, arguments, root
            link.Save()
        helper = AppEntry(id="app-live-helper", name="Live helper", launch_type=launch_type, target=target,
                          source="user", arguments=arguments if launch_type == "executable" else "",
                          working_dir=root, exe_path=pythonw, custom=True)
        registry.apply_scan([helper])
        tracker = WindowTracker(hooks=True, interval=1.0)
        tracker.start()
        self.addCleanup(tracker.stop)
        manager = WorkspaceManager(settings, registry, monitors=tracker.backend.monitors)
        wm = WorkspaceController(manager, registry, tracker, WindowController(tracker.backend),
                                 WorkspaceLauncher(registry, tracker))
        workspace = manager.create("Live", [helper.id])
        manager.set_layout(workspace.id, LayoutSpec(gap=16))
        end = time.monotonic() + 5
        while not tracker.ready and time.monotonic() < end:
            self.app.processEvents()
            time.sleep(0.02)
        self.assertEqual([s.state for s in wm.statuses(workspace.id)], [EntryState.MISSING])

        wm.activate(workspace.id)
        end = time.monotonic() + 30
        status = None
        while time.monotonic() < end:
            self.app.processEvents()
            status = wm.statuses(workspace.id)[0]
            if status.state is EntryState.RUNNING and not wm.busy(workspace.id):
                break
            time.sleep(0.05)
        self.assertIs(status.state, EntryState.RUNNING, status.message)
        hwnd = status.window.hwnd
        self.addCleanup(w.PostMessageW, hwnd, w.WM_CLOSE, 0, 0)  # politely, our own helper only
        monitor = tracker.backend.monitors()[0]
        target = tile(LayoutSpec(gap=16), monitor.work, 1, monitor.scale)[0]
        end = time.monotonic() + 5
        while time.monotonic() < end and max(abs(a - b) for a, b in zip(tracker.backend.frame_rect(hwnd), target)) > 2:
            self.app.processEvents()
            time.sleep(0.05)
        self.assertLessEqual(max(abs(a - b) for a, b in zip(tracker.backend.frame_rect(hwnd), target)), 2)


@unittest.skipUnless(LIVE, "live desktop test: set CIRANA_LIVE=1")
class LiveDynamicTilingTests(unittest.TestCase):
    """Real windows, real WinEvent hooks: closing one tiled window makes the other fill the screen by itself."""

    @classmethod
    def setUpClass(cls):
        LiveWindowManagerTests.setUpClass()
        cls.app = QApplication.instance()

    def wait(self, condition, timeout=8.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.app.processEvents()
            if condition():
                return True
            time.sleep(0.02)
        return False

    def test_windows_closing_retile_by_themselves(self):
        from pathlib import Path

        from cirana.core.application_registry import ApplicationRegistry
        from cirana.core.dynamic_tiling import DynamicTiler
        from cirana.core.workspace_controller import WorkspaceController
        from cirana.core.workspace_launcher import WorkspaceLauncher
        from cirana.core.workspaces import WorkspaceManager
        from cirana.native import win32_api as w
        from cirana.storage.settings_manager import SettingsManager
        from cirana.wm.controller import WindowController
        from cirana.wm.layout import SIDE_BY_SIDE, LayoutSpec, tile
        from cirana.wm.tracker import WindowTracker

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        home = tempfile.mkdtemp(prefix="cirana-live-tiling-")
        helpers = []
        for _ in range(2):
            proc = subprocess.Popen([sys.executable, "-m", "tests.hang_window_helper", os.path.join(home, "x"), "1"],
                                    stdout=subprocess.PIPE, text=True, cwd=root)
            helpers.append((proc, int(proc.stdout.readline().split()[1])))
        for proc, hwnd in helpers:
            self.addCleanup(proc.stdout.close)
            self.addCleanup(proc.wait, 10)
            self.addCleanup(w.PostMessageW, hwnd, w.WM_CLOSE, 0, 0)  # politely, our own helpers only

        settings = SettingsManager(Path(home))
        registry = ApplicationRegistry(settings)
        tracker = WindowTracker(hooks=True, interval=60)  # a long reconcile: the close must come from the hook
        tracker.start()
        self.addCleanup(tracker.stop)
        self.assertTrue(self.wait(lambda: tracker.ready and all(tracker.inventory.by_hwnd(h) for _p, h in helpers)))
        manager = WorkspaceManager(settings, registry, monitors=tracker.backend.monitors)
        wm = WorkspaceController(manager, registry, tracker, WindowController(tracker.backend),
                                 WorkspaceLauncher(registry, tracker))
        tiler = DynamicTiler(wm)
        self.addCleanup(tiler.stop)
        workspace = manager.create("Live tiling")
        for _proc, hwnd in helpers:
            window = tracker.inventory.by_hwnd(hwnd)
            entry = manager.add_window(workspace.id, window)
            wm.choose(workspace.id, entry.id, window.key, place=False)
        manager.set_layout(workspace.id, LayoutSpec(SIDE_BY_SIDE, gap=10, dynamic=True))
        manager.set_active(workspace.id)

        monitor = tracker.backend.monitors()[0]
        two = tile(LayoutSpec(SIDE_BY_SIDE, gap=10), monitor.work, 2, monitor.scale)
        one = tile(LayoutSpec(SIDE_BY_SIDE, gap=10), monitor.work, 1, monitor.scale)

        def at(hwnd, rect):
            return max(abs(a - b) for a, b in zip(tracker.backend.frame_rect(hwnd), rect)) <= 2

        self.assertTrue(self.wait(lambda: at(helpers[0][1], two[0]) and at(helpers[1][1], two[1])))
        w.PostMessageW(helpers[0][1], w.WM_CLOSE, 0, 0)  # the first window closes
        self.assertTrue(self.wait(lambda: at(helpers[1][1], one[0])), "the other window did not fill the gap")
        retiles = tiler.count
        time.sleep(0.5)
        self.wait(lambda: False, timeout=1.0)
        self.assertEqual(tiler.count, retiles)  # settled: its own moves did not start another round


@unittest.skipUnless(LIVE, "live desktop test: set CIRANA_LIVE=1")
class LiveGlobalTilingTests(unittest.TestCase):
    """All-window tiling on the real desktop, without any workspace. To keep the user's own windows untouched, every
    program except the test helper is marked floating for the duration of the test (in a temporary data folder)."""

    @classmethod
    def setUpClass(cls):
        LiveWindowManagerTests.setUpClass()
        cls.app = QApplication.instance()

    def wait(self, condition, timeout=8.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.app.processEvents()
            if condition():
                return True
            time.sleep(0.02)
        return False

    def test_all_windows_tile_and_refill_without_a_workspace(self):
        from pathlib import Path

        from cirana.core.application_registry import ApplicationRegistry
        from cirana.core.dynamic_tiling import DynamicTiler
        from cirana.core.workspace_controller import WorkspaceController
        from cirana.core.workspace_launcher import WorkspaceLauncher
        from cirana.core.workspaces import WorkspaceManager
        from cirana.native import win32_api as w
        from cirana.storage.settings_manager import SettingsManager
        from cirana.wm.controller import WindowController
        from cirana.wm.layout import SIDE_BY_SIDE, LayoutSpec, tile
        from cirana.wm.tracker import WindowTracker

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        home = tempfile.mkdtemp(prefix="cirana-live-global-")
        settings = SettingsManager(Path(home))
        registry = ApplicationRegistry(settings)
        tracker = WindowTracker(hooks=True, interval=60)
        tracker.start()
        self.addCleanup(tracker.stop)
        self.assertTrue(self.wait(lambda: tracker.ready))
        helper_name = os.path.basename(sys.executable).lower()
        # never arrange the user's windows during a test: float everything that is not the helper's program
        others = {win.exe_name.lower() for win in tracker.windows()} - {helper_name, ""}
        settings.set("tiling_exclusions", sorted(others))

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
        before = {win.hwnd: tracker.backend.frame_rect(win.hwnd) for win in unrelated}

        manager = WorkspaceManager(settings, registry, monitors=tracker.backend.monitors)
        wm = WorkspaceController(manager, registry, tracker, WindowController(tracker.backend),
                                 WorkspaceLauncher(registry, tracker))
        tiler = DynamicTiler(wm, settings)
        self.addCleanup(tiler.stop)
        tiler.set_global_layout(LayoutSpec(SIDE_BY_SIDE, gap=10))
        tiler.set_global(True)

        monitors = tracker.backend.monitors()
        home_of = {h: next(m for m in monitors if m.device == tiler._home.get(tracker.inventory.by_hwnd(h).key,
                                                                               monitors[0].device))
                   for _p, h in helpers}

        def at(hwnd, rect):
            return max(abs(a - b) for a, b in zip(tracker.backend.frame_rect(hwnd), rect)) <= 2

        def tiled_alone_or_together():
            if not all(tiler._home.get(tracker.inventory.by_hwnd(h).key) for _p, h in helpers):
                return False
            same = len({home_of[h].device for _p, h in helpers}) == 1
            if same:
                rects = tile(LayoutSpec(SIDE_BY_SIDE, gap=10), home_of[helpers[0][1]].work, 2,
                             home_of[helpers[0][1]].scale)
                return {tuple(tracker.backend.frame_rect(h)) for _p, h in helpers} == set(rects) or all(
                    any(at(h, r) for r in rects) for _p, h in helpers)
            return all(at(h, tile(LayoutSpec(gap=10), home_of[h].work, 1, home_of[h].scale)[0]) for _p, h in helpers)

        self.assertTrue(self.wait(tiled_alone_or_together), "the two windows were not tiled")
        survivor = helpers[1][1]
        w.PostMessageW(helpers[0][1], w.WM_CLOSE, 0, 0)
        alone = tile(LayoutSpec(gap=10), home_of[survivor].work, 1, home_of[survivor].scale)[0]
        self.assertTrue(self.wait(lambda: at(survivor, alone)), "the remaining window did not fill the screen")
        self.assertEqual({hwnd: tracker.backend.frame_rect(hwnd) for hwnd in before if w.IsWindow(hwnd)},
                         {hwnd: rect for hwnd, rect in before.items() if w.IsWindow(hwnd)})  # nothing else moved


@unittest.skipUnless(LIVE, "live desktop test: set CIRANA_LIVE=1")
class LiveOverlayTests(unittest.TestCase):
    """Borders and drop preview on the real Windows Qt platform (a separate process: the suite runs offscreen)."""

    def test_borders_and_preview_on_the_real_desktop(self):
        import json

        from cirana.native import win32_api as w

        home = tempfile.mkdtemp(prefix="cirana-live-overlay-")
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = {k: v for k, v in os.environ.items() if k != "QT_QPA_PLATFORM"}
        done = subprocess.run([sys.executable, "-m", "tests.live_overlay_script", home], cwd=root, env=env,
                              capture_output=True, text=True, timeout=90)
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        out = json.loads(done.stdout.strip().splitlines()[-1])
        wanted_ex = w.WS_EX_TOOLWINDOW | w.WS_EX_NOACTIVATE | 0x20 | 0x80000  # tool, no-activate, transparent, layered
        for helper in out["helpers"]:
            t = helper["thickness"]
            frame = helper["frame"]
            expected = [frame[0] - t, frame[1] - t, frame[2] + t, frame[3] + t]
            self.assertLessEqual(max(abs(x - y) for x, y in zip(helper["border"], expected)), 1, helper)
            self.assertEqual(helper["ex_style"] & wanted_ex, wanted_ex)
            self.assertFalse(helper["ex_style"] & 0x8)  # WS_EX_TOPMOST: borders are not stuck on top of everything
            self.assertFalse(helper["ex_style"] & w.WS_EX_APPWINDOW)  # no taskbar button / Alt+Tab entry
            self.assertTrue(helper["above"], "the border is not directly above its window")
            self.assertFalse(helper["in_inventory"])
        self.assertEqual(out["focus"], [False, True])
        self.assertTrue(out["preview"]["visible"])
        # Qt may round an odd physical size by a pixel at fractional scaling (e.g. 125 %)
        self.assertLessEqual(max(abs(x - y) for x, y in zip(out["preview"]["rect"], out["preview"]["target"])), 1)
        self.assertEqual(out["preview"]["ex_style"] & wanted_ex, wanted_ex)
        self.assertTrue(out["preview_hidden"])
        self.assertFalse(out["foreground_is_overlay"])
        self.assertTrue(out["cleared"])


@unittest.skipUnless(LIVE, "live desktop test: set CIRANA_LIVE=1")
class LiveMinimumSizeTests(unittest.TestCase):
    """Real windows with a minimum size (WM_GETMINMAXINFO of CIRANA's own helper): room is made for them, nothing
    sticks out under a neighbour or off the screen, and a window larger than the monitor floats with its top-left
    corner on the screen."""

    def test_minimum_sizes_on_the_real_desktop(self):
        import json

        home = tempfile.mkdtemp(prefix="cirana-live-minimum-")
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = {k: v for k, v in os.environ.items() if k != "QT_QPA_PLATFORM"}
        done = subprocess.run([sys.executable, "-m", "tests.live_minimum_script", home], cwd=root, env=env,
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        out = json.loads(done.stdout.strip().splitlines()[-1])
        work = out["work"]
        for rows in (out["tiled"], out["with_huge"][:3]):
            frames = [row["frame"] for row in rows]
            for i, (row, frame) in enumerate(zip(rows, frames)):
                self.assertEqual(row["reason"], "")
                self.assertLessEqual(max(abs(x - y) for x, y in zip(frame, row["target"])), 2, row)  # placed
                self.assertTrue(work[0] <= frame[0] and frame[2] <= work[2] and frame[3] <= work[3], row)
                # GetWindowRect, DWM frame bounds and the client area agree (same physical pixel space)
                self.assertTrue(0 <= (frame[2] - frame[0]) - row["client"][0] <= 4, row)
                for other in frames[i + 1:]:
                    self.assertTrue(frame[2] <= other[0] or other[2] <= frame[0], (frame, other))  # no overlap
        self.assertGreater(out["tiled"][0]["frame"][2] - out["tiled"][0]["frame"][0], 1000)  # its 900 logical px
        self.assertLessEqual(out["count"], 3)  # one round, one with room for the minimum: no loop
        huge = out["with_huge"][3]
        self.assertIn("cannot be shown completely", huge["reason"])
        self.assertIsNone(huge["target"])
        self.assertEqual(huge["frame"][0], work[0])  # its left edge (and title bar) on the screen
        self.assertTrue(work[1] <= huge["frame"][1] and huge["frame"][3] <= work[3])
        self.assertEqual(len(out["messages"]), 1)


@unittest.skipUnless(LIVE, "live desktop test: set CIRANA_LIVE=1")
class LiveThirdWindowAndResizeTests(unittest.TestCase):
    """On the real desktop with real windows of CIRANA's own helper (it answers WM_GETMINMAXINFO): three windows that
    each need 40 % of the width all get a tile, and a window resized "by the mouse" (the move / size events are fed to
    the tracker and the window is resized with SetWindowPos from the test, since synthetic input is refused) keeps
    its size, the neighbour follows, nothing overlaps. Every program except the helper is set to float first."""

    @classmethod
    def setUpClass(cls):
        LiveWindowManagerTests.setUpClass()
        cls.app = QApplication.instance()

    def wait(self, condition, timeout=10.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.app.processEvents()
            if condition():
                return True
            time.sleep(0.02)
        return False

    def start(self, count, min_size=None):
        from pathlib import Path

        from cirana.core.application_registry import ApplicationRegistry
        from cirana.core.dynamic_tiling import DynamicTiler
        from cirana.core.workspace_controller import WorkspaceController
        from cirana.core.workspace_launcher import WorkspaceLauncher
        from cirana.core.workspaces import WorkspaceManager
        from cirana.native import win32_api as w
        from cirana.storage.settings_manager import SettingsManager
        from cirana.wm.controller import WindowController
        from cirana.wm.tracker import WindowTracker

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        home = tempfile.mkdtemp(prefix="cirana-live-resize-")
        settings = SettingsManager(Path(home))
        registry = ApplicationRegistry(settings)
        tracker = WindowTracker(hooks=True, interval=60)
        tracker.start()
        self.addCleanup(tracker.stop)
        self.assertTrue(self.wait(lambda: tracker.ready))
        helper_name = os.path.basename(sys.executable).lower()
        others = {win.exe_name.lower() for win in tracker.windows()} - {helper_name, ""}
        settings.set("tiling_exclusions", sorted(others))  # never arrange the user's own windows
        monitor = tracker.backend.monitors()[0]
        command = [sys.executable, "-m", "tests.hang_window_helper", os.path.join(home, "x"), "1"]
        if min_size:
            command.append("{}x{}".format(int(min_size[0] / monitor.scale), int(min_size[1] / monitor.scale)))
        helpers = []
        for _ in range(count):
            proc = subprocess.Popen(command, stdout=subprocess.PIPE, text=True, cwd=root)
            helpers.append((proc, int(proc.stdout.readline().split()[1])))
        for proc, hwnd in helpers:
            self.addCleanup(proc.stdout.close)
            self.addCleanup(proc.wait, 10)
            self.addCleanup(w.PostMessageW, hwnd, w.WM_CLOSE, 0, 0)  # politely, our own helpers only
        self.assertTrue(self.wait(lambda: all(tracker.inventory.by_hwnd(h) for _p, h in helpers)))
        if any(tracker.inventory.by_hwnd(h).exe_name.lower() != helper_name for _p, h in helpers):
            self.skipTest("the helper runs under another interpreter image than expected")
        manager = WorkspaceManager(settings, registry, monitors=tracker.backend.monitors)
        wm = WorkspaceController(manager, registry, tracker, WindowController(tracker.backend),
                                 WorkspaceLauncher(registry, tracker))
        tiler = DynamicTiler(wm, settings)
        self.addCleanup(tiler.stop)
        return tracker, tiler, [h for _p, h in helpers], monitor

    @staticmethod
    def apart(frames):
        return all(a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1]
                   for i, a in enumerate(frames) for b in frames[i + 1:])

    def test_three_windows_with_minimum_sizes_get_three_tiles(self):
        from cirana.wm.layout import SIDE_BY_SIDE, LayoutSpec

        monitor = None
        probe = __import__("cirana.wm.backend", fromlist=["Win32Backend"]).Win32Backend().monitors()[0]
        wide = int((probe.work[2] - probe.work[0]) * 0.4)
        tracker, tiler, hwnds, monitor = self.start(3, (wide, 200))
        tiler.set_global_layout(LayoutSpec(SIDE_BY_SIDE, gap=10))
        tiler.set_global(True)
        work = monitor.work

        def tiled():
            frames = [tracker.backend.frame_rect(h) for h in hwnds]
            return (all(work[0] <= f[0] and f[2] <= work[2] and work[1] <= f[1] and f[3] <= work[3] for f in frames)
                    and self.apart(frames))

        self.assertTrue(self.wait(tiled, 15.0), [tracker.backend.frame_rect(h) for h in hwnds])
        self.wait(lambda: False, 1.5)
        frames = [tracker.backend.frame_rect(h) for h in hwnds]
        self.assertTrue(self.apart(frames), frames)
        for hwnd in hwnds:
            state, why = tiler.status(tracker.inventory.by_hwnd(hwnd))
            self.assertEqual(state, "tiled", why)
            frame = tracker.backend.frame_rect(hwnd)
            self.assertGreaterEqual(frame[2] - frame[0], wide - 24)  # (its minimum counts the invisible borders)
        self.assertEqual(tiler.stack_count(), 0)
        self.assertTrue(tiler.shown_groups()[0].fallback, "side by side cannot hold them: another layout was used")

    def test_a_mouse_resize_is_kept(self):
        from cirana.native import win32_api as w
        from cirana.wm.layout import SIDE_BY_SIDE, LayoutSpec

        tracker, tiler, hwnds, monitor = self.start(2)
        tiler.set_global_layout(LayoutSpec(SIDE_BY_SIDE, gap=10))
        tiler.set_global(True)
        first, second = hwnds

        def frame(h):
            return tracker.backend.frame_rect(h)

        def halves():
            a, b = frame(first), frame(second)
            return abs((a[2] - a[0]) - (b[2] - b[0])) <= 2 and self.apart([a, b]) and a[2] - a[0] > 100

        self.assertTrue(self.wait(halves, 15.0))
        self.wait(lambda: False, 1.0)
        first, second = sorted(hwnds, key=lambda h: frame(h)[0])  # the left tile: its right edge is a divider
        before = frame(first)
        wanted_right = before[2] - 150
        tracker._on_event(w.EVENT_SYSTEM_MOVESIZESTART, first)  # the user grabs the right edge
        self.wait(lambda: tiler.paused())
        outer, visible = tracker.backend.window_rect(first), frame(first)
        w.SetWindowPos(first, None, outer[0], outer[1], outer[2] - outer[0] - 150, outer[3] - outer[1],
                       w.SWP_NOZORDER | w.SWP_NOACTIVATE)
        self.wait(lambda: abs(frame(first)[2] - wanted_right) <= 2)
        self.assertEqual(frame(first)[0], visible[0])
        tracker._on_event(w.EVENT_SYSTEM_MOVESIZEEND, first)  # released
        self.wait(lambda: False, 2.5)  # long enough for the re-tile and its verification
        a, b = frame(first), frame(second)
        self.assertLessEqual(abs(a[2] - wanted_right), 2, (a, wanted_right))  # it did not go back
        self.assertLessEqual(abs(b[0] - (a[2] + round(10 * monitor.scale))), 2, (a, b))  # the neighbour followed
        self.assertTrue(self.apart([a, b]))
        self.assertGreater(b[2] - b[0], a[2] - a[0])
        count = tiler.count
        self.wait(lambda: False, 1.0)
        self.assertEqual(tiler.count, count)  # no loop afterwards
