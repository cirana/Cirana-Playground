"""Floating strip that appears while an application card is dragged, so it can be dropped on any workspace."""
from __future__ import annotations

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QWidget

from cirana.ui.context import AppContext
from cirana.widgets.dnd import app_id_from


class DropChip(QPushButton):
    dropped = Signal(str)  # application id

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.setObjectName("DropChip")
        self.setAcceptDrops(True)
        self.setFlat(True)

    def _highlight(self, on: bool) -> None:
        self.setProperty("hover", "true" if on else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def dragEnterEvent(self, event) -> None:
        if app_id_from(event.mimeData()):
            event.acceptProposedAction()
            self._highlight(True)

    def dragLeaveEvent(self, event) -> None:
        self._highlight(False)

    def dropEvent(self, event) -> None:
        app_id = app_id_from(event.mimeData())
        self._highlight(False)
        if app_id:
            event.acceptProposedAction()
            self.dropped.emit(app_id)


class WorkspaceDropBar(QFrame):
    def __init__(self, ctx: AppContext, parent: QWidget) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.setObjectName("DropBar")
        self._row = QHBoxLayout(self)
        self._row.setContentsMargins(16, 12, 16, 12)
        self._row.setSpacing(10)
        self.hide()

    def begin(self) -> None:
        """Rebuild the chips for the current workspaces and show the bar at the bottom of the window."""
        while self._row.count():
            item = self._row.takeAt(0)
            if item.widget():
                item.widget().hide()  # deleteLater() alone would not run inside the (non-Qt) drag loop
                item.widget().deleteLater()
        title = QLabel("Drop on a workspace:")
        title.setObjectName("Muted")
        self._row.addWidget(title)
        for workspace in self.ctx.workspaces.all():
            chip = DropChip(workspace.name)
            chip.dropped.connect(lambda app_id, wid=workspace.id: self.ctx.actions.add_to_workspace(app_id, wid))
            self._row.addWidget(chip)
        new_chip = DropChip("+ New workspace")
        # A modal name prompt must not open inside the drop handler; defer it until the drag has finished.
        new_chip.dropped.connect(lambda app_id: QTimer.singleShot(0, lambda: self.ctx.actions.add_to_new_workspace(app_id)))
        self._row.addWidget(new_chip)
        self.adjustSize()
        parent = self.parentWidget()
        width = min(self.sizeHint().width(), max(parent.width() - 40, 200))
        self.resize(width, self.sizeHint().height())
        self.move((parent.width() - self.width()) // 2, parent.height() - self.height() - 24)
        self.raise_()
        self.show()

    def end(self) -> None:
        self.hide()
