"""Phase 4: the Workspaces and Windows pages, the window pickers and drag and drop, on a simulated desktop.

One offscreen CIRANA instance whose window manager runs on tests/fake_desktop.py: nothing here can move, raise or
launch a real window (the catalog's applications point at files that do not exist).
"""
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QByteArray, QMimeData  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

from cirana.core.models import AppEntry, stable_app_id  # noqa: E402
from cirana.core.workspace_controller import EntryState  # noqa: E402
from cirana.widgets.dnd import APP_MIME, entry_mime  # noqa: E402
from cirana.wm.layout import FREE, GRID, MAIN_SIDE, SIDE_BY_SIDE, LayoutSpec  # noqa: E402
from tests.fake_desktop import FakeDesktop  # noqa: E402


def exe_app(name):
    path = f"C:\\NoSuchFolder\\{name}.exe"
    return AppEntry(id=stable_app_id(name), name=name, launch_type="executable", target=path, source="user",
                    exe_path=path)


class UiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.mkdtemp(prefix="cirana-ui-ws-")
        os.environ["CIRANA_HOME"] = cls.home
        from cirana.main import build_app

        cls.desktop = FakeDesktop()
        cls.app, cls.window, cls.ctx = build_app([], window_hooks=False, window_backend=cls.desktop)
        cls.ctx.registry.apply_scan([exe_app("Alpha"), exe_app("Beta"), exe_app("Gamma")])
        cls.window.show()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.windows.stop()
        cls.ctx.tracker.shutdown()
        cls.window.close()
        shutil.rmtree(cls.home, ignore_errors=True)

    def setUp(self):
        for workspace in self.ctx.workspaces.all():
            self.ctx.workspaces.delete(workspace.id)
        self.desktop.windows.clear()
        self.sync()
        self.view = self.window.pages["workspaces"]

    def sync(self):
        self.ctx.windows.feed_snapshot([w for w in (self.desktop.describe(h) for h in self.desktop.enum_windows())
                                        if w])

    @staticmethod
    def flush():
        """Delete the widgets a refresh replaced (deleteLater) so findChildren only sees the current ones."""
        from PySide6.QtCore import QEvent

        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)

    def pump(self, until=None, timeout=2.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.app.processEvents()
            if until is not None and until():
                return True
            time.sleep(0.005)
        return until is None

    def show(self, workspace_id):
        self.window.show_page("workspaces", save=False)
        self.view.select(workspace_id)
        self.view.refresh()

    def rows(self):
        from cirana.ui.workspace_view import EntryRow

        return [self.view.rows_layout.itemAt(i).widget() for i in range(self.view.rows_layout.count())
                if isinstance(self.view.rows_layout.itemAt(i).widget(), EntryRow)]

    @staticmethod
    def drop(widget, mime):
        event = mock.Mock()
        event.mimeData.return_value = mime
        widget.dragEnterEvent(event)
        event.acceptProposedAction.assert_called()
        widget.dropEvent(event)

    @staticmethod
    def app_mime(app_id):
        mime = QMimeData()
        mime.setData(APP_MIME, QByteArray(app_id.encode()))
        return mime

    # -- the page ------------------------------------------------------------------------------------------------
    def test_page_lists_workspaces_and_edits_them(self):
        workspace = self.ctx.workspaces.create("Study", [stable_app_id("Alpha")])
        self.show(workspace.id)
        self.assertEqual(self.view.title.text(), "Study")
        self.assertEqual(len(self.rows()), 1)
        with mock.patch("cirana.ui.workspace_view.QInputDialog.getText", return_value=("Exams", True)):
            self.view.rename(workspace.id)
        self.assertEqual(self.ctx.workspaces.get(workspace.id).name, "Exams")
        with mock.patch("cirana.ui.workspace_view.QMessageBox.question", return_value=mock.sentinel.no):
            self.view.delete(workspace.id)
        self.assertIsNotNone(self.ctx.workspaces.get(workspace.id))
        from PySide6.QtWidgets import QMessageBox

        with mock.patch("cirana.ui.workspace_view.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
            self.view.delete(workspace.id)
        self.assertIsNone(self.ctx.workspaces.get(workspace.id))
        self.view.refresh()
        self.assertTrue(self.view.detail.isHidden())  # nothing left: the empty state takes over

    def test_rows_show_the_state_of_each_window(self):
        self.desktop.add("c:\\nosuchfolder\\alpha.exe", "Alpha main")
        self.desktop.add("c:\\nosuchfolder\\beta.exe", "Beta one")
        self.desktop.add("c:\\nosuchfolder\\beta.exe", "Beta two")
        self.sync()
        workspace = self.ctx.workspaces.create("States", [stable_app_id(n) for n in ("Alpha", "Beta", "Gamma")])
        self.show(workspace.id)
        from cirana.ui.common import QLabel

        pills = [r.findChild(QLabel, "StatePill").text() for r in self.rows()]
        self.assertEqual(pills, ["Open", "Choose a window", "Starts on activate"])
        self.assertIn("1 open", self.view.subtitle.text())

    def test_layout_controls_are_stored_and_previewed(self):
        workspace = self.ctx.workspaces.create("Layout", [stable_app_id("Alpha"), stable_app_id("Beta")])
        self.show(workspace.id)
        self.view.kind_combo.setCurrentIndex(1)  # main + side panel
        self.view.ratio_spin.setValue(70)
        self.view.gap_spin.setValue(4)
        spec = self.ctx.workspaces.get(workspace.id).layout
        self.assertEqual((spec.kind, spec.ratio, spec.gap), (MAIN_SIDE, 0.7, 4))
        self.view.refresh()
        tiles = self.view.preview.tiles()
        self.assertEqual(len(tiles), 2)
        self.assertGreater(tiles[0][2] - tiles[0][0], tiles[1][2] - tiles[1][0])  # the main window is wider
        self.view.kind_combo.setCurrentIndex(2)  # grid 2x2
        self.assertEqual((self.ctx.workspaces.get(workspace.id).layout.kind,
                          self.ctx.workspaces.get(workspace.id).layout.columns), (GRID, 2))
        self.view.kind_combo.setCurrentIndex(4)  # saved positions: none saved yet
        self.view.refresh()
        self.assertEqual(self.view.preview.tiles(), [None, None])
        self.assertFalse(self.view.gap_spin.isEnabled())

    def test_activate_and_apply_only_touch_the_group(self):
        alpha = self.desktop.add("c:\\nosuchfolder\\alpha.exe", "Alpha")
        other = self.desktop.add("c:\\other\\other.exe", "Other", rect=(1, 1, 300, 300))
        self.sync()
        self.addCleanup(self.ctx.tiler.set_global, True)
        self.ctx.tiler.set_global(False)  # (a new installation tiles every window; this test is about the manual button)
        workspace = self.ctx.workspaces.create("Go", [stable_app_id("Alpha")])
        self.ctx.workspaces.set_layout(workspace.id, LayoutSpec(GRID, gap=8))
        self.show(workspace.id)
        self.view.activate_button.click()
        self.assertTrue(self.pump(lambda: not self.ctx.wm.busy(workspace.id)))
        work = self.desktop.monitors_[0].work
        self.assertEqual(alpha.rect, (work[0] + 8, work[1] + 8, work[2] - 8, work[3] - 8))
        self.assertEqual(other.rect, (1, 1, 300, 300))
        self.assertEqual(self.ctx.workspaces.active_id, workspace.id)

    def test_dynamic_tiling_switch(self):
        self.addCleanup(self.ctx.tiler.set_global, True)
        self.ctx.tiler.set_global(False)  # all-window tiling (on in a new installation) would be in charge instead
        workspace = self.ctx.workspaces.create("Tiles", [stable_app_id("Alpha")])
        self.show(workspace.id)
        self.assertTrue(self.view.dynamic_check.isChecked())  # new workspaces start with dynamic tiling on
        self.view.dynamic_check.setChecked(False)
        self.assertFalse(self.ctx.workspaces.get(workspace.id).layout.dynamic)
        self.view.dynamic_check.setChecked(True)
        self.assertTrue(self.ctx.workspaces.get(workspace.id).layout.dynamic)
        self.view.refresh()
        self.assertIn("starts when this workspace is activated", self.view.tiling_state.text())
        self.ctx.workspaces.set_active(workspace.id)
        self.view.refresh()
        self.assertIn("following", self.view.tiling_state.text())
        self.assertIn("tiling", self.view.subtitle.text())
        self.view.gap_spin.setValue(12)  # editing the layout keeps the switch
        self.assertTrue(self.ctx.workspaces.get(workspace.id).layout.dynamic)
        self.view.kind_combo.setCurrentIndex(4)  # saved positions cannot be tiled
        self.view.refresh()
        self.assertFalse(self.view.dynamic_check.isEnabled())
        self.assertFalse(self.ctx.tiler.manages(workspace.id))
        self.view.kind_combo.setCurrentIndex(0)
        self.view.refresh()
        self.view.dynamic_check.setChecked(False)
        self.assertFalse(self.ctx.workspaces.get(workspace.id).layout.dynamic)
        self.ctx.workspaces.set_active("")

    # -- drag and drop -------------------------------------------------------------------------------------------
    def test_dragging_an_application_onto_a_workspace_adds_it(self):
        from cirana.widgets.workspace_card import WorkspaceCard

        first, second = self.ctx.workspaces.create("One"), self.ctx.workspaces.create("Two")
        self.show(first.id)
        self.flush()
        self.drop(self.view, self.app_mime(stable_app_id("Alpha")))  # onto the page: the selected workspace
        self.assertTrue(self.ctx.workspaces.get(first.id).has_app(stable_app_id("Alpha")))
        card = next(c for c in self.view.list_host.findChildren(WorkspaceCard) if c.workspace_id == second.id)
        self.drop(card, self.app_mime(stable_app_id("Beta")))  # onto a card: that workspace
        self.drop(card, self.app_mime(stable_app_id("Beta")))  # twice is harmless
        self.assertEqual([a.app_id for a in self.ctx.workspaces.get(second.id).apps], [stable_app_id("Beta")])

    def test_rows_reorder_by_drag_and_an_app_card_inserts_in_place(self):
        workspace = self.ctx.workspaces.create("Order", [stable_app_id("Alpha"), stable_app_id("Beta")])
        self.show(workspace.id)
        alpha, beta = self.ctx.workspaces.get(workspace.id).apps
        self.drop(self.rows()[0], entry_mime(workspace.id, beta.id, ""))  # Beta dropped on the first row
        self.assertEqual([a.id for a in self.ctx.workspaces.get(workspace.id).apps], [beta.id, alpha.id])
        self.view.refresh()
        self.drop(self.rows()[1], self.app_mime(stable_app_id("Gamma")))
        self.assertEqual([a.app_id for a in self.ctx.workspaces.get(workspace.id).apps],
                         [stable_app_id("Beta"), stable_app_id("Gamma"), stable_app_id("Alpha")])

    def test_dragging_a_row_out_removes_it_and_another_workspace_ignores_it(self):
        workspace = self.ctx.workspaces.create("Remove", [stable_app_id("Alpha"), stable_app_id("Beta")])
        other = self.ctx.workspaces.create("Elsewhere", [stable_app_id("Gamma")])
        self.show(workspace.id)
        entry = self.ctx.workspaces.get(workspace.id).apps[0]
        self.view.set_entry_drag(True)
        self.assertIn("remove", self.view.drop_hint.text())
        self.drop(self.view, entry_mime(workspace.id, entry.id, ""))
        self.view.set_entry_drag(False)
        self.assertEqual(len(self.ctx.workspaces.get(workspace.id).apps), 1)
        foreign = entry_mime(other.id, self.ctx.workspaces.get(other.id).apps[0].id, "")
        event = mock.Mock()
        event.mimeData.return_value = foreign
        self.view.dragEnterEvent(event)
        event.acceptProposedAction.assert_not_called()  # an entry of another workspace is not accepted here

    # -- picking windows -----------------------------------------------------------------------------------------
    def test_window_picker_follows_windows_live_and_adds_them(self):
        from cirana.ui.dialogs import WindowPickerDialog

        first = self.desktop.add("c:\\tools\\editor.exe", "Report.txt - Editor")
        self.desktop.add("c:\\tools\\paint.exe", "")
        self.sync()
        dialog = WindowPickerDialog(self.ctx)
        texts = [dialog.list.item(i).text() for i in range(dialog.list.count())]
        self.assertIn("Report.txt - Editor\neditor.exe", texts)
        self.assertIn("paint.exe\npaint.exe", texts)  # untitled: shown by its program
        self.desktop.add("c:\\tools\\new.exe", "New window")
        self.sync()
        self.assertTrue(self.pump(lambda: dialog.list.count() == 3))
        dialog.filter.setText("report")
        visible = [dialog.list.item(i) for i in range(dialog.list.count()) if not dialog.list.item(i).isHidden()]
        self.assertEqual(len(visible), 1)
        visible[0].setSelected(True)
        self.assertEqual([w.hwnd for w in dialog.selected_windows()], [first.hwnd])
        dialog.reject()

        workspace = self.ctx.workspaces.create("Picked")
        self.show(workspace.id)
        chosen = self.ctx.windows.get(self.desktop.tracked(first.hwnd).key)
        with mock.patch("cirana.ui.workspace_view.WindowPickerDialog") as picker:
            picker.return_value.exec.return_value = True
            picker.return_value.selected_windows.return_value = [chosen]
            self.view.add_windows(workspace.id)
        [status] = self.ctx.wm.statuses(workspace.id)
        self.assertEqual((status.state, status.window.hwnd), (EntryState.RUNNING, first.hwnd))

    def test_ambiguous_entry_asks_once_and_remembers_the_answer(self):
        self.desktop.add("c:\\nosuchfolder\\beta.exe", "Beta one")
        two = self.desktop.add("c:\\nosuchfolder\\beta.exe", "Beta two")
        self.sync()
        workspace = self.ctx.workspaces.create("Ask", [stable_app_id("Beta")])
        target = self.ctx.windows.get(self.desktop.tracked(two.hwnd).key)
        with mock.patch("cirana.ui.main_window.choose_window", return_value=target) as asked:
            self.ctx.wm.activate(workspace.id)
            self.assertTrue(self.pump(lambda: asked.called))
        self.assertEqual(asked.call_count, 1)
        self.assertEqual(self.ctx.wm.statuses(workspace.id)[0].window.hwnd, two.hwnd)

    def test_unrecognised_window_can_be_chosen_and_is_learned(self):
        # A tray-resident program answered through another process: no window matches the entry's rule.
        tray = self.desktop.add("c:\\users\\u\\discord\\app-1\\discord.exe", "Discord")
        self.sync()
        workspace = self.ctx.workspaces.create("Chat", [stable_app_id("Gamma")])
        entry = self.ctx.workspaces.get(workspace.id).apps[0]
        self.assertIs(self.ctx.wm.statuses(workspace.id)[0].state, EntryState.MISSING)
        target = self.ctx.windows.get(self.desktop.tracked(tray.hwnd).key)
        with mock.patch("cirana.ui.workspace_view.choose_window", return_value=target) as asked:
            self.view.choose_window_for(workspace.id, entry.id)
        self.assertIn("None of the open windows", asked.call_args.args[4])
        self.pump(timeout=0.1)
        self.assertIn("discord.exe", self.ctx.workspaces.get(workspace.id).apps[0].rule.alt_exes)
        self.assertEqual(self.ctx.wm.statuses(workspace.id)[0].window.hwnd, tray.hwnd)

    # -- the Windows page ----------------------------------------------------------------------------------------
    def test_windows_page_lists_and_acts_on_windows(self):
        from cirana.ui.windows_view import WindowRow

        win = self.desktop.add("c:\\tools\\editor.exe", "Notes - Editor")
        self.sync()
        workspace = self.ctx.workspaces.create("Win")
        self.window.show_page("windows", save=False)
        page = self.window.pages["windows"]
        page.refresh()
        self.flush()
        rows = page.rows_host.findChildren(WindowRow)
        self.assertEqual(len(rows), 1)
        page.run(lambda c: [c.minimize(self.desktop.tracked(win.hwnd))])
        self.assertTrue(self.pump(lambda: win.minimized))
        page._add(workspace.id, self.ctx.windows.get(self.desktop.tracked(win.hwnd).key))
        self.assertEqual(len(self.ctx.workspaces.get(workspace.id).apps), 1)
        self.assertIn("Notes - Editor", self.window.toast.text())

    def test_windows_page_switches_all_window_tiling(self):
        win = self.desktop.add("c:\\tools\\editor.exe", "Notes - Editor")
        self.sync()
        self.window.show_page("windows", save=False)
        page = self.window.pages["windows"]
        page.refresh()
        self.assertTrue(page.tile_all.isChecked())  # a new installation tiles every window by itself
        self.assertEqual(page.tiling_kind.currentText(), "Automatic (adaptive)")
        page.tile_all.setChecked(False)
        self.assertIn("Off", page.tiling_state.text())
        page.tiling_kind.setCurrentIndex(0)  # side by side
        page.tile_all.setChecked(True)
        self.assertTrue(self.ctx.settings.get("global_tiling"))
        work = self.desktop.monitors_[0].work
        self.assertTrue(self.pump(lambda: win.rect == (work[0] + 8, work[1] + 8, work[2] - 8, work[3] - 8)))
        page.refresh()
        self.assertIn("1 window tiled", page.tiling_state.text())
        from cirana.ui.windows_view import WindowRow

        self.flush()
        row = page.rows_host.findChildren(WindowRow)[0]
        self.assertIn("· tiled", " ".join(text.text() for text in row.findChildren(QLabel)))
        self.ctx.tiler.set_floating("editor.exe", True)
        self.assertIn("editor.exe", self.ctx.settings.get("tiling_exclusions"))
        self.ctx.tiler.set_floating("editor.exe", False)
        page.tile_all.setChecked(False)
        self.assertFalse(self.ctx.settings.get("global_tiling"))

    def test_home_shows_workspace_cards_with_activate(self):
        from cirana.widgets.workspace_card import WorkspaceCard

        workspace = self.ctx.workspaces.create("Home card", [stable_app_id("Alpha")])
        self.window.show_page("home", save=False)
        self.window.pages["home"].refresh()
        self.flush()
        cards = self.window.pages["home"].workspaces_host.findChildren(WorkspaceCard)
        self.assertEqual([c.workspace_id for c in cards], [workspace.id])
        with mock.patch.object(self.ctx.wm, "activate") as activate:
            cards[0].activate_requested.emit(workspace.id)
        activate.assert_called_once_with(workspace.id)

    def test_context_menu_and_drop_bar_add_to_workspace(self):
        from PySide6.QtCore import QPoint
        from PySide6.QtWidgets import QMenu

        from cirana.widgets.workspace_drop_bar import DropChip

        self.ctx.workspaces.create("Menu target")
        built = []

        class NonBlockingMenu(QMenu):
            def exec(self, pos):
                built.append([a.text() for a in self.actions()])

        with mock.patch("cirana.ui.app_actions.QMenu", NonBlockingMenu):
            self.ctx.actions.show_menu(stable_app_id("Alpha"), QPoint(1, 1))
        self.assertIn("Add to workspace", built[0])
        self.assertFalse(any("Playground" in text for text in built[0]))
        bar = self.window.drop_bar
        self.ctx.actions.drag_active.emit(True)
        chip = next(c for c in bar.findChildren(DropChip) if c.text() == "Menu target")
        self.drop(chip, self.app_mime(stable_app_id("Gamma")))
        self.ctx.actions.drag_active.emit(False)
        self.assertTrue(self.ctx.workspaces.all()[0].has_app(stable_app_id("Gamma")))

    def test_app_card_drag_starts_only_after_the_threshold(self):
        from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
        from PySide6.QtGui import QMouseEvent

        from cirana.widgets import app_card

        self.window.show_page("applications", save=False)
        self.window._refresh_timer.stop()
        self.window._refresh_current()
        card = self.window.pages["applications"].collection._items[stable_app_id("Alpha")]
        started = []

        class FakeDrag:
            def __init__(self, _source):
                started.append(self)

            def setMimeData(self, mime):
                self.mime = mime

            def setPixmap(self, _p):
                pass

            def setHotSpot(self, _p):
                pass

            def exec(self, _action):
                pass

        def mouse(kind, point, buttons=Qt.MouseButton.LeftButton):
            QApplication.sendEvent(card, QMouseEvent(kind, QPointF(point), QPointF(card.mapToGlobal(point)),
                                                     Qt.MouseButton.LeftButton, buttons, Qt.KeyboardModifier.NoModifier))

        with mock.patch.object(app_card, "QDrag", FakeDrag):
            mouse(QEvent.Type.MouseButtonPress, QPoint(20, 20))
            mouse(QEvent.Type.MouseMove, QPoint(22, 21))
            self.assertEqual(started, [])
            mouse(QEvent.Type.MouseMove, QPoint(60, 50))
            self.assertEqual(len(started), 1)
            mouse(QEvent.Type.MouseButtonRelease, QPoint(60, 50), Qt.MouseButton.NoButton)
        self.assertEqual(bytes(started[0].mime.data(APP_MIME)).decode(), stable_app_id("Alpha"))


if __name__ == "__main__":
    unittest.main()
