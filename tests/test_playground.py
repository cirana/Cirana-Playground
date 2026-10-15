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

    def test_chromium_browsers_are_docked_but_electron_apps_stay_in_a_tab(self):
        for exe in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                    r"C:\Program Files\BraveSoftware\Brave-Browser\Application\BRAVE.EXE"):
            self.assertEqual(playground.hosting_kind(make("Browser", target=exe)), "docked", exe)
        shortcut = make("Chrome", "shortcut", r"C:\x\Google Chrome.lnk",
                        exe=r"C:\Program Files\Google\Chrome\Application\chrome.exe")
        self.assertEqual(playground.hosting_kind(shortcut), "docked")
        code = make("Code", target=r"C:\Users\u\AppData\Local\Programs\Microsoft VS Code\Code.exe")
        self.assertEqual(playground.hosting_kind(code), "tab")  # Electron: same window class, but works as a child
        lookalike = make("Not Chrome", target=r"C:\Tools\chrome-helper.exe")
        self.assertEqual(playground.hosting_kind(lookalike), "tab")

    def test_hosting_mode_docks_browser_windows_by_executable(self):
        def window(exe):
            return WindowInfo(0x10, 1, 1, "Title", "Chrome_WidgetWin_1", exe.lower())

        for exe in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                    r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"):
            self.assertEqual(embedder.hosting_mode(window(exe)), embedder.DOCKED, exe)
        self.assertEqual(embedder.hosting_mode(window(r"C:\Apps\Discord\Discord.exe")), embedder.CHILD)
        self.assertEqual(embedder.hosting_mode(WindowInfo(0x10, 1, 1, "Map", "MyDlgClass", r"c:\windows\charmap.exe")),
                         embedder.CHILD)

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

    def test_misplaced_detects_a_child_window_moved_or_resized_inside_the_host(self):
        rects = {0x55: (100, 200, 900, 800), 0x77: (100, 200, 900, 800)}
        with mock.patch.object(embedder.w, "window_rect", side_effect=lambda h: rects[h]):
            self.assertFalse(embedder.misplaced(self.embedded, 800, 600))
            rects[0x77] = (-68, 340, 732, 940)  # dragged by its own (Chromium) title bar
            self.assertTrue(embedder.misplaced(self.embedded, 800, 600))
            rects[0x77] = (100, 200, 700, 800)  # resized itself
            self.assertTrue(embedder.misplaced(self.embedded, 800, 600))
            docked = embedder.EmbeddedWindow(0x77, 0x55, self.embedded.state, 9, True, mode=embedder.DOCKED)
            self.assertFalse(embedder.misplaced(docked, 800, 600))  # docked windows are placed by place_docked
        with mock.patch.object(embedder.w, "IsWindow", return_value=False):
            self.assertFalse(embedder.misplaced(self.embedded, 800, 600))

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
        title_bars, hosts = kw.pop("title_bars", set()), kw.pop("hosts", {})
        kw.setdefault("max_timeout", kw.get("timeout", 1.0))  # the started process is "running" in these fakes
        with mock.patch.object(playground, "list_top_level_windows", side_effect=lambda: list(windows)), \
                mock.patch.object(playground.w, "IsWindow", return_value=True), \
                mock.patch.object(playground.w, "IsHungAppWindow", return_value=False), \
                mock.patch.object(playground.w, "GetForegroundWindow", return_value=kw.pop("foreground", 0)), \
                mock.patch.object(playground, "_has_title_bar", side_effect=lambda h: h in title_bars), \
                mock.patch.object(playground, "_hosts_of", side_effect=lambda tree: dict(hosts)), \
                mock.patch.object(playground, "_grow_process_tree", side_effect=kw.pop("grow", lambda tree: True)), \
                mock.patch.object(playground, "describe_window", side_effect=lambda h: next(i for i in windows if i.hwnd == h)):
            return wait_for_app_window(kw.pop("before", set()), kw.pop("exe", r"c:\tools\tool.exe"), kw.pop("pid", 0),
                                       threading.Event(), timeout=kw.pop("timeout", 1.0), settle=0.15, poll=0.02,
                                       **kw)

    def test_new_titled_window_of_the_app_is_found_after_settling(self):
        started = time.monotonic()
        found = self.run_wait([window(1), window(2, exe=r"c:\other\x.exe")])
        self.assertEqual(found.hwnd, 1)
        self.assertGreaterEqual(time.monotonic() - started, 0.15)

    def test_existing_untitled_and_foreign_windows_are_ignored(self):
        windows = [window(1), window(2, title=" "), window(3, exe=r"c:\other\x.exe")]
        result = self.run_wait(windows, before={1}, timeout=0.4)
        self.assertFalse(result)  # nothing found: a reason instead of a window
        self.assertIsInstance(result, playground.NoWindow)
        self.assertIn("No new window of tool.exe", result.reason)

    def test_launcher_stub_is_matched_by_file_name(self):
        packaged = window(5, exe=r"c:\program files\windowsapps\pkg\tool.exe")
        self.assertEqual(self.run_wait([packaged]).hwnd, 5)

    # -- consoles in a terminal: the terminal window owns the (hidden) console window of each shell it shows --------
    TERMINAL = r"c:\program files\windowsapps\microsoft.windowsterminal_1.24.0_x64__8wekyb3d8bbwe\windowsterminal.exe"
    SHELL = r"c:\windows\system32\cmd.exe"

    def test_terminal_window_showing_the_started_shell_is_found(self):
        terminal = window(7, title=r"C:\WINDOWS\System32\cmd.exe", exe=self.TERMINAL, pid=900)
        found = self.run_wait([terminal, window(8, exe=r"c:\other\x.exe", pid=901)], exe=self.SHELL, pid=50,
                              hosts={7: {50}})
        self.assertEqual(found.hwnd, 7)

    def test_tab_in_an_existing_terminal_window_is_refused_at_once(self):
        terminal = window(7, title="Windows PowerShell", exe=self.TERMINAL, pid=900)
        started = time.monotonic()
        result = self.run_wait([terminal], exe=self.SHELL, pid=50, before={7}, hosts={7: {50}}, timeout=5.0)
        self.assertIsInstance(result, playground.NoWindow)
        self.assertIn("tab", result.reason)
        self.assertLess(time.monotonic() - started, 2.0)  # not after the full timeout
        shared = self.run_wait([terminal], exe=self.SHELL, pid=50, hosts={7: {50, 77}})  # new, but shows others too
        self.assertIsInstance(shared, playground.NoWindow)

    def test_window_of_a_child_process_is_found(self):
        def grow(tree):
            tree.add(61)  # the launcher started the real program and may already be gone
            return True

        child = window(9, exe=r"c:\tools\real\app.exe", pid=61)
        self.assertEqual(self.run_wait([child], exe=r"c:\tools\launcher.exe", pid=50, grow=grow).hwnd, 9)

    def test_untitled_window_needs_a_strong_link_and_a_title_bar(self):
        untitled = window(10, title="", pid=50)
        self.assertEqual(self.run_wait([untitled], pid=50, title_bars={10}).hwnd, 10)
        self.assertFalse(self.run_wait([untitled], pid=50, timeout=0.4))  # an untitled splash screen
        stranger = window(11, title="", exe=r"c:\other\tool.exe", pid=99)  # same file name only: too weak
        self.assertFalse(self.run_wait([stranger], title_bars={11}, timeout=0.4))

    def test_alias_package_finds_new_windows_but_never_the_users_existing_one(self):
        package = ("microsoft.windowsterminal", "8wekyb3d8bbwe")
        alias = r"c:\users\u\appdata\local\microsoft\windowsapps\wt.exe"
        users = window(12, title="Windows PowerShell", exe=self.TERMINAL, pid=900)
        result = self.run_wait([users], exe=alias, pid=50, before={12}, package=package, timeout=0.4)
        self.assertFalse(result)
        fresh = window(13, title="Windows PowerShell", exe=self.TERMINAL, pid=900)
        self.assertEqual(self.run_wait([users, fresh], exe=alias, pid=50, before={12}, package=package).hwnd, 13)

    def test_running_copy_window_is_used_only_when_it_comes_to_the_front(self):
        running = window(14)  # existed before the launch; the started copy handed over and exited
        with mock.patch.object(playground, "EXITED_GRACE", 0.3):
            found = self.run_wait([running], pid=50, before={14}, grow=lambda tree: False, foreground=14)
            self.assertEqual(found.hwnd, 14)
            result = self.run_wait([running], pid=50, before={14}, grow=lambda tree: False, foreground=0)
        self.assertIsInstance(result, playground.NoWindow)
        self.assertIn("exited without opening a window", result.reason)

    def test_slow_application_gets_more_time_while_it_runs_and_says_so(self):
        result = self.run_wait([], pid=50, timeout=0.2, max_timeout=0.5)
        self.assertIsInstance(result, playground.NoWindow)
        self.assertIn("still running", result.reason)

    def test_cancel_stops_the_wait(self):
        cancel = threading.Event()
        cancel.set()
        self.assertIsNone(wait_for_app_window(set(), "x.exe", 0, cancel, timeout=5))


