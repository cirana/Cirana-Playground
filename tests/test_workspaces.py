"""Phase 3 / 5: the workspace data model, its storage, and the migration of the Playground-era (version 1) file."""
import json
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from cirana.core.application_registry import ApplicationRegistry  # noqa: E402
from cirana.core.models import AppEntry, stable_app_id  # noqa: E402
from cirana.core.workspaces import WorkspaceManager, legacy_entry_id  # noqa: E402
from cirana.storage.settings_manager import SettingsManager  # noqa: E402
from cirana.wm.identity import MatchRule, TrackedWindow  # noqa: E402
from cirana.wm.layout import FREE, GRID, MAIN_SIDE, FreeSlot, LayoutSpec  # noqa: E402
from cirana.wm.monitors import Monitor  # noqa: E402
from tests.support import TempHomeTestCase  # noqa: E402

app = QApplication.instance() or QApplication([])
MONITOR = Monitor("\\\\.\\DISPLAY1", (0, 0, 2000, 1000), (0, 0, 2000, 1000), True, 96)


def exe_app(name):
    path = f"C:\\Tools\\{name}.exe"
    return AppEntry(id=stable_app_id(name), name=name, launch_type="executable", target=path, source="user",
                    exe_path=path)


def window(exe, title="Window", hwnd=0x10, pid=5):
    return TrackedWindow(hwnd, pid, 1.0, exe.lower(), "", title, "Cls")


