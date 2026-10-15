"""Application card (grid) and row (list) widgets."""
from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QContextMenuEvent, QDrag, QMouseEvent
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

from cirana.core.models import AppEntry
from cirana.ui.icons import icon
from cirana.ui.theme import TEXT2, WARN
from cirana.widgets.dnd import app_mime

ICON_SIZE = 40


class _AppItemBase(QFrame):
    launch_requested = Signal(str)
    favorite_toggled = Signal(str)
    context_requested = Signal(str, QPoint)
    drag_state_changed = Signal(bool)  # a drag of this application started / ended

    def __init__(self, app: AppEntry) -> None:
        super().__init__()
        self.app_id = app.id
        self._icon_loaded = False
        self._icon_provider = None
        self._app = app
        self._press_pos: QPoint | None = None
        self.icon_label = QLabel()
        self.icon_label.setObjectName("IconTile")
        self.icon_label.setFixedSize(ICON_SIZE + 12, ICON_SIZE + 12)
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.name_label = QLabel()
        self.name_label.setObjectName("AppName")
        self.category_label = QLabel()
        self.category_label.setObjectName("AppCategory")
        self.running_dot = QLabel()
        self.running_dot.setObjectName("RunningDot")
        self.running_dot.setFixedSize(8, 8)
        self.running_dot.setToolTip("Running")
        self.running_dot.hide()
        self.star = QPushButton()
        self.star.setObjectName("StarButton")
        self.star.setCursor(Qt.CursorShape.PointingHandCursor)
        self.star.setFixedSize(28, 28)
        self.star.clicked.connect(lambda: self.favorite_toggled.emit(self.app_id))
        self.launch_button = QPushButton("Launch")
        self.launch_button.setObjectName("LaunchButton")
        self.launch_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.launch_button.clicked.connect(lambda: self.launch_requested.emit(self.app_id))
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    # -- state -----------------------------------------------------------------
    def bind_icons(self, provider) -> None:
        self._icon_provider = provider

    def update_app(self, app: AppEntry, category_name: str, favorite: bool, running: bool) -> None:
        self._app = app
        self.name_label.setText(app.name)
        self.name_label.setToolTip(app.name)
        self.category_label.setText(category_name)
        self.star.setIcon(icon("star", WARN if favorite else TEXT2, 16, filled=favorite))
        self.star.setToolTip("Remove from favorites" if favorite else "Add to favorites")
        self.running_dot.setVisible(running)

    def ensure_icon(self) -> None:
        if not self._icon_loaded and self._icon_provider is not None:
            self._icon_loaded = True
            self._icon_provider.request(self._app, ICON_SIZE, self, self.icon_label.setPixmap)

    def refresh_icon(self) -> None:
        self._icon_loaded = False
        if self.isVisible():
            self.ensure_icon()

    def showEvent(self, event) -> None:  # lazy: icons are only built for cards that become visible
        super().showEvent(event)
        self.ensure_icon()

    # -- input -----------------------------------------------------------------
    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        self.context_requested.emit(self.app_id, event.globalPos())

    def mousePressEvent(self, event: QMouseEvent) -> None:
        self._press_pos = event.position().toPoint() if event.button() == Qt.MouseButton.LeftButton else None
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        """Dragging starts only after the drag threshold, so clicks and double-click launches keep working."""
        if (event.buttons() & Qt.MouseButton.LeftButton and self._press_pos is not None
                and (event.position().toPoint() - self._press_pos).manhattanLength() >= QApplication.startDragDistance()):
            self._press_pos = None
            self._start_drag()
            return
        super().mouseMoveEvent(event)

    def _start_drag(self) -> None:
        drag = QDrag(self)
        drag.setMimeData(app_mime(self.app_id, self._app.name))
        pixmap = self.grab().scaledToWidth(180, Qt.TransformationMode.SmoothTransformation)
        drag.setPixmap(pixmap)
        drag.setHotSpot(QPoint(pixmap.width() // 2, pixmap.height() // 2))
        self.drag_state_changed.emit(True)
        try:
            drag.exec(Qt.DropAction.CopyAction)
        finally:
            self.drag_state_changed.emit(False)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._press_pos = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.launch_requested.emit(self.app_id)


class AppCard(_AppItemBase):
    def __init__(self, app: AppEntry) -> None:
        super().__init__(app)
        self.setObjectName("AppCard")
        self.setFixedSize(212, 150)
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 12)
        root.setSpacing(2)
        top = QHBoxLayout()
        top.addWidget(self.icon_label)
        top.addStretch(1)
        top.addWidget(self.running_dot, 0, Qt.AlignmentFlag.AlignTop)
        top.addWidget(self.star, 0, Qt.AlignmentFlag.AlignTop)
        root.addLayout(top)
        root.addSpacing(6)
        root.addWidget(self.name_label)
        root.addWidget(self.category_label)
        root.addStretch(1)
        root.addWidget(self.launch_button)
        self.name_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)


class AppRow(_AppItemBase):
    def __init__(self, app: AppEntry) -> None:
        super().__init__(app)
        self.setObjectName("AppRow")
        self.setFixedHeight(66)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.icon_label.setFixedSize(ICON_SIZE + 4, ICON_SIZE + 4)
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 8, 12, 8)
        row.setSpacing(12)
        row.addWidget(self.icon_label)
        text = QVBoxLayout()
        text.setSpacing(0)
        text.addWidget(self.name_label)
        text.addWidget(self.category_label)
        row.addLayout(text, 1)
        row.addWidget(self.running_dot)
        row.addWidget(self.star)
        row.addWidget(self.launch_button)
