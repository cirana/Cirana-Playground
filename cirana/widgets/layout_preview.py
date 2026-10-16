"""A small drawing of a workspace layout: the monitor's work area and one numbered tile per window.

It uses the same geometry as the window controller (cirana.wm.layout), so what it shows is what "Apply layout" does.
"""
from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from cirana.ui.theme import qcolor
from cirana.wm.layout import FREE, LayoutSpec, from_fraction, tile

PREVIEW_WORK = (0, 0, 1600, 900)  # a 16:9 stand-in when the real monitor is unknown


class LayoutPreview(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("LayoutPreview")
        self.setMinimumSize(200, 120)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._spec = LayoutSpec()
        self._names: list[str] = []
        self._ids: list[str] = []
        self._work = PREVIEW_WORK

    def sizeHint(self) -> QSize:
        return QSize(260, 150)

    def set_layout(self, spec: LayoutSpec, entries: list[tuple[str, str]], work: tuple[int, int, int, int] | None) -> None:
        """``entries``: (entry id, name) of the windows that take part, in order."""
        self._spec = spec
        self._ids = [entry_id for entry_id, _name in entries]
        self._names = [name for _entry_id, name in entries]
        self._work = work if work and work[2] > work[0] and work[3] > work[1] else PREVIEW_WORK
        self.update()

    def tiles(self) -> list[tuple[int, int, int, int] | None]:
        """Rectangles in work-area coordinates (None: this entry has no saved position in a free layout)."""
        if self._spec.kind == FREE:
            work = self._work
            return [from_fraction(self._spec.free[i].fraction, work) if i in self._spec.free else None
                    for i in self._ids]
        return list(tile(self._spec, self._work, len(self._ids), ids=self._ids))  # BSP: its stored tree

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        left, top, right, bottom = self._work
        ratio = (right - left) / max(1, bottom - top)
        area = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        width = min(area.width(), area.height() * ratio)
        height = width / ratio
        screen = QRectF(area.x() + (area.width() - width) / 2, area.y() + (area.height() - height) / 2, width, height)
        painter.setPen(QPen(qcolor("border"), 1))
        painter.setBrush(qcolor("bg2"))
        painter.drawRoundedRect(screen, 8, 8)
        scale = width / max(1, right - left)
        font = QFont(self.font())
        font.setPointSizeF(max(7.0, font.pointSizeF() - 1))
        painter.setFont(font)
        accent = qcolor("accent")
        fill = QColor(accent)
        fill.setAlphaF(0.16)
        tiles = self.tiles()
        if not tiles:
            painter.setPen(qcolor("text2"))
            painter.drawText(screen, Qt.AlignmentFlag.AlignCenter, "No windows yet")
        for index, rect in enumerate(tiles):
            if rect is None:
                continue
            box = QRectF(screen.x() + (rect[0] - left) * scale, screen.y() + (rect[1] - top) * scale,
                         (rect[2] - rect[0]) * scale, (rect[3] - rect[1]) * scale).adjusted(1, 1, -1, -1)
            painter.setPen(QPen(accent, 1))
            painter.setBrush(fill)
            painter.drawRoundedRect(box, 4, 4)
            painter.setPen(qcolor("text"))
            name = self._names[index] if index < len(self._names) else ""
            text = painter.fontMetrics().elidedText(f"{index + 1}  {name}", Qt.TextElideMode.ElideRight,
                                                    int(box.width()) - 8)
            painter.drawText(box.adjusted(4, 2, -4, -2), Qt.AlignmentFlag.AlignCenter, text)
        missing = sum(1 for rect in tiles if rect is None)
        if missing:
            painter.setPen(qcolor("text2"))
            painter.drawText(screen.adjusted(6, 0, -6, -4), Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight,
                             f"{missing} without a saved position")
        painter.end()
