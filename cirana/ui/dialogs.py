"""Small modal dialogs used by the Workspaces and Windows pages."""
from __future__ import annotations

import hashlib

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout, QLineEdit, QListWidget, QListWidgetItem,
    QPushButton, QVBoxLayout, QWidget,
)

from cirana.core.legacy_recovery import Orphan
from cirana.core.models import AppEntry, LaunchType
from cirana.ui.common import label
from cirana.ui.context import AppContext
from cirana.wm.identity import TrackedWindow


def _buttons(dialog: QDialog, ok_text: str, ok_name: str = "PrimaryButton") -> QDialogButtonBox:
    box = QDialogButtonBox()
    ok = box.addButton(ok_text, QDialogButtonBox.ButtonRole.AcceptRole)
    ok.setObjectName(ok_name)
    cancel = box.addButton("Cancel", QDialogButtonBox.ButtonRole.RejectRole)
    cancel.setObjectName("SecondaryButton")
    box.accepted.connect(dialog.accept)
    box.rejected.connect(dialog.reject)
    return box


def window_icon_app(ctx: AppContext, window: TrackedWindow) -> AppEntry:
    """What to take a window's icon from: its catalog application (packaged apps have no icon file), else its
    executable."""
    app = ctx.workspaces.app_for_window(window)
    if app is not None:
        return app
    exe = window.app_exe
    return AppEntry(id="window-" + hashlib.sha1(exe.encode("utf-8")).hexdigest()[:12], name=window.display_title,
                    launch_type=LaunchType.EXECUTABLE.value, target=exe or window.display_title, source="user",
                    exe_path=exe)


def window_subtitle(window: TrackedWindow) -> str:
    state = " · minimized" if window.minimized else " · maximized" if window.maximized else ""
    return f"{window.exe_name or 'unknown program'}{state}"


class AppPickerDialog(QDialog):
    """Choose one or several applications from the registry."""

    def __init__(self, ctx: AppContext, title: str, ok_text: str = "Select", multi: bool = False,
                 exclude: set[str] | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.setWindowTitle(title)
        self.resize(520, 560)
        self._exclude = exclude or set()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)
        layout.addWidget(label(title, "SectionTitle"))
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter applications…")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self._apply_filter)
        layout.addWidget(self.filter)
        self.list = QListWidget()
        self.list.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection if multi else QAbstractItemView.SelectionMode.SingleSelection
        )
        self.list.setIconSize(self.list.iconSize() * 1.4)
        self.list.itemDoubleClicked.connect(lambda _item: self.accept())
        layout.addWidget(self.list, 1)
        layout.addWidget(_buttons(self, ok_text))
        self._fill()
        self.filter.setFocus()

    def _fill(self) -> None:
        for app in self.ctx.registry.all_apps():
            if app.id in self._exclude:
                continue
            item = QListWidgetItem(app.name)
            item.setData(Qt.ItemDataRole.UserRole, app.id)
            item.setData(Qt.ItemDataRole.UserRole + 1, app.name.lower())
            self.list.addItem(item)
            self.ctx.icons.request(app, 32, self, lambda pix, it=item: it.setIcon(QIcon(pix)))
        if self.list.count():
            self.list.setCurrentRow(0)

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().lower()
        for row in range(self.list.count()):
            item = self.list.item(row)
            item.setHidden(bool(needle) and needle not in item.data(Qt.ItemDataRole.UserRole + 1))

    def selected_ids(self) -> list[str]:
        return [item.data(Qt.ItemDataRole.UserRole) for item in self.list.selectedItems() if not item.isHidden()]


