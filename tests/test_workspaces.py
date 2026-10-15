"""Workspaces: storage, CRUD, one-click opening (with duplicate prevention), saved sessions and the page UI."""
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QByteArray, QMimeData  # noqa: E402

from cirana.core import playground, workspace_launcher  # noqa: E402
from cirana.core.application_registry import ApplicationRegistry  # noqa: E402
from cirana.core.models import AppEntry, LaunchResult, stable_app_id  # noqa: E402
from cirana.core.playground import SessionState  # noqa: E402
from cirana.core.workspaces import MODE_EMBEDDED, MODE_EXTERNAL, SavedLayout, WorkspaceManager  # noqa: E402
from cirana.native import window_embedder as embedder  # noqa: E402
from cirana.native.window_discovery import WindowInfo  # noqa: E402
from cirana.storage.settings_manager import SettingsManager  # noqa: E402
from cirana.widgets.dnd import APP_MIME  # noqa: E402
from tests.support import TempHomeTestCase  # noqa: E402


def exe_app(name):
    path = f"C:\\Tools\\{name}.exe"
    return AppEntry(id=stable_app_id(name), name=name, launch_type="executable", target=path, source="user",
                    exe_path=path)


def unhostable_app(name):
    # a shell item (no package, no executable): CIRANA cannot tell which window is its window
    return AppEntry(id=stable_app_id(name), name=name, launch_type="aumid", target=f"Shell.{name}", source="packaged")


