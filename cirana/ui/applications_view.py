"""Applications (and Favorites) browsing view."""
from __future__ import annotations

import os

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QInputDialog, QLineEdit, QMessageBox, QPushButton, QVBoxLayout, QWidget

from cirana.core.models import LaunchType
from cirana.ui.common import EmptyState, label, make_scroll
from cirana.ui.context import AppContext
from cirana.ui.icons import icon
from cirana.ui.theme import ON_ACCENT, TEXT
from cirana.widgets.app_collection import AppCollection
from cirana.widgets.flow_layout import FlowLayout


class ApplicationsView(QWidget):
    def __init__(self, ctx: AppContext, favorites_only: bool = False) -> None:
        super().__init__()
        self.ctx = ctx
        self.favorites_only = favorites_only
        self._category: str | None = None  # None = all
        self._text = ""

        body = QWidget()
        root = QVBoxLayout(body)
        root.setContentsMargins(28, 24, 28, 28)
        root.setSpacing(14)

        head = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(0)
        titles.addWidget(label("Favorites" if favorites_only else "Applications", "PageTitle"))
        self.subtitle = label("", "PageSubtitle")
        titles.addWidget(self.subtitle)
        head.addLayout(titles)
        head.addStretch(1)
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter this list…")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.setFixedWidth(220)
        self._debounce = QTimer(self, singleShot=True, interval=120)
        self._debounce.timeout.connect(self._apply_filter_text)
        self.filter_edit.textChanged.connect(lambda _t: self._debounce.start())
        head.addWidget(self.filter_edit)
        if not favorites_only:
            self.scan_button = QPushButton(" Refresh")
            self.scan_button.setObjectName("SecondaryButton")
            self.scan_button.setIcon(icon("refresh", TEXT, 16))
            self.scan_button.clicked.connect(self.ctx.scanner.start)
            head.addWidget(self.scan_button)
            add_button = QPushButton(" Add application")
            add_button.setObjectName("PrimaryButton")
            add_button.setIcon(icon("plus", ON_ACCENT, 16))
            add_button.clicked.connect(self._add_application)
            head.addWidget(add_button)
        root.addLayout(head)

        self.chip_host = QWidget()
        self.chip_layout = FlowLayout(self.chip_host, spacing=8)
        self.chip_host.setVisible(not favorites_only)
        root.addWidget(self.chip_host)

        self.collection = AppCollection(ctx.registry, ctx.actions, ctx.icons)
        root.addWidget(self.collection)
        self.empty = EmptyState(
            "star" if favorites_only else "apps",
            "No favorites yet" if favorites_only else "Nothing matches",
            "Use the star on any application card to pin it here." if favorites_only
            else "Try a different category or filter, or refresh the application list.",
        )
        root.addWidget(self.empty)
        root.addStretch(1)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(make_scroll(body))

    # -- public ------------------------------------------------------------
    def set_category(self, category_id: str | None) -> None:
        self._category = category_id
        self.refresh()

    # -- internals ----------------------------------------------------------
    def _apply_filter_text(self) -> None:
        self._text = self.filter_edit.text().strip().lower()
        self.refresh()

    def _visible_apps(self):
        reg = self.ctx.registry
        apps = reg.favorites() if self.favorites_only else reg.all_apps()
        if self._category and not self.favorites_only:
            apps = [a for a in apps if reg.category_of(a) == self._category]
        if self._text:
            apps = [a for a in apps if self._text in a.name.lower()
                    or self._text in reg.category_name(reg.category_of(a)).lower()]
        return apps

    def _rebuild_chips(self) -> None:
        reg = self.ctx.registry
        while self.chip_layout.count():
            item = self.chip_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        counts = [(cid, name, len(reg.apps_in_category(cid))) for cid, name in reg.categories()]
        if self._category and not any(c[0] == self._category for c in counts):
            self._category = None
        entries = [(None, "All", len(reg.all_apps()))] + [c for c in counts if c[2] or c[0] == self._category]
        for cid, name, count in entries:
            chip = QPushButton(f"{name}  {count}")
            chip.setObjectName("Chip")
            chip.setCheckable(True)
            chip.setChecked(cid == self._category)
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.clicked.connect(lambda _=False, c=cid: self.set_category(c))
            self.chip_layout.addWidget(chip)
        self.chip_host.updateGeometry()

    def refresh(self) -> None:
        if not self.favorites_only:
            self._rebuild_chips()
            self.scan_button.setEnabled(not self.ctx.scanner.scanning)
        apps = self._visible_apps()
        self.collection.set_apps(apps, self.ctx.view_mode, self.ctx.tracker.running)
        self.collection.setVisible(bool(apps))
        self.empty.setVisible(not apps)
        total = len(self.ctx.registry.favorites() if self.favorites_only else self.ctx.registry.all_apps())
        self.subtitle.setText(f"{len(apps)} of {total} shown" if len(apps) != total else f"{total} applications")

    def _add_application(self) -> None:
        reg = self.ctx.registry
        path, _ = QFileDialog.getOpenFileName(self, "Choose an executable", "", "Applications (*.exe *.bat *.cmd);;All files (*)")
        if not path:
            return
        default = os.path.splitext(os.path.basename(path))[0]
        name, ok = QInputDialog.getText(self, "Add application", "Display name:", text=default)
        if not ok or not name.strip():
            return
        try:
            app = reg.add_custom_app(name, path, LaunchType.EXECUTABLE.value)
        except ValueError as exc:
            QMessageBox.warning(self, "Cannot add application", str(exc))
            return
        self.ctx.actions.message.emit(f"Added {app.name}")
