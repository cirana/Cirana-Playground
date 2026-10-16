"""The window manager's screens offscreen, on a simulated desktop: BSP and automatic in both layout pickers, monocle /
stack / tiling states, the shortcut editor, the window rule editor and the Settings cards."""
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel, QMenu  # noqa: E402

from cirana.core.models import AppEntry, stable_app_id  # noqa: E402
from cirana.wm.bsp import build, to_dict  # noqa: E402
from cirana.wm.layout import AUTO, BSP, SIDE_BY_SIDE, LayoutSpec  # noqa: E402
from cirana.wm.rules import FLOAT, IGNORE, WindowRule  # noqa: E402
from tests.fake_desktop import FakeDesktop  # noqa: E402


def exe_app(name):
    path = f"C:\\NoSuchFolder\\{name}.exe"
    return AppEntry(id=stable_app_id(name), name=name, launch_type="executable", target=path, source="user",
                    exe_path=path)


class WindowManagerUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.mkdtemp(prefix="cirana-ui-wm-")
        os.environ["CIRANA_HOME"] = cls.home
        from cirana.main import build_app

        cls.desktop = FakeDesktop()
        cls.app, cls.window, cls.ctx = build_app([], window_hooks=False, window_backend=cls.desktop)
        cls.ctx.registry.apply_scan([exe_app("Alpha"), exe_app("Beta")])
        cls.window.show()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.tiler.stop()
        cls.ctx.windows.stop()
        cls.ctx.tracker.shutdown()
        cls.window.close()
        shutil.rmtree(cls.home, ignore_errors=True)

    def setUp(self):
        self.ctx.tiler.set_global(False)
        self.ctx.tiler.set_rules([])
        for workspace in self.ctx.workspaces.all():
            self.ctx.workspaces.delete(workspace.id)
        self.desktop.windows.clear()
        self.sync()

    def sync(self):
        self.ctx.windows.feed_snapshot([w for w in (self.desktop.describe(h) for h in self.desktop.enum_windows())
                                        if w])

    def pump(self, until=None, timeout=2.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            QApplication.processEvents()
            if until is not None and until():
                return True
            time.sleep(0.005)
        return until is None

    def texts(self, widget):
        return " ".join(item.text() for item in widget.findChildren(QLabel))

    def test_windows_page_offers_bsp_and_automatic_and_shows_monocle_and_stacks(self):
        page = self.window.pages["windows"]
        self.window.show_page("windows", save=False)
        page.refresh()
        names = [page.tiling_kind.itemText(i) for i in range(page.tiling_kind.count())]
        self.assertIn("BSP (split per window)", names)
        page.tiling_kind.setCurrentIndex(names.index("By window count (fixed steps)"))
        self.assertEqual(self.ctx.tiler.global_layout().kind, AUTO)
        page.refresh()
        self.assertTrue(page.tiling_auto.isVisibleTo(page))
        page.tiling_auto.combos["4"].setCurrentIndex(page.tiling_auto.combos["4"].findData(BSP))
        self.assertEqual(self.ctx.tiler.global_layout().auto["4"], BSP)
        page.tiling_kind.setCurrentIndex(names.index("BSP (split per window)"))
        self.assertEqual(self.ctx.tiler.global_layout().kind, BSP)
        a = self.desktop.add("c:\\tools\\a.exe", "A")
        b = self.desktop.add("c:\\tools\\b.exe", "B")
        self.sync()
        page.tile_all.setChecked(True)
        self.assertTrue(self.pump(lambda: a.rect != b.rect and self.ctx.tiler.count > 0 and self.ctx.tiler._job is None
                                  and bool(self.ctx.tiler.shown_groups())))
        self.desktop.foreground = a.hwnd
        self.ctx.tiler.toggle_monocle()
        self.assertTrue(self.pump(lambda: self.ctx.tiler.monocle_on() and self.ctx.tiler._job is None))
        page.refresh()
        self.assertIn("monocle", page.tiling_state.text())
        self.ctx.tiler.toggle_monocle()
        self.pump(lambda: not self.ctx.tiler.monocle_on() and self.ctx.tiler._job is None)
        self.ctx.tiler.stack_with(self.desktop.tracked(b.hwnd), self.desktop.tracked(a.hwnd))
        self.assertTrue(self.pump(lambda: self.ctx.tiler.stack_count() == 1 and self.ctx.tiler._job is None))
        page.refresh()
        self.pump(timeout=0.05)
        self.assertIn("1 stack", page.tiling_state.text())
        self.assertIn("stacked (2/2, shown)", self.texts(page.rows_host))
        built = []

        class Menu(QMenu):  # the row's tiling menu builds (and is not shown modally)
            def exec(self, *_args):
                built.append([action.text() for action in self.actions()])

        with mock.patch("cirana.ui.windows_view.QMenu", Menu):
            page.show_tiling_menu(self.desktop.tracked(a.hwnd), page.mapToGlobal(page.rect().center()))
        self.assertIn("Take out of the stack", built[0])
        self.assertIn("Window rule for this program…", built[0])

    def test_workspace_picker_keeps_tree_stacks_and_shows_auto(self):
        view = self.window.pages["workspaces"]
        workspace = self.ctx.workspaces.create("Picker", [stable_app_id("Alpha"), stable_app_id("Beta")])
        ids = [a.id for a in self.ctx.workspaces.get(workspace.id).apps]
        self.ctx.workspaces.set_layout(workspace.id, LayoutSpec(SIDE_BY_SIDE, bsp=to_dict(build(ids, (0, 0, 9, 9))),
                                                                stacks=[{"id": "s1", "members": ids, "active": ids[0]}]))
        view.select(workspace.id)
        self.window.show_page("workspaces", save=False)
        view.refresh()
        names = [view.kind_combo.itemText(i) for i in range(view.kind_combo.count())]
        view.kind_combo.setCurrentIndex(names.index("BSP (split per window)"))
        layout = self.ctx.workspaces.get(workspace.id).layout
        self.assertEqual(layout.kind, BSP)
        self.assertTrue(layout.bsp)  # kept
        self.assertEqual(len(layout.stacks), 1)  # kept
        view.kind_combo.setCurrentIndex(names.index("By window count (fixed steps)"))
        view.refresh()
        self.assertTrue(view.auto_picker.isVisibleTo(view))
        self.assertEqual(self.ctx.workspaces.get(workspace.id).layout.kind, AUTO)
        self.assertEqual(len(view.preview.tiles()), 2)

    def test_settings_cards_and_the_rule_editor(self):
        from cirana.ui.wm_dialogs import RuleDialog

        page = self.window.pages["settings"]
        self.window.show_page("settings", save=False)
        page.refresh()
        self.assertIn("shortcuts", page.hotkey_status.text())
        self.assertFalse(page.ffm.isChecked())  # focus follows the mouse: off by default
        window = self.desktop.add("c:\\tools\\editor.exe", "Notes - Editor", class_name="EditorFrame")
        self.desktop.add("c:\\tools\\editor.exe", "Other - Editor", class_name="EditorFrame")
        self.sync()
        dialog = RuleDialog(self.ctx, parent=page)
        dialog.title.setText("Notes")
        self.assertFalse(dialog.ok.isEnabled())  # a title alone is refused
        self.assertIn("title alone", dialog.problem.text())
        dialog.pick.setCurrentIndex(dialog.pick.findData(self.desktop.tracked(window.hwnd).key))
        self.assertEqual((dialog.exe.text(), dialog.class_name.text()), ("editor.exe", "EditorFrame"))
        self.assertTrue(dialog.ok.isEnabled())
        self.assertIn("Matches 1 open window", dialog.matches.text())  # the title narrows it to one
        dialog.title.setText("")
        self.assertIn("Matches 2 open windows", dialog.matches.text())
        self.ctx.tiler.set_rules([dialog.current(), WindowRule("r2", exe="x.exe", action=IGNORE)])
        page.refresh()
        self.assertEqual(page.rule_list.count(), 2)
        self.assertIn("Float", page.rule_list.item(0).text())
        page.rule_list.setCurrentRow(1)
        page._move_rule(-1)
        self.assertEqual([r.id for r in self.ctx.tiler.rules()][0], "r2")
        page._delete_rule()
        self.assertEqual([r.action for r in self.ctx.tiler.rules()], [FLOAT])

    def test_shortcut_editor_refuses_conflicts_and_saves(self):
        from cirana.ui.wm_dialogs import ShortcutsDialog

        dialog = ShortcutsDialog(self.ctx, self.window)
        dialog.list.setCurrentRow(0)  # focus left
        for name, box in dialog.mods.items():
            box.setChecked(name == "Alt")
        dialog.key.setCurrentIndex(dialog.key.findData("F4"))
        self.assertIn("closes the window", dialog.note.text())
        dialog._set()
        self.assertEqual(dialog.bindings["focus_left"], "Ctrl+Alt+Left")  # not taken over
        dialog.mods["Ctrl"].setChecked(True)
        dialog.key.setCurrentIndex(dialog.key.findData("Q"))
        self.assertIn("AltGr", dialog.note.text())  # allowed, with a warning for AltGr keyboards
        dialog.key.setCurrentIndex(dialog.key.findData("F9"))
        dialog._set()
        self.assertEqual(dialog.bindings["focus_left"], "Ctrl+Alt+F9")
        dialog.accept()
        self.assertEqual(self.ctx.hotkeys.bindings()["focus_left"], "Ctrl+Alt+F9")
        self.ctx.hotkeys.reset()


if __name__ == "__main__":
    unittest.main()
