"""Home dashboard: running, favorites, recents, categories, workspaces."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from cirana.ui.common import label, make_scroll, section_header
from cirana.ui.context import AppContext
from cirana.widgets.app_collection import AppCollection
from cirana.widgets.flow_layout import FlowLayout

SECTION_LIMIT = 8


class _Stat(QFrame):
    def __init__(self, caption: str) -> None:
        super().__init__()
        self.setObjectName("Card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(0)
        self.value = label("0", "StatValue")
        layout.addWidget(self.value)
        layout.addWidget(label(caption, "Muted"))
        self.setMinimumWidth(130)


class _CategoryTile(QFrame):
    clicked = Signal(str)

    def __init__(self, category_id: str, name: str, count: int) -> None:
        super().__init__()
        self.category_id = category_id
        self.setObjectName("CategoryTile")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(172, 70)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(0)
        layout.addWidget(label(name, "AppName"))
        layout.addWidget(label(f"{count} app{'s' if count != 1 else ''}", "AppCategory"))

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.category_id)


class _WorkspaceTile(QFrame):
    selected = Signal(str)
    open_requested = Signal(str)

    def __init__(self, workspace_id: str, name: str, count: int, active: bool) -> None:
        super().__init__()
        self.workspace_id = workspace_id
        self.setObjectName("CategoryTile")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(236, 84)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(0)
        layout.addWidget(label(name + ("  ·  active" if active else ""), "AppName"))
        layout.addWidget(label(f"{count} app{'s' if count != 1 else ''}", "AppCategory"))
        layout.addStretch(1)
        button = QPushButton("Open workspace")
        button.setObjectName("LaunchButton")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(lambda: self.open_requested.emit(workspace_id))
        layout.addWidget(button)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.selected.emit(self.workspace_id)


class DashboardView(QWidget):
    navigate = Signal(str, object)

    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        body = QWidget()
        root = QVBoxLayout(body)
        root.setContentsMargins(28, 24, 28, 28)
        root.setSpacing(14)

        root.addWidget(label("Welcome back", "PageTitle"))
        self.subtitle = label("", "PageSubtitle")
        root.addWidget(self.subtitle)

        stats = QHBoxLayout()
        stats.setSpacing(12)
        self.stat_apps, self.stat_favs, self.stat_running, self.stat_cats = (
            _Stat("Applications"), _Stat("Favorites"), _Stat("Running now"), _Stat("Categories"),
        )
        for stat in (self.stat_apps, self.stat_favs, self.stat_running, self.stat_cats):
            stats.addWidget(stat)
        stats.addStretch(1)
        root.addLayout(stats)
        root.addSpacing(6)

        root.addWidget(section_header("Running now"))
        self.running = AppCollection(ctx.registry, ctx.actions, ctx.icons)
        self.running_empty = label("No tracked application is running right now.", "Muted")
        root.addWidget(self.running)
        root.addWidget(self.running_empty)

        root.addWidget(section_header("Favorites", "View all", lambda: self.navigate.emit("favorites", None)))
        self.favorites = AppCollection(ctx.registry, ctx.actions, ctx.icons)
        self.favorites_empty = label("Star an application and it will show up here.", "Muted")
        root.addWidget(self.favorites)
        root.addWidget(self.favorites_empty)

        root.addWidget(section_header("Recently launched"))
        self.recent = AppCollection(ctx.registry, ctx.actions, ctx.icons)
        self.recent_empty = label("Applications you launch through CIRANA will appear here.", "Muted")
        root.addWidget(self.recent)
        root.addWidget(self.recent_empty)

        root.addWidget(section_header("Categories", "Browse all", lambda: self.navigate.emit("applications", None)))
        self.categories_host = QWidget()
        self.categories_layout = FlowLayout(self.categories_host, spacing=12)
        root.addWidget(self.categories_host)

        root.addWidget(section_header("Workspaces", "Open", lambda: self.navigate.emit("workspaces", None)))
        self.workspaces_host = QWidget()
        self.workspaces_layout = FlowLayout(self.workspaces_host, spacing=12)
        root.addWidget(self.workspaces_host)
        self.workspaces_empty = label(
            "Create a workspace to open a group of applications together. Drag cards onto it or use the "
            "Workspaces page.", "Muted")
        root.addWidget(self.workspaces_empty)
        root.addStretch(1)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(make_scroll(body))

    def refresh(self) -> None:
        ctx, reg = self.ctx, self.ctx.registry
        running_ids = ctx.tracker.running
        apps = reg.all_apps()
        favorites = reg.favorites()[:SECTION_LIMIT]
        recents = reg.recents(SECTION_LIMIT)
        running_apps = [a for a in apps if a.id in running_ids][:SECTION_LIMIT]

        self.stat_apps.value.setText(str(len(apps)))
        self.stat_favs.value.setText(str(len(reg.favorites())))
        self.stat_running.value.setText(str(len([a for a in apps if a.id in running_ids])))
        categories = [(cid, name, len(reg.apps_in_category(cid))) for cid, name in reg.categories()]
        self.stat_cats.value.setText(str(len([c for c in categories if c[2]])))
        if reg.scanned_at:
            self.subtitle.setText("Your applications, organized. Press Ctrl+K to search and launch anything.")
        else:
            self.subtitle.setText("Scanning for installed applications…")

        for collection, empty, items in (
            (self.running, self.running_empty, running_apps),
            (self.favorites, self.favorites_empty, favorites),
            (self.recent, self.recent_empty, recents),
        ):
            collection.set_apps(items, "grid", running_ids)
            collection.setVisible(bool(items))
            empty.setVisible(not items)

        while self.categories_layout.count():
            item = self.categories_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for cid, name, count in categories:
            if count:
                tile = _CategoryTile(cid, name, count)
                tile.clicked.connect(lambda c: self.navigate.emit("applications", c))
                self.categories_layout.addWidget(tile)
        self.categories_host.updateGeometry()

        while self.workspaces_layout.count():
            item = self.workspaces_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        workspaces = ctx.workspaces.all()
        for workspace in workspaces:
            tile = _WorkspaceTile(workspace.id, workspace.name, len(workspace.apps), workspace.id == ctx.workspaces.active_id)
            tile.selected.connect(lambda wid: self.navigate.emit("workspaces", wid))
            tile.open_requested.connect(self._open_workspace)
            self.workspaces_layout.addWidget(tile)
        self.workspaces_host.setVisible(bool(workspaces))
        self.workspaces_empty.setVisible(not workspaces)
        self.workspaces_host.updateGeometry()

    def _open_workspace(self, workspace_id: str) -> None:
        if not self.ctx.launcher.open(workspace_id):
            self.ctx.actions.message.emit("Another workspace is still opening. Please wait a moment")
