"""A collection of app cards/rows that syncs in place instead of rebuilding."""
from __future__ import annotations

from PySide6.QtWidgets import QVBoxLayout, QWidget

from cirana.core.application_registry import ApplicationRegistry
from cirana.core.models import AppEntry
from cirana.ui.app_actions import AppActions
from cirana.ui.app_icons import AppIconProvider
from cirana.widgets.app_card import AppCard, AppRow, _AppItemBase
from cirana.widgets.flow_layout import FlowLayout


class AppCollection(QWidget):
    def __init__(self, registry: ApplicationRegistry, actions: AppActions, icons: AppIconProvider) -> None:
        super().__init__()
        self._registry = registry
        self._actions = actions
        self._icons = icons
        self._mode = "grid"
        self._items: dict[str, _AppItemBase] = {}
        self._order: list[str] = []
        self._layout: FlowLayout | QVBoxLayout | None = None
        self._apply_mode("grid")

    @property
    def count(self) -> int:
        return len(self._order)

    def _apply_mode(self, mode: str) -> None:
        self._clear()
        if self._layout is not None:  # swap layouts: reparent the old one away
            QWidget().setLayout(self.layout())
        if mode == "grid":
            self._layout = FlowLayout(self, spacing=12)
        else:
            self._layout = QVBoxLayout(self)
            self._layout.setContentsMargins(0, 0, 0, 0)
            self._layout.setSpacing(8)
        self._mode = mode

    def _clear(self) -> None:
        for item in self._items.values():
            item.setParent(None)
            item.deleteLater()
        self._items.clear()
        self._order = []

    def set_apps(self, apps: list[AppEntry], mode: str, running: dict[str, int] | None = None) -> None:
        running = running or {}
        self.setUpdatesEnabled(False)
        try:
            if mode != self._mode:
                self._apply_mode(mode)
            wanted = {a.id for a in apps}
            for app_id in [i for i in self._items if i not in wanted]:
                widget = self._items.pop(app_id)
                self._layout.removeWidget(widget)
                widget.setParent(None)
                widget.deleteLater()
            for app in apps:
                item = self._items.get(app.id)
                if item is None:
                    item = AppCard(app) if mode == "grid" else AppRow(app)
                    item.bind_icons(self._icons)
                    item.launch_requested.connect(self._actions.launch)
                    item.favorite_toggled.connect(self._actions.toggle_favorite)
                    item.context_requested.connect(self._actions.show_menu)
                    item.drag_state_changed.connect(self._actions.drag_active)
                    self._items[app.id] = item
                    item.setParent(self)
                item.update_app(
                    app,
                    self._registry.category_name(self._registry.category_of(app)),
                    self._registry.is_favorite(app.id),
                    app.id in running,
                )
            new_order = [a.id for a in apps]
            if new_order != self._order or self._layout.count() != len(new_order):
                while self._layout.count():
                    self._layout.takeAt(0)
                for app_id in new_order:
                    self._layout.addWidget(self._items[app_id])
                    self._items[app_id].show()
                self._order = new_order
        finally:
            self.setUpdatesEnabled(True)
        self.updateGeometry()
