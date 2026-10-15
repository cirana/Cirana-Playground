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
