"""Shared user actions on applications (launch, favorite, context menu) used by every view."""
from __future__ import annotations

import logging
import os
import subprocess
from typing import Callable

from PySide6.QtCore import QObject, QPoint, QTimer, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QInputDialog, QMenu, QMessageBox, QWidget

from cirana.core import application_launcher
from cirana.core.application_registry import ApplicationRegistry
from cirana.core.playground import OpenOutcome, PlaygroundManager
from cirana.core.process_tracker import ProcessTracker
from cirana.core.workspaces import WorkspaceManager

log = logging.getLogger(__name__)


class AppActions(QObject):
    message = Signal(str)  # transient status text for the toast
    navigate = Signal(str, object)  # page key, optional payload (e.g. category id)
    drag_active = Signal(bool)  # an application card is being dragged (shows the workspace drop bar)

    def __init__(self, registry: ApplicationRegistry, tracker: ProcessTracker) -> None:
        super().__init__()
        self._registry = registry
        self._tracker = tracker
        self._parent: QWidget | None = None  # dialog / menu parent, set by the main window
        self.playground: PlaygroundManager | None = None  # set once the Playground exists
        self.workspace_id: Callable[[], str] = lambda: ""  # id of the active workspace
        self.workspaces: WorkspaceManager | None = None

    def set_parent_widget(self, widget: QWidget) -> None:
        self._parent = widget

    def launch(self, app_id: str, mode: str | None = None, allow_duplicate: bool = False) -> None:
        """Open an application the way its launch mode says: in a Playground tab or as a normal window."""
        app = self._registry.get(app_id)
        if app is None:
            return
        mode = mode or self._registry.launch_mode(app_id)
        if mode == "embedded" and self.playground is not None:
            outcome = self.playground.open_app(app_id, self.workspace_id(), allow_duplicate=allow_duplicate)
            self._report(app.name, outcome)
            return
        self._launch_external(app_id)

    def _report(self, name: str, outcome: OpenOutcome) -> None:
        if outcome.kind == "failed":
            log.warning("Launch failed for %s: %s", name, outcome.message)
            self._warn_launch_failed(outcome.message)
            return
        if outcome.kind in ("started", "activated"):
            self.navigate.emit("playground", None)
        else:  # opened as a normal window because it cannot be hosted
            QTimer.singleShot(2500, self._tracker.refresh)
        self.message.emit(outcome.message)

    def _launch_external(self, app_id: str) -> None:
        app = self._registry.get(app_id)
        if app is None:
            return
        result = application_launcher.launch(app)
        if result.ok:
            self._registry.record_launch(app_id)
            self.message.emit(f"Launched {app.name}")
            QTimer.singleShot(2500, self._tracker.refresh)
        else:
            log.warning("Launch failed for %s: %s", app.name, result.message)
            self._warn_launch_failed(result.message)

    def _warn_launch_failed(self, message: str) -> None:
        QMessageBox.warning(
            self._parent, "Could not launch",
            f"{message}\n\nThe application may have been moved or uninstalled. Try refreshing the application list.",
        )

    # -- workspaces --------------------------------------------------------------------------
    def add_to_workspace(self, app_id: str, workspace_id: str) -> None:
        app = self._registry.get(app_id)
        workspace = self.workspaces.get(workspace_id) if self.workspaces else None
        if app is None or workspace is None:
            return
        if self.workspaces.add_app(workspace_id, app_id):
            self.message.emit(f"Added {app.name} to {workspace.name}")
        else:
            self.message.emit(f"{app.name} is already in {workspace.name}")

    def add_to_new_workspace(self, app_id: str) -> None:
        if self.workspaces is None:
            return
        name, ok = QInputDialog.getText(self._parent, "New workspace", "Workspace name:")
        if not ok:
            return
        try:
            workspace = self.workspaces.create(name)
        except ValueError as exc:
            QMessageBox.warning(self._parent, "Cannot create workspace", str(exc))
            return
        self.add_to_workspace(app_id, workspace.id)

    def toggle_favorite(self, app_id: str) -> None:
        app = self._registry.get(app_id)
        state = self._registry.toggle_favorite(app_id)
        if app:
            self.message.emit(f"{'Added' if state else 'Removed'} {app.name} {'to' if state else 'from'} favorites")

    def hide(self, app_id: str) -> None:
        app = self._registry.get(app_id)
        self._registry.hide(app_id)
        if app:
            self.message.emit(f"Hid {app.name} (restore it in Settings)")

    def open_location(self, app_id: str) -> None:
        app = self._registry.get(app_id)
        path = app.exe_path or (app.target if app.launch_type in ("shortcut", "executable") else "") if app else ""
        if path and os.path.exists(path):
            subprocess.Popen(["explorer.exe", "/select,", os.path.normpath(path)])

    def show_menu(self, app_id: str, global_pos: QPoint) -> None:
        app = self._registry.get(app_id)
        if app is None:
            return
        menu = QMenu(self._parent)
        menu.addAction("Launch", lambda: self.launch(app_id))
        menu.addAction("Open in a Playground tab", lambda: self.launch(app_id, "embedded"))
        menu.addAction("Open in its own window", lambda: self.launch(app_id, "external"))
        mode_menu = menu.addMenu("Default launch mode")
        current_mode = self._registry.launch_mode(app_id)
        for mode_id, text in (("embedded", "Playground tab"), ("external", "Own window")):
            action = QAction(text, mode_menu, checkable=True, checked=(mode_id == current_mode))
            action.triggered.connect(lambda _=False, m=mode_id: self._registry.set_launch_mode(app_id, m))
            mode_menu.addAction(action)
        fav = self._registry.is_favorite(app_id)
        menu.addAction("Remove from favorites" if fav else "Add to favorites", lambda: self.toggle_favorite(app_id))
        if self.workspaces is not None:
            ws_menu = menu.addMenu("Add to workspace")
            for workspace in self.workspaces.all():
                action = QAction(workspace.name, ws_menu, checkable=True, checked=workspace.has(app_id))
                action.setEnabled(not workspace.has(app_id))
                action.triggered.connect(lambda _=False, w=workspace.id: self.add_to_workspace(app_id, w))
                ws_menu.addAction(action)
            if self.workspaces.all():
                ws_menu.addSeparator()
            ws_menu.addAction("New workspace…", lambda: self.add_to_new_workspace(app_id))
        menu.addSeparator()
        cat_menu = menu.addMenu("Category")
        current = self._registry.category_of(app)
        for cid, name in self._registry.categories():
            action = QAction(name, cat_menu, checkable=True, checked=(cid == current))
            action.triggered.connect(lambda _=False, c=cid: self._registry.set_category(app_id, c))
            cat_menu.addAction(action)
        cat_menu.addSeparator()
        cat_menu.addAction("Reset to automatic", lambda: self._registry.reset_category(app_id))
        if app.exe_path or app.launch_type in ("shortcut", "executable"):
            menu.addAction("Show in Explorer", lambda: self.open_location(app_id))
        menu.addSeparator()
        menu.addAction("Hide from CIRANA", lambda: self.hide(app_id))
        if app.custom:
            menu.addAction("Remove custom application", lambda: self._registry.remove_custom_app(app_id))
        menu.exec(global_pos)
