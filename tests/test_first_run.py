"""Start-up defaults: a new installation tiles every window with the adaptive engine by itself, a new workspace is
adaptive and dynamic, nothing is arranged before the first window inventory is in - and nobody who used CIRANA
before is touched: their switches, layouts and workspaces are read, never rewritten."""
import json
import tempfile
import unittest
from pathlib import Path

from cirana.core.models import stable_app_id
from cirana.storage.settings_manager import SettingsManager
from cirana.wm.layout import ADAPTIVE, AUTO, GRID, MAIN_SIDE, SIDE_BY_SIDE, LayoutSpec
from tests.test_dynamic_tiling import TilingEnv
from tests.test_workspace_controller import LEFT, Env
from tests.support import TempHomeTestCase


def write(home, name, data):
    (Path(home) / name).write_text(json.dumps(data), encoding="utf-8")


class SettingsDefaultsTests(TempHomeTestCase):
    def test_a_new_installation_starts_with_tiling_on_and_the_adaptive_layout(self):
        settings = SettingsManager(self.home)
        self.assertTrue(settings.fresh)
        self.assertFalse(settings.get("global_tiling"))  # (the plain default: tests and tools are not first runs)
        self.assertTrue(settings.apply_first_run_defaults())
        self.assertTrue(settings.get("global_tiling"))
        self.assertEqual(settings.get("global_layout"), {})  # nothing stored = the adaptive engine
        self.assertFalse(settings.apply_first_run_defaults())  # once

    def test_the_next_start_is_not_a_first_run_and_an_off_switch_stays_off(self):
        SettingsManager(self.home).apply_first_run_defaults()
        again = SettingsManager(self.home)
        self.assertFalse(again.fresh)
        self.assertTrue(again.get("global_tiling"))
        again.set("global_tiling", False)  # the user turned it off
        for _ in range(2):  # and starts CIRANA again, twice
            later = SettingsManager(self.home)
            self.assertFalse(later.apply_first_run_defaults())
            self.assertFalse(later.get("global_tiling"))

    def test_an_old_configuration_is_never_changed(self):
        cases = {
            "off": {"global_tiling": False},
            "off with a layout": {"global_tiling": False, "global_layout": {"kind": "main_side", "gap": 4}},
            "no tiling keys at all (an early version)": {"last_page": "apps", "view_mode": "list"},
            "on": {"global_tiling": True, "global_layout": {"kind": "grid"}},
        }
        for name, config in cases.items():
            with self.subTest(name):
                home = tempfile.mkdtemp(prefix="cirana-old-")
                write(home, "config.json", {"version": 1, "data": config})
                settings = SettingsManager(Path(home))
                self.assertFalse(settings.fresh)
                self.assertFalse(settings.apply_first_run_defaults())
                self.assertEqual(settings.get("global_tiling"), config.get("global_tiling", False))
                self.assertEqual(settings.get("global_layout"), config.get("global_layout", {}))

    def test_any_earlier_data_file_marks_an_old_installation(self):
        for name in ("workspaces.json", "applications.json"):
            with self.subTest(name):
                home = tempfile.mkdtemp(prefix="cirana-old-")
                write(home, name, {"version": 2, "data": {"workspaces": [], "applications": []}})
                settings = SettingsManager(Path(home))
                self.assertFalse(settings.fresh)
                self.assertFalse(settings.apply_first_run_defaults())
                self.assertFalse(settings.get("global_tiling"))

    def test_damaged_configuration_is_still_an_old_installation(self):
        (Path(self.home) / "config.json").write_text("{not json", encoding="utf-8")
        settings = SettingsManager(self.home)
        self.assertFalse(settings.fresh)
        self.assertFalse(settings.apply_first_run_defaults())


