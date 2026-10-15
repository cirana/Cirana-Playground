"""Hosted applications do not show a taskbar button of their own; detaching (or the next start after a crash) gives
it back. Every Win32 / shell call is faked: no real window or taskbar button is touched."""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
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
        taskbar._records.clear()
        self.addCleanup(taskbar._records.clear)

    def restore(self, entries, window_pid=None, candidate=True, shell_ok=True):
        with mock.patch.object(orphans.w, "window_pid", return_value=(window_pid or self.entry["pid"], 1)), \
                mock.patch.object(orphans, "is_main_window_candidate", return_value=candidate), \
                mock.patch.object(taskbar, "_same_window", return_value=True), \
                mock.patch.object(taskbar, "_tab_call", return_value=shell_ok) as call:
            count = orphans.restore_taskbar_buttons(entries)
        show = mock.Mock()
        for args in call.call_args_list:
            if args.args[0] == taskbar._ADD_TAB:
                show(args.args[1])
        return count, show

    def test_button_the_shell_refuses_now_stays_recorded_for_later(self):
        count, _show = self.restore([self.entry], shell_ok=False)  # e.g. Explorer not ready right after logging in
        self.assertEqual(count, 0)
        with mock.patch.object(taskbar, "_same_window", return_value=True):
            self.assertEqual([r["hwnd"] for r in taskbar.records()], [0x9300])  # kept, so it is written back
            with mock.patch.object(taskbar, "_tab_call", return_value=True):
                self.assertEqual(taskbar.retry_pending(), 1)  # a later retry succeeds
            self.assertEqual(taskbar.records(), [])

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


class RecordTests(unittest.TestCase):
    """One source of truth for buttons CIRANA still owes: kept until the shell really gave them back."""

    def setUp(self):
        taskbar._records.clear()
        self.addCleanup(taskbar._records.clear)
        same = mock.patch.object(taskbar, "_same_window", return_value=True)
        same.start()
        self.addCleanup(same.stop)

    def docked(self):
        state = WindowState(BROWSER.hwnd, 0, 0x16CF0000, 0, (10, 10, 900, 700), False, False)
        embedded = embedder.EmbeddedWindow(BROWSER.hwnd, 0x55, state, BROWSER.pid, True, mode=embedder.DOCKED)
        with mock.patch.object(taskbar, "_tab_call", return_value=True):
            embedded.taskbar_hidden = taskbar.hide_button(BROWSER.hwnd)
        return embedded

    def undock(self, embedded, shell_ok):
        with mock.patch.object(w, "IsWindow", return_value=True), \
                mock.patch.object(w, "window_pid", return_value=(embedded.pid, 1)), \
                mock.patch.object(w, "SetWindowPos"), mock.patch.object(w, "ShowWindowAsync"), \
                mock.patch.object(taskbar, "_tab_call", return_value=shell_ok):
            embedder.detach(embedded)

    def test_failed_add_tab_keeps_the_record_through_detach_persist_and_shutdown(self):
        from cirana.core.application_registry import ApplicationRegistry
        from cirana.core.playground import PlaygroundManager
        from cirana.storage.settings_manager import SettingsManager

        home = tempfile.mkdtemp(prefix="cirana-test-")
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        settings = SettingsManager(Path(home))
        manager = PlaygroundManager(ApplicationRegistry(settings), settings)
        embedded = self.docked()
        self.undock(embedded, shell_ok=False)
        self.assertTrue(embedded.taskbar_hidden)  # not cleared: the button is still missing
        self.assertEqual([(r["hwnd"], r["hosted"]) for r in taskbar.records()], [(BROWSER.hwnd, False)])
        with mock.patch.object(taskbar, "_tab_call", return_value=False):
            manager.clear_persisted_state()  # normal shutdown while the shell still refuses
        stored = settings.playground_store.load()["docked"]
        self.assertEqual([r["hwnd"] for r in stored], [BROWSER.hwnd])  # survives for the next start
        with mock.patch.object(taskbar, "_tab_call", return_value=True):
            manager.clear_persisted_state()  # retried first: this time it works and the record goes
        self.assertEqual(settings.playground_store.load()["docked"], [])

    def test_successful_detach_forgets_the_record(self):
        embedded = self.docked()
        self.undock(embedded, shell_ok=True)
        self.assertFalse(embedded.taskbar_hidden)
        self.assertEqual(taskbar.records(), [])

    def test_closed_window_is_forgotten(self):
        embedded = self.docked()
        with mock.patch.object(w, "IsWindow", return_value=False):
            embedder.detach(embedded)
        self.assertEqual(taskbar._records, {})

    def test_explorer_restart_hides_hosted_buttons_again_and_drops_pending_ones(self):
        hosted = self.docked()
        taskbar._records[0x9400] = {"pid": 1, "created": 0.0, "hosted": False}  # AddTab had failed for this one
        calls = []
        with mock.patch.object(taskbar, "reset") as reset, \
                mock.patch.object(taskbar, "_tab_call", side_effect=lambda i, h: calls.append((i, h)) or True):
            self.assertEqual(taskbar.taskbar_restarted(), 1)
        reset.assert_called_once()  # a new shell object for the new taskbar
        self.assertEqual(calls, [(taskbar._DELETE_TAB, hosted.hwnd)])
        self.assertEqual(list(taskbar._records), [hosted.hwnd])  # the new taskbar gave 0x9400 its button

    def test_stale_shell_object_is_recreated_once(self):
        results = iter([-2147417848, 0])  # RPC_E_DISCONNECTED, then success on the new object
        with mock.patch.object(taskbar, "_taskbar", return_value=object()), \
                mock.patch.object(taskbar, "_vtable", return_value=[None] * 6), \
                mock.patch.object(taskbar, "_TabProto", return_value=lambda ptr, hwnd: next(results)), \
                mock.patch.object(taskbar, "reset") as reset:
            self.assertTrue(taskbar._tab_call(taskbar._ADD_TAB, 0x9300))
        reset.assert_called_once()


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
