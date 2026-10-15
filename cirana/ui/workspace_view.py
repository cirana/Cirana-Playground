"""Workspaces page: create, rename, delete, fill (drag & drop), open with one click, save and restore sessions."""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QInputDialog, QLabel, QMenu, QMessageBox, QPushButton, QToolButton, QVBoxLayout,
    QWidget,
)

from cirana.core.background import run_async
from cirana.core.playground import embed_precheck
from cirana.core.workspace_launcher import build_layout, external_placements
from cirana.core.workspaces import MODE_EMBEDDED, MODE_EXTERNAL, Workspace, WorkspaceApp
from cirana.ui.common import EmptyState, label, make_scroll
from cirana.ui.context import AppContext
from cirana.ui.dialogs import AppPickerDialog
from cirana.ui.icons import icon
from cirana.ui.theme import ACCENT, ON_ACCENT, TEXT, TEXT2
from cirana.widgets.dnd import app_id_from


def _icon_button(glyph: str, tip: str) -> QToolButton:
    button = QToolButton()
    button.setObjectName("IconButton")
    button.setIcon(icon(glyph, TEXT2, 16))
    button.setToolTip(tip)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    return button


def _repolish(widget: QWidget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)


class WorkspaceItem(QFrame):
    """One entry of the workspace list. Also a drop target for application cards."""

    def __init__(self, ctx: AppContext, workspace: Workspace, selected: bool, active: bool, view: "WorkspaceView") -> None:
        super().__init__()
        self.workspace_id = workspace.id
        self._view = view
        self.setObjectName("WorkspaceItem")
        self.setProperty("selected", "true" if selected else "false")
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        row = QHBoxLayout(self)
        row.setContentsMargins(14, 10, 12, 10)
        glyph = QLabel()
        glyph.setPixmap(icon("workspaces", ACCENT if active else TEXT2, 20).pixmap(20, 20))
        text = QVBoxLayout()
        text.setSpacing(0)
        name = label(workspace.name, "AppName")
        name.setToolTip(workspace.name)
        count = len(workspace.apps)
        text.addWidget(name)
        text.addWidget(label(f"{count} app{'s' if count != 1 else ''}"
                             + (" · session saved" if workspace.layout else ""), "AppCategory"))
        row.addWidget(glyph)
        row.addLayout(text, 1)
        if active:
            pill = label("Active", "StatusPill")
            row.addWidget(pill)
        self.setMinimumHeight(58)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._view.select(self.workspace_id)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._view.open_workspace(self.workspace_id)

    def contextMenuEvent(self, event) -> None:
        self._view.show_item_menu(self.workspace_id, event.globalPos())

    def _highlight(self, on: bool) -> None:
        self.setProperty("dropTarget", "true" if on else "false")
        _repolish(self)

    def dragEnterEvent(self, event) -> None:
        if app_id_from(event.mimeData()):
            event.acceptProposedAction()
            self._highlight(True)

    def dragLeaveEvent(self, event) -> None:
        self._highlight(False)

    def dropEvent(self, event) -> None:
        app_id = app_id_from(event.mimeData())
        self._highlight(False)
        if app_id:
            event.acceptProposedAction()
            self._view.ctx.actions.add_to_workspace(app_id, self.workspace_id)


