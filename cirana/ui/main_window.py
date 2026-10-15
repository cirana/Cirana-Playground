"""CIRANA main window."""
from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes

from PySide6.QtCore import QByteArray, QEvent, Qt, QTimer
from PySide6.QtGui import QCloseEvent, QKeySequence, QShortcut
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMenu, QMessageBox, QStackedWidget, QVBoxLayout, QWidget

from cirana.core.orphans import end_orphan, find_orphans, restore_taskbar_buttons
from cirana.native import taskbar
from cirana.ui.dialogs import OrphanDialog

from cirana.ui.applications_view import ApplicationsView
from cirana.ui.context import AppContext
from cirana.ui.dashboard import DashboardView
from cirana.ui.playground_view import PlaygroundView
from cirana.ui.search_overlay import SearchOverlay
from cirana.ui.settings_view import SettingsView
from cirana.ui.sidebar import NAV_ITEMS, Sidebar
from cirana.ui.theme import colorref
from cirana.ui.topbar import TopBar
from cirana.ui.workspace_view import WorkspaceView
from cirana.widgets.workspace_drop_bar import WorkspaceDropBar

log = logging.getLogger(__name__)
_TASKBAR_CREATED = ctypes.windll.user32.RegisterWindowMessageW("TaskbarCreated")  # broadcast when Explorer restarts

PAGE_KEYS = [key for key, _t, _g in NAV_ITEMS]


def style_native_frame(hwnd: int) -> None:
    """Tint the native title bar to the CIRANA palette (Windows 11). Silently ignored on older builds."""
    try:
        dwm = ctypes.WinDLL("dwmapi")
        set_attr = dwm.DwmSetWindowAttribute
        set_attr.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
        for attribute, value in (
            (20, 1),          # DWMWA_USE_IMMERSIVE_DARK_MODE
            (34, colorref("border")),  # DWMWA_BORDER_COLOR
            (35, colorref("bg")),      # DWMWA_CAPTION_COLOR
            (36, colorref("text")),    # DWMWA_TEXT_COLOR
        ):
            val = ctypes.c_int(value)
            set_attr(ctypes.c_void_p(hwnd), attribute, ctypes.byref(val), 4)
    except (OSError, AttributeError):
        log.debug("DWM frame styling unavailable")


