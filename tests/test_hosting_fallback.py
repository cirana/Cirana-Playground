"""Hosting failures: Win32 refusals are detected step by step, explained, rolled back verifiably, and only then answered
with the docked fallback (or with nothing, when docking is not safe either). Every Win32 call is faked: no real window
is touched."""
import ctypes
import os
import threading
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from cirana.core import playground  # noqa: E402
from cirana.core.playground import NoWindow, PlaygroundSession, find_replacement_window  # noqa: E402
from cirana.native import taskbar  # noqa: E402
from cirana.native import window_embedder as embedder  # noqa: E402
from cirana.native.window_discovery import WindowInfo  # noqa: E402
from cirana.native.window_restorer import WindowState  # noqa: E402
from cirana.widgets import native_host  # noqa: E402

w = embedder.w
HOST = 0x55
TOOL = WindowInfo(0x77, 4242, 1, "Tool", "ToolClass", r"c:\tools\tool.exe")
ORIGINAL = WindowState(0x77, 0, 0x16CF0000, 0x100, (0, 0, 640, 480), False, False)


def refused(code):
    """A fake Win32 call that fails the way Windows does: returns 0 and sets the thread's last error."""
    def call(*_args):
        ctypes.set_last_error(code)
        return 0
    return call


class Win32Fakes:
    """Patches for one embed attempt; every write is recorded, nothing reaches Windows."""

    def __init__(self, test, set_long=None, parent_after=HOST, restored=True, move=None):
        self.writes, self.parents = [], []
        patches = [
            mock.patch.object(w, "IsWindow", return_value=True),
            mock.patch.object(w, "IsHungAppWindow", return_value=False),
            mock.patch.object(w, "process_is_elevated", return_value=False),
            mock.patch.object(w, "process_integrity", return_value=0x2000),
            mock.patch.object(w, "dpi_awareness_mismatch", return_value=""),
            mock.patch.object(w, "window_pid", return_value=(TOOL.pid, 1)),
            mock.patch.object(embedder, "own_pid", return_value=1),
            mock.patch.object(embedder, "capture_state", return_value=ORIGINAL),
            mock.patch.object(w, "GetWindowLongPtrW", return_value=ORIGINAL.style),
            mock.patch.object(w, "SetWindowLongPtrW",
                              side_effect=set_long or (lambda h, i, v: self.writes.append((i, v)) or 1)),
            mock.patch.object(w, "SetParent", side_effect=lambda h, p: self.parents.append(p)),
            mock.patch.object(w, "GetParent", return_value=parent_after),
            mock.patch.object(w, "SetWindowPos", side_effect=move or (lambda *a: 1)),
            mock.patch.object(w, "ShowWindowAsync"), mock.patch.object(w, "ShowWindow"),
            mock.patch.object(embedder, "restore_state", return_value=True),
            mock.patch.object(embedder, "is_restored", return_value=restored),
            mock.patch.object(embedder, "is_main_window_candidate", return_value=True),
            mock.patch.object(taskbar, "hide_button", return_value=True),
        ]
        for patch in patches:
            test.addCleanup(patch.stop)
            started = patch.start()
            if patch.attribute == "restore_state":
                self.restore = started
            elif patch.attribute == "hide_button":
                self.hide_button = started


class StepCheckTests(unittest.TestCase):
    def test_refused_style_change_stops_before_set_parent_and_is_explained(self):
        fakes = Win32Fakes(self, set_long=refused(5))
        with mock.patch.object(w, "process_integrity", return_value=None):  # rights not readable: the cause is named
            result, embedded = embedder.embed(TOOL, HOST, 640, 480, True)
        self.assertIsNone(embedded)
        self.assertEqual(result.status, embedder.EmbedStatus.FAILED)
        self.assertEqual(fakes.parents, [], "SetParent must not run after a refused style change")
        self.assertIn("Changing the window style was refused", result.reason)
        self.assertIn("Win32 error 5", result.reason)
        self.assertIn("UIPI", result.reason)
        fakes.restore.assert_called_once_with(ORIGINAL)
        self.assertTrue(result.rolled_back)

    def test_a_zero_previous_value_without_an_error_is_not_a_refusal(self):
        fakes = Win32Fakes(self, set_long=lambda h, i, v: ctypes.set_last_error(0) or 0)
        result, embedded = embedder.embed(TOOL, HOST, 640, 480, True)
        self.assertTrue(result.ok)
        self.assertEqual(fakes.parents, [HOST])

    def test_set_parent_refusal_reports_the_real_cause(self):
        Win32Fakes(self, parent_after=0)
        with mock.patch.object(w, "SetParent", side_effect=refused(5)):
            result, _ = embedder.embed(TOOL, HOST, 640, 480, True)
        self.assertIn("SetParent was refused", result.reason)
        self.assertIn("same rights as CIRANA", result.reason)  # integrity levels are equal here: not UIPI