class ManagerTests(TempHomeTestCase):
    def setUp(self):
        super().setUp()
        self.build()

    def build(self):
        self.settings = SettingsManager(self.home)
        self.registry = ApplicationRegistry(self.settings)
        self.registry.apply_scan([exe_app("Alpha"), exe_app("Beta"), unhostable_app("Gamma")])
        self.ws = WorkspaceManager(self.settings, self.registry)

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
        with self.assertRaises(ValueError):
            self.ws.rename(work.id, "Ghost")

    def test_applications_are_deduplicated_and_modes_default_sensibly(self):
        w = self.ws.create("W")
        self.assertTrue(self.ws.add_app(w.id, stable_app_id("Alpha")))
        self.assertFalse(self.ws.add_app(w.id, stable_app_id("Alpha")))  # already there
        self.assertFalse(self.ws.add_app(w.id, "app-unknown"))
        self.registry.set_launch_mode(stable_app_id("Beta"), MODE_EMBEDDED)  # the app's own preference wins
        self.registry.set_launch_mode(stable_app_id("Gamma"), MODE_EMBEDDED)  # ...unless it cannot be hosted at all
        self.ws.add_app(w.id, stable_app_id("Beta"))
        self.ws.add_app(w.id, stable_app_id("Gamma"))
        modes = {a.name: a.mode for a in self.ws.get(w.id).apps}
        self.assertEqual(modes, {"Alpha": MODE_EXTERNAL, "Beta": MODE_EMBEDDED, "Gamma": MODE_EXTERNAL})

    def test_mode_order_and_removal(self):
        w = self.ws.create("W", [stable_app_id("Alpha"), stable_app_id("Beta")])
        alpha, beta = stable_app_id("Alpha"), stable_app_id("Beta")
        self.ws.set_mode(w.id, alpha, MODE_EXTERNAL)
        with self.assertRaises(ValueError):
            self.ws.set_mode(w.id, alpha, "floating")
        self.ws.move_app(w.id, beta, -1)
        self.ws.move_app(w.id, beta, -1)  # already first: no-op
        self.assertEqual([a.app_id for a in self.ws.get(w.id).apps], [beta, alpha])
        self.ws.remove_app(w.id, beta)
        self.assertEqual([a.app_id for a in self.ws.get(w.id).apps], [alpha])

    def test_everything_survives_a_restart(self):
        w = self.ws.create("Persisted", [stable_app_id("Alpha")])
        self.ws.set_mode(w.id, stable_app_id("Alpha"), MODE_EXTERNAL)
        self.ws.save_layout(w.id, SavedLayout(["a"], "a", {"a": {"rect": [1, 2, 3, 4], "maximized": True}}))
        self.ws.set_active(w.id)
        self.build()
        again = self.ws.get(w.id)
        self.assertEqual(again.name, "Persisted")
        self.assertEqual(again.apps[0].mode, MODE_EXTERNAL)
        self.assertEqual(again.layout.windows["a"], {"rect": [1, 2, 3, 4], "maximized": True})
        self.assertGreater(again.layout.saved_at, 0)
        self.assertEqual(self.ws.active_id, w.id)

    def test_deleting_the_active_workspace_clears_it(self):
        w = self.ws.create("W")
        seen = []
        self.ws.active_changed.connect(seen.append)
        self.ws.set_active(w.id)
        self.ws.delete(w.id)
        self.assertEqual(self.ws.active_id, "")
        self.assertEqual(seen, [w.id, ""])
        self.assertEqual(self.settings.get("active_workspace"), "")

    def test_damaged_file_entries_are_dropped_not_fatal(self):
        good = self.ws.create("Good", [stable_app_id("Alpha")])
        self.settings.workspaces_store.save({"workspaces": [
            good.to_dict(), "junk", {"id": "x"}, {"id": "ws-2", "name": "  "},
            {"id": "ws-3", "name": "Odd", "apps": [{"app_id": ""}, {"app_id": "a", "mode": "weird"}, 5],
             "layout": {"windows": {"a": {"rect": [1, 2]}}, "tab_order": ["a", 3]}},
        ]})
        self.build()
        self.assertEqual([w.name for w in self.ws.all()], ["Good", "Odd"])
        odd = self.ws.get("ws-3")
        self.assertEqual([(a.app_id, a.mode) for a in odd.apps], [("a", MODE_EXTERNAL)])
        self.assertEqual(odd.layout.windows, {})
        self.assertEqual(odd.layout.tab_order, ["a"])

    def test_malformed_nested_fields_are_skipped_and_good_data_kept(self):
        good = self.ws.create("Good", [stable_app_id("Alpha")])
        self.ws.save_layout(good.id, SavedLayout([stable_app_id("Alpha")], stable_app_id("Alpha"),
                                                 {stable_app_id("Alpha"): {"rect": [1, 2, 3, 4], "maximized": False}}))
        alpha = {"app_id": "a", "name": "A", "mode": MODE_EMBEDDED}
        self.settings.workspaces_store.save({"workspaces": [
            self.ws.get(good.id).to_dict(),
            {"id": "ws-apps-null", "name": "Apps null", "apps": None},
            {"id": "ws-apps-int", "name": "Apps int", "apps": 5, "created": "yesterday", "updated": [1]},
            {"id": "ws-windows-list", "name": "Windows list", "apps": [alpha],
             "layout": {"windows": ["a"], "tab_order": None, "active": 7, "saved_at": "soon"}},
            {"id": "ws-windows-str", "name": "Windows str", "layout": {"windows": "a", "tab_order": 3}},
            {"id": "ws-tab-str", "name": "Tab str", "layout": {"tab_order": "abc", "windows": {"a": [1, 2, 3, 4]}}},
            {"id": "ws-bad-rect", "name": "Bad rect",
             "layout": {"windows": {"a": {"rect": [True, 2, 3, 4]}, "b": {"rect": "1,2,3,4"},
                                    "c": {"rect": [1, 2, 3, 4], "maximized": True}}}},
            {"id": "ws-bad-app", "name": "Bad app", "apps": [{"app_id": "a", "name": {"x": 1}, "mode": ["embedded"]},
                                                             {"app_id": ["b"]}, None]},
            {"id": "ws-layout-list", "name": "Layout list", "layout": [1, 2], "created": float("nan")},
            {"id": ["x"], "name": "Bad id"}, {"id": "ws-bad-name", "name": {"n": 1}}, {"id": "", "name": "Empty id"},
            {"id": "ws-number", "name": 2024},
        ]})
        self.settings.set("active_workspace", ["not", "a", "string"])
        self.build()  # must not raise
        self.assertEqual([w.id for w in self.ws.all()], [
            good.id, "ws-apps-null", "ws-apps-int", "ws-windows-list", "ws-windows-str", "ws-tab-str", "ws-bad-rect",
            "ws-bad-app", "ws-layout-list", "ws-number",
        ])
        self.assertEqual(self.ws.get("ws-number").name, "2024")  # loaded as before
        kept = self.ws.get(good.id)  # the intact workspace is untouched
        self.assertEqual([a.app_id for a in kept.apps], [stable_app_id("Alpha")])
        self.assertEqual(kept.layout.windows[stable_app_id("Alpha")], {"rect": [1, 2, 3, 4], "maximized": False})
        self.assertEqual(self.ws.get("ws-apps-null").apps, [])
        self.assertEqual((self.ws.get("ws-apps-int").created, self.ws.get("ws-apps-int").updated), (0.0, 0.0))
        damaged = self.ws.get("ws-windows-list")
        self.assertEqual([(a.app_id, a.mode) for a in damaged.apps], [("a", MODE_EMBEDDED)])
        self.assertEqual((damaged.layout.windows, damaged.layout.tab_order, damaged.layout.active,
                          damaged.layout.saved_at), ({}, [], "", 0.0))
        self.assertEqual(self.ws.get("ws-windows-str").layout.tab_order, [])
        self.assertEqual(self.ws.get("ws-tab-str").layout.tab_order, [])
        self.assertEqual(self.ws.get("ws-tab-str").layout.windows, {})
        self.assertEqual(self.ws.get("ws-bad-rect").layout.windows, {"c": {"rect": [1, 2, 3, 4], "maximized": True}})
        self.assertEqual([(a.app_id, a.name, a.mode) for a in self.ws.get("ws-bad-app").apps],
                         [("a", "a", MODE_EXTERNAL)])
        self.assertIsNone(self.ws.get("ws-layout-list").layout)
        self.assertEqual(self.ws.get("ws-layout-list").created, 0.0)
        self.assertEqual(self.ws.active_id, "")
        self.ws.rename("ws-apps-int", "Repaired")  # the cleaned data saves and reloads
        self.build()
        self.assertEqual(self.ws.get("ws-apps-int").name, "Repaired")

    def test_missing_application_keeps_its_stored_name(self):
        w = self.ws.create("W", [stable_app_id("Alpha")])
        self.registry.apply_scan([])  # Alpha vanished from the system
        item = self.ws.get(w.id).apps[0]
        self.assertEqual(self.ws.display_name(item), "Alpha")
        self.assertIsNone(self.ws.resolve(item))


