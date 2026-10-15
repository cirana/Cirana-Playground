"""Hosted applications do not show a taskbar button of their own; detaching (or the next start after a crash) gives
it back. Every Win32 / shell call is faked: no real window or taskbar button is touched."""
import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import psutil  # noqa: E402

from cirana.core import orphans  # noqa: E402
from cirana.native import taskbar  # noqa: E402
from cirana.native import window_embedder as embedder  # noqa: E402
from cirana.native.window_discovery import WindowInfo  # noqa: E402
from cirana.native.window_restorer import WindowState  # noqa: E402

w = embedder.w
BROWSER = WindowInfo(0x9300, 4242, 1, "New Tab - Google Chrome", "Chrome_WidgetWin_1",
                     r"c:\program files\google\chrome\application\chrome.exe")


class ChildModeTests(unittest.TestCase):
    """A re-parented window gives up WS_EX_APPWINDOW while hosted; detaching restores its captured ex-style."""

    def embed(self, ex_style):
        state = WindowState(0x77, 0, 0x16CF0000, ex_style, (0, 0, 640, 480), False, False)
        writes = []
        info = WindowInfo(0x77, 4242, 1, "Tool", "ToolClass", r"c:\tools\tool.exe")
        with mock.patch.object(embedder, "check_compatibility", return_value=embedder.EmbedResult(embedder.EmbedStatus.OK)), \
                mock.patch.object(embedder, "capture_state", return_value=state), \
                mock.patch.object(w, "GetWindowLongPtrW", return_value=0x16CF0000), \
                mock.patch.object(w, "SetWindowLongPtrW", side_effect=lambda h, i, v: writes.append((i, v))), \
                mock.patch.object(w, "SetParent"), mock.patch.object(w, "GetParent", return_value=0x55), \
                mock.patch.object(w, "SetWindowPos"), \
                mock.patch.object(taskbar, "hide_button") as hide:
            result, embedded = embedder.embed(info, 0x55, 640, 480, True)
        self.assertTrue(result.ok)
        hide.assert_not_called()  # a child window gets no button: the shell is not asked for anything
        return embedded, [v for i, v in writes if i == w.GWL_EXSTYLE]

    def test_app_window_flag_is_dropped_while_hosted_and_restored_on_detach(self):
        embedded, ex_writes = self.embed(0x10000 | w.WS_EX_APPWINDOW)
        self.assertEqual(ex_writes, [0x10000])  # same ex-style, just without WS_EX_APPWINDOW
        with mock.patch.object(embedder, "restore_state", return_value=True) as restore:
            embedder.detach(embedded)
        self.assertTrue(restore.call_args.args[0].ex_style & w.WS_EX_APPWINDOW)  # the original ex-style comes back

    def test_other_windows_keep_their_ex_style(self):
        _embedded, ex_writes = self.embed(0x10000)
        self.assertEqual(ex_writes, [])