class WorkspaceAppRow(QFrame):
    def __init__(self, view: "WorkspaceView", workspace: Workspace, item: WorkspaceApp, first: bool, last: bool) -> None:
        super().__init__()
        ctx = view.ctx
        self.setObjectName("WorkspaceAppRow")
        self.setFixedHeight(68)
        app = ctx.registry.get(item.app_id)
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 8, 10, 8)
        row.setSpacing(10)

        tile = QLabel()
        tile.setObjectName("IconTile")
        tile.setFixedSize(44, 44)
        tile.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if app is not None:
            ctx.icons.request(app, 32, tile, tile.setPixmap)
        row.addWidget(tile)

        text = QVBoxLayout()
        text.setSpacing(0)
        name = label(ctx.workspaces.display_name(item), "AppName")
        reason = embed_precheck(app) if app else None
        if app is None:
            note, tip = "Not found. Refresh the application list", ""
        elif ctx.playground.session_for_app(app.id):
            note, tip = "Open in a tab", ""
        elif app.id in ctx.tracker.running:
            note, tip = "Running now", ""
        elif reason and item.mode == MODE_EMBEDDED:
            note, tip = "Will open in its own window (cannot be hosted)", reason
        else:
            note, tip = ctx.registry.category_name(ctx.registry.category_of(app)), ""
        sub = label(note, "AppCategory")
        sub.setToolTip(tip)
        text.addWidget(name)
        text.addWidget(sub)
        row.addLayout(text, 1)

        mode = QComboBox()
        mode.addItem("Playground tab", MODE_EMBEDDED)
        mode.addItem("Own window", MODE_EXTERNAL)
        mode.setCurrentIndex(0 if item.mode == MODE_EMBEDDED else 1)
        mode.setToolTip(reason or "How this application opens when the workspace is opened")
        mode.setFixedWidth(150)
        mode.currentIndexChanged.connect(
            lambda _i, c=mode: ctx.workspaces.set_mode(workspace.id, item.app_id, c.currentData()))
        row.addWidget(mode)

        for glyph, tip, enabled, handler in (
            ("chevron-down", "Move down", not last, lambda: ctx.workspaces.move_app(workspace.id, item.app_id, 1)),
            ("x", "Remove from workspace", True, lambda: ctx.workspaces.remove_app(workspace.id, item.app_id)),
        ):
            button = _icon_button(glyph, tip)
            button.setEnabled(enabled)
            button.clicked.connect(handler)
            row.addWidget(button)
        up = _icon_button("chevron-up", "Move up")
        up.setEnabled(not first)
        up.clicked.connect(lambda: ctx.workspaces.move_app(workspace.id, item.app_id, -1))
        row.insertWidget(row.count() - 2, up)