def win(hwnd, exe, title="Window"):
    return WindowInfo(hwnd, 77, 1, title, "Cls", exe.lower())


class UiBase(unittest.TestCase):
    """One real (offscreen) CIRANA instance; Win32 embedding, launching and window enumeration are faked."""

    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.mkdtemp(prefix="cirana-ws-")
        os.environ["CIRANA_HOME"] = cls.home
        from cirana.main import build_app

        cls.app, cls.window, cls.ctx = build_app([])
        cls.ctx.registry.apply_scan([exe_app(n) for n in ("Alpha", "Beta", "Delta")] + [unhostable_app("Gamma")])
        cls.window.show()
        cls.pump()

    @classmethod
    def tearDownClass(cls):
        cls.window.pages["playground"].shutdown()
        cls.ctx.tracker.shutdown()
        cls.window.close()
        shutil.rmtree(cls.home, ignore_errors=True)

    @classmethod
    def pump(cls, seconds=0.0, until=None, timeout=8.0):
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
        self.launches, self.placed, self.fronted = [], [], []
        self.running_windows = []  # what the launcher sees as already-open windows
        self.next_hwnd = 0x2000
        self.manager._launch = self.ctx.launcher._launch = self.fake_launch
        patches = [
            mock.patch.object(embedder, "embed", side_effect=self.fake_embed),
            mock.patch.object(embedder, "detach", return_value=True),
            mock.patch.object(embedder, "is_alive", return_value=True),
            mock.patch.object(embedder, "is_still_embedded", return_value=True),
            mock.patch.object(embedder, "is_hung", return_value=False),
            mock.patch.object(embedder, "focus"),
            mock.patch.object(embedder, "set_active"),  # fake HWNDs: never post to a real window
            mock.patch.object(embedder, "has_focus", return_value=True),
            mock.patch.object(embedder, "contains_point", return_value=False),
            mock.patch.object(embedder, "bring_to_front", side_effect=lambda h: self.fronted.append(h) or True),
            mock.patch.object(playground, "snapshot_handles", return_value=set()),
            mock.patch.object(workspace_launcher, "snapshot_handles", return_value=set()),
            mock.patch.object(playground, "wait_for_app_window", side_effect=self.fake_wait),
            mock.patch.object(workspace_launcher, "wait_for_app_window", side_effect=self.fake_wait),
            mock.patch.object(workspace_launcher, "list_top_level_windows", side_effect=lambda: list(self.running_windows)),
            mock.patch.object(workspace_launcher, "place_window", side_effect=lambda *a: self.placed.append(a) or True),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.ctx.workspaces.set_active("")
        for session in list(self.manager.sessions()):
            self.view.close_tab(session.id)
        for workspace in self.ctx.workspaces.all():
            self.ctx.workspaces.delete(workspace.id)
        self.pump(0.05)

    def fake_launch(self, app):
        self.launches.append(app.name)
        return LaunchResult(True, "ok", pid=4242)

    def fake_wait(self, before, exe, pid, cancel, **kw):
        self.next_hwnd += 1
        return WindowInfo(self.next_hwnd, 4242, 1, "Fake", "Cls", exe)

    def fake_embed(self, info, host_hwnd, width, height, launched_by_cirana):
        from cirana.native.window_restorer import WindowState

        state = WindowState(info.hwnd, 0, 0, 0, (0, 0, 100, 100), False, False)
        return embedder.EmbedResult(embedder.EmbedStatus.OK), \
            embedder.EmbeddedWindow(info.hwnd, host_hwnd, state, info.pid, launched_by_cirana)

    def open_workspace(self, workspace_id):
        summaries = []
        self.ctx.launcher.finished.connect(summaries.append)
        self.assertTrue(self.ctx.launcher.open(workspace_id))
        self.assertTrue(self.pump(until=lambda: bool(summaries)))
        self.ctx.launcher.finished.disconnect(summaries.append)
        return summaries[0]


class LauncherTests(UiBase):
    def make(self, names, **modes):
        ids = [stable_app_id(n) for n in names]
        workspace = self.ctx.workspaces.create("Test", ids)
        for name in names:  # every application asks for a tab unless the test says otherwise
            self.ctx.workspaces.set_mode(workspace.id, stable_app_id(name), modes.get(name, MODE_EMBEDDED))
        return workspace

    def test_one_click_opens_tabs_and_normal_windows(self):
        workspace = self.make(["Alpha", "Beta", "Gamma"], Beta=MODE_EXTERNAL)
        summary = self.open_workspace(workspace.id)
        self.assertEqual(summary.in_tabs, ["Alpha"])
        self.assertEqual(summary.opened, ["Beta"])
        self.assertEqual([n for n, _ in summary.own_window], ["Gamma"])  # asked for a tab, but CIRANA cannot identify its window
        self.assertEqual(self.launches, ["Alpha", "Beta", "Gamma"])
        self.assertEqual(self.view.tabs.count(), 1)
        self.assertEqual(self.ctx.workspaces.active_id, workspace.id)
        self.assertIn("1 in tabs, 2 in their own window", summary.message())

    def test_tabs_opened_by_a_workspace_belong_to_it(self):
        workspace = self.make(["Alpha", "Delta"])
        self.open_workspace(workspace.id)
        self.assertTrue(all(s.workspace_id == workspace.id for s in self.manager.sessions()))
        self.ctx.workspaces.set_active("")  # switching away hides them but keeps the applications alive
        self.assertEqual(self.view.visible_session_ids(), [])
        self.assertEqual(len(self.manager.sessions()), 2)
        self.ctx.workspaces.set_active(workspace.id)
        self.assertEqual(len(self.view.visible_session_ids()), 2)

    def test_second_open_does_not_start_anything_twice(self):
        workspace = self.make(["Alpha", "Beta"], Beta=MODE_EXTERNAL)
        self.open_workspace(workspace.id)
        self.launches.clear()
        self.running_windows = [win(900, r"C:\Tools\Beta.exe")]
        summary = self.open_workspace(workspace.id)
        self.assertEqual(self.launches, [])
        self.assertCountEqual(summary.already_open, ["Alpha", "Beta"])
        self.assertEqual(self.fronted, [900])  # the running window was brought forward, not duplicated
        self.assertEqual(self.view.tabs.count(), 1)

    def test_running_window_is_adopted_into_a_tab_instead_of_started_again(self):
        workspace = self.make(["Alpha"])
        self.running_windows = [win(901, r"C:\Tools\Alpha.exe")]
        summary = self.open_workspace(workspace.id)
        self.assertEqual(self.launches, [])
        self.assertEqual(summary.in_tabs, ["Alpha"])
        self.assertFalse(self.manager.sessions()[0].launched_by_cirana)  # not CIRANA's to close

    def test_unsupported_app_wanting_a_tab_opens_in_its_own_window(self):
        workspace = self.make(["Gamma"], Gamma=MODE_EMBEDDED)
        summary = self.open_workspace(workspace.id)
        self.assertEqual(summary.in_tabs, [])
        self.assertEqual([n for n, _ in summary.own_window], ["Gamma"])
        self.assertEqual(self.view.tabs.count(), 0)

    def test_missing_and_failing_applications_do_not_stop_the_rest(self):
        workspace = self.make(["Alpha", "Beta", "Delta"], Beta=MODE_EXTERNAL)
        self.ctx.registry.apply_scan([exe_app("Alpha"), exe_app("Beta"), unhostable_app("Gamma")])  # Delta is gone
        failing = lambda app: LaunchResult(False, "boom") if app.name == "Beta" else LaunchResult(True, "ok", pid=1)  # noqa: E731
        self.manager._launch = self.ctx.launcher._launch = failing
        summary = self.open_workspace(workspace.id)
        self.assertEqual(summary.in_tabs, ["Alpha"])
        self.assertEqual(summary.missing, ["Delta"])
        self.assertEqual(summary.failed, [("Beta", "boom")])
        self.ctx.registry.apply_scan([exe_app(n) for n in ("Alpha", "Beta", "Delta")] + [unhostable_app("Gamma")])

    def test_only_one_workspace_opens_at_a_time(self):
        workspace = self.make(["Alpha", "Delta"])
        summaries = []
        self.ctx.launcher.finished.connect(summaries.append)
        self.assertTrue(self.ctx.launcher.open(workspace.id))
        self.assertFalse(self.ctx.launcher.open(workspace.id))
        self.assertTrue(self.pump(until=lambda: bool(summaries)))
        self.ctx.launcher.finished.disconnect(summaries.append)
        self.assertFalse(self.ctx.launcher.busy)

    def test_deleting_a_workspace_does_not_strand_its_tabs(self):
        workspace = self.make(["Alpha", "Delta"])
        self.open_workspace(workspace.id)
        self.assertEqual(len(self.view.visible_session_ids()), 2)
        other = self.ctx.workspaces.create("Other")
        self.ctx.workspaces.set_active(other.id)  # the doomed workspace is NOT active
        self.assertEqual(self.view.visible_session_ids(), [])
        self.assertIn("2 applications running in other workspaces", self.view.subtitle.text())
        self.ctx.workspaces.delete(workspace.id)
        self.ctx.workspaces.set_active("")
        self.assertEqual(len(self.view.visible_session_ids()), 2)  # they moved to "No workspace"
        self.assertTrue(all(self.view._pages[s.id].host.embedded is not None for s in self.manager.sessions()))
        self.assertTrue(all(s.workspace_id == "" for s in self.manager.sessions()))

    def test_deleting_the_active_workspace_keeps_its_tabs_reachable(self):
        workspace = self.make(["Alpha"])
        self.open_workspace(workspace.id)
        self.ctx.workspaces.delete(workspace.id)  # active workspace goes away -> active becomes "none"
        self.assertEqual(self.ctx.workspaces.active_id, "")
        self.assertEqual(len(self.view.visible_session_ids()), 1)

    def test_saved_session_restores_tab_order_active_tab_and_window_positions(self):
        alpha, delta, beta = (stable_app_id(n) for n in ("Alpha", "Delta", "Beta"))
        workspace = self.make(["Alpha", "Delta", "Beta"], Beta=MODE_EXTERNAL)
        layout = SavedLayout(tab_order=[delta, alpha], active=alpha,
                             windows={beta: {"rect": [10, 20, 810, 620], "maximized": False}})
        self.ctx.workspaces.save_layout(workspace.id, layout)
        self.open_workspace(workspace.id)
        self.pump(0.2)
        titles = [self.manager.get(sid).app_id for sid in self.view.tab_order()]
        self.assertEqual(titles, [delta, alpha])
        self.assertEqual(self.manager.get(self.view.current_session_id()).app_id, alpha)
        self.assertTrue(self.pump(until=lambda: bool(self.placed)))
        self.assertEqual(self.placed[0][1:], ((10, 20, 810, 620), False))

    def test_build_layout_captures_what_is_open(self):
        workspace = self.make(["Alpha"])
        self.open_workspace(workspace.id)
        extra = self.manager.open_app(stable_app_id("Delta"), workspace.id)
        self.pump(until=lambda: self.manager.get(extra.session_id).state is not SessionState.LAUNCHING)
        self.ctx.workspaces.set_mode(workspace.id, stable_app_id("Alpha"), MODE_EMBEDDED)
        current = self.ctx.workspaces.get(workspace.id)
        placements = {stable_app_id("Beta"): {"rect": [1, 2, 3, 4], "maximized": True}, stable_app_id("Alpha"): {"rect": [9, 9, 9, 9], "maximized": False}}
        layout, new_apps = workspace_launcher.build_layout(current, self.manager, self.ctx.registry, placements)
        self.assertEqual(layout.tab_order, [stable_app_id("Alpha"), stable_app_id("Delta")])
        self.assertEqual(new_apps, [(stable_app_id("Delta"), MODE_EMBEDDED)])  # open here but not listed yet
        self.assertEqual(layout.windows, {stable_app_id("Beta"): {"rect": [1, 2, 3, 4], "maximized": True}})  # hosted apps have no window position
        self.assertTrue(layout.active)


class WorkspaceViewTests(UiBase):
    def drop(self, widget, app_id):
        mime = QMimeData()
        mime.setData(APP_MIME, QByteArray(app_id.encode()))
        event = mock.Mock()
        event.mimeData.return_value = mime
        widget.dragEnterEvent(event)
        event.acceptProposedAction.assert_called()
        widget.dropEvent(event)

    def test_page_lists_workspaces_and_edits_them(self):
        view = self.window.pages["workspaces"]
        workspace = self.ctx.workspaces.create("Study", [stable_app_id("Alpha")])
        self.window.show_page("workspaces", save=False)
        view.select(workspace.id)
        view.refresh()
        self.assertEqual(view.title.text(), "Study")
        self.assertEqual(view.rows_layout.count(), 3)  # one row + drop zone + stretch
        with mock.patch("cirana.ui.workspace_view.QInputDialog.getText", return_value=("Exams", True)):
            view.rename(workspace.id)
        self.assertEqual(self.ctx.workspaces.get(workspace.id).name, "Exams")
        with mock.patch("cirana.ui.workspace_view.QMessageBox.question", return_value=mock.sentinel.no):
            view.delete(workspace.id)
        self.assertIsNotNone(self.ctx.workspaces.get(workspace.id))
        from PySide6.QtWidgets import QMessageBox

        with mock.patch("cirana.ui.workspace_view.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
            view.delete(workspace.id)
        self.assertIsNone(self.ctx.workspaces.get(workspace.id))
        view.refresh()
        self.assertTrue(view.detail.isHidden())  # nothing left: the empty state takes over

    def test_dragging_an_application_onto_a_workspace_adds_it(self):
        view = self.window.pages["workspaces"]
        first, second = self.ctx.workspaces.create("One"), self.ctx.workspaces.create("Two")
        view.select(first.id)
        view.refresh()
        self.drop(view, stable_app_id("Alpha"))  # onto the page: the selected workspace
        self.assertTrue(self.ctx.workspaces.get(first.id).has(stable_app_id("Alpha")))
        item = next(i for i in view.list_host.findChildren(type(view.list_layout.itemAt(0).widget()))
                    if i.workspace_id == second.id)
        self.drop(item, stable_app_id("Beta"))  # onto a list entry: that workspace
        self.assertTrue(self.ctx.workspaces.get(second.id).has(stable_app_id("Beta")))
        self.drop(item, stable_app_id("Beta"))  # twice is harmless
        self.assertEqual(len(self.ctx.workspaces.get(second.id).apps), 1)

    def test_mode_combo_is_stored(self):
        view = self.window.pages["workspaces"]
        workspace = self.ctx.workspaces.create("Modes", [stable_app_id("Alpha")])
        view.select(workspace.id)
        view.refresh()
        from PySide6.QtWidgets import QComboBox

        combo = view.rows_host.findChildren(QComboBox)[0]
        self.assertEqual(self.ctx.workspaces.get(workspace.id).apps[0].mode, MODE_EXTERNAL)  # tabs are opt-in
        combo.setCurrentIndex(0)
        self.assertEqual(self.ctx.workspaces.get(workspace.id).apps[0].mode, MODE_EMBEDDED)

    def card_for(self, name):
        self.window.show_page("applications", save=False)
        self.pump(0.1)
        self.window._refresh_timer.stop()
        self.window._refresh_current()
        return self.window.pages["applications"].collection._items[stable_app_id(name)]

    def mouse(self, widget, kind, local, buttons=None):
        from PySide6.QtCore import QEvent, QPointF, Qt
        from PySide6.QtGui import QMouseEvent
        from PySide6.QtWidgets import QApplication

        left = Qt.MouseButton.LeftButton
        pressed = {QEvent.Type.MouseButtonPress: left, QEvent.Type.MouseButtonRelease: left}.get(kind, Qt.MouseButton.NoButton)
        held = buttons if buttons is not None else (left if kind is not QEvent.Type.MouseButtonRelease else Qt.MouseButton.NoButton)
        QApplication.sendEvent(widget, QMouseEvent(kind, QPointF(local), QPointF(widget.mapToGlobal(local)), pressed, held,
                                                   Qt.KeyboardModifier.NoModifier))

    def test_drag_starts_only_after_the_threshold_and_carries_the_application(self):
        from PySide6.QtCore import QEvent, QPoint

        from cirana.widgets import app_card

        card = self.card_for("Alpha")
        started = []

        class FakeDrag:
            def __init__(self, source):
                started.append(self)
                self.mime = None

            def setMimeData(self, mime):
                self.mime = mime

            def setPixmap(self, _p):
                pass

            def setHotSpot(self, _p):
                pass

            def exec(self, _action):
                states.append(("exec", self.mime.hasFormat(APP_MIME), bytes(self.mime.data(APP_MIME)).decode()))

        states = []
        card.drag_state_changed.connect(lambda on: states.append(on))
        with mock.patch.object(app_card, "QDrag", FakeDrag):
            self.mouse(card, QEvent.Type.MouseButtonPress, QPoint(20, 20))
            self.mouse(card, QEvent.Type.MouseMove, QPoint(22, 21))  # a click with a shaky hand
            self.assertEqual(started, [])
            self.mouse(card, QEvent.Type.MouseMove, QPoint(60, 50))  # clearly a drag
            self.assertEqual(len(started), 1)
            self.mouse(card, QEvent.Type.MouseMove, QPoint(80, 60))  # still the same press: no second drag
            self.assertEqual(len(started), 1)
            self.mouse(card, QEvent.Type.MouseButtonRelease, QPoint(80, 60))
        self.assertEqual(states, [True, ("exec", True, stable_app_id("Alpha")), False])  # drop bar shown for the drag only

    def test_drop_bar_lists_workspaces_while_dragging(self):
        self.ctx.workspaces.create("Alpha group")
        bar = self.window.drop_bar
        self.ctx.actions.drag_active.emit(True)
        self.assertFalse(bar.isHidden())
        from cirana.widgets.workspace_drop_bar import DropChip

        names = [c.text() for c in bar.findChildren(DropChip)]
        self.assertIn("Alpha group", names)
        self.assertIn("+ New workspace", names)
        chip = next(c for c in bar.findChildren(DropChip) if c.text() == "Alpha group")
        self.drop(chip, stable_app_id("Delta"))
        self.ctx.actions.drag_active.emit(False)
        self.assertTrue(bar.isHidden())
        self.assertTrue(self.ctx.workspaces.all()[0].has(stable_app_id("Delta")))

    def test_save_session_stores_open_tabs_and_new_applications(self):
        view = self.window.pages["workspaces"]
        workspace = self.ctx.workspaces.create("Session", [stable_app_id("Alpha")])
        self.ctx.workspaces.set_active(workspace.id)
        out = self.manager.open_app(stable_app_id("Delta"), workspace.id)
        self.pump(until=lambda: self.manager.get(out.session_id).state is not SessionState.LAUNCHING)
        with mock.patch("cirana.ui.workspace_view.external_placements", return_value={}):
            view.save_session(workspace.id)
            self.assertTrue(self.pump(until=lambda: self.ctx.workspaces.get(workspace.id).layout is not None))
        saved = self.ctx.workspaces.get(workspace.id)
        self.assertEqual(saved.layout.tab_order, [stable_app_id("Delta")])
        self.assertTrue(saved.has(stable_app_id("Delta")))

    def test_context_menu_can_add_to_workspace(self):
        from PySide6.QtCore import QPoint
        from PySide6.QtWidgets import QMenu

        self.ctx.workspaces.create("Menu target")
        built = []

        class NonBlockingMenu(QMenu):
            def exec(self, pos):
                built.append([a.text() for a in self.actions()])

        with mock.patch("cirana.ui.app_actions.QMenu", NonBlockingMenu):
            self.ctx.actions.show_menu(stable_app_id("Alpha"), QPoint(1, 1))
        self.assertIn("Add to workspace", built[0])
        self.assertIn("Open in a Playground tab", built[0])
        self.ctx.actions.add_to_workspace(stable_app_id("Alpha"), self.ctx.workspaces.all()[0].id)
        self.assertTrue(self.ctx.workspaces.all()[0].has(stable_app_id("Alpha")))


if __name__ == "__main__":
    unittest.main()