class ManagerTests(TempHomeTestCase):
    def setUp(self):
        super().setUp()
        self.monitors = [MONITOR]
        self.build()

    def build(self):
        self.settings = SettingsManager(self.home)
        self.registry = ApplicationRegistry(self.settings)
        self.registry.apply_scan([exe_app("Alpha"), exe_app("Beta")])
        self.ws = WorkspaceManager(self.settings, self.registry, monitors=lambda: self.monitors)

    def test_create_rename_delete(self):
        work = self.ws.create("  Work   Stuff ")
        self.assertEqual(work.name, "Work Stuff")
        self.assertEqual(self.ws.create("work stuff").name, "work stuff 2")  # never two identical names
        self.ws.rename(work.id, "Deep Work")
        self.assertEqual(self.ws.get(work.id).name, "Deep Work")
        other = self.ws.create("Play")
        with self.assertRaises(ValueError):
            self.ws.rename(other.id, "deep work")
        for bad in ("", "   ", "x" * 200):
            with self.assertRaises(ValueError):
                self.ws.create(bad)
        self.ws.delete(work.id)
        self.assertIsNone(self.ws.get(work.id))

    def test_catalog_apps_are_deduplicated_but_windows_of_one_app_are_separate(self):
        w = self.ws.create("W")
        self.assertIsNotNone(self.ws.add_app(w.id, stable_app_id("Alpha")))
        self.assertIsNone(self.ws.add_app(w.id, stable_app_id("Alpha")))  # dragged twice: once
        self.assertIsNone(self.ws.add_app(w.id, "app-unknown"))
        first = self.ws.add_window(w.id, window("C:\\Tools\\Beta.exe", "Beta - one"))
        second = self.ws.add_window(w.id, window("C:\\Tools\\Beta.exe", "Beta - two", hwnd=0x20))
        self.assertNotEqual(first.id, second.id)
        self.assertEqual((first.app_id, first.hint_title), (stable_app_id("Beta"), "Beta - one"))  # recognised
        self.assertEqual(len(self.ws.get(w.id).apps), 3)

    def test_window_without_a_catalog_application(self):
        w = self.ws.create("W")
        entry = self.ws.add_window(w.id, window("D:\\Portable\\tool.exe", "Tool"))
        self.assertEqual((entry.app_id, entry.name, entry.launch), ("", "Tool", False))
        self.assertTrue(self.ws.rule_for(entry).matches(window("D:\\Portable\\tool.exe", "Other title")))

    def test_order_launch_flag_and_removal(self):
        w = self.ws.create("W", [stable_app_id("Alpha"), stable_app_id("Beta")])
        alpha, beta = self.ws.get(w.id).apps
        self.ws.move_entry(w.id, beta.id, 0)
        self.assertEqual([a.app_id for a in self.ws.get(w.id).apps], [stable_app_id("Beta"), stable_app_id("Alpha")])
        self.ws.move_entry(w.id, beta.id, 99)  # clamped
        self.assertEqual(self.ws.get(w.id).apps[-1].id, beta.id)
        self.ws.set_launch(w.id, alpha.id, False)
        self.assertFalse(self.ws.get(w.id).entry(alpha.id).launch)
        self.ws.save_positions(w.id, {alpha.id: FreeSlot((0.1, 0.1, 0.5, 0.5), MONITOR.ref())})
        self.ws.remove_entry(w.id, alpha.id)
        self.assertEqual([a.id for a in self.ws.get(w.id).apps], [beta.id])
        self.assertNotIn(alpha.id, self.ws.get(w.id).layout.free)

    def test_everything_survives_a_restart_and_no_handle_or_pid_is_stored(self):
        w = self.ws.create("Dev", [stable_app_id("Alpha")])
        entry = self.ws.add_window(w.id, window("C:\\Tools\\Beta.exe", "Beta"))
        self.ws.set_layout(w.id, LayoutSpec(MAIN_SIDE, gap=12, ratio=0.7, monitor=MONITOR.ref()))
        self.ws.save_positions(w.id, {entry.id: FreeSlot((0, 0, 0.5, 1), MONITOR.ref(), True)})
        self.ws.set_active(w.id)
        self.build()
        again = self.ws.get(w.id)
        self.assertEqual(self.ws.active_id, w.id)
        self.assertEqual(again.layout.kind, FREE)
        self.assertEqual(again.layout.ratio, 0.7)
        self.assertTrue(again.layout.free[entry.id].maximized)
        self.assertEqual([a.id for a in again.apps], [a.id for a in w.apps])
        raw = (self.home / "workspaces.json").read_text(encoding="utf-8")
        self.assertNotIn("hwnd", raw)
        self.assertNotIn('"pid"', raw)
        self.assertEqual(json.loads(raw)["version"], 2)

    def test_learning_the_program_that_shows_a_window(self):
        shell = AppEntry(id=stable_app_id("PowerShell"), name="PowerShell", launch_type="shortcut",
                         target="C:\\x\\PowerShell.lnk", source="start_menu_user",
                         exe_path="C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe")
        self.registry.apply_scan([exe_app("Alpha"), shell])
        w = self.ws.create("Shell", [shell.id])
        entry = self.ws.get(w.id).apps[0]
        terminal = window("C:\\Program Files\\WindowsApps\\Microsoft.WindowsTerminal_1_x64__8wekyb3d8bbwe\\"
                          "WindowsTerminal.exe", "Windows PowerShell")
        self.assertFalse(self.ws.rule_for(entry).matches(terminal))
        self.ws.remember_window(w.id, entry.id, terminal)
        self.build()
        entry = self.ws.get(w.id).apps[0]
        self.assertTrue(self.ws.rule_for(entry).matches(terminal))  # recognised after a restart
        self.assertEqual(entry.hint_title, "Windows PowerShell")

    def test_deleting_the_active_workspace_clears_it(self):
        w = self.ws.create("W")
        seen = []
        self.ws.active_changed.connect(seen.append)
        self.ws.set_active(w.id)
        self.ws.delete(w.id)
        self.assertEqual(self.ws.active_id, "")
        self.assertEqual(seen, [w.id, ""])
        self.assertEqual(self.settings.get("active_workspace"), "")

    def test_damaged_records_are_dropped_one_by_one(self):
        good = self.ws.create("Good", [stable_app_id("Alpha")])
        self.settings.workspaces_store.save({"workspaces": [
            good.to_dict(), "junk", {"id": "x"}, {"id": "ws-2", "name": "  "},
            {"id": "ws-3", "name": "Odd", "apps": [{"id": ""}, {"id": "e-1"}, {"id": "e-2", "app_id": "a"}, 5,
                                                   {"id": "e-3", "rule": {"exe": "c:\\x.exe"}, "launch": "yes"},
                                                   {"id": "e-2", "app_id": "dup"}],
             "layout": {"kind": "spiral", "gap": -5, "free": {"e-2": {"fraction": [0, 0, 0, 0]}}}},
            {"id": "ws-4", "name": 2024, "apps": None, "layout": [1], "created": float("nan")},
        ]})
        self.settings.set("active_workspace", ["not", "a", "string"])
        self.build()  # must not raise
        self.assertEqual([w.id for w in self.ws.all()], [good.id, "ws-3", "ws-4"])
        odd = self.ws.get("ws-3")
        self.assertEqual([(a.id, a.app_id, a.launch) for a in odd.apps], [("e-2", "a", True), ("e-3", "", True)])
        self.assertEqual((odd.layout.kind, odd.layout.gap, odd.layout.free), (GRID, 0, {}))
        self.assertEqual((self.ws.get("ws-4").name, self.ws.get("ws-4").apps, self.ws.get("ws-4").created),
                         ("2024", [], 0.0))
        self.assertEqual(self.ws.active_id, "")

    def test_records_from_before_the_adaptive_engine_keep_their_layout(self):
        old = {"kind": "main_side", "gap": 12, "ratio": 0.7, "columns": 0, "weights": [], "monitor": None, "free": {},
               "dynamic": True, "heights": [], "bsp": {}, "auto": {"2": "side_by_side", "3": "main_side", "4": "grid"},
               "stacks": []}  # no grid proportions, no "adaptive"
        kept = self.ws.create("Kept", [stable_app_id("Alpha")])
        auto = self.ws.create("Auto", [stable_app_id("Alpha")])
        self.settings.workspaces_store.save({"workspaces": [
            dict(kept.to_dict(), layout=old), dict(auto.to_dict(), layout=dict(old, kind="auto"))]})
        self.build()
        self.assertEqual((self.ws.get(kept.id).layout.kind, self.ws.get(kept.id).layout.ratio,
                          self.ws.get(kept.id).layout.dynamic), ("main_side", 0.7, True))
        self.assertEqual(self.ws.get(auto.id).layout.kind, "auto")  # never turned into the adaptive engine
        self.assertEqual(self.ws.get(kept.id).layout.grid_cols, [])
        self.ws.set_layout(kept.id, LayoutSpec("adaptive", gap=4, dynamic=True))
        self.build()
        self.assertEqual(self.ws.get(kept.id).layout.kind, "adaptive")  # and the new kind survives a restart

    def test_missing_application_keeps_its_stored_name(self):
        w = self.ws.create("W", [stable_app_id("Alpha")])
        self.registry.apply_scan([])  # Alpha vanished from the system
        item = self.ws.get(w.id).apps[0]
        self.assertEqual(self.ws.display_name(item), "Alpha")
        self.assertIsNone(self.ws.resolve(item))


