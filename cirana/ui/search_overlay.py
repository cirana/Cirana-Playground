"""Ctrl+K command-palette style search over applications, categories and workspaces."""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QIcon, QKeyEvent
from PySide6.QtWidgets import QFrame, QLabel, QLineEdit, QListWidget, QListWidgetItem, QVBoxLayout, QWidget

from cirana.core.search import SearchResult, search
from cirana.ui.context import AppContext
from cirana.ui.icons import icon
from cirana.ui.theme import ACCENT


class SearchOverlay(QWidget):
    """Child widget covering the main window (not a separate top-level window, so focus stays in CIRANA)."""

    navigate = Signal(str, object)
    visibility_changed = Signal(bool)

    def __init__(self, ctx: AppContext, parent: QWidget) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.setObjectName("OverlayScrim")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.hide()

        self.panel = QFrame(self)
        self.panel.setObjectName("SearchPanel")
        self.panel.setFixedWidth(640)
        layout = QVBoxLayout(self.panel)
        layout.setContentsMargins(0, 0, 0, 8)
        layout.setSpacing(0)
        self.input = QLineEdit()
        self.input.setObjectName("SearchInput")
        self.input.setPlaceholderText("Search applications, categories and workspaces…")
        self.input.textChanged.connect(self._refresh)
        self.input.installEventFilter(self)
        layout.addWidget(self.input)
        self.results = QListWidget()
        self.results.setFixedHeight(360)
        self.results.setIconSize(self.results.iconSize() * 1.6)
        self.results.itemActivated.connect(self._activate_item)
        self.results.itemClicked.connect(self._activate_item)
        layout.addWidget(self.results)
        self.hint = QLabel("↑ ↓ navigate   ·   Enter open   ·   Esc close")
        self.hint.setObjectName("Muted")
        self.hint.setContentsMargins(18, 6, 0, 2)
        layout.addWidget(self.hint)

        self.workspace_provider = lambda: []  # the main window plugs the workspaces in
        parent.installEventFilter(self)

    # -- lifecycle --------------------------------------------------------------
    def open(self, text: str = "") -> None:
        self.setGeometry(self.parentWidget().rect())
        self.raise_()
        self.show()
        self.visibility_changed.emit(True)
        self._reposition()
        self.input.setText(text)
        self._refresh()
        self.input.setFocus()
        self.input.selectAll()

    def close_overlay(self) -> None:
        self.hide()
        self.visibility_changed.emit(False)
        self.input.clear()
        self.parentWidget().setFocus()

    def _reposition(self) -> None:
        self.panel.adjustSize()
        self.panel.move((self.width() - self.panel.width()) // 2, max(40, int(self.height() * 0.14)))

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if obj is self.parentWidget() and event.type() == QEvent.Type.Resize and self.isVisible():
            self.setGeometry(self.parentWidget().rect())
            self._reposition()
        if obj is self.input and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
                self._move(1 if key == Qt.Key.Key_Down else -1)
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._activate_item(self.results.currentItem())
                return True
            if key == Qt.Key.Key_Escape:
                self.close_overlay()
                return True
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.close_overlay()
        else:
            super().keyPressEvent(event)

    def mousePressEvent(self, event) -> None:
        if not self.panel.geometry().contains(event.position().toPoint()):
            self.close_overlay()

    # -- results ------------------------------------------------------------------
    def _move(self, delta: int) -> None:
        count = self.results.count()
        if count:
            self.results.setCurrentRow((self.results.currentRow() + delta) % count)

    def _refresh(self) -> None:
        reg = self.ctx.registry
        apps = [(a.id, a.name, reg.category_name(reg.category_of(a))) for a in reg.all_apps()]
        categories = [(cid, name, len(reg.apps_in_category(cid))) for cid, name in reg.categories()]
        found = search(
            self.input.text(), apps, categories, self.workspace_provider(), [a.id for a in reg.recents(30)]
        )
        self.results.clear()
        if not self.input.text().strip() and not found:
            self._placeholder("Type to search. Applications you launch will be listed here.")
            return
        if not found:
            self._placeholder("No results")
            return
        for result in found:
            item = QListWidgetItem(f"{result.title}\n{result.subtitle}")
            item.setData(Qt.ItemDataRole.UserRole, result)
            if result.kind == "app":
                app = reg.get(result.key)
                if app:
                    item.setIcon(QIcon(self.ctx.icons.pixmap(app, 32)))
            else:
                item.setIcon(icon("tag" if result.kind == "category" else "workspaces", ACCENT, 24))
            self.results.addItem(item)
        self.results.setCurrentRow(0)

    def _placeholder(self, text: str) -> None:
        item = QListWidgetItem(text)
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        self.results.addItem(item)

    def _activate_item(self, item: QListWidgetItem | None) -> None:
        if item is None:
            return
        result: SearchResult | None = item.data(Qt.ItemDataRole.UserRole)
        if result is None:
            return
        self.close_overlay()
        if result.kind == "app":
            self.ctx.actions.launch(result.key)
        elif result.kind == "category":
            self.navigate.emit("applications", result.key)
        else:
            self.navigate.emit("workspaces", result.key)