class DockedModeTests(unittest.TestCase):
    def dock(self, has_button=True, hidden=True, ex_style=0):
        state = WindowState(BROWSER.hwnd, 0, 0x16CF0000, ex_style, (10, 10, 900, 700), False, False)
        with mock.patch.object(embedder, "dock_compatibility", return_value=embedder.EmbedResult(embedder.EmbedStatus.OK)), \
                mock.patch.object(embedder, "capture_state", return_value=state), \
                mock.patch.object(embedder, "is_main_window_candidate", return_value=has_button), \
                mock.patch.object(w, "GetWindowLongPtrW", return_value=ex_style), \
                mock.patch.object(taskbar, "hide_button", return_value=hidden) as hide:
            _result, embedded = embedder.dock(BROWSER, 0x55, True)
        return embedded, hide

    def undock(self, embedded, ours=True):
        with mock.patch.object(w, "IsWindow", return_value=True), \
                mock.patch.object(w, "window_pid", return_value=(embedded.pid if ours else 999, 1)), \
                mock.patch.object(w, "SetWindowPos"), mock.patch.object(w, "ShowWindowAsync"), \
                mock.patch.object(w, "IsHungAppWindow", return_value=False), \
                mock.patch.object(w, "SetForegroundWindow"), \
                mock.patch.object(taskbar, "show_button", return_value=True) as show:
            embedder.detach(embedded)
        return show

    def test_button_is_hidden_while_docked_and_given_back_on_detach(self):
        embedded, hide = self.dock()
        hide.assert_called_once_with(BROWSER.hwnd)
        self.assertTrue(embedded.taskbar_hidden)
        show = self.undock(embedded)
        show.assert_called_once_with(BROWSER.hwnd)
        self.assertFalse(embedded.taskbar_hidden)
        self.undock(embedded).assert_not_called()  # a second detach does not add a second button

    def test_shell_refusing_leaves_the_button_alone(self):
        embedded, hide = self.dock(hidden=False)  # no Explorer / COM refused: the button simply stays
        hide.assert_called_once()
        self.assertFalse(embedded.taskbar_hidden)
        self.undock(embedded).assert_not_called()  # and nothing is added on detach

    def test_window_without_a_button_of_its_own_is_left_alone(self):
        embedded, hide = self.dock(has_button=False)  # e.g. an owned palette: it never had a button
        hide.assert_not_called()
        self.undock(embedded).assert_not_called()  # undocking must not invent one
        embedded, hide = self.dock(has_button=False, ex_style=w.WS_EX_APPWINDOW)  # owned, but asks for a button
        hide.assert_called_once()

    def test_recycled_handle_never_gets_a_button(self):
        embedded, _hide = self.dock()
        self.undock(embedded, ours=False).assert_not_called()  # the handle now names another process's window

    def test_styles_owner_and_alt_tab_are_untouched(self):
        with mock.patch.object(w, "SetWindowLongPtrW") as set_long, mock.patch.object(w, "SetParent") as set_parent:
            embedded, _hide = self.dock()
            self.undock(embedded)
        set_long.assert_not_called()  # no WS_EX_TOOLWINDOW trick, no owner change: Alt+Tab keeps listing it
        set_parent.assert_not_called()


class CrashRecoveryTests(unittest.TestCase):
    """After CIRANA died with a docked window, the next start gives that window its button back."""

    def setUp(self):
        me = psutil.Process()
        self.entry = {"hwnd": 0x9300, "pid": me.pid, "created": me.create_time(), "owner": {"pid": 0, "created": 0}}

    def restore(self, entries, window_pid=None, candidate=True):
        with mock.patch.object(orphans.w, "window_pid", return_value=(window_pid or self.entry["pid"], 1)), \
                mock.patch.object(orphans, "is_main_window_candidate", return_value=candidate), \
                mock.patch.object(taskbar, "show_button", return_value=True) as show:
            count = orphans.restore_taskbar_buttons(entries)
        return count, show

    def test_button_comes_back_for_the_same_window(self):
        count, show = self.restore([self.entry, {"nonsense": 1}])
        self.assertEqual(count, 1)
        show.assert_called_once_with(0x9300)

    def test_unsafe_cases_are_skipped(self):
        me = psutil.Process()
        alive_owner = dict(self.entry, owner={"pid": me.pid, "created": me.create_time()})
        self.assertEqual(self.restore([alive_owner])[0], 0)  # that CIRANA still hosts it
        self.assertEqual(self.restore([self.entry], window_pid=1)[0], 0)  # the handle belongs to another process now
        self.assertEqual(self.restore([dict(self.entry, created=1.0)])[0], 0)  # the pid was recycled
        self.assertEqual(self.restore([self.entry], candidate=False)[0], 0)  # not an ordinary main window any more


class ShellObjectTests(unittest.TestCase):
    def test_no_window_means_no_call(self):
        self.assertFalse(taskbar.hide_button(0))
        self.assertFalse(taskbar.show_button(0))

    def test_shell_object_can_be_created(self):
        # creates ITaskbarList and runs HrInit only: no button is touched
        if taskbar._taskbar() is None:
            self.skipTest("no shell taskbar in this session")


if __name__ == "__main__":
    unittest.main()