class FallbackTests(unittest.TestCase):
    def test_refused_child_mode_is_docked_after_a_verified_rollback(self):
        fakes = Win32Fakes(self, parent_after=0)
        with mock.patch.object(w, "SetParent", side_effect=refused(87)):
            result, embedded = embedder.embed_or_dock(TOOL, HOST, 640, 480, True)
        self.assertTrue(result.ok)
        self.assertTrue(embedded.docked)
        self.assertEqual(embedded.state, ORIGINAL)  # docked from the restored, original state
        self.assertIn("docked over the tab instead", result.warnings[0])
        self.assertIn("SetParent was refused", result.warnings[0])
        fakes.restore.assert_called_once()

    def test_no_fallback_when_the_rollback_cannot_be_verified(self):
        Win32Fakes(self, parent_after=0, restored=False)
        with mock.patch.object(w, "SetParent", side_effect=refused(87)), \
                mock.patch.object(embedder, "dock") as dock:
            result, embedded = embedder.embed_or_dock(TOOL, HOST, 640, 480, True)
        dock.assert_not_called()
        self.assertIsNone(embedded)
        self.assertFalse(result.rolled_back)
        self.assertEqual(result.stranded.hwnd, TOOL.hwnd)
        self.assertIn("could not verify", result.reason)

    def test_no_dock_when_windows_refuses_to_move_the_window_either(self):
        fakes = Win32Fakes(self, parent_after=0, move=refused(5))
        with mock.patch.object(w, "SetParent", side_effect=refused(5)), \
                mock.patch.object(w, "process_integrity", return_value=None):
            result, embedded = embedder.embed_or_dock(TOOL, HOST, 640, 480, True)
        self.assertIsNone(embedded)
        self.assertEqual(result.status, embedder.EmbedStatus.FAILED)
        self.assertIn("Docking it over the tab was not possible either", result.reason)
        self.assertIn("not allowed to move this window", result.reason)
        fakes.hide_button.assert_not_called()  # nothing about the window or its taskbar button changed

    def test_blocked_windows_get_no_fallback(self):
        Win32Fakes(self)
        with mock.patch.object(embedder, "dock") as dock:
            result, _ = embedder.embed_or_dock(WindowInfo(0x77, 1, 1, "Mine", "C", ""), HOST, 640, 480, True)  # CIRANA's
        self.assertEqual(result.status, embedder.EmbedStatus.BLOCKED)
        dock.assert_not_called()


class RightsTests(unittest.TestCase):
    def levels(self, theirs, ours, elevated=False, own_elevated=False):
        Win32Fakes(self)
        levels = {TOOL.pid: theirs, 1: ours}
        elevation = {TOOL.pid: elevated, 1: own_elevated}
        for patch in (mock.patch.object(w, "process_integrity", side_effect=levels.get),
                      mock.patch.object(w, "process_is_elevated", side_effect=elevation.get)):
            patch.start()
            self.addCleanup(patch.stop)

    def test_higher_integrity_is_refused_before_anything_is_changed(self):
        self.levels(0x2010, 0x2000)  # e.g. a uiAccess tool: not "elevated", but UIPI still protects it
        child, docked = embedder.check_compatibility(TOOL), embedder.dock_compatibility(TOOL)
        self.assertEqual(child.status, embedder.EmbedStatus.BLOCKED)
        self.assertEqual(docked.status, embedder.EmbedStatus.BLOCKED)
        self.assertIn("higher rights", child.reason)

    def test_administrator_app_is_refused_with_its_reason(self):
        self.levels(0x3000, 0x2000, elevated=True)
        self.assertIn("administrator", embedder.check_compatibility(TOOL).reason)

    def test_equal_rights_are_fine_even_when_both_are_elevated(self):
        self.levels(0x3000, 0x3000, elevated=True, own_elevated=True)
        self.assertTrue(embedder.check_compatibility(TOOL).ok)
        self.assertTrue(embedder.dock_compatibility(TOOL).ok)

    def test_unreadable_rights_are_a_warning_not_a_refusal(self):
        self.levels(None, 0x2000, elevated=None)
        result = embedder.check_compatibility(TOOL)
        self.assertTrue(result.ok)
        self.assertTrue(any("privilege level" in text for text in result.warnings))


