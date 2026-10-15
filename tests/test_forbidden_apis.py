"""Guards for the safety promises: CIRANA arranges windows only with documented, non-invasive calls.

Scans CIRANA's source code (names and exact string literals, so explanations in comments and docstrings are fine)
for APIs it must never use: re-parenting and style changes, attaching input queues, synthetic input, code injection,
hooks that load into other processes, hiding taskbar buttons, and ending processes (allowed only in the one-time
clean-up after the old embedding versions, after two explicit confirmations).
"""
import io
import tokenize
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "cirana"
FORBIDDEN = {
    "SetParent", "SetWindowLong", "SetWindowLongW", "SetWindowLongPtr", "SetWindowLongPtrW", "SetClassLongPtrW",
    "AttachThreadInput", "SendInput", "keybd_event", "mouse_event", "CreateRemoteThread", "WriteProcessMemory",
    "VirtualAllocEx", "SetWindowsHookEx", "SetWindowsHookExW", "DeleteTab", "TerminateProcess", "NtSuspendProcess",
    "EndTask",
}
TERMINATING = {"terminate", "kill", "TerminateProcess"}
MAY_TERMINATE = {"legacy_recovery.py"}  # ends a windowless orphan only after the user ticked it and confirmed twice


def tokens(path: Path):
    source = path.read_text(encoding="utf-8")
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.NAME:
            yield token.string, token.start[0]
        elif token.type == tokenize.STRING:
            literal = token.string.strip("rbuRBU").strip("\"'")
            if literal.isidentifier():
                yield literal, token.start[0]


class ForbiddenApiTests(unittest.TestCase):
    def test_no_invasive_window_or_process_api_is_used(self):
        found = []
        for path in ROOT.rglob("*.py"):
            for name, line in tokens(path):
                if name in FORBIDDEN:
                    found.append(f"{path.relative_to(ROOT)}:{line} {name}")
        self.assertEqual(found, [])

    def test_processes_are_only_ended_by_the_legacy_clean_up(self):
        found = []
        for path in ROOT.rglob("*.py"):
            if path.name in MAY_TERMINATE:
                continue
            for name, line in tokens(path):
                if name in TERMINATING:
                    found.append(f"{path.relative_to(ROOT)}:{line} {name}")
        self.assertEqual(found, [])

    def test_win_event_hooks_are_out_of_context(self):
        source = (ROOT / "wm" / "tracker.py").read_text(encoding="utf-8")
        self.assertIn("WINEVENT_OUTOFCONTEXT", source)
        self.assertNotIn("WINEVENT_INCONTEXT", source)  # in-context hooks load a DLL into other processes

    def test_moves_are_posted_never_sent(self):
        from cirana.native import win32_api as w
        from cirana.wm import backend

        self.assertTrue(backend.MOVE_FLAGS & w.SWP_ASYNCWINDOWPOS)
        self.assertTrue(backend.MOVE_FLAGS & w.SWP_NOACTIVATE)
        self.assertFalse(hasattr(w, "SetParent") or hasattr(w, "SetWindowLongPtrW"))


if __name__ == "__main__":
    unittest.main()