class WindowPickerDialog(QDialog):
    """The application windows open right now, kept up to date while the dialog is open (from the window tracker,
    so listing them never blocks). Several can be picked."""

    def __init__(self, ctx: AppContext, title: str = "Add open windows", ok_text: str = "Add",
                 exclude: set[str] | None = None, multi: bool = True, windows: list[TrackedWindow] | None = None,
                 hint: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.setWindowTitle(title)
        self.resize(560, 520)
        self._exclude = exclude or set()
        self._fixed = windows  # a given list (choosing among candidates) instead of every open window
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)
        layout.addWidget(label(title, "SectionTitle"))
        layout.addWidget(label(hint or "Windows that are open right now. The list follows windows as they open and "
                                       "close.", "Muted", wrap=True))
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter windows…")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self._apply_filter)
        layout.addWidget(self.filter)
        self.status = label("", "Muted")
        layout.addWidget(self.status)
        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection if multi
                                   else QAbstractItemView.SelectionMode.SingleSelection)
        self.list.setIconSize(self.list.iconSize() * 1.4)
        self.list.itemDoubleClicked.connect(lambda _item: self.accept())
        layout.addWidget(self.list, 1)
        layout.addWidget(_buttons(self, ok_text))
        self._refresh_timer = QTimer(self, singleShot=True, interval=150)
        self._refresh_timer.timeout.connect(self.refresh)
        if windows is None:
            for signal in (ctx.windows.window_added, ctx.windows.window_removed, ctx.windows.window_changed):
                signal.connect(self._schedule)
            self.finished.connect(self._disconnect)
        self.refresh()

    def _schedule(self, _window: object = None) -> None:
        self._refresh_timer.start()

    def _disconnect(self, _result: int = 0) -> None:
        for signal in (self.ctx.windows.window_added, self.ctx.windows.window_removed,
                       self.ctx.windows.window_changed):
            try:
                signal.disconnect(self._schedule)
            except (RuntimeError, TypeError):
                pass

    def windows(self) -> list[TrackedWindow]:
        if self._fixed is not None:
            return [w for w in self._fixed if self.ctx.windows.get(w.key) is not None]
        return [w for w in self.ctx.windows.windows() if w.key not in self._exclude]

    def refresh(self) -> None:
        selected = {item.data(Qt.ItemDataRole.UserRole) for item in self.list.selectedItems()}
        current = self.list.currentItem().data(Qt.ItemDataRole.UserRole) if self.list.currentItem() else ""
        self.list.clear()
        windows = self.windows()
        for window in windows:
            item = QListWidgetItem(f"{window.display_title}\n{window_subtitle(window)}")
            item.setData(Qt.ItemDataRole.UserRole, window.key)
            item.setData(Qt.ItemDataRole.UserRole + 1, f"{window.title} {window.exe_name}".lower())
            item.setToolTip(window.app_exe)
            self.list.addItem(item)
            item.setSelected(window.key in selected)
            if window.key == current:
                self.list.setCurrentItem(item)
            self.ctx.icons.request(window_icon_app(self.ctx, window), 32, self,
                                   lambda pix, it=item: it.setIcon(QIcon(pix)))
        if self.list.count() and self.list.currentItem() is None:
            self.list.setCurrentRow(0)
        if not self.ctx.windows.ready and self._fixed is None:
            self.status.setText("Looking for windows…")
        else:
            self.status.setText(f"{len(windows)} window{'s' if len(windows) != 1 else ''}" if windows
                                else "No application windows are open.")
        self._apply_filter(self.filter.text())

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().lower()
        for row in range(self.list.count()):
            item = self.list.item(row)
            item.setHidden(bool(needle) and needle not in item.data(Qt.ItemDataRole.UserRole + 1))

    def selected_windows(self) -> list[TrackedWindow]:
        keys = [item.data(Qt.ItemDataRole.UserRole) for item in self.list.selectedItems() if not item.isHidden()]
        if not keys and self.list.currentItem() is not None and not self.list.currentItem().isHidden():
            keys = [self.list.currentItem().data(Qt.ItemDataRole.UserRole)]
        return [w for w in (self.ctx.windows.get(k) for k in keys) if w is not None]


def choose_window(ctx: AppContext, entry_name: str, candidates: list[TrackedWindow],
                  parent: QWidget | None = None, hint: str = "") -> TrackedWindow | None:
    """Ask which of several open windows belongs to a workspace entry."""
    dialog = WindowPickerDialog(
        ctx, f"Which window is {entry_name}?", "Use this window", multi=False, windows=candidates,
        hint=hint or f"{len(candidates)} open window{'s' if len(candidates) != 1 else ''} fit {entry_name}. Pick the "
                     "one that belongs to this workspace; CIRANA remembers the choice.", parent=parent)
    if dialog.exec():
        chosen = dialog.selected_windows()
        return chosen[0] if chosen else None
    return None


class OrphanDialog(QDialog):
    """Applications left without a window by an earlier (embedding) CIRANA. Nothing is ended without a choice."""

    def __init__(self, orphans: list[Orphan], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Applications without a window")
        self.resize(520, 380)
        self.orphans = orphans
        self.boxes: list[tuple[QCheckBox, Orphan]] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)
        layout.addWidget(label("An earlier CIRANA ended while hosting applications", "SectionTitle"))
        layout.addWidget(label(
            "These applications are still running but their windows were closed together with the earlier version "
            "of CIRANA, so they are invisible. (This version no longer embeds windows, so this cannot happen again.) "
            "Leave them alone to keep them running, or tick the ones you want ended. Ending an application loses any "
            "unsaved work in it.", "Muted", wrap=True))
        for orphan in orphans:
            box = QCheckBox(f"{orphan.title}   (pid {orphan.pid})")
            box.setToolTip(orphan.exe)
            layout.addWidget(box)
            self.boxes.append((box, orphan))
        layout.addStretch(1)
        row = QHBoxLayout()
        row.addStretch(1)
        keep = QPushButton("Leave them running")
        keep.setObjectName("SecondaryButton")
        keep.clicked.connect(self.reject)
        end = QPushButton("End selected…")
        end.setObjectName("DangerButton")
        end.clicked.connect(self.accept)
        row.addWidget(keep)
        row.addWidget(end)
        layout.addLayout(row)

    def selected(self) -> list[Orphan]:
        return [orphan for box, orphan in self.boxes if box.isChecked()]
