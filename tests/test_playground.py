"""Playground logic: pre-launch checks, window discovery, orphan recovery and the tab UI (embedder mocked).

The UI half runs on the offscreen Qt platform, whose HWNDs are fake, so every Win32 embedding call is replaced.
Real embedding is covered by tests/test_playground_live.py.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import psutil  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from cirana.core import orphans, playground  # noqa: E402
from cirana.core.models import AppEntry, LaunchResult, stable_app_id  # noqa: E402
from cirana.core.playground import SessionState, embed_precheck, wait_for_app_window  # noqa: E402
from cirana.native import window_embedder as embedder  # noqa: E402
from cirana.native.window_discovery import WindowInfo  # noqa: E402


def make(name, launch="executable", target=None, exe=""):
    return AppEntry(id=stable_app_id(name), name=name, launch_type=launch, target=target or f"C:\\Tools\\{name}.exe",
                    source="user", exe_path=exe)


class PrecheckTests(unittest.TestCase):
    def test_unknown_apps_and_scripts_are_not_attempted(self):
        self.assertIn("unknown", embed_precheck(make("Lnk", "shortcut", r"C:\x\Lnk.lnk")))
        self.assertIn("unknown", embed_precheck(make("Panel", "aumid", "Microsoft.Windows.ControlPanel")))  # shell item
        self.assertIn(".exe", embed_precheck(make("Script", target=r"C:\x\run.bat", exe=r"C:\x\run.bat")))
        self.assertEqual(playground.hosting_kind(make("Panel", "aumid", "Microsoft.Windows.ControlPanel")), "external")
        self.assertIsNotNone(embed_precheck(make("Klavye", "aumid", r"C:\WINDOWS\System32\osk.exe")))  # accessibility

    def test_classic_executable_is_eligible(self):
        self.assertIsNone(embed_precheck(make("Tool", target=r"C:\Tools\tool.exe")))
        self.assertIsNone(embed_precheck(make("Shortcut", "shortcut", r"C:\x\a.lnk", exe=r"C:\Tools\a.exe")))
        self.assertEqual(playground.hosting_kind(make("Tool", target=r"C:\Tools\tool.exe")), "tab")

    def test_store_apps_are_docked(self):
        calc = make("Calc", "aumid", "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App")
        self.assertIsNone(embed_precheck(calc))
        self.assertEqual(playground.hosting_kind(calc), "docked")
        self.assertEqual(playground.aumid_package(calc), ("microsoft.windowscalculator", "8wekyb3d8bbwe"))
        notepad = make("N", target=r"C:\Program Files\WindowsApps\x\notepad.exe")
        self.assertEqual(playground.hosting_kind(notepad), "docked")

    def test_classic_programs_listed_by_path_as_aumid_are_ordinary_programs(self):
        charmap = make("Karakter Eşlem", "aumid", r"C:\WINDOWS\System32\charmap.exe")
        self.assertEqual(playground.app_exe(charmap), os.path.normcase(r"C:\WINDOWS\System32\charmap.exe"))
        self.assertEqual(playground.hosting_kind(charmap), "tab")
        rdp = make("Uzak Masaüstü Bağlantısı", "aumid", "Microsoft.Windows.RemoteDesktop")
        self.assertTrue(playground.app_exe(rdp).endswith("mstsc.exe"))

    def test_package_folder_and_window_matching(self):
        package = ("microsoft.windowscalculator", "8wekyb3d8bbwe")
        inside = r"c:\program files\windowsapps\microsoft.windowscalculator_11.2502.2.0_x64__8wekyb3d8bbwe\calculatorapp.exe"
        self.assertTrue(playground.in_package(inside, package))
        self.assertFalse(playground.in_package(inside.replace("calculator_", "calculatorx_"), package))
        hidden = WindowInfo(1, 5, 1, "Something", "Cls", "")  # a process CIRANA cannot inspect has exe == ""
        self.assertFalse(playground.window_matches(hidden, ""))  # an unknown exe must never match it
        self.assertFalse(playground.window_matches(hidden, "", package=package))
        titled = WindowInfo(2, 5, 1, "Başlıksız - Not Defteri", "Cls", "")
        self.assertTrue(playground.window_matches(titled, "", title="Not Defteri"))
        self.assertFalse(playground.window_matches(titled, "", title="Defteri"))


class EmbedderFocusTests(unittest.TestCase):
    def setUp(self):
        from cirana.native.window_restorer import WindowState

        self.embedded = embedder.EmbeddedWindow(0x77, 0x55, WindowState(0x77, 0, 0, 0, (0, 0, 1, 1), False, False), 9, True)
        self.calls = []
        w = embedder.w
        patches = [
            mock.patch.object(w, "IsWindow", return_value=True),
            mock.patch.object(w, "IsHungAppWindow", return_value=False),
            mock.patch.object(w, "GetParent", return_value=0x55),
            mock.patch.object(w, "PostMessageW", side_effect=lambda h, m, wp, lp: self.calls.append(("post", m, wp)) or True),
            mock.patch.object(w, "SetFocus", side_effect=lambda h: self.calls.append(("focus", h))),
            mock.patch.object(w, "IsChild", return_value=False),
            mock.patch.object(w, "window_pid", return_value=(1, 1)),  # focus is not in the app's process
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_focus_replays_activation_before_moving_keyboard_focus(self):
        w = embedder.w
        with mock.patch.object(w, "GetFocus", return_value=0x11):  # CIRANA's own widget has the keyboard
            embedder.focus(self.embedded)
        self.assertEqual(self.calls, [("post", w.WM_NCACTIVATE, 1), ("post", w.WM_ACTIVATE, w.WA_ACTIVE), ("focus", 0x77)])

    def test_focus_already_inside_the_app_is_left_alone(self):
        w = embedder.w
        with mock.patch.object(w, "GetFocus", return_value=0x99), mock.patch.object(w, "IsChild", return_value=True):
            embedder.focus(self.embedded)
        self.assertNotIn(("focus", 0x77), self.calls)

    def test_hung_app_gets_nothing(self):
        with mock.patch.object(embedder.w, "IsHungAppWindow", return_value=True), \
                mock.patch.object(embedder.w, "GetFocus", return_value=0):
            embedder.focus(self.embedded)
        self.assertEqual(self.calls, [])

    def test_deactivation_is_never_posted_to_a_window_that_is_not_hosted(self):
        with mock.patch.object(embedder.w, "GetParent", return_value=0):
            embedder.set_active(self.embedded, False)
        self.assertEqual(self.calls, [])


class DockedPlacementTests(unittest.TestCase):
    def test_visible_frame_is_sized_to_the_tab_and_nothing_else_is_touched(self):
        from cirana.native.window_restorer import WindowState

        docked = embedder.EmbeddedWindow(0x77, 0x55, WindowState(0x77, 0, 0, 0, (0, 0, 1, 1), False, False), 9, True,
                                         mode=embedder.DOCKED)
        w = embedder.w
        with mock.patch.object(w, "IsWindow", return_value=True), \
                mock.patch.object(w, "window_pid", return_value=(9, 1)), \
                mock.patch.object(w, "IsIconic", return_value=False), \
                mock.patch.object(w, "IsZoomed", return_value=False), \
                mock.patch.object(w, "window_rect", return_value=(93, 200, 707, 607)), \
                mock.patch.object(w, "visible_frame_rect", return_value=(100, 200, 700, 600)), \
                mock.patch.object(w, "SetWindowPos") as set_pos, \
                mock.patch.object(w, "SetParent") as set_parent, \
                mock.patch.object(w, "SetWindowLongPtrW") as set_long:
            embedder.place_docked(docked, (10, 20, 810, 620))
            embedder.resize(docked, 100, 100)  # child-mode sizing never applies to a docked window
        # 7 px invisible borders left/right/bottom are added around the tab rectangle
        _hwnd, _after, x, y, cx, cy, flags = set_pos.call_args.args
        self.assertEqual((x, y, cx, cy), (3, 20, 814, 607))
        self.assertTrue(flags & w.SWP_ASYNCWINDOWPOS and flags & w.SWP_NOACTIVATE)
        self.assertEqual(set_pos.call_count, 1)
        set_parent.assert_not_called()
        set_long.assert_not_called()

    def test_a_maximized_docked_window_is_restored_first_and_placed_on_the_next_tick(self):
        from cirana.native.window_restorer import WindowState

        docked = embedder.EmbeddedWindow(0x77, 0x55, WindowState(0x77, 0, 0, 0, (0, 0, 1, 1), False, False), 9, True,
                                         mode=embedder.DOCKED)
        w = embedder.w
        with mock.patch.object(w, "IsWindow", return_value=True), \
                mock.patch.object(w, "window_pid", return_value=(9, 1)), \
                mock.patch.object(w, "IsIconic", return_value=False), \
                mock.patch.object(w, "IsZoomed", return_value=True), \
                mock.patch.object(w, "ShowWindowAsync") as show, \
                mock.patch.object(w, "SetWindowPos") as set_pos:
            embedder.place_docked(docked, (0, 0, 10, 10))
        show.assert_called_once_with(0x77, w.SW_RESTORE)
        set_pos.assert_not_called()

    def test_a_recycled_handle_is_never_moved(self):
        from cirana.native.window_restorer import WindowState

        docked = embedder.EmbeddedWindow(0x77, 0x55, WindowState(0x77, 0, 0, 0, (0, 0, 1, 1), False, False), 9, True,
                                         mode=embedder.DOCKED)
        w = embedder.w
        with mock.patch.object(w, "IsWindow", return_value=True), \
                mock.patch.object(w, "window_pid", return_value=(1234, 1)), \
                mock.patch.object(w, "SetWindowPos") as set_pos, mock.patch.object(w, "ShowWindowAsync") as show:
            embedder.place_docked(docked, (0, 0, 10, 10))
            embedder.send_docked_behind(docked, 0x55)
            embedder.show_docked(docked, minimized=True)
            self.assertFalse(embedder.detach(docked))
        set_pos.assert_not_called()
        show.assert_not_called()


def window(hwnd, title="App", exe=r"c:\tools\tool.exe", pid=10):
    return WindowInfo(hwnd, pid, 1, title, "Cls", exe)


class WindowWaitTests(unittest.TestCase):
    def run_wait(self, windows, **kw):
        with mock.patch.object(playground, "list_top_level_windows", side_effect=lambda: list(windows)), \
                mock.patch.object(playground.w, "IsWindow", return_value=True), \
                mock.patch.object(playground.w, "IsHungAppWindow", return_value=False), \
                mock.patch.object(playground, "describe_window", side_effect=lambda h: next(i for i in windows if i.hwnd == h)):
            return wait_for_app_window(kw.pop("before", set()), kw.pop("exe", r"c:\tools\tool.exe"), kw.pop("pid", 0),
                                       threading.Event(), timeout=kw.pop("timeout", 1.0), settle=0.15, poll=0.02)

    def test_new_titled_window_of_the_app_is_found_after_settling(self):
        started = time.monotonic()
        found = self.run_wait([window(1), window(2, exe=r"c:\other\x.exe")])
        self.assertEqual(found.hwnd, 1)
        self.assertGreaterEqual(time.monotonic() - started, 0.15)

    def test_existing_untitled_and_foreign_windows_are_ignored(self):
        windows = [window(1), window(2, title=" "), window(3, exe=r"c:\other\x.exe")]
        self.assertIsNone(self.run_wait(windows, before={1}, timeout=0.4))

    def test_launcher_stub_is_matched_by_file_name(self):
        packaged = window(5, exe=r"c:\program files\windowsapps\pkg\tool.exe")
        self.assertEqual(self.run_wait([packaged]).hwnd, 5)

    def test_cancel_stops_the_wait(self):
        cancel = threading.Event()
        cancel.set()
        self.assertIsNone(wait_for_app_window(set(), "x.exe", 0, cancel, timeout=5))


class OrphanTests(unittest.TestCase):
    def setUp(self):
        self.proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                     creationflags=subprocess.CREATE_NO_WINDOW)
        self.addCleanup(self._kill)
        self.created = psutil.Process(self.proc.pid).create_time()

    def _kill(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()

    def entry(self, **kw):
        base = {"pid": self.proc.pid, "created": self.created, "title": "Sleeper", "exe": sys.executable,
                "owner": {"pid": 0, "created": 0}}
        base.update(kw)
        return base

    def test_running_process_without_window_is_an_orphan(self):
        found = orphans.find_orphans([self.entry()])
        self.assertEqual([o.pid for o in found], [self.proc.pid])

    def test_recycled_pid_is_not_reported(self):
        self.assertEqual(orphans.find_orphans([self.entry(created=self.created + 500)]), [])

    def test_entries_of_a_live_cirana_instance_are_skipped(self):
        owner = {"pid": os.getppid(), "created": psutil.Process(os.getppid()).create_time()}
        self.assertEqual(orphans.find_orphans([self.entry(owner=owner)]), [])

    def test_dead_process_and_garbage_are_ignored(self):
        self.assertEqual(orphans.find_orphans([self.entry(pid=2_000_000_000), {"nonsense": 1}]), [])

    def test_ending_requires_the_explicit_call_and_checks_identity(self):
        orphan = orphans.find_orphans([self.entry()])[0]
        self.assertIsNone(self.proc.poll())  # finding never ends anything
        message = orphans.end_orphan(orphan)
        self.assertIn("Ended", message)
        self.proc.wait(timeout=5)
        self.assertIn("no longer running", orphans.end_orphan(orphan))


class PlaygroundUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.mkdtemp(prefix="cirana-pg-")
        os.environ["CIRANA_HOME"] = cls.home
        from cirana.main import build_app

        cls.app, cls.window, cls.ctx = build_app([])
        cls.window.show()
        cls.window.show_page("playground", save=False)
        cls.pump()

    @classmethod
    def tearDownClass(cls):
        cls.window.pages["playground"].shutdown()
        cls.ctx.tracker.shutdown()
        cls.window.close()
        shutil.rmtree(cls.home, ignore_errors=True)

    @classmethod
    def pump(cls, seconds=0.0, until=None, timeout=5.0):
        end = time.monotonic() + (timeout if until else seconds)
        while time.monotonic() < end:
            cls.app.processEvents()
            if until and until():
                return True
            time.sleep(0.005)
        return bool(until and until())

    def setUp(self):
        self.view = self.window.pages["playground"]
        self.manager = self.ctx.playground
        self.detached = []
        self.next_hwnd = 0x1000 + len(self.manager.sessions())
        self.launches = []
        self.manager._launch = self.fake_launch
        patches = [
            mock.patch.object(embedder, "embed", side_effect=self.fake_embed),
            mock.patch.object(embedder, "detach", side_effect=lambda e, activate=False: self.detached.append(e.hwnd) or True),
            mock.patch.object(embedder, "is_alive", return_value=True),
            mock.patch.object(embedder, "is_still_embedded", return_value=True),
            mock.patch.object(embedder, "is_hung", return_value=False),
            mock.patch.object(embedder, "focus"),
            # the offscreen HWNDs are fake numbers: never post anything to whatever real window has that handle
            mock.patch.object(embedder, "set_active"),
            mock.patch.object(embedder, "has_focus", return_value=True),
            mock.patch.object(embedder, "contains_point", return_value=False),
            mock.patch.object(playground, "snapshot_handles", return_value=set()),
            mock.patch.object(playground, "wait_for_app_window", side_effect=self.fake_wait),
            mock.patch.object(playground, "windows_of_executable", return_value=[]),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self.cleanup_sessions)

    def cleanup_sessions(self):
        for session in list(self.manager.sessions()):
            self.view.close_tab(session.id)
        self.pump(0.05)

    # -- fakes ---------------------------------------------------------------------------
    def fake_launch(self, app):
        self.launches.append(app.name)
        return LaunchResult(True, "ok", pid=4242)

    def fake_wait(self, before, exe, pid, cancel, **kw):
        self.next_hwnd += 1
        return WindowInfo(self.next_hwnd, 4242, 1, "Fake window", "Cls", exe)

    def fake_embed(self, info, host_hwnd, width, height, launched_by_cirana):
        from cirana.native.window_restorer import WindowState

        state = WindowState(info.hwnd, 0, 0, 0, (0, 0, 100, 100), False, False)
        return embedder.EmbedResult(embedder.EmbedStatus.OK, warnings=["be careful"]), \
            embedder.EmbeddedWindow(info.hwnd, host_hwnd, state, info.pid, launched_by_cirana)

    def add_app(self, name):
        return self.ctx.registry.add_custom_app(name, f"C:\\Tools\\{name}.exe", "executable")

    def open(self, app, **kw):
        outcome = self.manager.open_app(app.id, **kw)
        self.assertEqual(outcome.kind, "started", outcome.message)
        self.assertTrue(self.pump(until=lambda: self.manager.get(outcome.session_id).state is not SessionState.LAUNCHING))
        return self.manager.get(outcome.session_id)

    # -- tests ------------------------------------------------------------------------------
    def test_open_creates_a_hosted_tab(self):
        session = self.open(self.add_app("Alpha"))
        self.assertEqual(session.state, SessionState.EMBEDDED)
        self.assertEqual(self.view.tabs.count(), 1)
        self.assertEqual(self.view.current_session_id(), session.id)
        self.assertEqual(session.warnings, ["be careful"])
        self.assertIsNotNone(self.view._pages[session.id].host.embedded)
        self.assertEqual(self.ctx.settings.playground_store.load()["embedded"][0]["pid"], 4242)

    def test_closing_a_tab_detaches_and_never_closes_the_application(self):
        session = self.open(self.add_app("Beta"))
        hwnd = session.hwnd
        with mock.patch.object(embedder, "request_close") as close:
            self.view.close_tab(session.id)
        close.assert_not_called()
        self.assertIn(hwnd, self.detached)
        self.assertEqual(self.view.tabs.count(), 0)
        self.assertIsNone(self.manager.get(session.id))
        self.assertEqual(self.ctx.settings.playground_store.load()["embedded"], [])

    def test_same_app_activates_its_tab_instead_of_launching_again(self):
        app = self.add_app("Gamma")
        first = self.open(app)
        again = self.manager.open_app(app.id)
        self.assertEqual(again.kind, "activated")
        self.assertEqual(again.session_id, first.id)
        self.assertEqual(self.launches, ["Gamma"])
        second = self.open(app, allow_duplicate=True)
        self.assertNotEqual(second.id, first.id)
        self.assertEqual(self.view.tabs.count(), 2)

    def test_detach_and_reattach(self):
        session = self.open(self.add_app("Delta"))
        self.view.detach(session.id)
        self.assertEqual(self.manager.get(session.id).state, SessionState.DETACHED)
        self.assertIn(session.hwnd, self.detached)
        with mock.patch.object(playground.w, "IsWindow", return_value=True), \
                mock.patch.object(playground, "describe_window", return_value=WindowInfo(session.hwnd, 4242, 1, "W", "C", "")):
            self.view.reattach(session.id)
        self.assertEqual(self.manager.get(session.id).state, SessionState.EMBEDDED)

    def test_blocked_window_becomes_external_and_is_not_touched(self):
        blocked = embedder.EmbedResult(embedder.EmbedStatus.BLOCKED, "Packaged apps cannot be re-parented.")
        with mock.patch.object(embedder, "embed", return_value=(blocked, None)):
            session = self.open(self.add_app("Epsilon"))
        self.assertEqual(session.state, SessionState.EXTERNAL)
        self.assertIn("Packaged apps", session.message)
        self.assertEqual(self.detached, [])

    def test_unsupported_app_opens_externally_without_a_tab(self):
        calc = AppEntry(id=stable_app_id("Calc"), name="Calc", launch_type="aumid", target="Microsoft.Calc!App",
                        source="packaged")
        self.ctx.registry.apply_scan([calc])
        outcome = self.manager.open_app(calc.id)
        self.assertEqual(outcome.kind, "external")
        self.assertEqual(self.launches, ["Calc"])
        self.assertEqual(self.view.tabs.count(), 0)

    def test_timeout_leaves_application_alone(self):
        with mock.patch.object(playground, "wait_for_app_window", return_value=None):
            session = self.open(self.add_app("Zeta"))
        self.assertEqual(session.state, SessionState.EXTERNAL)
        self.assertIn("did not touch", session.message)

    def test_application_closing_its_window_is_reported(self):
        session = self.open(self.add_app("Eta"))
        self.view._pages[session.id].host.window_lost.emit("closed")
        self.assertEqual(self.manager.get(session.id).state, SessionState.CLOSED)

    def test_shutdown_detaches_every_hosted_window(self):
        a, b = self.open(self.add_app("Theta")), self.open(self.add_app("Iota"))
        self.assertEqual(self.view.shutdown(), 2)
        self.assertEqual(set(self.detached), {a.hwnd, b.hwnd})
        self.assertEqual(self.view.shutdown(), 0)  # idempotent
        self.assertEqual(self.ctx.settings.playground_store.load()["embedded"], [])

    def test_workspace_filter_hides_other_workspaces_tabs(self):
        other_ws = self.ctx.workspaces.create("Other")
        self.addCleanup(self.ctx.workspaces.delete, other_ws.id)
        mine = self.open(self.add_app("Kappa"))
        other = self.open(self.add_app("Lambda"), workspace_id=other_ws.id)
        self.view.set_active_workspace("")
        self.assertEqual(self.view.visible_session_ids(), [mine.id])
        self.view.set_active_workspace(other_ws.id)
        self.assertEqual(self.view.visible_session_ids(), [other.id])
        self.assertEqual(self.view.current_session_id(), other.id)
        self.view.set_active_workspace("")

    def test_a_host_still_holding_a_hung_window_is_not_deleted(self):
        from cirana.widgets import native_host

        session = self.open(self.add_app("Hung"))
        page = self.view._pages[session.id]
        current_parent = [int(page.host.winId())]  # the hung application has not yet released its window
        with mock.patch.object(embedder, "detach", return_value=False),                 mock.patch.object(native_host.w, "IsWindow", return_value=True),                 mock.patch.object(native_host.w, "GetParent", side_effect=lambda _h: current_parent[0]):
            self.view.close_tab(session.id)
            self.assertEqual(self.view._limbo, [page])  # kept alive (hidden) instead of deleted
            self.view._sweep_limbo()
            self.assertEqual(self.view._limbo, [page])
            current_parent[0] = 0  # the app finally answered and the window left the host
            self.view._sweep_limbo()
            self.assertEqual(self.view._limbo, [])
            self.assertFalse(self.view._limbo_timer.isActive())

    def test_close_request_to_a_hung_application_never_deletes_its_host(self):
        from cirana.widgets import native_host

        session = self.open(self.add_app("Stuck"))
        page = self.view._pages[session.id]
        current_parent = [int(page.host.winId())]  # hung: the window is still a child of the host after the request
        with mock.patch.object(embedder, "request_close", return_value=True) as close,                 mock.patch.object(native_host.w, "IsWindow", return_value=True),                 mock.patch.object(native_host.w, "GetParent", side_effect=lambda _h: current_parent[0]):
            self.assertTrue(page.host.request_application_close())
            close.assert_called_once()
            self.assertTrue(page.host.restore_pending())
            self.view.close_tab(session.id)
            self.assertEqual(self.view._limbo, [page])  # kept alive (hidden) instead of deleted with the window
            current_parent[0] = 0  # the application answered: its window left the host
            self.assertFalse(page.host.restore_pending())
            self.view._sweep_limbo()
            self.assertEqual(self.view._limbo, [])

    def test_close_request_without_a_pending_restore_still_deletes_the_host(self):
        from cirana.widgets import native_host

        session = self.open(self.add_app("Quick"))
        page = self.view._pages[session.id]
        with mock.patch.object(embedder, "request_close", return_value=True),                 mock.patch.object(native_host.w, "IsWindow", return_value=False):  # it closed right away
            page.host.request_application_close()
            self.assertFalse(page.host.restore_pending())
            self.view.close_tab(session.id)
        self.assertEqual(self.view._limbo, [])

    def test_shutdown_keeps_the_recovery_record_of_a_window_not_yet_released(self):
        from cirana.widgets import native_host

        hung, fine = self.open(self.add_app("Frozen")), self.open(self.add_app("Fine"))
        hung_host = int(self.view._pages[hung.id].host.winId())
        restored = {fine.hwnd: True, hung.hwnd: False}
        with mock.patch.object(embedder, "detach", side_effect=lambda e, activate=False: restored[e.hwnd]),                 mock.patch.object(native_host.w, "IsWindow", return_value=True),                 mock.patch.object(native_host.w, "GetParent",
                                  side_effect=lambda h: hung_host if h == hung.hwnd else 0):
            self.assertEqual(self.view.shutdown(), 1)
            recorded = self.ctx.settings.playground_store.load()["embedded"]
            self.assertEqual([(e["session"], e["pid"]) for e in recorded], [(hung.id, 4242)])
            self.view.shutdown()  # a second call (Ctrl+C path, then closeEvent) must not forget it
            self.assertEqual([e["session"] for e in self.ctx.settings.playground_store.load()["embedded"]], [hung.id])
        self.manager.update_state(hung.id, SessionState.DETACHED)  # let the cleanup forget it

    def test_shutdown_keeps_the_record_of_a_hung_window_detached_earlier(self):
        from cirana.widgets import native_host

        session = self.open(self.add_app("Frozen Earlier"))
        host = int(self.view._pages[session.id].host.winId())
        with mock.patch.object(embedder, "detach", return_value=False),                 mock.patch.object(native_host.w, "IsWindow", return_value=True),                 mock.patch.object(native_host.w, "GetParent", side_effect=lambda _h: host):
            self.view.detach(session.id)  # not responding: the tab says detached, the window is still in the host
            self.assertEqual(self.manager.get(session.id).state, SessionState.DETACHED)
            self.view.shutdown()
            self.assertEqual([e["session"] for e in self.ctx.settings.playground_store.load()["embedded"]],
                             [session.id])

    def recorded(self):
        return [e["session"] for e in self.ctx.settings.playground_store.load()["embedded"]]

    def hung_host(self, page, current_parent):
        """Win32 as seen by a host whose hung application keeps its window parented until current_parent changes."""
        from contextlib import ExitStack

        from cirana.widgets import native_host

        stack = ExitStack()
        stack.enter_context(mock.patch.object(embedder, "detach", return_value=False))
        stack.enter_context(mock.patch.object(embedder, "request_close", return_value=True))
        stack.enter_context(mock.patch.object(native_host.w, "IsWindow", return_value=True))
        stack.enter_context(mock.patch.object(native_host.w, "GetParent", side_effect=lambda _h: current_parent[0]))
        return stack

    def test_detaching_a_hung_window_keeps_its_record_until_it_leaves_the_host(self):
        session = self.open(self.add_app("Hung Detach"))
        page = self.view._pages[session.id]
        parent = [int(page.host.winId())]
        with self.hung_host(page, parent):
            self.view.detach(session.id)
            self.assertEqual(self.manager.get(session.id).state, SessionState.DETACHED)
            self.assertEqual(self.recorded(), [session.id])  # not dropped by the state change
            self.view._sweep_limbo()
            self.assertEqual(self.recorded(), [session.id])  # still in the host
            parent[0] = 0  # the application answered and took its window back
            self.view._sweep_limbo()
            self.assertEqual(self.recorded(), [])
            self.assertFalse(self.view._limbo_timer.isActive())

    def test_closing_the_tab_of_a_hung_window_keeps_its_record_while_in_limbo(self):
        session = self.open(self.add_app("Hung Close Tab"))
        page = self.view._pages[session.id]
        parent = [int(page.host.winId())]
        with self.hung_host(page, parent):
            self.view.close_tab(session.id)
            self.assertIsNone(self.manager.get(session.id))
            self.assertEqual(self.view._limbo, [page])
            self.assertEqual(self.recorded(), [session.id])
            self.view._sweep_limbo()
            self.assertEqual(self.recorded(), [session.id])
            parent[0] = 0
            self.view._sweep_limbo()
            self.assertEqual(self.view._limbo, [])
            self.assertEqual(self.recorded(), [])

    def test_close_request_to_a_hung_window_keeps_its_record(self):
        from PySide6.QtWidgets import QMessageBox

        session = self.open(self.add_app("Hung Close App"))
        page = self.view._pages[session.id]
        parent = [int(page.host.winId())]
        with self.hung_host(page, parent), \
                mock.patch("cirana.ui.playground_view.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
            self.view.close_application(session.id)
            self.assertEqual(self.recorded(), [session.id])
            parent[0] = 0
            self.view._sweep_limbo()
            self.assertEqual(self.recorded(), [])

    def test_shutdown_keeps_the_record_of_a_closed_tab_waiting_in_limbo(self):
        hung, fine = self.open(self.add_app("Limbo")), self.open(self.add_app("Fine Too"))
        page = self.view._pages[hung.id]
        parent = [int(page.host.winId())]
        with self.hung_host(page, parent):
            self.view.close_tab(hung.id)
            self.view.shutdown()
            self.assertEqual(self.recorded(), [hung.id])  # the cleanly detached one is forgotten, the hung one not
            self.view.shutdown()
            self.assertEqual(self.recorded(), [hung.id])
            parent[0] = 0
            self.view._sweep_limbo()
            self.assertEqual(self.view._limbo, [])

    def test_a_window_that_left_the_host_releases_its_record_at_once(self):
        session = self.open(self.add_app("Answers"))
        self.view.detach(session.id)  # the default fakes restore it immediately
        self.assertEqual(self.recorded(), [])
        self.assertEqual(self.manager.unreleased_ids(), [])

    def test_startup_orphan_check_never_ends_anything_without_confirmation(self):
        from PySide6.QtWidgets import QMessageBox

        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], creationflags=subprocess.CREATE_NO_WINDOW)
        self.addCleanup(lambda: (proc.kill(), proc.wait()))
        entry = {"session": "x", "app_id": "", "title": "Sleeper", "exe": sys.executable, "pid": proc.pid,
                 "created": psutil.Process(proc.pid).create_time(), "hwnd": 1, "owner": {"pid": 0, "created": 0}}

        class FakeDialog:
            chosen = []

            def __init__(self, found, parent=None):
                self.found = found

            def exec(self):
                return True

            def selected(self):
                return list(FakeDialog.chosen) or []

        def run(chosen, answer):
            self.ctx.settings.playground_store.save({"embedded": [entry]})
            FakeDialog.chosen = list(chosen)
            with mock.patch("cirana.ui.main_window.OrphanDialog", FakeDialog),                     mock.patch("cirana.ui.main_window.QMessageBox.question", return_value=answer):
                self.window.check_orphans()

        run([], QMessageBox.StandardButton.Yes)  # dialog accepted but nothing ticked
        self.assertIsNone(proc.poll())
        self.assertEqual(self.ctx.settings.playground_store.load()["embedded"], [])  # reported once, then forgotten
        found = orphans.find_orphans([entry])
        run(found, QMessageBox.StandardButton.No)  # ticked, but the confirmation was refused
        self.assertIsNone(proc.poll())
        run(found, QMessageBox.StandardButton.Yes)
        proc.wait(timeout=5)  # ended only after both the selection and the confirmation

    def test_tab_order_follows_dragging(self):
        a, b, c = (self.open(self.add_app(n)) for n in ("Mu", "Nu", "Xi"))
        self.view.tabs.moveTab(0, 2)
        self.assertEqual(self.view.tab_order(), [b.id, c.id, a.id])
        self.assertEqual([s.id for s in self.manager.sessions()], [b.id, c.id, a.id])

    # -- compact layout ----------------------------------------------------------------------
    def test_status_messages_never_resize_the_hosted_window(self):
        session = self.open(self.add_app("Omicron"))
        host = self.view._pages[session.id].host
        self.pump(0.05)
        before = host.geometry()
        self.window.show_toast("A rather long message that must stay inside the strip " * 4)
        self.pump(0.05)
        self.assertEqual(host.geometry(), before)
        self.view.show_status("")
        self.pump(0.05)
        self.assertEqual(host.geometry(), before)

    def test_open_tabs_hide_the_top_bar_and_full_window_hides_the_sidebar(self):
        self.assertFalse(self.view.has_tabs())
        self.assertTrue(self.window.topbar.isVisible())
        session = self.open(self.add_app("Pi"))
        self.assertTrue(self.view.has_tabs())
        self.assertFalse(self.window.topbar.isVisible())
        self.assertTrue(self.window.sidebar.isVisible())
        self.view.tabs.tabBarDoubleClicked.emit(0)  # double-clicking a tab toggles full-window mode
        self.assertFalse(self.window.sidebar.isVisible())
        self.window.toggle_focus_mode()
        self.assertTrue(self.window.sidebar.isVisible())
        self.view.set_focus_mode(True)
        self.view.close_tab(session.id)  # last tab gone: the strip's exit button is gone too, so leave the mode
        self.assertTrue(self.window.sidebar.isVisible())
        self.assertTrue(self.window.topbar.isVisible())
        self.open(self.add_app("Rho"))
        self.window.show_page("home", save=False)
        self.assertTrue(self.window.topbar.isVisible())
        self.window.show_page("playground", save=False)
        self.assertFalse(self.window.topbar.isVisible())

    def test_escape_closes_the_search_overlay_before_leaving_full_window_mode(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        self.open(self.add_app("Upsilon"))
        self.view.set_focus_mode(True)
        self.view.search_button.click()  # the strip's search opens the overlay
        overlay = self.window.overlay
        self.assertTrue(overlay.isVisible())
        QTest.keyClick(overlay.input, Qt.Key.Key_Escape)
        self.pump(0.05)
        self.assertFalse(overlay.isVisible())
        self.assertFalse(self.window.sidebar.isVisible())  # still in full-window mode
        self.assertTrue(self.window._escape.isEnabled())
        self.view.set_focus_mode(False)

    def test_click_into_the_hosted_app_gives_it_the_keyboard_back(self):
        from cirana.widgets import native_host

        session = self.open(self.add_app("Phi"))
        host = self.view._pages[session.id].host
        host._focus_watch.stop()  # driven by hand below
        root, other_app, app_popup = 0x500, 0x600, 0x700
        state = {"fg": root, "down": False, "focused": False, "inside": False}
        nw = native_host.w
        with mock.patch.object(nw, "GetAncestor", return_value=root), \
                mock.patch.object(nw, "GetForegroundWindow", side_effect=lambda: state["fg"]), \
                mock.patch.object(nw, "mouse_button_down", side_effect=lambda: state["down"]), \
                mock.patch.object(nw, "cursor_pos", return_value=(10, 10)), \
                mock.patch.object(embedder, "contains_point", side_effect=lambda e, x, y: state["inside"]), \
                mock.patch.object(embedder, "has_focus", side_effect=lambda e: state["focused"]), \
                mock.patch.object(embedder, "owns_window", side_effect=lambda e, h: h == app_popup), \
                mock.patch.object(embedder, "focus") as focus, \
                mock.patch.object(embedder, "set_active") as set_active:
            host._check_focus()
            focus.assert_not_called()  # pointer over CIRANA's own widgets: nothing to do
            state["inside"] = True  # the pointer enters the app, before any click (e.g. heading for the address bar)
            host._check_focus()
            host._check_focus()  # staying inside does not refocus again and again
            self.assertEqual(focus.call_count, 1)
            state["down"] = True  # a click inside while the keyboard is elsewhere (enter check was too slow)
            host._check_focus()
            host._check_focus()  # still held down: no second refocus
            self.assertEqual(focus.call_count, 2)
            state["focused"] = True
            state["down"] = False
            host._check_focus()
            state["down"] = True  # a click inside when the app already has the keyboard is the app's business
            host._check_focus()
            self.assertEqual(focus.call_count, 2)
            state["down"] = False
            state["fg"] = app_popup  # the app's own menu in front: still "inside" the app
            host._check_focus()
            set_active.assert_not_called()
            state["fg"] = other_app  # another program in front: the app is told it is inactive, once
            host._check_focus()
            host._check_focus()
            set_active.assert_called_once_with(host.embedded, False)
            state["fg"] = root  # back to CIRANA: activation and keyboard focus are given back
            host._check_focus()
            self.assertEqual(focus.call_count, 3)

    def test_store_app_window_is_docked_never_reparented(self):
        from cirana.native import win32_api as w
        from cirana.native.window_restorer import WindowState

        app = self.add_app("Chi")
        store = WindowInfo(0x9100, 4242, 1, "Chi", "ApplicationFrameWindow",
                           r"c:\program files\windowsapps\chi_1.0_x64__abc\chi.exe")
        with mock.patch.object(playground, "wait_for_app_window", return_value=store), \
                mock.patch.object(embedder, "embed") as embed, \
                mock.patch.object(embedder, "dock_compatibility", return_value=embedder.EmbedResult(
                    embedder.EmbedStatus.OK, warnings=[embedder.DOCKED_NOTE])), \
                mock.patch.object(embedder, "capture_state",
                                  return_value=WindowState(0x9100, 0, 0, 0, (0, 0, 9, 9), False, False)), \
                mock.patch.object(w, "SetParent") as set_parent, \
                mock.patch.object(w, "SetWindowLongPtrW") as set_long, \
                mock.patch.object(w, "ShowWindowAsync"), mock.patch.object(w, "SetWindowPos"):
            session = self.open(app)
            embed.assert_not_called()
            set_parent.assert_not_called()
            set_long.assert_not_called()
            self.assertEqual(session.state, SessionState.EMBEDDED)
            self.assertTrue(session.docked)
            self.assertTrue(self.view._pages[session.id].host.embedded.docked)
            # its pid may be ApplicationFrameHost.exe: never listed for crash recovery ("End" would hit every UWP app)
            self.assertEqual(self.ctx.settings.playground_store.load()["embedded"], [])
            # minimized together with CIRANA while its tab was hidden: showing the tab must bring it back
            host = self.view._pages[session.id].host
            with mock.patch.object(w, "IsWindow", return_value=True), \
                    mock.patch.object(w, "window_pid", return_value=(4242, 1)), \
                    mock.patch.object(w, "IsIconic", side_effect=lambda h: h == 0x9100), \
                    mock.patch.object(w, "ShowWindowAsync") as show:
                host.hide()
                host.show()
                show.assert_any_call(0x9100, w.SW_SHOWNOACTIVATE)
            self.view.close_tab(session.id)
            set_parent.assert_not_called()

    def test_strip_actions_follow_the_current_tab(self):
        mine = self.open(self.add_app("Sigma"))
        self.assertTrue(self.view.detach_button.isVisible())
        self.assertTrue(self.view.close_app_button.isVisible())  # started by CIRANA
        self.assertTrue(self.view.warn_button.isVisible())  # the fake embed reports a warning
        self.view.detach(mine.id)
        self.assertFalse(self.view.detach_button.isVisible())
        self.assertFalse(self.view.close_app_button.isVisible())
        self.assertFalse(self.view.warn_button.isVisible())

    def test_middle_click_detaches_instead_of_closing(self):
        session = self.open(self.add_app("Tau"))
        with mock.patch.object(embedder, "request_close") as close:
            self.view.tabs.middle_clicked.emit(self.view._index_of(session.id))
        close.assert_not_called()
        self.assertIn(session.hwnd, self.detached)
        self.assertIsNone(self.manager.get(session.id))


if __name__ == "__main__":
    unittest.main()
