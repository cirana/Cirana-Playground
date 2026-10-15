"""Phase 1: window identity, match rules and the application-window filter."""
import unittest

from cirana.core.models import AppEntry
from cirana.native import win32_api as w
from cirana.wm.filters import WindowFacts, manageable
from cirana.wm.identity import MatchRule, TrackedWindow, app_exe


def window(**kwargs):
    base = dict(hwnd=0x100, pid=10, process_created=50.0, exe="c:\\apps\\code\\code.exe", title="main.py - VS Code",
                class_name="Chrome_WidgetWin_1")
    base.update(kwargs)
    return TrackedWindow(**base)


def facts(**kwargs):
    base = dict(hwnd=0x100, pid=10, title="App", class_name="AppWindow", exe="c:\\apps\\app.exe",
                style=w.WS_CAPTION | w.WS_THICKFRAME | w.WS_VISIBLE, ex_style=0, owner=0, owner_visible=False,
                visible=True, cloaked=False, minimized=False, rect=(0, 0, 800, 600))
    base.update(kwargs)
    return WindowFacts(**base)


class IdentityTests(unittest.TestCase):
    def test_key_changes_when_the_handle_or_pid_is_recycled(self):
        original = window()
        self.assertEqual(original.key, window().key)
        self.assertNotEqual(original.key, window(pid=11).key)  # the handle now belongs to another process
        self.assertNotEqual(original.key, window(process_created=51.0).key)  # the pid was reused by a new process
        self.assertNotEqual(original.key, window(hwnd=0x200).key)

    def test_uwp_frames_are_identified_by_their_content(self):
        frame = window(exe="c:\\windows\\system32\\applicationframehost.exe", class_name="ApplicationFrameWindow",
                       content_exe="c:\\program files\\windowsapps\\microsoft.windowscalculator_11_x64__8wekyb3d8bbwe"
                                   "\\calculatorapp.exe",
                       content_aumid="Microsoft.WindowsCalculator_8wekyb3d8bbwe!App")
        self.assertEqual(frame.exe_name, "calculatorapp.exe")
        rule = MatchRule(aumid="Microsoft.WindowsCalculator_8wekyb3d8bbwe!App")
        self.assertTrue(rule.matches(frame))
        self.assertFalse(MatchRule(exe="c:\\windows\\system32\\notepad.exe").matches(frame))

    def test_rules_match_by_path_file_name_package_and_learned_programs(self):
        code = window()
        self.assertTrue(MatchRule(exe="c:\\apps\\code\\code.exe").matches(code))
        self.assertTrue(MatchRule(exe="d:\\portable\\code.exe").matches(code))  # launcher stubs / moved installs
        self.assertFalse(MatchRule(exe="c:\\apps\\other.exe").matches(code))
        packaged = window(exe="c:\\program files\\windowsapps\\microsoft.windowsterminal_1.2_x64__8wekyb3d8bbwe"
                              "\\windowsterminal.exe")
        self.assertTrue(MatchRule(aumid="Microsoft.WindowsTerminal_8wekyb3d8bbwe!App").matches(packaged))
        # PowerShell started from the Start menu shows up in Windows Terminal: learned once, recognised afterwards.
        rule = MatchRule(exe="c:\\windows\\system32\\windowspowershell\\v1.0\\powershell.exe")
        self.assertFalse(rule.matches(packaged))
        self.assertTrue(rule.learn(packaged))
        self.assertFalse(rule.learn(packaged))
        self.assertTrue(MatchRule.from_dict(rule.to_dict()).matches(packaged))

    def test_class_and_title_filters_narrow_a_rule(self):
        code = window()
        self.assertTrue(MatchRule(exe=code.exe, title="vs code").matches(code))
        self.assertFalse(MatchRule(exe=code.exe, title="notes").matches(code))
        self.assertFalse(MatchRule(exe=code.exe, class_name="Other").matches(code))
        self.assertTrue(MatchRule(class_name="Chrome_WidgetWin_1", title="VS Code").matches(window(exe="")))
        self.assertFalse(MatchRule().matches(code))  # an empty rule never matches anything

    def test_damaged_rule_records_are_harmless(self):
        for raw in (None, [], {"exe": 5, "alt_exes": "x"}, {"alt_exes": [1, "", "a.exe"]}):
            rule = MatchRule.from_dict(raw)
            self.assertIsInstance(rule, MatchRule)
        self.assertEqual(MatchRule.from_dict({"alt_exes": [1, "", "A.exe"]}).alt_exes, ["a.exe"])

    def test_rule_for_an_application(self):
        app = AppEntry("app-1", "Code", "shortcut", "C:\\x\\Code.lnk", "start_menu_user", exe_path="C:\\Apps\\Code\\Code.exe")
        self.assertEqual(MatchRule.for_app(app).exe, app_exe(app))
        packaged = AppEntry("app-2", "Calc", "aumid", "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App", "packaged")
        self.assertEqual(MatchRule.for_app(packaged).aumid, packaged.target)


class FilterTests(unittest.TestCase):
    def test_ordinary_application_window(self):
        self.assertEqual(manageable(facts(), own_pid=1), (True, ""))

    def test_rejected_windows(self):
        cases = {
            "own": facts(pid=1),
            "hidden": facts(visible=False),
            "cloaked": facts(cloaked=True),
            "child": facts(style=w.WS_CHILD | w.WS_VISIBLE),
            "taskbar": facts(class_name="Shell_TrayWnd"),
            "desktop": facts(class_name="Progman"),
            "start menu": facts(exe="c:\\windows\\systemapps\\startmenuexperiencehost.exe"),
            "tooltip": facts(class_name="tooltips_class32"),
            "tool window": facts(ex_style=w.WS_EX_TOOLWINDOW),
            "popup": facts(ex_style=w.WS_EX_NOACTIVATE),
            "dialog": facts(owner=0x50, owner_visible=True),
            "untitled popup": facts(title="", style=w.WS_POPUP | w.WS_VISIBLE),
            "tiny": facts(rect=(0, 0, 40, 20)),
        }
        for name, case in cases.items():
            with self.subTest(name):
                self.assertFalse(manageable(case, own_pid=1)[0])

    def test_accepted_special_cases(self):
        # Delphi / VCL style: the main window is owned by a hidden helper window.
        self.assertTrue(manageable(facts(owner=0x50, owner_visible=False), 1)[0])
        # An owned window that asks for a taskbar button, and a frameless Electron window (thick frame, title).
        self.assertTrue(manageable(facts(owner=0x50, owner_visible=True, ex_style=w.WS_EX_APPWINDOW), 1)[0])
        self.assertTrue(manageable(facts(style=w.WS_POPUP | w.WS_THICKFRAME | w.WS_VISIBLE, title="Discord"), 1)[0])
        # Minimized windows have a tiny off-screen rectangle but are still application windows.
        self.assertTrue(manageable(facts(minimized=True, rect=(-32000, -32000, -31840, -31972)), 1)[0])


if __name__ == "__main__":
    unittest.main()
