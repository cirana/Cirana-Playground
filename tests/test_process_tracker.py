import os
import unittest

from cirana.core.models import AppEntry, stable_app_id
from cirana.core.process_tracker import match_running, running_executables


def app(name, exe, args=""):
    return AppEntry(id=stable_app_id(name), name=name, launch_type="shortcut", target=f"C:\s\{name}.lnk",
                    source="start_menu_user", exe_path=exe, arguments=args)


class MatchRunningTests(unittest.TestCase):
    def test_plain_app_is_detected(self):
        chrome = app("Chrome", r"C:\c\chrome.exe")
        self.assertEqual(match_running([chrome], {os.path.normcase(r"C:\c\chrome.exe"): 42}), {chrome.id: 42})

    def test_apps_sharing_an_executable_are_never_reported(self):
        python = app("Python", r"C:\p\python.exe")
        docs = app("Python Docs", r"C:\p\python.exe", "-m pydoc -b")
        running = {os.path.normcase(r"C:\p\python.exe"): 7}
        self.assertEqual(match_running([python, docs], running), {})

    def test_shortcut_with_arguments_is_ambiguous(self):
        pwa = app("Some PWA", r"C:\c\chrome.exe", "--app-id=abc")
        self.assertEqual(match_running([pwa], {os.path.normcase(r"C:\c\chrome.exe"): 1}), {})

    def test_own_process_is_excluded(self):
        import sys

        self.assertNotIn(os.path.normcase(sys.executable), [k for k, v in running_executables().items() if v == os.getpid()])
        self.assertNotIn(os.getpid(), running_executables().values())


if __name__ == "__main__":
    unittest.main()
