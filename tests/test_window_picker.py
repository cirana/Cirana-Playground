"""Running window picker: untitled and owned windows, background refresh."""
import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from cirana.native import window_discovery as discovery  # noqa: E402
from cirana.native import window_embedder as embedder  # noqa: E402
from cirana.native.window_discovery import WindowInfo  # noqa: E402
from cirana.ui import dialogs  # noqa: E402

w = discovery.w
CAPTION_STYLE = w.WS_CAPTION | w.WS_VISIBLE


class OwnedWindowFilterTests(unittest.TestCase):
    """Only owned windows a user really works with are offered; menus, tooltips and blocked windows are not."""

    def check(self, owner=0x50, style=CAPTION_STYLE, ex_style=0, visible=True, cloaked=False, rect=(0, 0, 400, 300)):
        longs = {w.GWL_STYLE: style, w.GWL_EXSTYLE: ex_style}
        with mock.patch.object(w, "IsWindow", return_value=True), \
                mock.patch.object(w, "GetWindow", side_effect=lambda h, cmd: owner if cmd == w.GW_OWNER else 0), \
                mock.patch.object(w, "IsWindowVisible", return_value=visible), \
                mock.patch.object(w, "is_cloaked", return_value=cloaked), \
                mock.patch.object(w, "GetWindowLongPtrW", side_effect=lambda h, index: longs[index]), \
                mock.patch.object(w, "window_rect", return_value=rect):
            return discovery.is_interactive_owned_window(0x77)

    def test_an_owned_window_with_a_title_bar_is_offered(self):
        self.assertTrue(self.check())  # e.g. a VCL main window owned by its hidden application window
        self.assertTrue(self.check(style=w.WS_POPUP | w.WS_VISIBLE, ex_style=w.WS_EX_APPWINDOW))  # own taskbar button

    def test_hidden_transient_and_blocked_windows_are_not_offered(self):
        self.assertFalse(self.check(owner=0))  # un-owned windows are judged by is_main_window_candidate
        self.assertFalse(self.check(visible=False))
        self.assertFalse(self.check(cloaked=True))
        self.assertFalse(self.check(style=CAPTION_STYLE | w.WS_DISABLED))  # blocked by a modal dialog
        self.assertFalse(self.check(style=CAPTION_STYLE | w.WS_CHILD))
        self.assertFalse(self.check(ex_style=w.WS_EX_NOACTIVATE))
        self.assertFalse(self.check(ex_style=w.WS_EX_TOOLWINDOW))  # tool palettes, floating toolbars
        self.assertFalse(self.check(style=w.WS_POPUP | w.WS_VISIBLE))  # menus, tooltips, drop-downs, splash screens
        self.assertFalse(self.check(rect=(0, 0, 60, 40)))

    def test_launch_detection_still_sees_main_windows_only(self):
        handles = [0x10, 0x20]
        owned = {0x20}

        def enum(callback, _lparam):
            for h in handles:
                callback(h, 0)
            return True

        with mock.patch.object(w, "EnumWindows", side_effect=enum), \
                mock.patch.object(discovery, "is_main_window_candidate", side_effect=lambda h: h not in owned), \
                mock.patch.object(discovery, "is_interactive_owned_window", side_effect=lambda h: h in owned), \
                mock.patch.object(discovery, "_describe", side_effect=lambda h: WindowInfo(h, 1, 1, "", "C", "")):
            self.assertEqual([i.hwnd for i in discovery.list_top_level_windows()], [0x10])
            self.assertEqual([i.hwnd for i in discovery.list_top_level_windows(include_owned=True)], [0x10, 0x20])

    def test_an_owned_window_is_docked_never_reparented(self):
        owned = WindowInfo(0x77, 1, 1, "Palette", "TForm1", r"c:\apps\tool.exe", owner=0x50)
        self.assertEqual(embedder.hosting_mode(owned), embedder.DOCKED)  # re-parenting would lose its owner
        unowned = WindowInfo(0x78, 1, 1, "Main", "TForm1", r"c:\apps\tool.exe")
        self.assertEqual(embedder.hosting_mode(unowned), embedder.CHILD)


class WindowPickerDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.windows = [
            WindowInfo(0x10, 100, 1, "Report.txt - Editor", "Edit", r"c:\apps\editor.exe"),
            WindowInfo(0x20, 200, 1, "   ", "Canvas", r"c:\apps\paint.exe"),  # a valid window without a title
            WindowInfo(0x30, 300, 1, "", "X", ""),  # untitled, and its process cannot be inspected
            WindowInfo(0x40, 400, 1, "Already hosted", "Edit", r"c:\apps\hosted.exe"),
            WindowInfo(0x50, os.getpid(), 1, "CIRANA", "Qt", r"c:\python.exe"),
        ]
        self.jobs = []
        ok = embedder.EmbedResult(embedder.EmbedStatus.OK)
        patches = [
            mock.patch.object(dialogs, "list_top_level_windows",
                              side_effect=lambda include_owned=False: list(self.windows)),
            mock.patch.object(dialogs, "own_pid", return_value=os.getpid()),
            mock.patch.object(dialogs, "check_compatibility", return_value=ok),
            mock.patch.object(dialogs, "dock_compatibility", return_value=ok),
            # scans are run by hand below, so the test controls when a "background" scan finishes
            mock.patch.object(dialogs, "run_async", side_effect=self.fake_run_async),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.dialog = dialogs.WindowPickerDialog({0x40})
        self.addCleanup(self.dialog.deleteLater)

    def fake_run_async(self, fn, on_done, on_error):
        job = mock.Mock()
        job.fn, job.on_done, job.on_error = fn, on_done, on_error
        self.jobs.append(job)
        return job

    def finish(self, job):
        job.on_done(job.fn(None))

    def texts(self):
        return [self.dialog.list.item(row).text() for row in range(self.dialog.list.count())]

    def test_untitled_windows_are_listed_by_program_name(self):
        self.finish(self.jobs[0])
        texts = self.texts()
        self.assertIn("paint.exe\nuntitled window", texts)
        self.assertIn("Window of process 300\nuntitled window", texts)
        self.assertIn("Report.txt - Editor\neditor.exe", texts)
        self.assertFalse(any("Already hosted" in t or "CIRANA" in t for t in texts))  # hosted / CIRANA's own

    def test_refresh_runs_in_the_background_one_scan_at_a_time(self):
        self.assertEqual(len(self.jobs), 1)  # the first scan starts with the dialog
        self.assertTrue(self.dialog.scanning)
        self.assertFalse(self.dialog.refresh_button.isEnabled())
        self.dialog.refresh_button.click()  # ignored while that scan is still running
        self.assertEqual(len(self.jobs), 1)
        self.finish(self.jobs[0])
        self.assertFalse(self.dialog.scanning)
        self.assertTrue(self.dialog.refresh_button.isEnabled())
        self.dialog.list.setCurrentRow(self.texts().index("paint.exe\nuntitled window"))
        self.windows.append(WindowInfo(0x60, 600, 1, "New window", "Edit", r"c:\apps\new.exe"))
        self.dialog.refresh_button.click()
        self.assertEqual(len(self.jobs), 2)
        self.assertEqual(self.dialog.list.count(), 3)  # the old list stays until the new one arrives
        self.finish(self.jobs[1])
        self.assertIn("New window\nnew.exe", self.texts())
        self.assertEqual(self.dialog.list.currentItem().text(), "paint.exe\nuntitled window")  # selection kept

    def test_a_failed_scan_reenables_refresh(self):
        self.jobs[0].on_error("access denied")
        self.assertIn("access denied", self.dialog.status.text())
        self.assertTrue(self.dialog.refresh_button.isEnabled())
        self.dialog.refresh()
        self.assertEqual(len(self.jobs), 2)

    def test_closing_the_dialog_cancels_a_running_scan(self):
        self.dialog.reject()
        self.jobs[0].cancel.assert_called_once()


if __name__ == "__main__":
    unittest.main()