class AppAliasTests(unittest.TestCase):
    """App execution aliases (``WindowsApps\\wt.exe``) name the package they start in their reparse data."""

    @staticmethod
    def buffer(*strings, tag=0x8000001B):
        body = (3).to_bytes(4, "little") + "\0".join(strings).encode("utf-16-le") + b"\0\0"
        return tag.to_bytes(4, "little") + len(body).to_bytes(2, "little") + b"\0\0" + body

    def test_alias_buffer_is_parsed(self):
        from cirana.native import win32_api

        raw = self.buffer("Microsoft.WindowsTerminal_8wekyb3d8bbwe", "Microsoft.WindowsTerminal_8wekyb3d8bbwe!App",
                          r"C:\Program Files\WindowsApps\Microsoft.WindowsTerminal_1.24_x64__8wekyb3d8bbwe\wt.exe", "0")
        self.assertEqual(win32_api.parse_app_alias(raw)[:2],
                         ["Microsoft.WindowsTerminal_8wekyb3d8bbwe", "Microsoft.WindowsTerminal_8wekyb3d8bbwe!App"])
        self.assertEqual(win32_api.parse_app_alias(self.buffer("x", tag=0xA000000C)), [])  # a symbolic link
        self.assertEqual(win32_api.parse_app_alias(b"\x1b\x00\x00\x80"), [])  # truncated

    def test_alias_package_only_for_aliases(self):
        alias = r"c:\users\u\appdata\local\microsoft\windowsapps\wt.exe"
        with mock.patch.object(playground.w, "read_app_alias",
                               return_value=["Microsoft.WindowsTerminal_8wekyb3d8bbwe", "x!App", "y"]) as read:
            self.assertEqual(playground.alias_package(alias), ("microsoft.windowsterminal", "8wekyb3d8bbwe"))
            self.assertIsNone(playground.alias_package(r"c:\windows\system32\cmd.exe"))  # not even read
        read.assert_called_once_with(alias)
        with mock.patch.object(playground.w, "read_app_alias", return_value=[]):
            self.assertIsNone(playground.alias_package(alias))  # unreadable: no package, plain matching


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
            # fake HWNDs: never hide or add a taskbar button of whatever real window has that handle
            mock.patch.object(embedder.taskbar, "hide_button", return_value=True),
            mock.patch.object(embedder.taskbar, "show_button", return_value=True),
            mock.patch.object(embedder, "has_focus", return_value=True),
            mock.patch.object(embedder, "contains_point", return_value=False),
            mock.patch.object(playground, "snapshot_handles", return_value=set()),
            mock.patch.object(playground, "wait_for_app_window", side_effect=self.fake_wait),
            mock.patch.object(playground, "windows_of_executable", return_value=[]),
            # the fake pid 4242 may name a real process: never treat it as the hosted application's process
            mock.patch.object(playground, "process_started", return_value=0.0),
            mock.patch.object(playground, "process_facts", return_value=None),
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

    def test_hosted_window_dragged_away_is_put_back_into_the_host(self):
        from cirana.widgets import native_host

        session = self.open(self.add_app("Psi"))
        host = self.view._pages[session.id].host
        host._focus_watch.stop()  # driven by hand below
        size = host._device_size()
        root = 0x500
        box = {"rect": (0, 0, *size), "fixable": True}
        nw = native_host.w

        def resize(e, width, height):
            resizes.append((width, height))
            if box["fixable"]:
                box["rect"] = (0, 0, width, height)

        resizes = []
        with mock.patch.object(nw, "GetAncestor", return_value=root),                 mock.patch.object(nw, "GetForegroundWindow", return_value=root),                 mock.patch.object(nw, "mouse_button_down", return_value=False),                 mock.patch.object(nw, "cursor_pos", return_value=(10, 10)),                 mock.patch.object(nw, "window_rect", side_effect=lambda h: box["rect"]),                 mock.patch.object(embedder, "misplaced", side_effect=lambda e, wd, ht: box["rect"] != (0, 0, wd, ht)),                 mock.patch.object(embedder, "resize", side_effect=resize),                 mock.patch.object(embedder, "contains_point", return_value=False):
            host._check_focus()
            self.assertEqual(resizes, [])  # in place: nothing is sent
            for step in range(1, 6):  # a title-bar drag in progress: every new position is corrected
                box["rect"] = (-12 * step, 10 * step, size[0] - 12 * step, size[1] + 10 * step)
                box["fixable"] = False  # the move loop wins until the button is released
                host._check_focus()
            self.assertEqual(len(resizes), 5)
            box["fixable"] = True  # released: the next correction sticks
            host._check_focus()
            self.assertEqual(box["rect"], (0, 0, *size))
            self.assertEqual(resizes[-1], size)
            host._check_focus()
            self.assertEqual(len(resizes), 6)  # back in place: nothing more is sent
            box["rect"], box["fixable"] = (0, 0, size[0] + 1, size[1]), False  # keeps a size of its own (DPI)
            for _ in range(10):
                host._check_focus()
            self.assertEqual(len(resizes), 9)  # corrected a few times, then left alone instead of 20 times a second
            host.hide()
            box["rect"] = (50, 50, 60, 60)
            host._check_focus()
            self.assertEqual(len(resizes), 9)  # hidden tab: nothing is touched
            host.show()

    def test_chromium_browser_window_is_docked_never_reparented(self):
        from cirana.native import win32_api as w
        from cirana.native.window_restorer import WindowState

        app = self.ctx.registry.add_custom_app("Chrome", r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                                               "executable")
        browser = WindowInfo(0x9200, 4242, 1, "New Tab - Google Chrome", "Chrome_WidgetWin_1",
                             r"c:\program files\google\chrome\application\chrome.exe")
        with mock.patch.object(playground, "wait_for_app_window", return_value=browser), \
                mock.patch.object(embedder, "embed") as embed, \
                mock.patch.object(embedder, "dock_compatibility", return_value=embedder.EmbedResult(
                    embedder.EmbedStatus.OK, warnings=[embedder.DOCKED_NOTE])), \
                mock.patch.object(embedder, "capture_state",
                                  return_value=WindowState(0x9200, 0, 0, 0, (0, 0, 9, 9), False, False)), \
                mock.patch.object(w, "SetParent") as set_parent, \
                mock.patch.object(w, "SetWindowLongPtrW") as set_long, \
                mock.patch.object(w, "ShowWindowAsync"), mock.patch.object(w, "SetWindowPos"):
            session = self.open(app)
            embed.assert_not_called()  # a WS_CHILD browser can never become the active window again
            set_parent.assert_not_called()
            set_long.assert_not_called()
            self.assertEqual(session.state, SessionState.EMBEDDED)
            self.assertTrue(session.docked)
            self.assertTrue(self.view._pages[session.id].host.embedded.docked)

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

    # -- lost windows: verified reconnection, safe mode, bounded retries ----------------------------
    def lose(self, session, reason="closed", unstable=False):
        """What the host's liveness check does when the window is gone; ``unstable``: right after it was hosted."""
        from cirana.widgets.native_host import OBSERVE_SECONDS

        host = self.view._pages[session.id].host
        host._attached_at = time.monotonic() - (0.0 if unstable else OBSERVE_SECONDS + 1)
        host._lose(reason)

    def replace_with(self, results):
        """The replacement search answers with ``results`` in turn (the identity check of the process passes)."""
        return [mock.patch.object(playground, "process_started", return_value=111.0),
                mock.patch.object(playground, "find_replacement_window", side_effect=list(results))]

    def hosted(self, name):
        session = self.open(self.add_app(name))
        session.process_created = 111.0
        return session

    def test_replaced_window_is_reconnected_after_verification(self):
        session = self.hosted("Phoenix")
        new = WindowInfo(0x7700, 4242, 1, "Fake window", "Cls", session.exe)
        old_hwnd = session.hwnd
        identity, find = self.replace_with([new])
        with identity, find as search:
            self.lose(session)
            self.assertEqual(session.state, SessionState.LAUNCHING)
            self.assertIn("reconnecting", session.message)
            self.assertTrue(self.pump(until=lambda: session.state is SessionState.EMBEDDED))
        self.assertEqual(search.call_args.args[1], old_hwnd)  # the lost window is never a candidate itself
        self.assertEqual(session.hwnd, 0x7700)
        self.assertFalse(session.safe_mode)  # a loss long after hosting is no compatibility failure
        self.assertTrue(any("replaced its window" in text for text in session.warnings))
        self.assertEqual(self.view._pages[session.id].host.embedded.hwnd, 0x7700)

    def test_window_unstable_in_child_mode_is_docked_from_then_on(self):
        from cirana.native.window_restorer import WindowState

        session = self.hosted("Wobbly")
        docked = []

        def fake_dock(info, host_hwnd, launched_by_cirana):
            docked.append(info.hwnd)
            state = WindowState(info.hwnd, 0, 0, 0, (0, 0, 100, 100), False, False)
            return embedder.EmbedResult(embedder.EmbedStatus.OK, warnings=[embedder.DOCKED_NOTE]), \
                embedder.EmbeddedWindow(info.hwnd, host_hwnd, state, info.pid, launched_by_cirana, mode=embedder.DOCKED)

        identity, find = self.replace_with([WindowInfo(session.hwnd, 4242, 1, "Fake window", "Cls", session.exe)])
        with identity, find as search, mock.patch.object(embedder, "dock", side_effect=fake_dock), \
                mock.patch.object(embedder, "place_docked"), mock.patch.object(embedder, "keep_docked_above"), \
                mock.patch.object(embedder, "send_docked_behind"), mock.patch.object(embedder, "show_docked"):
            self.lose(session, "escaped", unstable=True)  # it left the host right after being placed there
            self.assertTrue(self.pump(until=lambda: session.state is SessionState.EMBEDDED))
            self.assertEqual(docked, [session.hwnd])
            self.assertNotIn(session.hwnd, search.call_args.args[3])  # the escaped window itself may be taken
            self.assertTrue(session.docked and session.safe_mode)
            self.assertTrue(any("safe mode" in text for text in session.warnings))
            self.assertEqual(self.ctx.settings.playground_store.load()["embedded"], [])  # docked: no crash record
            later = playground.PlaygroundSession(id="x", app_id="", title="Wobbly", exe=session.exe)
            self.assertTrue(self.manager.dock_only(later))  # the same program is docked right away next time
            self.view.close_tab(session.id)

    def test_reconnecting_stops_after_repeated_failures(self):
        session = self.hosted("Flicker")
        windows = [WindowInfo(0x7800 + i, 4242, 1, "Fake window", "Cls", session.exe)
                   for i in range(playground.MAX_RECONNECTS)]
        identity, find = self.replace_with(windows)
        with identity, find as search, mock.patch.object(embedder, "dock", side_effect=self.fake_embed_as_dock):
            for _ in windows:
                self.lose(session, unstable=True)
                self.assertTrue(self.pump(until=lambda: session.state is SessionState.EMBEDDED))
            self.lose(session, unstable=True)  # lost right away once more: no endless loop
        self.assertEqual(search.call_count, playground.MAX_RECONNECTS)
        self.assertEqual(session.state, SessionState.EXTERNAL)  # it still runs: left as a normal window
        self.assertIn("stopped reconnecting", session.message)

    def fake_embed_as_dock(self, info, host_hwnd, launched_by_cirana):  # safe mode docks: keep it a fake child
        return self.fake_embed(info, host_hwnd, 10, 10, launched_by_cirana)

    def test_ended_process_is_closed_without_a_search(self):
        session = self.hosted("Gone")
        with mock.patch.object(playground, "process_started", return_value=0.0), \
                mock.patch.object(playground, "find_replacement_window") as search:
            self.lose(session)
        search.assert_not_called()
        self.assertEqual(session.state, SessionState.CLOSED)
        self.assertEqual(session.message, "Gone closed its window.")

    def test_program_that_restarted_in_a_new_process_is_looked_for(self):
        session = self.hosted("Restarter")
        session.process_exe = r"c:\tools\restarter.exe"  # known: related processes of it may carry its window
        successor = WindowInfo(0x7a00, 5151, 1, "Fake window", "Cls", session.exe)
        with mock.patch.object(playground, "process_started", return_value=0.0), \
                mock.patch.object(playground, "find_replacement_window", return_value=successor) as search:
            self.lose(session)  # its first process has ended
            self.assertTrue(self.pump(until=lambda: session.state is SessionState.EMBEDDED))
        search.assert_called_once()
        self.assertEqual((session.hwnd, session.pid), (0x7a00, 5151))  # the tab follows the new process

    def test_uncertain_replacement_is_not_taken(self):
        session = self.hosted("Twins")
        identity, find = self.replace_with([playground.NoWindow("It opened 2 new windows; CIRANA cannot tell.")])
        with identity, find:
            self.lose(session)
            self.assertTrue(self.pump(until=lambda: session.state is not SessionState.LAUNCHING))
        self.assertEqual(session.state, SessionState.EXTERNAL)
        self.assertIn("cannot tell", session.message)
        self.assertIsNone(self.view._pages[session.id].host.embedded)

    def test_a_window_hosted_in_another_tab_is_never_taken(self):
        first, second = self.hosted("Holder"), self.hosted("Seeker")
        identity, find = self.replace_with([WindowInfo(first.hwnd, 4242, 1, "Fake window", "Cls", second.exe)])
        with identity, find, mock.patch.object(embedder, "embed_or_dock") as attach:
            self.lose(second)
            self.assertTrue(self.pump(until=lambda: second.state is not SessionState.LAUNCHING))
        attach.assert_not_called()
        self.assertEqual(second.state, SessionState.EXTERNAL)
        self.assertIn("already hosted in another tab", second.message)
        self.assertEqual(first.state, SessionState.EMBEDDED)
        self.assertEqual(self.view._pages[first.id].host.embedded.hwnd, first.hwnd)

    def test_no_replacement_search_while_another_application_opens(self):
        session = self.hosted("Quiet")
        opening = self.manager._new_session(None, "", launched_by_cirana=True, title="Opening")  # LAUNCHING
        self.addCleanup(self.manager.remove_session, opening.id)
        with mock.patch.object(playground, "process_started", return_value=111.0), \
                mock.patch.object(playground, "find_replacement_window") as search:
            self.lose(session)
        search.assert_not_called()  # its new window could be taken for the replacement
        self.assertEqual(session.state, SessionState.CLOSED)

    def test_closing_the_tab_cancels_the_reconnection(self):
        session = self.hosted("Late")
        released = threading.Event()

        def slow_search(session_, old_hwnd, escaped, exclude, cancel, **_kw):
            cancel.wait(5)
            released.set()
            return WindowInfo(0x7900, 4242, 1, "Fake window", "Cls", session_.exe)  # too late: must be dropped

        with mock.patch.object(playground, "process_started", return_value=111.0), \
                mock.patch.object(playground, "find_replacement_window", side_effect=slow_search), \
                mock.patch.object(embedder, "embed_or_dock") as attach:
            self.lose(session)
            self.view.close_tab(session.id)
            self.assertTrue(released.wait(5))
            self.pump(0.2)
        attach.assert_not_called()
        self.assertIsNone(self.manager.get(session.id))

    def test_middle_click_detaches_instead_of_closing(self):
        session = self.open(self.add_app("Tau"))
        with mock.patch.object(embedder, "request_close") as close:
            self.view.tabs.middle_clicked.emit(self.view._index_of(session.id))
        close.assert_not_called()
        self.assertIn(session.hwnd, self.detached)
        self.assertIsNone(self.manager.get(session.id))


if __name__ == "__main__":
    unittest.main()