class RestoreCheckTests(unittest.TestCase):
    def check(self, parent, style, ex_style):
        with mock.patch.object(w, "IsWindow", return_value=True), \
                mock.patch.object(w, "GetParent", return_value=parent), \
                mock.patch.object(w, "GetWindowLongPtrW",
                                  side_effect=lambda h, i: style if i == w.GWL_STYLE else ex_style):
            return embedder.is_restored(ORIGINAL)

    def test_only_what_cirana_changes_is_compared(self):
        self.assertTrue(self.check(0, ORIGINAL.style, ORIGINAL.ex_style))
        self.assertTrue(self.check(0, ORIGINAL.style ^ w.WS_VISIBLE, ORIGINAL.ex_style ^ w.WS_EX_CLIENTEDGE))
        self.assertFalse(self.check(HOST, ORIGINAL.style, ORIGINAL.ex_style))  # still parented to the host
        self.assertFalse(self.check(0, (ORIGINAL.style & ~w.WS_CAPTION) | w.WS_CHILD, ORIGINAL.ex_style))
        self.assertFalse(self.check(0, ORIGINAL.style, ORIGINAL.ex_style | w.WS_EX_APPWINDOW))


class EscapeTests(unittest.TestCase):
    def embedded(self, docked=False):
        return embedder.EmbeddedWindow(TOOL.hwnd, HOST, ORIGINAL, TOOL.pid, True,
                                       mode=embedder.DOCKED if docked else embedder.CHILD)

    def give_back(self, embedded, top_level=True, pid=TOOL.pid):
        with mock.patch.object(w, "IsWindow", return_value=True), \
                mock.patch.object(w, "window_pid", return_value=(pid, 1)), \
                mock.patch.object(embedder, "is_top_level", return_value=top_level), \
                mock.patch.object(embedder, "restore_state", return_value=True) as restore:
            given = embedder.give_back_escaped(embedded)
        return given, restore

    def test_escaped_top_level_window_gets_its_original_styles_back(self):
        given, restore = self.give_back(self.embedded())
        self.assertTrue(given)
        restore.assert_called_once_with(ORIGINAL)

    def test_window_moved_into_another_window_or_reused_handle_is_left_alone(self):
        for kwargs in ({"top_level": False}, {"pid": 999}):
            given, restore = self.give_back(self.embedded(), **kwargs)
            self.assertFalse(given)
            restore.assert_not_called()
        given, restore = self.give_back(self.embedded(docked=True))
        self.assertFalse(given)
        restore.assert_not_called()


class HostTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.host = native_host.NativeHost()
        self.addCleanup(self.host.deleteLater)
        self.lost = []
        self.host.window_lost.connect(self.lost.append)

    def attach_child(self):
        embedded = embedder.EmbeddedWindow(TOOL.hwnd, int(self.host.winId()), ORIGINAL, TOOL.pid, True)
        with mock.patch.object(embedder, "embed_or_dock", return_value=(embedder.EmbedResult(embedder.EmbedStatus.OK),
                                                                        embedded)), \
                mock.patch.object(embedder, "hosting_mode", return_value=embedder.CHILD), \
                mock.patch.object(embedder, "resize"):
            self.assertTrue(self.host.attach(TOOL, True).ok)
        self.addCleanup(self.host._stop_watching)
        return embedded

    def test_force_dock_skips_child_mode(self):
        docked = embedder.EmbeddedWindow(TOOL.hwnd, 1, ORIGINAL, TOOL.pid, True, mode=embedder.DOCKED)
        with mock.patch.object(embedder, "dock", return_value=(embedder.EmbedResult(embedder.EmbedStatus.OK),
                                                               docked)) as dock, \
                mock.patch.object(embedder, "embed_or_dock") as child, \
                mock.patch.object(embedder, "hosting_mode", return_value=embedder.CHILD), \
                mock.patch.object(embedder, "place_docked"):
            self.host.attach(TOOL, True, force_dock=True)
        dock.assert_called_once()
        child.assert_not_called()
        self.host._stop_watching()
        self.host._embedded = None

    def test_unverified_rollback_keeps_the_host_alive(self):
        stranded = embedder.EmbeddedWindow(TOOL.hwnd, int(self.host.winId()), ORIGINAL, TOOL.pid, True)
        failed = embedder.EmbedResult(embedder.EmbedStatus.FAILED, "x", rolled_back=False, stranded=stranded)
        with mock.patch.object(embedder, "embed_or_dock", return_value=(failed, None)), \
                mock.patch.object(embedder, "hosting_mode", return_value=embedder.CHILD):
            self.assertFalse(self.host.attach(TOOL, True).ok)
        with mock.patch.object(w, "IsWindow", return_value=True), \
                mock.patch.object(w, "GetParent", return_value=int(self.host.winId())):
            self.assertTrue(self.host.restore_pending(), "deleting the host now would destroy the window")

    def test_hung_during_observation_is_not_a_loss(self):
        self.attach_child()
        with mock.patch.object(embedder, "is_alive", return_value=True), \
                mock.patch.object(embedder, "is_still_embedded", return_value=True), \
                mock.patch.object(embedder, "is_hung", return_value=True):
            self.host._check_alive()
        self.assertEqual(self.lost, [])
        self.assertTrue(self.host.hung)
        self.assertIsNotNone(self.host.embedded)

    def test_escape_right_after_attaching_is_reported_as_unstable_and_given_back(self):
        embedded = self.attach_child()
        with mock.patch.object(embedder, "is_alive", return_value=True), \
                mock.patch.object(embedder, "is_still_embedded", return_value=False), \
                mock.patch.object(embedder, "give_back_escaped", return_value=True) as give_back:
            self.host._check_alive()
        give_back.assert_called_once_with(embedded)
        self.assertEqual(self.lost, ["escaped"])
        self.assertTrue(self.host.lost_while_observing)

    def test_loss_after_the_observation_time_is_a_normal_loss(self):
        self.attach_child()
        self.host._attached_at -= native_host.OBSERVE_SECONDS + 1
        with mock.patch.object(embedder, "is_alive", return_value=False):
            self.host._check_alive()
        self.assertEqual(self.lost, ["closed"])
        self.assertFalse(self.host.lost_while_observing)