class MigrationTests(TempHomeTestCase):
    """Version 1 = the Playground design: tab / own-window modes, tab order, absolute window rectangles."""

    V1 = {"version": 1, "data": {"workspaces": [
        {"id": "ws-a", "name": "Daily", "created": 1.0, "updated": 2.0,
         "apps": [{"app_id": "app-1", "name": "One", "mode": "external"},
                  {"app_id": "app-2", "name": "Two", "mode": "embedded"},
                  {"app_id": "app-3", "name": "Three", "mode": "embedded"},
                  {"app_id": "app-1", "name": "dup"}],
         "layout": {"tab_order": ["app-3", "app-2"], "active": "app-3", "saved_at": 5,
                    "windows": {"app-1": {"rect": [100, 50, 1100, 550], "maximized": True},
                                "app-2": {"rect": [-9000, -9000, -8000, -8500]}}}},
        {"id": "ws-b", "name": "Empty", "apps": [], "layout": None},
        {"id": "ws-c", "name": "Broken", "apps": "nope", "layout": {"windows": "x", "tab_order": 7}},
    ]}}

    def write(self, payload):
        (self.home / "workspaces.json").write_text(json.dumps(payload), encoding="utf-8")

    def load(self, monitors):
        settings = SettingsManager(self.home)
        registry = ApplicationRegistry(settings)
        return settings, WorkspaceManager(settings, registry, monitors=monitors)

    def test_version_1_is_migrated_without_losing_workspaces(self):
        self.write(self.V1)
        _settings, ws = self.load(lambda: [MONITOR])
        self.assertEqual([w.name for w in ws.all()], ["Daily", "Empty", "Broken"])
        daily = ws.get("ws-a")
        # the tab order becomes the entry order; the mode is gone; ids are stable and derived from the app
        self.assertEqual([a.app_id for a in daily.apps], ["app-3", "app-2", "app-1"])
        self.assertEqual(daily.apps[2].id, legacy_entry_id("app-1"))
        self.assertEqual((daily.created, daily.updated > 0), (1.0, True))
        # saved rectangles became monitor-relative positions; one that is on no monitor is dropped
        self.assertEqual(daily.layout.kind, FREE)
        slot = daily.layout.free[legacy_entry_id("app-1")]
        self.assertEqual((slot.fraction, slot.maximized, slot.monitor.device), ((0.05, 0.05, 0.5, 0.5), True,
                                                                                MONITOR.device))
        self.assertNotIn(legacy_entry_id("app-2"), daily.layout.free)
        self.assertEqual(ws.get("ws-b").layout.kind, GRID)
        self.assertEqual(ws.get("ws-c").apps, [])
        saved = json.loads((self.home / "workspaces.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["version"], 2)
        self.assertNotIn("legacy", json.dumps(saved))
        backup = json.loads((self.home / "workspaces.v1.json").read_text(encoding="utf-8"))
        self.assertEqual(backup, self.V1)  # the original, untouched

    def test_positions_wait_for_the_monitors_when_they_cannot_be_read(self):
        self.write(self.V1)

        def broken():
            raise OSError("no display")

        _settings, ws = self.load(broken)
        self.assertEqual(ws.get("ws-a").layout.free, {})
        self.assertTrue(ws.get("ws-a").legacy)  # kept for the next start
        ws.rename("ws-a", "Daily work")  # saving keeps them too
        _settings, ws = self.load(lambda: [MONITOR])
        self.assertIn(legacy_entry_id("app-1"), ws.get("ws-a").layout.free)

    def test_a_workspace_that_cannot_be_migrated_does_not_cost_the_others(self):
        payload = json.loads(json.dumps(self.V1))
        payload["data"]["workspaces"].insert(0, {"id": "ws-x", "name": "X", "apps": [{"app_id": "a"}],
                                                 "layout": {"tab_order": "abc"}})
        payload["data"]["workspaces"].insert(0, 12)
        self.write(payload)
        _settings, ws = self.load(lambda: [MONITOR])
        self.assertIn("Daily", [w.name for w in ws.all()])

    def test_rules_are_derived_from_the_catalog_after_migration(self):
        self.write(self.V1)
        settings = SettingsManager(self.home)
        registry = ApplicationRegistry(settings)
        registry.apply_scan([AppEntry(id="app-1", name="One", launch_type="executable", target="C:\\One\\one.exe",
                                      source="user", exe_path="C:\\One\\one.exe")])
        ws = WorkspaceManager(settings, registry, monitors=lambda: [MONITOR])
        entry = ws.get("ws-a").entry(legacy_entry_id("app-1"))
        self.assertEqual(entry.rule, MatchRule())
        self.assertTrue(ws.rule_for(entry).matches(window("C:\\One\\one.exe")))


if __name__ == "__main__":
    unittest.main()