class TilerDefaultsTests(TilingEnv):
    def test_the_default_layout_is_adaptive_and_a_stored_choice_wins(self):
        self.assertEqual(self.tiler.global_layout().kind, ADAPTIVE)
        for kind in (SIDE_BY_SIDE, MAIN_SIDE, GRID, AUTO):
            self.settings.set("global_layout", LayoutSpec(kind, gap=3).to_dict())
            self.assertEqual(self.tiler.global_layout().kind, kind)
        self.settings.set("global_layout", {})
        self.assertEqual(self.tiler.global_layout().kind, ADAPTIVE)

    def test_a_new_installation_tiles_by_itself_after_the_first_inventory(self):
        self.settings.set("global_tiling", True)  # what apply_first_run_defaults does
        self.tiler._on_scope_changed()
        self.assertTrue(self.tiler.in_charge())
        self.assertEqual(self.tiler.global_layout().kind, ADAPTIVE)
        self.settle()
        baseline = self.tiler.count
        windows = [self.desktop.add(f"c:\\apps\\a{i}.exe", f"A{i}", rect=(10 + i, 10, 400, 300)) for i in range(4)]
        self.tracker.ready = False  # the window tracker has not finished its first pass
        self.tracker.feed_snapshot([])  # (re-arms nothing: ready is set by a full pass with windows)
        self.tracker.ready = False
        for window in windows:
            self.tracker.feed(window.hwnd, self.desktop.describe(window.hwnd))  # the start-up burst, piece by piece
        self.settle(timeout=0.5)
        self.assertEqual(self.tiler.count, baseline)  # nothing arranged from a half-known desktop
        self.assertTrue(all(w.rect[2] < 500 for w in windows))
        self.sync()  # the first complete inventory
        self.assertTrue(self.tracker.ready)
        self.assertTrue(self.settle(timeout=3.0))
        self.assertEqual(self.tiler.count, baseline + 1)  # the whole burst is one re-tile
        rects = [w.rect for w in windows]
        self.assertEqual(len(set(rects)), 4)
        self.assertTrue(all(LEFT.work[0] <= r[0] and r[2] <= LEFT.work[2] for r in rects))

    def test_global_tiling_keeps_priority_over_a_dynamic_workspace(self):
        self.settings.set("global_tiling", True)
        self.tiler._on_scope_changed()
        ws = self.tiled_workspace("Code", "Notes", kind=ADAPTIVE)
        self.assertFalse(self.tiler.manages(ws.id))
        code, notes, game = self.open("Code"), self.open("Notes"), self.open("Game")
        self.settle()
        self.assertEqual({tuple(g.id for g in self.tiler.shown_groups())}, {(LEFT.device,)})  # one group: the global one
        self.assertEqual(len({code.rect, notes.rect, game.rect}), 3)


class NewWorkspaceTests(Env):
    def test_a_new_workspace_is_adaptive_and_dynamic_and_does_nothing_by_itself(self):
        window = self.desktop.add("c:\\apps\\code.exe", "Code", rect=(5, 5, 300, 300))
        self.sync()
        calls = len(self.desktop.calls)
        workspace = self.manager.create("Fresh", [stable_app_id("Code"), stable_app_id("Notes")])
        layout = self.manager.get(workspace.id).layout
        self.assertEqual((layout.kind, layout.dynamic), (ADAPTIVE, True))
        self.pump(timeout=0.2)
        self.assertEqual(self.launches, [])  # nothing started
        self.assertEqual(len(self.desktop.calls), calls)  # nothing moved
        self.assertEqual(window.rect, (5, 5, 300, 300))
        self.assertEqual(self.manager.active_id, "")  # and not activated
        again = type(self.manager)(self.settings, self.registry, monitors=self.desktop.monitors)
        self.assertEqual((again.get(workspace.id).layout.kind, again.get(workspace.id).layout.dynamic),
                         (ADAPTIVE, True))

    def test_stored_workspaces_keep_what_they_have(self):
        records = []
        for index, layout in enumerate([
                {"kind": "grid", "gap": 8, "ratio": 0.6, "columns": 0, "weights": [], "monitor": None, "free": {}},
                {"kind": "grid", "dynamic": False, "gap": 8},
                {"kind": "bsp", "dynamic": True},
                {"kind": "side_by_side", "dynamic": False},
                {"kind": "auto", "dynamic": True},
                {"kind": "free"},
                None]):
            made = self.manager.create(f"Old{index}", [stable_app_id("Code")])
            records.append(dict(made.to_dict(), layout=layout))
        self.settings.workspaces_store.save({"workspaces": records})
        again = type(self.manager)(self.settings, self.registry, monitors=self.desktop.monitors)
        got = [(w.layout.kind, w.layout.dynamic) for w in again.all()]
        self.assertEqual(got, [("grid", False), ("grid", False), ("bsp", True), ("side_by_side", False),
                               ("auto", True), ("free", False), ("grid", False)])  # a missing field is "off", not new

    def test_restarts_do_not_change_a_workspace_the_user_set_up(self):
        workspace = self.manager.create("Mine", [stable_app_id("Code")])
        self.manager.set_layout(workspace.id, LayoutSpec(SIDE_BY_SIDE, gap=2, dynamic=False))
        for _ in range(2):
            again = type(self.manager)(self.settings, self.registry, monitors=self.desktop.monitors)
            layout = again.get(workspace.id).layout
            self.assertEqual((layout.kind, layout.dynamic, layout.gap), (SIDE_BY_SIDE, False, 2))


if __name__ == "__main__":
    unittest.main()
