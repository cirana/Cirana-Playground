"""Phase 5: the one-time clean-up of playground_session.json left by the embedding versions.

Every Win32 / shell call is faked: no real window, taskbar button or process is touched.
"""
import json
import os
import unittest
from unittest import mock

import psutil

from cirana.core import legacy_recovery as legacy
from tests.support import TempHomeTestCase

OWN = {"pid": os.getpid(), "created": psutil.Process().create_time()}


class LegacyRecoveryTests(TempHomeTestCase):
    def write(self, embedded=(), docked=(), raw=None):
        payload = raw if raw is not None else {"version": 1, "data": {"embedded": list(embedded),
                                                                       "docked": list(docked)}}
        (self.home / legacy.FILE_NAME).write_text(json.dumps(payload), encoding="utf-8")
        (self.home / (legacy.FILE_NAME + ".bak")).write_text("{}", encoding="utf-8")

    def test_nothing_to_do_without_the_file(self):
        self.assertEqual(legacy.recover(self.home), [])

    def test_empty_file_is_removed(self):
        self.write()
        self.assertEqual(legacy.recover(self.home), [])
        self.assertFalse((self.home / legacy.FILE_NAME).exists())
        self.assertFalse((self.home / (legacy.FILE_NAME + ".bak")).exists())

    def test_windowless_orphans_are_listed_never_ended(self):
        entry = {"pid": 4242, "created": 10.0, "title": "Character Map", "exe": "c:\\windows\\charmap.exe",
                 "owner": {"pid": 999999, "created": 1.0}}
        alive = mock.Mock()
        alive.name.return_value = "charmap.exe"
        self.write(embedded=[entry, {"pid": "junk"}])
        with mock.patch.object(legacy, "_alive_with_identity", side_effect=lambda pid, c: alive if pid == 4242 else None), \
                mock.patch.object(legacy, "_has_visible_window", return_value=False):
            orphans = legacy.recover(self.home)
        self.assertEqual([(o.pid, o.title) for o in orphans], [(4242, "Character Map")])
        alive.terminate.assert_not_called()  # only listed: ending one is the user's decision
        self.assertFalse((self.home / legacy.FILE_NAME).exists())

    def test_ending_an_orphan_rechecks_its_identity(self):
        orphan = legacy.Orphan(4242, "Character Map", "", 10.0)
        with mock.patch.object(legacy, "_alive_with_identity", return_value=None):
            self.assertIn("no longer running", legacy.end_orphan(orphan))
        proc = mock.Mock()
        with mock.patch.object(legacy, "_alive_with_identity", return_value=proc), \
                mock.patch.object(legacy, "_has_visible_window", return_value=True):
            self.assertIn("has a window again", legacy.end_orphan(orphan))
        proc.terminate.assert_not_called()

    def test_docked_windows_get_their_taskbar_button_back(self):
        docked = [{"hwnd": 0x500, "pid": 77, "created": 5.0, "owner": {"pid": 999999, "created": 1.0}},
                  {"hwnd": 0x600, "pid": 78, "created": 5.0},  # its handle now belongs to another process
                  {"hwnd": 0x700, "pid": 79, "created": 5.0, "owner": OWN}]  # recorded by this very process
        self.write(docked=docked)
        pids = {0x500: 77, 0x600: 1234, 0x700: 79}
        with mock.patch.object(legacy.w, "IsWindow", return_value=True), \
                mock.patch.object(legacy.w, "IsWindowVisible", return_value=True), \
                mock.patch.object(legacy.w, "window_pid", side_effect=lambda h: (pids[h], 1)), \
                mock.patch.object(legacy, "_alive_with_identity", side_effect=lambda pid, c: mock.Mock()
                                  if pid in (77, 78, 79) else None), \
                mock.patch.object(legacy, "_add_tabs", return_value=1) as add_tabs:
            legacy.recover(self.home)
        add_tabs.assert_called_once_with([0x500, 0x700])

    def test_file_is_kept_while_the_cirana_that_wrote_it_still_runs(self):
        self.write(embedded=[{"pid": 1, "created": 1.0, "owner": {"pid": 31337, "created": 2.0}}])
        with mock.patch.object(legacy, "_alive_with_identity", side_effect=lambda pid, c: mock.Mock()
                               if pid == 31337 else None):
            self.assertEqual(legacy.recover(self.home), [])
        self.assertTrue((self.home / legacy.FILE_NAME).exists())

    def test_damaged_file_is_harmless(self):
        (self.home / legacy.FILE_NAME).write_text("{not json", encoding="utf-8")
        self.assertEqual(legacy.recover(self.home), [])
        self.assertTrue((self.home / legacy.FILE_NAME).exists())  # left for a human to look at
        self.write(raw={"version": 1, "data": {"embedded": "x", "docked": [1, None]}})
        self.assertEqual(legacy.recover(self.home), [])


if __name__ == "__main__":
    unittest.main()