class MainWindow(QWidget):
    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        ctx.actions.set_parent_widget(self)
        self.setWindowTitle("CIRANA")
        self.setObjectName("Root")
        self.resize(1280, 820)
        self.setMinimumSize(920, 600)
        self._styled = False
        self._focus_mode = False
        self._dirty: set[str] = set()
        self._category_pending: str | None = None

        self.sidebar = Sidebar(bool(ctx.settings.get("sidebar_collapsed")))
        self.topbar = TopBar(ctx.settings.get("view_mode"))
        self.stack = QStackedWidget()
        self.pages: dict[str, QWidget] = {
            "home": DashboardView(ctx),
            "applications": ApplicationsView(ctx),
            "workspaces": WorkspaceView(ctx),
            "playground": PlaygroundView(ctx),
            "favorites": ApplicationsView(ctx, favorites_only=True),
            "settings": SettingsView(ctx),
        }
        for key in PAGE_KEYS:
            self.stack.addWidget(self.pages[key])

        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)
        right.addWidget(self.topbar)
        right.addWidget(self.stack, 1)
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self.sidebar)
        root.addLayout(right, 1)

        self.overlay = SearchOverlay(ctx, self)
        self.drop_bar = WorkspaceDropBar(ctx, self)
        self.toast = QLabel(self)
        self.toast.setObjectName("Toast")
        self.toast.hide()
        self._toast_timer = QTimer(self, singleShot=True, interval=3200)
        self._toast_timer.timeout.connect(self.toast.hide)

        self._refresh_timer = QTimer(self, singleShot=True, interval=30)  # coalesces bursts of change signals
        self._refresh_timer.timeout.connect(self._refresh_current)

        self._wire()
        geometry = ctx.settings.get("window_geometry")
        if geometry:
            self.restoreGeometry(QByteArray.fromBase64(geometry.encode("ascii")))
        self._on_active_workspace(ctx.workspaces.active_id)
        last = ctx.settings.get("last_page")
        self.show_page(last if last in PAGE_KEYS else "home", save=False)

    # -- wiring --------------------------------------------------------------
    def _wire(self) -> None:
        ctx = self.ctx
        self.sidebar.page_selected.connect(self.show_page)
        self.sidebar.collapsed_changed.connect(lambda v: ctx.settings.set("sidebar_collapsed", v))
        self.topbar.search_requested.connect(self.overlay.open)
        self.topbar.settings_clicked.connect(lambda: self.show_page("settings"))
        self.topbar.workspace_clicked.connect(lambda: self._show_workspace_menu())
        self.topbar.view_mode_changed.connect(self._set_view_mode)
        self.overlay.navigate.connect(self.navigate)
        self.pages["home"].navigate.connect(self.navigate)
        self.overlay.visibility_changed.connect(self.pages["playground"].set_suspended)
        self.overlay.visibility_changed.connect(lambda _v: self._sync_escape())
        playground = self.pages["playground"]
        playground.focus_mode_requested.connect(self.set_focus_mode)
        playground.compact_changed.connect(lambda _on: self._apply_chrome())
        playground.workspace_menu_requested.connect(self._show_workspace_menu)
        playground.search_requested.connect(lambda: self.overlay.open())
        QShortcut(QKeySequence("F11"), self, activated=self.toggle_focus_mode)
        self._escape = QShortcut(QKeySequence("Esc"), self, activated=lambda: self.set_focus_mode(False))
        self._escape.setEnabled(False)  # only while full-window mode is on, so it never steals Esc from the overlay
        ctx.actions.navigate.connect(self.navigate)
        ctx.actions.drag_active.connect(lambda on: self.drop_bar.begin() if on else self.drop_bar.end())
        self.overlay.workspace_provider = lambda: [(w.id, w.name, len(w.apps)) for w in ctx.workspaces.all()]
        ctx.workspaces.changed.connect(self._schedule_refresh)
        ctx.workspaces.active_changed.connect(self._on_active_workspace)
        ctx.launcher.progress.connect(self.show_toast)
        ctx.launcher.finished.connect(self._on_workspace_opened)
        ctx.actions.message.connect(self.show_toast)
        ctx.registry.changed.connect(self._schedule_refresh)
        ctx.tracker.running_changed.connect(lambda _r: self._schedule_refresh())
        ctx.scanner.started.connect(self._on_scan_started)
        ctx.scanner.finished.connect(self._on_scan_finished)
        for sequence, handler in (
            ("Ctrl+K", lambda: self.overlay.open()),
            ("Ctrl+B", lambda: self.sidebar.set_collapsed(not self.sidebar.collapsed)),
        ):
            QShortcut(QKeySequence(sequence), self, activated=handler)
        for index, key in enumerate(PAGE_KEYS, start=1):
            QShortcut(QKeySequence(f"Ctrl+{index}"), self, activated=lambda k=key: self.show_page(k))

    # -- navigation ------------------------------------------------------------
    def navigate(self, page: str, payload: object = None) -> None:
        if page == "applications":
            self.pages["applications"].set_category(payload if isinstance(payload, str) else None)
        elif page == "workspaces" and isinstance(payload, str):
            self.pages["workspaces"].select(payload)
        self.show_page(page)

    # -- window chrome: compact Playground and full-window mode ------------------------------------
    def set_focus_mode(self, on: bool) -> None:
        if on and self._current_key() != "playground":
            return
        self._focus_mode = on
        self._sync_escape()
        self._apply_chrome()
        playground = self.pages["playground"]
        if playground.focus_button.isChecked() != on:
            playground.focus_button.setChecked(on)

    def toggle_focus_mode(self) -> None:
        self.set_focus_mode(not self._focus_mode)

    def _sync_escape(self) -> None:
        # Esc leaves full-window mode, except while the search overlay is open: there it closes the overlay.
        self._escape.setEnabled(self._focus_mode and not self.overlay.isVisible())

    def _apply_chrome(self) -> None:
        """Playground with open tabs drops the top bar (its strip carries search and the workspace switch);
        full-window mode drops the sidebar as well."""
        compact = self._current_key() == "playground" and self.pages["playground"].has_tabs()
        self.sidebar.setVisible(not self._focus_mode)
        self.topbar.setVisible(not self._focus_mode and not compact)

    # -- workspaces ---------------------------------------------------------------------------
    def _on_active_workspace(self, workspace_id: str) -> None:
        workspace = self.ctx.workspaces.get(workspace_id)
        self.topbar.set_workspace_name(workspace.name if workspace else "")
        self.pages["playground"].set_active_workspace(workspace_id)
        self._schedule_refresh()

    def _on_workspace_opened(self, summary) -> None:
        self.show_toast(summary.message())
        if summary.started_tabs:
            self.show_page("playground")
        self._schedule_refresh()

    def _show_workspace_menu(self, anchor: QWidget | None = None) -> None:
        anchor = anchor or self.topbar.workspace
        menu = QMenu(self)
        active = self.ctx.workspaces.active_id
        none = QAction("No workspace", menu, checkable=True, checked=not active)
        none.triggered.connect(lambda: self.ctx.workspaces.set_active(""))
        menu.addAction(none)
        for workspace in self.ctx.workspaces.all():
            action = QAction(workspace.name, menu, checkable=True, checked=workspace.id == active)
            action.triggered.connect(lambda _=False, w=workspace.id: self.ctx.workspaces.set_active(w))
            menu.addAction(action)
        menu.addSeparator()
        menu.addAction("Manage workspaces…", lambda: self.show_page("workspaces"))
        menu.exec(anchor.mapToGlobal(anchor.rect().bottomLeft()))

    def show_page(self, key: str, save: bool = True) -> None:
        if key != "playground" and self._focus_mode:
            self.set_focus_mode(False)
        self.stack.setCurrentWidget(self.pages[key])
        self._apply_chrome()
        self.sidebar.select(key)
        self.sidebar.refresh_icons()
        if save:
            self.ctx.settings.set("last_page", key)
        self._dirty.add(key)
        self._refresh_current()

    def _current_key(self) -> str:
        return PAGE_KEYS[self.stack.currentIndex()]

    def _schedule_refresh(self) -> None:
        self._dirty.update(PAGE_KEYS)
        self._refresh_timer.start()

    def _refresh_current(self) -> None:
        key = self._current_key()
        if key in self._dirty:
            self._dirty.discard(key)
            page = self.pages[key]
            if hasattr(page, "refresh"):
                page.refresh()

    def _set_view_mode(self, mode: str) -> None:
        self.ctx.settings.set("view_mode", mode)
        self._schedule_refresh()

    # -- scanning ----------------------------------------------------------------
    def _on_scan_started(self) -> None:
        self.show_toast("Scanning for applications…")
        self._schedule_refresh()

    def _on_scan_finished(self, report) -> None:
        if report.errors and not report.apps:
            self.show_toast("Scan failed. Your previous application list was kept (see log)")
        else:
            self.show_toast(f"Found {report.counts.get('total', 0)} applications")
        self.ctx.tracker.set_apps(self.ctx.registry.all_apps())
        self._schedule_refresh()

    # -- chrome ----------------------------------------------------------------------
    def show_toast(self, text: str) -> None:
        playground = self.pages["playground"]
        playground.show_status(text)  # a native child window can cover the toast; this strip cannot be
        if self._current_key() == "playground" and playground.has_tabs():
            return  # the strip already shows it, and a toast would sit under the hosted window anyway
        self.toast.setText(text)
        self.toast.adjustSize()
        self.toast.move((self.width() - self.toast.width()) // 2, self.height() - self.toast.height() - 28)
        self.toast.raise_()
        self.toast.show()
        self._toast_timer.start()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._styled:
            self._styled = True
            style_native_frame(int(self.winId()))

    def nativeEvent(self, event_type, message):  # noqa: ANN001 - Qt signature
        """Explorer restarted: its "TaskbarCreated" broadcast reaches every top-level window. The new taskbar shows a
        button for every window again, including hosted ones; the shell object is re-created a moment later (no COM
        work inside a native event handler)."""
        if _TASKBAR_CREATED and bytes(event_type) == b"windows_generic_MSG" \
                and wintypes.MSG.from_address(int(message)).message == _TASKBAR_CREATED:
            QTimer.singleShot(1000, self._taskbar_restarted)
        return False, 0

    def _taskbar_restarted(self) -> None:
        hidden = taskbar.taskbar_restarted()
        log.info("The taskbar restarted; hid the buttons of %d hosted window(s) again", hidden)

    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.WindowStateChange:
            self.ctx.tracker.set_active(not self.isMinimized())
        elif event.type() == QEvent.Type.ActivationChange and self.isActiveWindow()                 and self._current_key() == "playground":
            self.pages["playground"].focus_current()  # hand keyboard focus back to the hosted application
        super().changeEvent(event)

    # -- playground lifecycle ----------------------------------------------------------
    def shutdown_playground(self) -> None:
        restored = self.pages["playground"].shutdown()
        if restored:
            log.info("Returned %d hosted window(s) to Windows", restored)

    def check_orphans(self) -> None:
        """Offer to deal with applications that lost their window when CIRANA last ended unexpectedly."""
        stored = self.ctx.settings.playground_store.load()
        entries, docked = stored.get("embedded", []), stored.get("docked", [])
        if docked and restore_taskbar_buttons(docked):
            log.info("Gave docked windows left by the previous session their taskbar button back")
        if not entries:
            if docked:
                self.ctx.playground.clear_persisted_state()
            return
        orphans = find_orphans(entries)
        self.ctx.playground.clear_persisted_state()
        if not orphans:
            return
        dialog = OrphanDialog(orphans, self)
        if not dialog.exec() or not dialog.selected():
            return
        names = ", ".join(o.title for o in dialog.selected())
        if QMessageBox.question(
            self, "End applications",
            f"End {names}?\n\nAny unsaved work in them is lost. This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        self.show_toast(" ".join(end_orphan(o) for o in dialog.selected()))

    def closeEvent(self, event: QCloseEvent) -> None:
        self.shutdown_playground()
        self.ctx.settings.set("window_geometry", bytes(self.saveGeometry().toBase64()).decode("ascii"))
        self.ctx.tracker.shutdown()
        self.ctx.scanner.shutdown()
        super().closeEvent(event)
