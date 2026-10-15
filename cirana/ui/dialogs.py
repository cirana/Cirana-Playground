"""Small modal dialogs used by the Playground and Workspaces pages."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QPushButton, QVBoxLayout, QWidget,
)

from cirana.core.background import run_async
from cirana.core.orphans import Orphan
from cirana.core.playground import hosting_kind
from cirana.native.window_discovery import WindowInfo, list_top_level_windows, own_pid
from cirana.native.window_embedder import DOCKED, EmbedStatus, check_compatibility, dock_compatibility, hosting_mode
from cirana.ui.common import label
from cirana.ui.context import AppContext


def _buttons(dialog: QDialog, ok_text: str, ok_name: str = "PrimaryButton") -> QDialogButtonBox:
    box = QDialogButtonBox()
    ok = box.addButton(ok_text, QDialogButtonBox.ButtonRole.AcceptRole)
    ok.setObjectName(ok_name)
    cancel = box.addButton("Cancel", QDialogButtonBox.ButtonRole.RejectRole)
    cancel.setObjectName("SecondaryButton")
    box.accepted.connect(dialog.accept)
    box.rejected.connect(dialog.reject)
    return box


class AppPickerDialog(QDialog):
    """Choose one or several applications from the registry."""

    def __init__(self, ctx: AppContext, title: str, ok_text: str = "Select", multi: bool = False,
                 exclude: set[str] | None = None, show_hosting: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.setWindowTitle(title)
        self.resize(520, 560)
        self._show_hosting = show_hosting
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
        reg = self.ctx.registry
        for app in reg.all_apps():
            if app.id in self._exclude:
                continue
            text = app.name
            if self._show_hosting:
                text += {"tab": "\nCan open in a Playground tab", "docked": "\nDocked over a Playground tab",
                         "external": "\nOpens in its own window"}[hosting_kind(app)]
            item = QListWidgetItem(text)
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
    """Lists the windows that are open right now (collected on a worker thread) and marks which can be hosted."""

    def __init__(self, hosted: set[int], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Host a running window")
        self.resize(560, 520)
        self._hosted = hosted
        self.chosen: WindowInfo | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)
        layout.addWidget(label("Host a running window", "SectionTitle"))
        layout.addWidget(label(
            "Pick a window that is already open. It moves into a Playground tab and can be detached again at any "
            "time. Applications that cannot be hosted safely are greyed out.", "Muted", wrap=True))
        self.status = label("Looking for windows…", "Muted")
        layout.addWidget(self.status)
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _item: self._accept_current())
        layout.addWidget(self.list, 1)
        box = _buttons(self, "Host window")
        box.accepted.disconnect()
        box.accepted.connect(self._accept_current)
        layout.addWidget(box)
        job = run_async(self._collect, self._show, lambda err: self.status.setText(f"Could not list windows: {err}"))
        self.finished.connect(lambda _result: job.cancel())  # a closed dialog must not receive late results

    def _collect(self, _cancel) -> list[tuple[WindowInfo, str, bool]]:
        rows = []
        for info in list_top_level_windows():
            if info.pid == own_pid() or info.hwnd in self._hosted or not info.title.strip():
                continue
            docked = hosting_mode(info) == DOCKED  # Store / UWP apps, consoles, Explorer: docked, never re-parented
            compat = dock_compatibility(info) if docked else check_compatibility(info)
            rows.append((info, "" if compat.status is EmbedStatus.OK else compat.reason, docked))
        rows.sort(key=lambda r: (bool(r[1]), r[0].title.lower()))
        return rows

    def _show(self, rows: list[tuple[WindowInfo, str, bool]]) -> None:
        self.list.clear()
        for info, reason, docked in rows:
            suffix = " (docked over the tab)" if docked and not reason else ""
            item = QListWidgetItem(f"{info.title}\n{info.exe_name}{suffix}")
            item.setData(Qt.ItemDataRole.UserRole, info)
            if reason:
                item.setFlags(Qt.ItemFlag.NoItemFlags)
                item.setToolTip(reason)
            self.list.addItem(item)
        usable = sum(1 for _i, reason, _docked in rows if not reason)
        self.status.setText(f"{usable} of {len(rows)} windows can be hosted." if rows else "No windows found.")
        for row in range(self.list.count()):
            if self.list.item(row).flags() & Qt.ItemFlag.ItemIsEnabled:
                self.list.setCurrentRow(row)
                break

    def _accept_current(self) -> None:
        item = self.list.currentItem()
        if item is not None and item.flags() & Qt.ItemFlag.ItemIsEnabled:
            self.chosen = item.data(Qt.ItemDataRole.UserRole)
            self.accept()


class OrphanDialog(QDialog):
    """Applications that lost their window when CIRANA ended unexpectedly. Nothing is ended without a choice."""

    def __init__(self, orphans: list[Orphan], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Applications without a window")
        self.resize(520, 380)
        self.orphans = orphans
        self.boxes: list[tuple[QCheckBox, Orphan]] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)
        layout.addWidget(label("CIRANA ended while hosting applications", "SectionTitle"))
        layout.addWidget(label(
            "These applications are still running but their windows were closed together with CIRANA, so they are "
            "invisible. Leave them alone to keep them running, or tick the ones you want ended. Ending an "
            "application loses any unsaved work in it.", "Muted", wrap=True))
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
