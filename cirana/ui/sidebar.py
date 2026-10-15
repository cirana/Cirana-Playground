"""Collapsible left navigation."""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import QButtonGroup, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from cirana.ui.icons import icon
from cirana.ui.theme import ACCENT, TEXT2, app_icon

NAV_ITEMS: list[tuple[str, str, str]] = [
    ("home", "Home", "home"),
    ("applications", "Applications", "apps"),
    ("workspaces", "Workspaces", "workspaces"),
    ("playground", "Playground", "playground"),
    ("favorites", "Favorites", "star"),
    ("settings", "Settings", "settings"),
]
EXPANDED_WIDTH = 224
COLLAPSED_WIDTH = 68


class Sidebar(QFrame):
    page_selected = Signal(str)
    collapsed_changed = Signal(bool)

    def __init__(self, collapsed: bool = False) -> None:
        super().__init__()
        self.setObjectName("Sidebar")
        self._collapsed = collapsed
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 16, 12, 12)
        root.setSpacing(4)

        brand = QHBoxLayout()
        brand.setContentsMargins(4, 0, 0, 0)
        self.mark = QLabel()
        self.mark.setObjectName("BrandMark")
        self.mark.setFixedSize(34, 34)
        self.mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.mark.setPixmap(app_icon().pixmap(QSize(34, 34)))
        self.name = QLabel("CIRANA")
        self.name.setObjectName("BrandName")
        brand.addWidget(self.mark)
        brand.addSpacing(8)
        brand.addWidget(self.name)
        brand.addStretch(1)
        root.addLayout(brand)
        root.addSpacing(18)

        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons: dict[str, QPushButton] = {}
        for key, text, glyph in NAV_ITEMS:
            button = QPushButton(text)
            button.setObjectName("NavButton")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setIconSize(QSize(20, 20))
            button.setProperty("glyph", glyph)
            button.setToolTip(text)
            button.clicked.connect(lambda _=False, k=key: self.page_selected.emit(k))
            self.group.addButton(button)
            self.buttons[key] = button
            root.addWidget(button)
        root.addStretch(1)

        self.toggle = QPushButton()
        self.toggle.setObjectName("SidebarToggle")
        self.toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggle.clicked.connect(lambda: self.set_collapsed(not self._collapsed))
        root.addWidget(self.toggle)
        self.set_collapsed(collapsed, emit=False)

    @property
    def collapsed(self) -> bool:
        return self._collapsed

    def select(self, key: str) -> None:
        if key in self.buttons:
            self.buttons[key].setChecked(True)

    def set_collapsed(self, collapsed: bool, emit: bool = True) -> None:
        self._collapsed = collapsed
        self.setFixedWidth(COLLAPSED_WIDTH if collapsed else EXPANDED_WIDTH)
        self.name.setVisible(not collapsed)
        for key, text, glyph in NAV_ITEMS:
            button = self.buttons[key]
            button.setText("" if collapsed else f"  {text}")
            button.setIcon(icon(glyph, ACCENT if button.isChecked() else TEXT2, 20))
        self.toggle.setIcon(icon("expand" if collapsed else "collapse", TEXT2, 18))
        self.toggle.setToolTip("Expand sidebar" if collapsed else "Collapse sidebar")
        if emit:
            self.collapsed_changed.emit(collapsed)

    def refresh_icons(self) -> None:
        for key, _text, glyph in NAV_ITEMS:
            button = self.buttons[key]
            button.setIcon(icon(glyph, ACCENT if button.isChecked() else TEXT2, 20))
