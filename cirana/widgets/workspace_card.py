"""A workspace at a glance: name, its applications' icons with a dot for each open one, and a one-click activate.

Used on the Home page (with the Activate button) and in the Workspaces page list (selectable). Application cards
can be dropped on it to add them.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from cirana.core.models import AppEntry, LaunchType
from cirana.core.workspace_controller import EntryState, EntryStatus
from cirana.core.workspaces import Workspace
from cirana.ui.common import label
from cirana.ui.icons import icon
from cirana.ui.theme import TEXT
from cirana.widgets.dnd import app_id_from

MAX_ICONS = 7
STATE_TEXT = {
    EntryState.RUNNING: "Open",
    EntryState.LAUNCHING: "Starting…",
    EntryState.AMBIGUOUS: "Choose a window",
    EntryState.MISSING: "Starts on activate",
    EntryState.STOPPED: "Not open",
    EntryState.UNAVAILABLE: "Not available",
}


def entry_icon_app(ctx, status: EntryStatus) -> AppEntry:  # noqa: ANN001 - AppContext (import cycle)
    app = ctx.workspaces.resolve(status.entry)
    if app is not None:
        return app
    if status.window is not None:
        from cirana.ui.dialogs import window_icon_app

        return window_icon_app(ctx, status.window)
    exe = status.entry.rule.exe
    return AppEntry(id="entry-" + status.entry.id, name=status.entry.name, launch_type=LaunchType.EXECUTABLE.value,
                    target=exe or status.entry.name, source="user", exe_path=exe)


def repolish(widget: QWidget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)


class _IconTile(QWidget):
    def __init__(self, ctx, status: EntryStatus, size: int) -> None:  # noqa: ANN001
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        tile = QLabel()
        tile.setObjectName("IconTile")
        tile.setFixedSize(size + 8, size + 8)
        tile.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tile.setToolTip(f"{ctx.workspaces.display_name(status.entry)}: {STATE_TEXT[status.state]}")
        tile.setProperty("dim", "false" if status.window is not None else "true")
        ctx.icons.request(entry_icon_app(ctx, status), size, tile, tile.setPixmap)
        dot = QLabel()
        dot.setObjectName("RunningDot" if status.state is EntryState.RUNNING else "IdleDot")
        dot.setFixedSize(8, 8)
        layout.addWidget(tile, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(dot, 0, Qt.AlignmentFlag.AlignHCenter)


class WorkspaceCard(QFrame):
    selected = Signal(str)
    activate_requested = Signal(str)

    def __init__(self, ctx, workspace: Workspace, active: bool, with_button: bool,  # noqa: ANN001
                 is_selected: bool = False) -> None:
        super().__init__()
        self.ctx = ctx
        self.workspace_id = workspace.id
        self.setObjectName("WorkspaceCard")
        self.setProperty("selected", "true" if is_selected else "false")
        self.setProperty("active", "true" if active else "false")
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        statuses = ctx.wm.statuses(workspace.id)
        open_count = sum(1 for s in statuses if s.state is EntryState.RUNNING)
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(8)
        head = QHBoxLayout()
        head.setSpacing(8)
        name = label(workspace.name, "AppName")
        name.setToolTip(workspace.name)
        head.addWidget(name, 1)
        if active:
            head.addWidget(label("Active", "ActivePill"))
        if workspace.layout.dynamic and workspace.layout.tiled and not ctx.tiler.global_on:
            tiling = label("Tiling", "StatePill")
            tiling.setProperty("kind", "busy" if active else "idle")
            tiling.setToolTip("Dynamic tiling is on: while this workspace is active, its windows are re-tiled as they "
                              "open, close, minimize and restore")
            head.addWidget(tiling)
        if ctx.wm.busy(workspace.id):
            head.addWidget(label("Working…", "StatusPill"))
        root.addLayout(head)
        count = len(workspace.apps)
        root.addWidget(label(f"{open_count} of {count} open" if count else "Empty: add applications or windows",
                             "AppCategory"))
        icons = QHBoxLayout()
        icons.setSpacing(6)
        for status in statuses[:MAX_ICONS]:
            icons.addWidget(_IconTile(ctx, status, 22))
        if len(statuses) > MAX_ICONS:
            icons.addWidget(label(f"+{len(statuses) - MAX_ICONS}", "Muted"), 0, Qt.AlignmentFlag.AlignTop)
        icons.addStretch(1)
        root.addLayout(icons)
        if with_button:
            button = QPushButton(" Activate")
            button.setObjectName("LaunchButton")
            button.setIcon(icon("play", TEXT, 14))
            button.setToolTip("Open what is missing, arrange the windows and bring them to the front")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setEnabled(bool(workspace.apps))
            button.clicked.connect(lambda: self.activate_requested.emit(workspace.id))
            root.addWidget(button)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.selected.emit(self.workspace_id)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.activate_requested.emit(self.workspace_id)

    def _highlight(self, on: bool) -> None:
        self.setProperty("dropTarget", "true" if on else "false")
        repolish(self)

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
            self.ctx.actions.add_to_workspace(app_id, self.workspace_id)
