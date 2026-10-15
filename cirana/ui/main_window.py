"""CIRANA main window."""
from __future__ import annotations

import ctypes
import logging

from PySide6.QtCore import QByteArray, QEvent, QTimer
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMenu, QMessageBox, QStackedWidget, QVBoxLayout, QWidget

from cirana.core import legacy_recovery
from cirana.core.workspace_controller import EntryState
from cirana.ui.applications_view import ApplicationsView
from cirana.ui.context import AppContext
from cirana.ui.dashboard import DashboardView
from cirana.ui.dialogs import OrphanDialog, choose_window
from cirana.ui.search_overlay import SearchOverlay
from cirana.ui.settings_view import SettingsView
from cirana.ui.sidebar import NAV_ITEMS, Sidebar
from cirana.ui.theme import colorref
from cirana.ui.topbar import TopBar
from cirana.ui.windows_view import WindowsView
from cirana.ui.workspace_view import WorkspaceView
from cirana.widgets.workspace_drop_bar import WorkspaceDropBar

log = logging.getLogger(__name__)

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
        self._dirty: set[str] = set()
        self._choices: list[tuple[str, str]] = []  # (workspace id, entry id) waiting for the user to pick a window
        self._choosing = False

        self.sidebar = Sidebar(bool(ctx.settings.get("sidebar_collapsed")))
        self.topbar = TopBar(ctx.settings.get("view_mode"))
        self.stack = QStackedWidget()
        self.pages: dict[str, QWidget] = {
            "home": DashboardView(ctx),
            "applications": ApplicationsView(ctx),
            "workspaces": WorkspaceView(ctx),
            "windows": WindowsView(ctx),
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
        self.toast.setWordWrap(True)
        self.toast.hide()
        self._toast_timer = QTimer(self, singleShot=True, interval=4200)
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
        ctx.actions.navigate.connect(self.navigate)
        ctx.actions.drag_active.connect(lambda on: self.drop_bar.begin() if on else self.drop_bar.end())
        self.overlay.workspace_provider = lambda: [(w.id, w.name, len(w.apps)) for w in ctx.workspaces.all()]
        ctx.workspaces.changed.connect(self._schedule_refresh)
        ctx.workspaces.active_changed.connect(self._on_active_workspace)
        ctx.wm.statuses_changed.connect(lambda _w: self._schedule_refresh(("home", "workspaces")))
        ctx.wm.busy_changed.connect(lambda _w, _b: self._schedule_refresh(("home", "workspaces")))
        ctx.tiler.state_changed.connect(lambda _w: self._schedule_refresh(("home", "workspaces")))
        ctx.tiler.reflowed.connect(lambda _w: self._schedule_refresh(("workspaces",)))
        ctx.tiler.state_changed.connect(lambda _w: self._schedule_refresh(("workspaces", "windows")))
        ctx.wm.message.connect(self.show_toast)
        ctx.wm.choice_needed.connect(self._queue_choice)
        ctx.windows.inventory_ready.connect(self._schedule_refresh)
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

    # -- workspaces ---------------------------------------------------------------------------
    def _on_active_workspace(self, workspace_id: str) -> None:
        workspace = self.ctx.workspaces.get(workspace_id)
        self.topbar.set_workspace_name(workspace.name if workspace else "")
        self._schedule_refresh()

    def _show_workspace_menu(self, anchor: QWidget | None = None) -> None:
        anchor = anchor or self.topbar.workspace
        menu = QMenu(self)
        active = self.ctx.workspaces.active_id
        for workspace in self.ctx.workspaces.all():
            action = QAction(workspace.name, menu, checkable=True, checked=workspace.id == active)
            action.setToolTip("Activate: open what is missing, arrange and raise its windows")
            action.triggered.connect(lambda _=False, w=workspace.id: self._activate(w))
            menu.addAction(action)
        if active:
            menu.addSeparator()
            menu.addAction("Bring active workspace to front", lambda: self.ctx.wm.bring_to_front(active))
            menu.addAction("Close active workspace (windows stay open)", lambda: self.ctx.wm.close(active))
        menu.addSeparator()
        menu.addAction("Manage workspaces…", lambda: self.show_page("workspaces"))
        menu.exec(anchor.mapToGlobal(anchor.rect().bottomLeft()))

    def _activate(self, workspace_id: str) -> None:
        workspace = self.ctx.workspaces.get(workspace_id)
        if workspace is not None and workspace.apps:
            self.ctx.wm.activate(workspace_id)
        elif workspace is not None:
            self.navigate("workspaces", workspace_id)

    def _queue_choice(self, workspace_id: str, entry_id: str) -> None:
        """Several open windows fit an entry: ask, one dialog at a time (never inside the signal itself)."""
        if (workspace_id, entry_id) not in self._choices:
            self._choices.append((workspace_id, entry_id))
        QTimer.singleShot(0, self._next_choice)

    def _next_choice(self) -> None:
        if self._choosing or not self._choices:
            return
        workspace_id, entry_id = self._choices.pop(0)
        status = next((s for s in self.ctx.wm.statuses(workspace_id) if s.entry.id == entry_id), None)
        if status is None or status.state is not EntryState.AMBIGUOUS:
            QTimer.singleShot(0, self._next_choice)  # settled meanwhile (closed, or another choice decided it)
            return
        self._choosing = True
        try:
            window = choose_window(self.ctx, self.ctx.workspaces.display_name(status.entry), status.candidates, self)
        finally:
            self._choosing = False
        if window is not None:
            self.ctx.wm.choose(workspace_id, entry_id, window.key)
        QTimer.singleShot(0, self._next_choice)

    def show_page(self, key: str, save: bool = True) -> None:
        self.stack.setCurrentWidget(self.pages[key])
        self.sidebar.select(key)
        self.sidebar.refresh_icons()
        if save:
            self.ctx.settings.set("last_page", key)
        self._dirty.add(key)
        self._refresh_current()

    def _current_key(self) -> str:
        return PAGE_KEYS[self.stack.currentIndex()]

    def _schedule_refresh(self, pages: tuple[str, ...] | None = None) -> None:
        self._dirty.update(pages or PAGE_KEYS)
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
        if not text:
            return
        self.toast.setText(text)
        self.toast.setMaximumWidth(max(320, self.width() - 160))
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

    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.WindowStateChange:
            self.ctx.tracker.set_active(not self.isMinimized())
        super().changeEvent(event)

    # -- the earlier (embedding) versions --------------------------------------------------------
    def check_legacy_session(self) -> None:
        """Clean up after an earlier CIRANA that hosted windows and ended unexpectedly (once)."""
        orphans = legacy_recovery.recover(self.ctx.settings.root)
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
        self.show_toast(" ".join(legacy_recovery.end_orphan(o) for o in dialog.selected()))

    def closeEvent(self, event: QCloseEvent) -> None:
        # Managed windows are ordinary windows: nothing has to be handed back, they simply stay where they are.
        self.ctx.tiler.stop()
        self.ctx.settings.set("window_geometry", bytes(self.saveGeometry().toBase64()).decode("ascii"))
        self.ctx.windows.stop()
        self.ctx.tracker.shutdown()
        self.ctx.scanner.shutdown()
        super().closeEvent(event)
