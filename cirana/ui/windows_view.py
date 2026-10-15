"""Windows page: every application window that is open right now (live), with quick actions.

The list comes from the window tracker, so it follows windows as they open, close, move and change their title,
and building it never waits for an application.
"""
from __future__ import annotations

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLineEdit, QMenu, QSizePolicy, QToolButton, QVBoxLayout, QWidget,
)

from cirana.core.background import run_async
from cirana.ui.common import EmptyState, label, make_scroll
from cirana.ui.context import AppContext
from cirana.ui.dialogs import window_icon_app, window_subtitle
from cirana.ui.icons import icon
from cirana.ui.theme import TEXT2
from cirana.wm.identity import TrackedWindow
from cirana.wm.monitors import monitor_for_rect


def _tool(glyph: str, tip: str) -> QToolButton:
    button = QToolButton()
    button.setObjectName("IconButton")
    button.setIcon(icon(glyph, TEXT2, 16))
    button.setToolTip(tip)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    return button


class WindowRow(QFrame):
    def __init__(self, view: "WindowsView", window: TrackedWindow, where: str, groups: list[str]) -> None:
        super().__init__()
        ctx = view.ctx
        self.setObjectName("WorkspaceAppRow")
        self.setMinimumHeight(60)
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 8, 10, 8)
        row.setSpacing(10)
        tile = QLabel()
        tile.setObjectName("IconTile")
        tile.setFixedSize(40, 40)
        tile.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ctx.icons.request(window_icon_app(ctx, window), 28, tile, tile.setPixmap)
        row.addWidget(tile)
        text = QVBoxLayout()
        text.setSpacing(0)
        title = label(window.display_title, "AppName")
        title.setToolTip(window.title)
        title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)  # long titles never widen it
        text.addWidget(title)
        meta = window_subtitle(window) + (f" · {where}" if where else "")
        if groups:
            meta += " · in " + ", ".join(groups)
        sub = label(meta, "AppCategory")
        sub.setToolTip(window.app_exe)
        sub.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        text.addWidget(sub)
        row.addLayout(text, 1)
        for glyph, tip, handler in (
            ("external", "Bring to front", lambda: view.run(lambda c: c.bring_to_front([window]))),
            ("minimize", "Minimize", lambda: view.run(lambda c: [c.minimize(window)])),
            ("maximize", "Restore" if window.maximized else "Maximize",
             lambda: view.run(lambda c: [c.restore(window) if window.maximized else c.maximize(window)])),
        ):
            button = _tool(glyph, tip)
            button.clicked.connect(handler)
            row.addWidget(button)
        add = _tool("plus", "Add to a workspace")
        add.clicked.connect(lambda: view.show_add_menu(window, add.mapToGlobal(add.rect().bottomLeft())))
        row.addWidget(add)


class WindowsView(QWidget):
    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(12)
        head = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(0)
        titles.addWidget(label("Open windows", "PageTitle"))
        self.subtitle = label("", "PageSubtitle")
        titles.addWidget(self.subtitle)
        head.addLayout(titles, 1)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter windows…")
        self.filter.setClearButtonEnabled(True)
        self.filter.setMaximumWidth(280)
        self.filter.textChanged.connect(lambda _t: self.refresh())
        head.addWidget(self.filter)
        root.addLayout(head)
        self.rows_host = QWidget()
        self.rows = QVBoxLayout(self.rows_host)
        self.rows.setContentsMargins(0, 0, 4, 0)
        self.rows.setSpacing(8)
        self.rows.addStretch(1)
        root.addWidget(make_scroll(self.rows_host), 1)
        self.empty = EmptyState("monitor", "No application windows",
                                "Windows you open appear here as they open. CIRANA lists ordinary application "
                                "windows; the taskbar, the desktop, menus and tool windows are left out.")
        root.addWidget(self.empty)
        self._visible = False
        self._timer = QTimer(self, singleShot=True, interval=250)  # coalesce bursts (a dragged window moves a lot)
        self._timer.timeout.connect(self.refresh)
        for signal in (ctx.windows.window_added, ctx.windows.window_removed, ctx.windows.window_changed):
            signal.connect(lambda _w: self._visible and self._timer.start())
        ctx.windows.inventory_ready.connect(lambda: self._visible and self._timer.start())

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._visible = True

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._visible = False

    def refresh(self) -> None:
        ctx = self.ctx
        windows = ctx.windows.windows()
        needle = self.filter.text().strip().lower()
        shown = [w for w in windows if not needle or needle in f"{w.title} {w.exe_name}".lower()]
        monitors = ctx.windows.backend.monitors()
        monitors = monitors if len(monitors) > 1 else []  # name the monitor only when there is a choice
        groups: dict[str, list[str]] = {}
        for workspace in ctx.workspaces.all():
            for status in ctx.wm.statuses(workspace.id):
                if status.window is not None:
                    groups.setdefault(status.window.key, []).append(workspace.name)
        while self.rows.count() > 1:
            item = self.rows.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for window in shown:
            monitor = monitor_for_rect(window.rect, monitors) if monitors and not window.minimized else None
            where = monitor.label.split(" · ")[0] if monitor else ""
            self.rows.insertWidget(self.rows.count() - 1, WindowRow(self, window, where, groups.get(window.key, [])))
        if not ctx.windows.ready:
            self.subtitle.setText("Looking for windows…")
        else:
            self.subtitle.setText(f"{len(windows)} application window{'s' if len(windows) != 1 else ''} open"
                                  + (f" · {len(shown)} shown" if needle else ""))
        self.empty.setVisible(ctx.windows.ready and not windows)

    def run(self, action) -> None:  # noqa: ANN001 - callable(WindowController) -> list[Result]
        controller = self.ctx.window_control
        run_async(lambda _cancel: action(controller),
                  lambda results: [self.ctx.actions.message.emit(r.message) for r in results if not r.ok])

    def show_add_menu(self, window: TrackedWindow, pos) -> None:  # noqa: ANN001
        menu = QMenu(self)
        for workspace in self.ctx.workspaces.all():
            action = QAction(workspace.name, menu)
            action.triggered.connect(lambda _=False, wid=workspace.id: self._add(wid, window))
            menu.addAction(action)
        if self.ctx.workspaces.all():
            menu.addSeparator()
        menu.addAction("New workspace…", lambda: self._add_new(window))
        menu.exec(pos)

    def _add(self, workspace_id: str, window: TrackedWindow) -> None:
        workspace = self.ctx.workspaces.get(workspace_id)
        if workspace is None or self.ctx.windows.get(window.key) is None:
            return
        entry = self.ctx.workspaces.add_window(workspace_id, window)
        self.ctx.wm.choose(workspace_id, entry.id, window.key, place=False)
        self.ctx.actions.message.emit(f"Added {window.display_title} to {workspace.name}")

    def _add_new(self, window: TrackedWindow) -> None:
        from PySide6.QtWidgets import QInputDialog, QMessageBox

        name, ok = QInputDialog.getText(self, "New workspace", "Workspace name:")
        if not ok:
            return
        try:
            workspace = self.ctx.workspaces.create(name)
        except ValueError as exc:
            QMessageBox.warning(self, "Cannot create workspace", str(exc))
            return
        self._add(workspace.id, window)
