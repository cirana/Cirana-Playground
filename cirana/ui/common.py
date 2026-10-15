"""Small shared UI building blocks."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget

from cirana.ui.icons import icon_pixmap
from cirana.ui.theme import ACCENT


def make_scroll(content: QWidget) -> QScrollArea:
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    area.setWidget(content)
    return area


def label(text: str, name: str = "", wrap: bool = False) -> QLabel:
    widget = QLabel(text)
    if name:
        widget.setObjectName(name)
    widget.setWordWrap(wrap)
    return widget


def section_header(title: str, action_text: str = "", on_action=None) -> QWidget:
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(label(title, "SectionTitle"))
    layout.addStretch(1)
    if action_text and on_action:
        button = QPushButton(action_text)
        button.setObjectName("LinkButton")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(on_action)
        layout.addWidget(button)
    return row


class EmptyState(QFrame):
    """Honest empty / not-yet-available state with an explanation."""

    def __init__(self, glyph: str, title: str, body: str) -> None:
        super().__init__()
        self.setObjectName("Panel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 32, 32, 32)
        layout.setSpacing(8)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge = QLabel()
        badge.setObjectName("EmptyIcon")
        badge.setFixedSize(56, 56)
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setPixmap(icon_pixmap(glyph, ACCENT, 26))
        layout.addWidget(badge, 0, Qt.AlignmentFlag.AlignCenter)
        heading = label(title, "SectionTitle")
        heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        text = label(body, "Muted", wrap=True)
        text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        text.setMaximumWidth(460)
        layout.addWidget(heading)
        layout.addWidget(text, 0, Qt.AlignmentFlag.AlignCenter)
