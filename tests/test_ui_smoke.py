"""Builds the whole UI offscreen and exercises the main flows."""
import os
import shutil
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from cirana.core.models import AppEntry, stable_app_id  # noqa: E402


def make(name, launch="shortcut", target=None):
    return AppEntry(id=stable_app_id(name), name=name, launch_type=launch, target=target or f"C:\\x\\{name}.lnk",
                    source="start_menu_user")


class UiSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.mkdtemp(prefix="cirana-ui-")
        os.environ["CIRANA_HOME"] = cls.home
        from cirana.main import build_app
        from tests.fake_desktop import FakeDesktop

        # A simulated desktop: nothing in these tests can move a real window.
        cls.app, cls.window, cls.ctx = build_app([], window_hooks=False, window_backend=FakeDesktop())
        cls.ctx.registry.apply_scan([make("Alpha"), make("Beta"), make("Discord"), make("Zed")])
        cls.window.show()
        cls.app.processEvents()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.tracker.shutdown()
        cls.ctx.windows.stop()
        cls.window.close()
        shutil.rmtree(cls.home, ignore_errors=True)

    def pump(self):
        for _ in range(5):
            self.app.processEvents()
        self.window._refresh_timer.stop()
        self.window._refresh_current()

    def test_app_icon_is_used_for_windows_and_the_sidebar(self):
        from PySide6.QtGui import QImage

        from cirana.paths import asset_path

        self.assertFalse(self.app.windowIcon().isNull())
        self.assertFalse(self.window.windowIcon().isNull())
        self.assertFalse(self.window.sidebar.mark.pixmap().isNull())
        self.assertFalse(QImage(str(asset_path("icons", "main_icon.ico"))).isNull())  # the packaged .exe's icon

    def test_all_pages_render(self):
        for key in ("home", "applications", "workspaces", "windows", "favorites", "settings"):
            self.window.show_page(key, save=False)
            self.pump()
            self.assertIs(self.window.stack.currentWidget(), self.window.pages[key])

    def test_applications_view_lists_apps_and_filters(self):
        view = self.window.pages["applications"]
        self.window.show_page("applications", save=False)
        self.pump()
        self.assertEqual(view.collection.count, 4)
        view.filter_edit.setText("dis")
        view._apply_filter_text()
        self.assertEqual(view.collection.count, 1)
        view.filter_edit.setText("")
        view._apply_filter_text()

    def test_favorite_toggle_updates_views(self):
        app_id = stable_app_id("Zed")
        self.ctx.actions.toggle_favorite(app_id)
        self.window.show_page("favorites", save=False)
        self.pump()
        self.assertEqual(self.window.pages["favorites"].collection.count, 1)
        self.ctx.actions.toggle_favorite(app_id)
        self.pump()
        self.assertEqual(self.window.pages["favorites"].collection.count, 0)

    def test_view_mode_switch(self):
        self.window._set_view_mode("list")
        self.window.show_page("applications", save=False)
        self.pump()
        from cirana.widgets.app_card import AppRow

        self.assertTrue(all(isinstance(i, AppRow) for i in self.window.pages["applications"].collection._items.values()))
        self.window._set_view_mode("grid")
        self.pump()
        self.assertEqual(self.window.pages["applications"].collection.count, 4)

    def test_search_overlay_results_and_escape(self):
        overlay = self.window.overlay
        overlay.open("disc")
        self.assertTrue(overlay.isVisible())
        self.assertEqual(overlay.results.item(0).data(0x0100).title, "Discord")
        overlay.close_overlay()
        self.assertFalse(overlay.isVisible())

    def test_category_navigation_filters_applications(self):
        view = self.window.pages["applications"]
        self.ctx.registry.set_category(stable_app_id("Discord"), "communication")
        try:
            self.window.navigate("applications", "communication")
            self.pump()
            self.assertEqual(view.collection.count, 1)
        finally:
            view.set_category(None)
            self.ctx.registry.reset_category(stable_app_id("Discord"))

    def test_context_menu_builds_and_failed_launch_is_reported(self):
        from unittest import mock

        from PySide6.QtCore import QPoint
        from PySide6.QtWidgets import QMenu, QMessageBox

        built = []

        class NonBlockingMenu(QMenu):
            def exec(self, pos):  # the real exec() would block the test on a modal event loop
                built.append([a.text() for a in self.actions()])

        with mock.patch("cirana.ui.app_actions.QMenu", NonBlockingMenu):
            self.ctx.actions.show_menu(stable_app_id("Alpha"), QPoint(5, 5))
        self.assertIn("Launch", built[0])
        self.assertIn("Hide from CIRANA", built[0])
        with mock.patch.object(QMessageBox, "warning") as warning:
            self.ctx.actions.launch(stable_app_id("Alpha"))  # points at a shortcut that does not exist
        warning.assert_called_once()

    def test_sidebar_collapse_persists(self):
        self.window.sidebar.set_collapsed(True)
        self.assertTrue(self.ctx.settings.get("sidebar_collapsed"))
        self.window.sidebar.set_collapsed(False)


if __name__ == "__main__":
    unittest.main()
