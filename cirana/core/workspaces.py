"""User workspaces: named, ordered groups of applications with a launch mode per application and a saved session.

Everything is persisted in ``workspaces.json`` (versioned, atomic, self-recovering through ``JsonStore``).
This module has no widgets; the UI listens to ``changed`` / ``active_changed``.
"""
from __future__ import annotations

import logging
import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from PySide6.QtCore import QObject, Signal

from cirana.core.application_registry import ApplicationRegistry
from cirana.core.models import AppEntry
from cirana.core.playground import embed_precheck
from cirana.storage.settings_manager import SettingsManager

log = logging.getLogger(__name__)

MODE_EMBEDDED = "embedded"
MODE_EXTERNAL = "external"
MODES = (MODE_EMBEDDED, MODE_EXTERNAL)
MAX_NAME = 60


def _number(value: Any) -> float:
    """A stored timestamp, or 0 when it is missing or damaged."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return 0.0
    try:
        number = float(value)
    except ValueError:
        return 0.0
    return number if math.isfinite(number) else 0.0


@dataclass
class WorkspaceApp:
    app_id: str
    name: str  # display-name snapshot, so an application that vanished from a rescan is still recognisable
    mode: str = MODE_EXTERNAL

    def to_dict(self) -> dict[str, Any]:
        return {"app_id": self.app_id, "name": self.name, "mode": self.mode}

    @classmethod
    def from_dict(cls, raw: Any) -> "WorkspaceApp | None":
        if not isinstance(raw, dict) or not isinstance(raw.get("app_id"), str) or not raw["app_id"]:
            return None
        mode, name = raw.get("mode"), raw.get("name")
        return cls(raw["app_id"], name if isinstance(name, str) and name else raw["app_id"],
                   mode if isinstance(mode, str) and mode in MODES else MODE_EXTERNAL)


@dataclass
class SavedLayout:
    """What a saved session restores: tab order + active tab (hosted apps) and window placement (external apps)."""

    tab_order: list[str] = field(default_factory=list)
    active: str = ""
    windows: dict[str, dict[str, Any]] = field(default_factory=dict)  # app_id -> {"rect": [l, t, r, b], "maximized": bool}
    saved_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"tab_order": list(self.tab_order), "active": self.active, "windows": dict(self.windows),
                "saved_at": self.saved_at}

    @classmethod
    def from_dict(cls, raw: Any) -> "SavedLayout | None":
        if not isinstance(raw, dict):
            return None
        windows: dict[str, dict[str, Any]] = {}
        raw_windows = raw.get("windows")
        for app_id, placement in (raw_windows.items() if isinstance(raw_windows, dict) else ()):
            rect = placement.get("rect") if isinstance(placement, dict) else None
            if isinstance(rect, list) and len(rect) == 4                     and all(isinstance(v, int) and not isinstance(v, bool) for v in rect):
                windows[str(app_id)] = {"rect": rect, "maximized": bool(placement.get("maximized"))}
        tab_order = raw.get("tab_order")
        active = raw.get("active")
        return cls(
            [i for i in tab_order if isinstance(i, str)] if isinstance(tab_order, list) else [],
            active if isinstance(active, str) else "",
            windows,
            _number(raw.get("saved_at")),
        )


@dataclass
class Workspace:
    id: str
    name: str
    apps: list[WorkspaceApp] = field(default_factory=list)
    layout: SavedLayout | None = None
    created: float = 0.0
    updated: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "created": self.created, "updated": self.updated,
            "apps": [a.to_dict() for a in self.apps], "layout": self.layout.to_dict() if self.layout else None,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "Workspace | None":
        name = raw.get("name") if isinstance(raw, dict) else None
        name = str(name).strip() if isinstance(name, (str, int, float)) and not isinstance(name, bool) else ""
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw["id"] or not name:
            return None
        seen: set[str] = set()
        apps: list[WorkspaceApp] = []
        raw_apps = raw.get("apps")
        for item in (raw_apps if isinstance(raw_apps, list) else ()):
            app = WorkspaceApp.from_dict(item)
            if app and app.app_id not in seen:
                seen.add(app.app_id)
                apps.append(app)
        return cls(raw["id"], name, apps, SavedLayout.from_dict(raw.get("layout")),
                   _number(raw.get("created")), _number(raw.get("updated")))

    def has(self, app_id: str) -> bool:
        return any(a.app_id == app_id for a in self.apps)

    def item(self, app_id: str) -> WorkspaceApp | None:
        return next((a for a in self.apps if a.app_id == app_id), None)


def clean_name(name: str) -> str:
    name = " ".join(name.split())
    if not name:
        raise ValueError("The workspace needs a name.")
    if len(name) > MAX_NAME:
        raise ValueError(f"The workspace name is too long (maximum {MAX_NAME} characters).")
    return name


class WorkspaceManager(QObject):
    changed = Signal()  # any workspace or its contents changed
    active_changed = Signal(str)  # id of the active workspace ("" = none)

    def __init__(self, settings: SettingsManager, registry: ApplicationRegistry) -> None:
        super().__init__()
        self._settings = settings
        self._registry = registry
        self._workspaces: list[Workspace] = []
        for raw in settings.workspaces_store.load().get("workspaces", []):
            try:
                workspace = Workspace.from_dict(raw)
            except Exception:  # noqa: BLE001 - one damaged record must not cost the others (or the app)
                log.exception("Skipped a damaged workspace record")
                continue
            if workspace and not any(w.id == workspace.id for w in self._workspaces):
                self._workspaces.append(workspace)
        active = settings.get("active_workspace")
        self._active = active if isinstance(active, str) and self.get(active) else ""

    # -- queries -----------------------------------------------------------------------------
    def all(self) -> list[Workspace]:
        return list(self._workspaces)

    def get(self, workspace_id: str) -> Workspace | None:
        return next((w for w in self._workspaces if w.id == workspace_id), None)

    @property
    def active_id(self) -> str:
        return self._active

    def active(self) -> Workspace | None:
        return self.get(self._active)

    def containing(self, app_id: str) -> list[Workspace]:
        return [w for w in self._workspaces if w.has(app_id)]

    def display_name(self, item: WorkspaceApp) -> str:
        app = self._registry.get(item.app_id)
        return app.name if app else item.name

    def resolve(self, item: WorkspaceApp) -> AppEntry | None:
        return self._registry.get(item.app_id)

    def default_mode(self, app: AppEntry) -> str:
        """The application's own launch preference, unless it cannot be hosted at all.

        Embedding is opt-in per application (context menu "Default launch mode" or the workspace row), because
        hosting a foreign window is experimental and not every application tolerates it.
        """
        if embed_precheck(app) is not None:
            return MODE_EXTERNAL
        return MODE_EMBEDDED if self._registry.launch_mode(app.id) == MODE_EMBEDDED else MODE_EXTERNAL

    # -- workspace CRUD ---------------------------------------------------------------------------
    def create(self, name: str, app_ids: list[str] | None = None) -> Workspace:
        name = self._unique_name(clean_name(name))
        now = time.time()
        workspace = Workspace(f"ws-{uuid.uuid4().hex[:8]}", name, created=now, updated=now)
        self._workspaces.append(workspace)
        for app_id in app_ids or []:
            self._add(workspace, app_id)
        self._save()
        return workspace

    def rename(self, workspace_id: str, name: str) -> None:
        workspace = self._require(workspace_id)
        name = clean_name(name)
        if any(w.id != workspace_id and w.name.lower() == name.lower() for w in self._workspaces):
            raise ValueError(f"A workspace named '{name}' already exists.")
        workspace.name = name
        self._touch(workspace)
        self._save()

    def delete(self, workspace_id: str) -> None:
        workspace = self._require(workspace_id)
        self._workspaces.remove(workspace)
        was_active = self._active == workspace_id
        if was_active:
            self._set_active("")
        self._save()

    # -- applications in a workspace -----------------------------------------------------------------
    def add_app(self, workspace_id: str, app_id: str, mode: str | None = None) -> bool:
        """Add an application; False when it is already in the workspace or unknown."""
        workspace = self._require(workspace_id)
        added = self._add(workspace, app_id, mode)
        if added:
            self._touch(workspace)
            self._save()
        return added

    def _add(self, workspace: Workspace, app_id: str, mode: str | None = None) -> bool:
        app = self._registry.get(app_id)
        if app is None or workspace.has(app_id):
            return False
        workspace.apps.append(WorkspaceApp(app_id, app.name, mode if mode in MODES else self.default_mode(app)))
        return True

    def remove_app(self, workspace_id: str, app_id: str) -> None:
        workspace = self._require(workspace_id)
        before = len(workspace.apps)
        workspace.apps = [a for a in workspace.apps if a.app_id != app_id]
        if workspace.layout:
            workspace.layout.tab_order = [i for i in workspace.layout.tab_order if i != app_id]
            workspace.layout.windows.pop(app_id, None)
            if workspace.layout.active == app_id:
                workspace.layout.active = ""
        if len(workspace.apps) != before:
            self._touch(workspace)
            self._save()

    def move_app(self, workspace_id: str, app_id: str, delta: int) -> None:
        workspace = self._require(workspace_id)
        index = next((i for i, a in enumerate(workspace.apps) if a.app_id == app_id), -1)
        target = index + delta
        if index < 0 or not 0 <= target < len(workspace.apps):
            return
        workspace.apps.insert(target, workspace.apps.pop(index))
        self._touch(workspace)
        self._save()

    def set_mode(self, workspace_id: str, app_id: str, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"Unknown launch mode {mode!r}")
        workspace = self._require(workspace_id)
        item = workspace.item(app_id)
        if item and item.mode != mode:
            item.mode = mode
            self._touch(workspace)
            self._save()

    # -- saved session -----------------------------------------------------------------------------------
    def save_layout(self, workspace_id: str, layout: SavedLayout, new_apps: list[tuple[str, str]] | None = None) -> None:
        """Store a session. ``new_apps`` (app_id, mode) are applications open in the workspace that were not listed yet."""
        workspace = self._require(workspace_id)
        for app_id, mode in new_apps or []:
            self._add(workspace, app_id, mode)
        layout.saved_at = time.time()
        workspace.layout = layout
        self._touch(workspace)
        self._save()

    def clear_layout(self, workspace_id: str) -> None:
        workspace = self._require(workspace_id)
        if workspace.layout is not None:
            workspace.layout = None
            self._touch(workspace)
            self._save()

    # -- active workspace ------------------------------------------------------------------------------------
    def set_active(self, workspace_id: str) -> None:
        if workspace_id and self.get(workspace_id) is None:
            raise ValueError("Unknown workspace")
        self._set_active(workspace_id)

    def _set_active(self, workspace_id: str) -> None:
        if workspace_id != self._active:
            self._active = workspace_id
            self._settings.set("active_workspace", workspace_id)
            self.active_changed.emit(workspace_id)

    # -- internals --------------------------------------------------------------------------------------------------
    def _require(self, workspace_id: str) -> Workspace:
        workspace = self.get(workspace_id)
        if workspace is None:
            raise ValueError("That workspace no longer exists.")
        return workspace

    def _unique_name(self, name: str) -> str:
        taken = {w.name.lower() for w in self._workspaces}
        if name.lower() not in taken:
            return name
        number = 2
        while f"{name} {number}".lower() in taken:
            number += 1
        return f"{name} {number}"

    @staticmethod
    def _touch(workspace: Workspace) -> None:
        workspace.updated = time.time()

    def _save(self) -> None:
        self._settings.workspaces_store.save({"workspaces": [w.to_dict() for w in self._workspaces]})
        self.changed.emit()