class WorkspaceView(QWidget):
    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self._selected = ctx.workspaces.active_id
        self.setAcceptDrops(True)  # dropping on the page adds to the selected workspace

        # -- left: list ------------------------------------------------------------------------
        left = QVBoxLayout()
        left.setSpacing(10)
        head = QHBoxLayout()
        head.addWidget(label("Workspaces", "PageTitle"))
        head.addStretch(1)
        left.addLayout(head)
        left.addWidget(label("Groups of applications you open together.", "PageSubtitle"))
        new_button = QPushButton(" New workspace")
        new_button.setObjectName("PrimaryButton")
        new_button.setIcon(icon("plus", ON_ACCENT, 16))
        new_button.clicked.connect(self.create_workspace)
        left.addWidget(new_button)
        self.list_host = QWidget()
        self.list_layout = QVBoxLayout(self.list_host)
        self.list_layout.setContentsMargins(0, 0, 4, 0)
        self.list_layout.setSpacing(8)
        self.list_layout.addStretch(1)
        left.addWidget(make_scroll(self.list_host), 1)
        left_box = QWidget()
        left_box.setLayout(left)
        left_box.setFixedWidth(300)

        # -- right: detail -------------------------------------------------------------------------
        self.empty = QWidget()
        empty_layout = QVBoxLayout(self.empty)
        empty_layout.setContentsMargins(0, 36, 0, 0)
        empty_layout.addWidget(EmptyState(
            "workspaces", "No workspace yet",
            "Create a workspace, then drag applications onto it from the Applications page (or use “Add "
            "applications”). One click opens everything in it, in Playground tabs or as normal windows, "
            "whichever you choose per application."), 1)

        self.detail = QWidget()
        detail = QVBoxLayout(self.detail)
        detail.setContentsMargins(0, 0, 0, 0)
        detail.setSpacing(12)
        title_row = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(0)
        self.title = label("", "PageTitle")
        self.subtitle = label("", "PageSubtitle")
        titles.addWidget(self.title)
        titles.addWidget(self.subtitle)
        title_row.addLayout(titles, 1)
        self.switch_button = QPushButton(" Switch to")
        self.switch_button.setObjectName("SecondaryButton")
        self.switch_button.setIcon(icon("check", TEXT, 16))
        self.switch_button.setToolTip("Make this the active workspace without opening anything")
        self.switch_button.clicked.connect(lambda: self.activate(self._selected))
        self.save_button = QPushButton(" Save session")
        self.save_button.setObjectName("SecondaryButton")
        self.save_button.setIcon(icon("save", TEXT, 16))
        self.save_button.setToolTip("Remember which applications are open, their tab order and window positions")
        self.save_button.clicked.connect(lambda: self.save_session(self._selected))
        self.open_button = QPushButton(" Open workspace")
        self.open_button.setObjectName("PrimaryButton")
        self.open_button.setIcon(icon("play", ON_ACCENT, 16))
        self.open_button.clicked.connect(lambda: self.open_workspace(self._selected))
        rename = _icon_button("edit", "Rename workspace")
        rename.clicked.connect(lambda: self.rename(self._selected))
        delete = _icon_button("trash", "Delete workspace")
        delete.clicked.connect(lambda: self.delete(self._selected))
        for widget in (self.switch_button, self.save_button, rename, delete, self.open_button):
            title_row.addWidget(widget)
        detail.addLayout(title_row)

        self.rows_host = QWidget()
        self.rows_layout = QVBoxLayout(self.rows_host)
        self.rows_layout.setContentsMargins(0, 0, 4, 0)
        self.rows_layout.setSpacing(8)
        self.drop_zone = QFrame()
        self.drop_zone.setObjectName("DropZone")
        zone = QVBoxLayout(self.drop_zone)
        zone.setContentsMargins(18, 18, 18, 18)
        self.drop_hint = label("Drag applications here from Applications, Favorites or Home", "Muted")
        self.drop_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        add_button = QPushButton(" Add applications…")
        add_button.setObjectName("SecondaryButton")
        add_button.setIcon(icon("plus", TEXT, 16))
        add_button.clicked.connect(lambda: self.add_applications(self._selected))
        zone.addWidget(self.drop_hint)
        zone.addWidget(add_button, 0, Qt.AlignmentFlag.AlignCenter)
        self.rows_layout.addWidget(self.drop_zone)
        self.rows_layout.addStretch(1)
        detail.addWidget(make_scroll(self.rows_host), 1)

        right = QVBoxLayout()
        right.addWidget(self.empty)
        right.addWidget(self.detail)

        root = QHBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(24)
        root.addWidget(left_box)
        root.addLayout(right, 1)

    # -- rendering ---------------------------------------------------------------------------------
    def refresh(self) -> None:
        ctx = self.ctx
        workspaces = ctx.workspaces.all()
        if not any(w.id == self._selected for w in workspaces):
            self._selected = ctx.workspaces.active_id or (workspaces[0].id if workspaces else "")
        # list
        while self.list_layout.count() > 1:
            item = self.list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for workspace in workspaces:
            self.list_layout.insertWidget(
                self.list_layout.count() - 1,
                WorkspaceItem(ctx, workspace, workspace.id == self._selected, workspace.id == ctx.workspaces.active_id, self),
            )
        # detail
        current = ctx.workspaces.get(self._selected)
        self.empty.setVisible(current is None)
        self.detail.setVisible(current is not None)
        if current is None:
            return
        active = current.id == ctx.workspaces.active_id
        self.title.setText(current.name)
        saved = ""
        if current.layout and current.layout.saved_at:
            saved = " · session saved " + datetime.fromtimestamp(current.layout.saved_at).strftime("%d %b %H:%M")
        count = len(current.apps)
        self.subtitle.setText(f"{count} application{'s' if count != 1 else ''}{saved}" + (" · active" if active else ""))
        self.switch_button.setVisible(not active)
        self.open_button.setEnabled(bool(current.apps) and not ctx.launcher.busy)
        self.save_button.setEnabled(True)
        while self.rows_layout.count() > 2:  # drop zone + stretch stay
            item = self.rows_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for index, item in enumerate(current.apps):
            self.rows_layout.insertWidget(
                self.rows_layout.count() - 2,
                WorkspaceAppRow(self, current, item, index == 0, index == count - 1),
            )
        self.drop_hint.setText(
            "Drag applications here from Applications, Favorites or Home" if count
            else "This workspace is empty. Drag applications here from Applications, Favorites or Home")

    def select(self, workspace_id: str) -> None:
        if workspace_id != self._selected and self.ctx.workspaces.get(workspace_id):
            self._selected = workspace_id
            self.refresh()

    # -- actions ----------------------------------------------------------------------------------------
    def create_workspace(self) -> None:
        name, ok = QInputDialog.getText(self, "New workspace", "Workspace name:")
        if not ok:
            return
        try:
            workspace = self.ctx.workspaces.create(name)
        except ValueError as exc:
            QMessageBox.warning(self, "Cannot create workspace", str(exc))
            return
        self._selected = workspace.id
        self.refresh()

    def rename(self, workspace_id: str) -> None:
        workspace = self.ctx.workspaces.get(workspace_id)
        if workspace is None:
            return
        name, ok = QInputDialog.getText(self, "Rename workspace", "Workspace name:", text=workspace.name)
        if not ok:
            return
        try:
            self.ctx.workspaces.rename(workspace_id, name)
        except ValueError as exc:
            QMessageBox.warning(self, "Cannot rename workspace", str(exc))

    def delete(self, workspace_id: str) -> None:
        workspace = self.ctx.workspaces.get(workspace_id)
        if workspace is None:
            return
        answer = QMessageBox.question(
            self, "Delete workspace",
            f"Delete “{workspace.name}”?\n\nOnly the workspace is removed. Its applications stay installed and "
            "nothing that is running gets closed.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.ctx.workspaces.delete(workspace_id)

    def activate(self, workspace_id: str) -> None:
        try:
            self.ctx.workspaces.set_active(workspace_id)
        except ValueError:
            return
        workspace = self.ctx.workspaces.get(workspace_id)
        if workspace:
            self.ctx.actions.message.emit(f"Switched to {workspace.name}")

    def open_workspace(self, workspace_id: str) -> None:
        workspace = self.ctx.workspaces.get(workspace_id)
        if workspace is None:
            return
        if not workspace.apps:
            self.ctx.actions.message.emit(f"{workspace.name} has no applications yet")
            return
        if not self.ctx.launcher.open(workspace_id):
            self.ctx.actions.message.emit("Another workspace is still opening. Please wait a moment")
        self.refresh()

    def add_applications(self, workspace_id: str) -> None:
        workspace = self.ctx.workspaces.get(workspace_id)
        if workspace is None:
            return
        dialog = AppPickerDialog(self.ctx, f"Add to {workspace.name}", ok_text="Add", multi=True,
                                 exclude={a.app_id for a in workspace.apps}, parent=self)
        if dialog.exec():
            for app_id in dialog.selected_ids():
                self.ctx.workspaces.add_app(workspace_id, app_id)

    def save_session(self, workspace_id: str) -> None:
        workspace = self.ctx.workspaces.get(workspace_id)
        if workspace is None:
            return
        apps = [a for a in (self.ctx.registry.get(i.app_id) for i in workspace.apps) if a]
        self.save_button.setEnabled(False)

        def done(placements: dict) -> None:
            self.save_button.setEnabled(True)
            current = self.ctx.workspaces.get(workspace_id)
            if current is None:
                return
            layout, new_apps = build_layout(current, self.ctx.playground, self.ctx.registry, placements)
            self.ctx.workspaces.save_layout(workspace_id, layout, new_apps)
            extra = f", added {len(new_apps)} open application{'s' if len(new_apps) != 1 else ''}" if new_apps else ""
            self.ctx.actions.message.emit(
                f"Saved session of {current.name}: {len(layout.tab_order)} tab(s), "
                f"{len(layout.windows)} window position(s){extra}")

        def failed(error: str) -> None:
            self.save_button.setEnabled(True)
            self.ctx.actions.message.emit(f"Could not save the session: {error}")

        run_async(lambda cancel: external_placements(apps, cancel), done, failed)

    def clear_session(self, workspace_id: str) -> None:
        self.ctx.workspaces.clear_layout(workspace_id)
        self.ctx.actions.message.emit("Cleared the saved session")

    def show_item_menu(self, workspace_id: str, pos: QPoint) -> None:
        workspace = self.ctx.workspaces.get(workspace_id)
        if workspace is None:
            return
        menu = QMenu(self)
        menu.addAction("Open workspace", lambda: self.open_workspace(workspace_id))
        if workspace_id != self.ctx.workspaces.active_id:
            menu.addAction("Switch to", lambda: self.activate(workspace_id))
        menu.addSeparator()
        menu.addAction("Save session", lambda: self.save_session(workspace_id))
        clear = menu.addAction("Clear saved session", lambda: self.clear_session(workspace_id))
        clear.setEnabled(workspace.layout is not None)
        menu.addSeparator()
        menu.addAction("Rename…", lambda: self.rename(workspace_id))
        menu.addAction("Delete…", lambda: self.delete(workspace_id))
        menu.exec(pos)

    # -- drag & drop onto the page ----------------------------------------------------------------------------
    def dragEnterEvent(self, event) -> None:
        if app_id_from(event.mimeData()) and self.ctx.workspaces.get(self._selected):
            event.acceptProposedAction()
            self.drop_zone.setProperty("active", "true")
            _repolish(self.drop_zone)

    def dragLeaveEvent(self, event) -> None:
        self.drop_zone.setProperty("active", "false")
        _repolish(self.drop_zone)

    def dropEvent(self, event) -> None:
        self.dragLeaveEvent(event)
        app_id = app_id_from(event.mimeData())
        if app_id and self._selected:
            event.acceptProposedAction()
            self.ctx.actions.add_to_workspace(app_id, self._selected)
