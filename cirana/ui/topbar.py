"""Top bar: global search, workspace name, quick launch, view mode, settings."""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QSize, Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QButtonGroup, QFrame, QHBoxLayout, QLineEdit, QPushButton

from cirana.ui.icons import icon
from cirana.ui.theme import ON_ACCENT, TEXT2


class TopBar(QFrame):
    search_requested = Signal(str)
    workspace_clicked = Signal()
    settings_clicked = Signal()
    view_mode_changed = Signal(str)

    def __init__(self, view_mode: str) -> None:
        super().__init__()
        self.setObjectName("TopBar")
        self.setFixedHeight(64)
        row = QHBoxLayout(self)
        row.setContentsMargins(20, 10, 20, 10)
        row.setSpacing(10)

        self.search = QLineEdit()
        self.search.setObjectName("TopSearch")
        self.search.setPlaceholderText("Search applications…   Ctrl+K")
        self.search.setMinimumWidth(260)
        self.search.setMaximumWidth(520)
        self.search.addAction(QAction(icon("search", TEXT2, 16), "", self.search), QLineEdit.ActionPosition.LeadingPosition)
        self.search.installEventFilter(self)
        row.addWidget(self.search, 1)
        row.addStretch(1)

        self.workspace = QPushButton("No workspace")
        self.workspace.setObjectName("WorkspacePill")
        self.workspace.setIcon(icon("workspaces", TEXT2, 16))
        self.workspace.setToolTip("Active workspace. Click to switch")
        self.workspace.setCursor(Qt.CursorShape.PointingHandCursor)
        self.workspace.clicked.connect(self.workspace_clicked)
        row.addWidget(self.workspace)

        self.quick = QPushButton(" Quick launch")
        self.quick.setObjectName("PrimaryButton")
        self.quick.setIcon(icon("bolt", ON_ACCENT, 16))
        self.quick.setCursor(Qt.CursorShape.PointingHandCursor)
        self.quick.clicked.connect(lambda: self.search_requested.emit(""))
        row.addWidget(self.quick)

        self.mode_group = QButtonGroup(self)
        self.grid_button = self._mode_button("apps", "Grid view", "grid")
        self.list_button = self._mode_button("list", "List view", "list")
        (self.grid_button if view_mode == "grid" else self.list_button).setChecked(True)
        row.addWidget(self.grid_button)
        row.addWidget(self.list_button)

        self.settings_button = QPushButton()
        self.settings_button.setObjectName("IconButton")
        self.settings_button.setIcon(icon("settings", TEXT2, 18))
        self.settings_button.setIconSize(QSize(18, 18))
        self.settings_button.setToolTip("Settings")
        self.settings_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.settings_button.clicked.connect(self.settings_clicked)
        row.addWidget(self.settings_button)

    def _mode_button(self, glyph: str, tip: str, mode: str) -> QPushButton:
        button = QPushButton()
        button.setObjectName("IconButton")
        button.setCheckable(True)
        button.setIcon(icon(glyph, TEXT2, 18))
        button.setToolTip(tip)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mode_group.addButton(button)
        button.clicked.connect(lambda: self.view_mode_changed.emit(mode))
        return button

    def set_workspace_name(self, name: str) -> None:
        self.workspace.setText(f" {name or 'No workspace'}")

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if obj is self.search and event.type() in (QEvent.Type.FocusIn, QEvent.Type.MouseButtonPress):
            self.search_requested.emit(self.search.text())
            self.search.clearFocus()
            return event.type() == QEvent.Type.MouseButtonPress
        return super().eventFilter(obj, event)
