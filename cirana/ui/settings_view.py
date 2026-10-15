"""Settings: scanning, categories, custom applications, hidden applications, data location."""
from __future__ import annotations

import os
import time

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox, QFileDialog, QFrame, QHBoxLayout, QInputDialog, QListWidget, QListWidgetItem, QMessageBox,
    QPushButton, QVBoxLayout, QWidget,
)

from cirana import __version__
from cirana.core.models import LaunchType
from cirana.ui.common import label, make_scroll
from cirana.ui.context import AppContext


def _card(title: str, hint: str = "") -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("Card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(20, 16, 20, 18)
    layout.setSpacing(10)
    layout.addWidget(label(title, "SectionTitle"))
    if hint:
        layout.addWidget(label(hint, "Muted", wrap=True))
    return frame, layout


def _button(text: str, handler, name: str = "SecondaryButton") -> QPushButton:
    button = QPushButton(text)
    button.setObjectName(name)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.clicked.connect(handler)
    return button


class SettingsView(QWidget):
    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        body = QWidget()
        root = QVBoxLayout(body)
        root.setContentsMargins(28, 24, 28, 28)
        root.setSpacing(14)
        root.addWidget(label("Settings", "PageTitle"))
        root.addWidget(label(f"CIRANA {__version__} · everything stays on this computer", "PageSubtitle"))

        # General / scanning
        card, lay = _card("Application discovery",
                          "Reads the Start Menu, packaged apps and App Paths registrations. No disk-wide scan.")
        self.scan_on_start = QCheckBox("Scan for applications when CIRANA starts")
        self.scan_on_start.setChecked(bool(ctx.settings.get("scan_on_startup")))
        self.scan_on_start.toggled.connect(lambda v: ctx.settings.set("scan_on_startup", bool(v)))
        lay.addWidget(self.scan_on_start)
        row = QHBoxLayout()
        self.scan_button = _button("Scan now", ctx.scanner.start, "PrimaryButton")
        row.addWidget(self.scan_button)
        self.scan_status = label("", "Muted", wrap=True)
        row.addWidget(self.scan_status, 1)
        lay.addLayout(row)
        root.addWidget(card)

        # Categories
        card, lay = _card("Categories", "Rename any category or add your own. Changes to individual apps are never overwritten by a rescan.")
        self.category_list = QListWidget()
        self.category_list.setFixedHeight(190)
        lay.addWidget(self.category_list)
        row = QHBoxLayout()
        row.addWidget(_button("Rename…", self._rename_category))
        row.addWidget(_button("New category…", self._add_category))
        row.addWidget(_button("Delete", self._delete_category, "DangerButton"))
        row.addStretch(1)
        lay.addLayout(row)
        root.addWidget(card)

        # Custom apps
        card, lay = _card("Custom applications", "Executables or commands you added yourself.")
        self.custom_list = QListWidget()
        self.custom_list.setFixedHeight(110)
        lay.addWidget(self.custom_list)
        row = QHBoxLayout()
        row.addWidget(_button("Add executable…", self._add_exe))
        row.addWidget(_button("Add command…", self._add_command))
        row.addWidget(_button("Remove", self._remove_custom, "DangerButton"))
        row.addStretch(1)
        lay.addLayout(row)
        root.addWidget(card)

        # Hidden
        card, lay = _card("Hidden applications", "Applications you removed from CIRANA's views.")
        self.hidden_list = QListWidget()
        self.hidden_list.setFixedHeight(110)
        lay.addWidget(self.hidden_list)
        row = QHBoxLayout()
        row.addWidget(_button("Restore selected", self._unhide))
        row.addStretch(1)
        lay.addLayout(row)
        root.addWidget(card)

        # Data
        card, lay = _card("Your data")
        lay.addWidget(label(str(ctx.settings.root), "Muted"))
        row = QHBoxLayout()
        row.addWidget(_button("Open data folder", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(ctx.settings.root)))))
        row.addWidget(_button("Open log folder", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(ctx.settings.root / "logs")))))
        row.addStretch(1)
        lay.addLayout(row)
        root.addWidget(card)
        root.addStretch(1)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(make_scroll(body))

    # -- refresh -------------------------------------------------------------
    def refresh(self) -> None:
        reg = self.ctx.registry
        self.scan_button.setEnabled(not self.ctx.scanner.scanning)
        report = self.ctx.scanner.last_report
        if self.ctx.scanner.scanning:
            self.scan_status.setText("Scanning…")
        elif report is not None:
            counts = report.counts
            extra = f" · {len(report.errors)} source error(s), see log" if report.errors else ""
            self.scan_status.setText(
                f"Found {counts.get('total', 0)} applications "
                f"(Start Menu {counts.get('start_menu', 0)}, packaged {counts.get('packaged', 0)}, "
                f"registry {counts.get('registry', 0)}){extra}"
            )
        elif reg.scanned_at:
            self.scan_status.setText("Last scan: " + time.strftime("%Y-%m-%d %H:%M", time.localtime(reg.scanned_at)))
        else:
            self.scan_status.setText("No scan has run yet.")

        self._fill(self.category_list, [(f"{name}  ({len(reg.apps_in_category(cid))})", cid) for cid, name in reg.categories()])
        self._fill(self.custom_list, [(a.name, a.id) for a in reg.all_apps(include_hidden=True) if a.custom])
        self._fill(self.hidden_list, [(a.name, a.id) for a in reg.hidden_apps()])

    @staticmethod
    def _fill(widget: QListWidget, rows: list[tuple[str, str]]) -> None:
        selected = widget.currentItem().data(Qt.ItemDataRole.UserRole) if widget.currentItem() else None
        widget.clear()
        for text, key in rows:
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, key)
            widget.addItem(item)
            if key == selected:
                widget.setCurrentItem(item)

    @staticmethod
    def _selected(widget: QListWidget) -> str | None:
        item = widget.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    # -- actions --------------------------------------------------------------
    def _rename_category(self) -> None:
        cid = self._selected(self.category_list)
        if not cid:
            return
        name, ok = QInputDialog.getText(self, "Rename category", "New name:", text=self.ctx.registry.category_name(cid))
        if ok:
            self._try(lambda: self.ctx.registry.rename_category(cid, name))

    def _add_category(self) -> None:
        name, ok = QInputDialog.getText(self, "New category", "Category name:")
        if ok:
            self._try(lambda: self.ctx.registry.add_category(name))

    def _delete_category(self) -> None:
        cid = self._selected(self.category_list)
        if cid:
            self._try(lambda: self.ctx.registry.delete_category(cid))

    def _add_exe(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose an executable", "", "Applications (*.exe *.bat *.cmd)")
        if not path:
            return
        name, ok = QInputDialog.getText(self, "Add application", "Display name:", text=os.path.splitext(os.path.basename(path))[0])
        if ok:
            self._try(lambda: self.ctx.registry.add_custom_app(name, path, LaunchType.EXECUTABLE.value))

    def _add_command(self) -> None:
        name, ok = QInputDialog.getText(self, "Add command", "Display name:")
        if not ok or not name.strip():
            return
        command, ok = QInputDialog.getText(self, "Add command", "Command line (runs exactly as typed, without a shell):")
        if ok:
            self._try(lambda: self.ctx.registry.add_custom_app(name, command, LaunchType.COMMAND.value))

    def _remove_custom(self) -> None:
        app_id = self._selected(self.custom_list)
        if app_id:
            self.ctx.registry.remove_custom_app(app_id)

    def _unhide(self) -> None:
        app_id = self._selected(self.hidden_list)
        if app_id:
            self.ctx.registry.unhide(app_id)

    def _try(self, func) -> None:
        try:
            func()
        except ValueError as exc:
            QMessageBox.warning(self, "CIRANA", str(exc))