class ReplacementSearchTests(unittest.TestCase):
    """Which window may take the place of a lost one (worker-thread search, Win32 faked)."""

    def session(self, **kw):
        values = dict(id="s", app_id="", title="Tool", exe=TOOL.exe, pid=TOOL.pid, hwnd=TOOL.hwnd,
                      process_created=111.0, window_class="ToolClass", known_windows={0x10, 0x20})
        values.update(kw)
        return PlaygroundSession(**values)

    def search(self, windows, created=111.0, session=None, escaped=False, exclude=()):
        windows = list(windows)
        with mock.patch.object(playground, "process_started", return_value=created), \
                mock.patch.object(playground, "list_top_level_windows", return_value=windows):
            return find_replacement_window(session or self.session(), TOOL.hwnd, escaped, set(exclude),
                                           threading.Event(), timeout=0.3, poll=0.01)

    def test_one_new_window_of_the_same_process_and_class_is_taken(self):
        new = WindowInfo(0x99, TOOL.pid, 1, "Tool", "ToolClass", TOOL.exe)
        self.assertEqual(self.search([new]), new)

    def test_more_than_one_candidate_takes_none(self):
        found = self.search([WindowInfo(0x98, TOOL.pid, 1, "A", "ToolClass", TOOL.exe),
                             WindowInfo(0x99, TOOL.pid, 1, "B", "ToolClass", TOOL.exe)])
        self.assertIsInstance(found, NoWindow)
        self.assertIn("cannot tell", found.reason)

    def test_reused_pid_is_never_trusted(self):
        found = self.search([WindowInfo(0x99, TOOL.pid, 1, "Tool", "ToolClass", TOOL.exe)], created=222.0)
        self.assertIsInstance(found, NoWindow)
        self.assertTrue(found.exited)

    def test_windows_that_do_not_match_everything_are_ignored(self):
        others = [
            WindowInfo(0x10, TOOL.pid, 1, "Existing", "ToolClass", TOOL.exe),  # existed when it was hosted
            WindowInfo(0x99, TOOL.pid, 1, "Dialog", "#32770", TOOL.exe),  # another kind of window
            WindowInfo(0x97, 777, 1, "Other", "ToolClass", TOOL.exe),  # another process
            WindowInfo(0x96, TOOL.pid, 1, "Tab", "ToolClass", TOOL.exe),  # already hosted in another tab
        ]
        found = self.search(others, exclude={0x96})
        self.assertIsInstance(found, NoWindow)
        self.assertFalse(found.exited)

    def test_escaped_window_is_taken_once_it_is_a_top_level_window(self):
        with mock.patch.object(w, "IsWindow", return_value=True), \
                mock.patch.object(w, "window_pid", return_value=(TOOL.pid, 1)), \
                mock.patch.object(embedder, "is_top_level", return_value=True), \
                mock.patch.object(playground, "is_main_window_candidate", return_value=True), \
                mock.patch.object(playground, "describe_window", return_value=TOOL):
            self.assertEqual(self.search([], escaped=True), TOOL)
        with mock.patch.object(w, "IsWindow", return_value=True), \
                mock.patch.object(w, "window_pid", return_value=(TOOL.pid, 1)), \
                mock.patch.object(embedder, "is_top_level", return_value=False):
            self.assertIsInstance(self.search([], escaped=True), NoWindow)  # it lives inside another window now

    def test_search_gives_up_in_time(self):
        started = time.monotonic()
        self.search([])
        self.assertLess(time.monotonic() - started, 2.0)


if __name__ == "__main__":
    unittest.main()
