"""User workspaces: named groups of real application windows, with a layout and the rules that find each window.

Persisted in ``workspaces.json`` (version 2, atomic and self-recovering through ``JsonStore``). Nothing stored here
names a window handle or a process id: each entry keeps the application (catalog id) and a :class:`MatchRule`, and
the running windows are found again by :class:`cirana.core.workspace_controller.WorkspaceController`.

Version 1 files (the earlier Playground design: per-application "tab"/"own window" mode, tab order, absolute window
rectangles) are migrated on load: the mode is dropped, the tab order becomes the entry order, and saved window
rectangles become a "saved positions" layout relative to the monitor they were on. A copy of the version 1 file is
kept as ``workspaces.v1.json``.
"""
from __future__ import annotations

import hashlib
import logging
import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal

from cirana.core.application_registry import ApplicationRegistry
from cirana.core.models import AppEntry
from cirana.storage.settings_manager import SettingsManager
from cirana.wm.identity import MatchRule, TrackedWindow
from cirana.wm.layout import FREE, GRID, FreeSlot, LayoutSpec, to_fraction
from cirana.wm.monitors import Monitor, monitor_for_rect

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2
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


def _text(raw: dict, key: str) -> str:
    value = raw.get(key)
    return value if isinstance(value, str) else ""


def new_entry_id() -> str:
    return f"e-{uuid.uuid4().hex[:8]}"


@dataclass
class WorkspaceApp:
    """One window of a workspace: which application, how to recognise its window, whether to start it."""

    id: str
    name: str  # display-name snapshot, so an application that vanished from a rescan is still recognisable
    app_id: str = ""  # catalog id ("" for a window added from the open-window list that is not in the catalog)
    rule: MatchRule = field(default_factory=MatchRule)  # extra or learned matching (combined with the app's own)
    launch: bool = True  # start the application when the workspace opens and no window of it is there
    hint_title: str = ""  # title of the window last bound to it: picks the right one of several after a restart

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "app_id": self.app_id, "rule": self.rule.to_dict(),
                "launch": self.launch, "hint_title": self.hint_title}

    @classmethod
    def from_dict(cls, raw: Any) -> "WorkspaceApp | None":
        if not isinstance(raw, dict) or not _text(raw, "id"):
            return None
        rule = MatchRule.from_dict(raw.get("rule"))
        app_id = _text(raw, "app_id")
        if not app_id and rule.empty:
            return None  # nothing could ever find its window
        name = _text(raw, "name") or app_id or rule.exe.rsplit("\\", 1)[-1] or "Window"
        launch = raw.get("launch")
        return cls(raw["id"], name, app_id, rule, launch if isinstance(launch, bool) else True,
                   _text(raw, "hint_title"))


@dataclass
class Workspace:
    id: str
    name: str
    apps: list[WorkspaceApp] = field(default_factory=list)
    layout: LayoutSpec = field(default_factory=LayoutSpec)
    created: float = 0.0
    updated: float = 0.0
    # Absolute rectangles migrated from a version 1 file, waiting to be turned into monitor-relative slots.
    legacy: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = {"id": self.id, "name": self.name, "created": self.created, "updated": self.updated,
                "apps": [a.to_dict() for a in self.apps], "layout": self.layout.to_dict()}
        if self.legacy:
            data["legacy"] = self.legacy
        return data

    @classmethod
    def from_dict(cls, raw: Any) -> "Workspace | None":
        name = raw.get("name") if isinstance(raw, dict) else None
        name = str(name).strip() if isinstance(name, (str, int, float)) and not isinstance(name, bool) else ""
        if not isinstance(raw, dict) or not _text(raw, "id") or not name:
            return None
        seen: set[str] = set()
        apps: list[WorkspaceApp] = []
        raw_apps = raw.get("apps")
        for item in (raw_apps if isinstance(raw_apps, list) else ()):
            try:
                app = WorkspaceApp.from_dict(item)
            except Exception:  # noqa: BLE001 - one damaged entry must not cost the rest
                log.exception("Skipped a damaged workspace entry")
                continue
            if app and app.id not in seen:
                seen.add(app.id)
                apps.append(app)
        legacy = raw.get("legacy")
        return cls(raw["id"], name, apps, LayoutSpec.from_dict(raw.get("layout")), _number(raw.get("created")),
                   _number(raw.get("updated")), dict(legacy) if isinstance(legacy, dict) else {})

    def entry(self, entry_id: str) -> WorkspaceApp | None:
        return next((a for a in self.apps if a.id == entry_id), None)

    def has_app(self, app_id: str) -> bool:
        return bool(app_id) and any(a.app_id == app_id for a in self.apps)


# -- version 1 -> 2 -------------------------------------------------------------------------------------------------
def legacy_entry_id(app_id: str) -> str:
    return "e-" + hashlib.sha1(app_id.encode("utf-8")).hexdigest()[:8]


def _migrate_v1_workspace(raw: dict) -> dict:
    layout = raw.get("layout") if isinstance(raw.get("layout"), dict) else {}
    order = [i for i in layout.get("tab_order", []) if isinstance(i, str)] if isinstance(layout.get("tab_order"),
                                                                                         list) else []
    apps, seen = [], set()
    for item in raw.get("apps", []) if isinstance(raw.get("apps"), list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("app_id"), str) or not item["app_id"]:
            continue
        if item["app_id"] in seen:
            continue
        seen.add(item["app_id"])
        apps.append({"id": legacy_entry_id(item["app_id"]), "app_id": item["app_id"],
                     "name": item.get("name") if isinstance(item.get("name"), str) else item["app_id"],
                     "rule": {}, "launch": True})
    rank = {app_id: i for i, app_id in enumerate(order)}
    apps.sort(key=lambda a: rank.get(a["app_id"], len(rank)))  # stable: the rest keep their list order
    legacy = {}
    windows = layout.get("windows") if isinstance(layout.get("windows"), dict) else {}
    for app_id, placement in windows.items():
        rect = placement.get("rect") if isinstance(placement, dict) else None
        if isinstance(rect, list) and len(rect) == 4 and all(isinstance(v, int) and not isinstance(v, bool)
                                                               for v in rect) and app_id in seen:
            legacy[legacy_entry_id(app_id)] = {"rect": rect, "maximized": bool(placement.get("maximized"))}
    return {"id": raw.get("id"), "name": raw.get("name"), "created": raw.get("created"), "updated": raw.get("updated"),
            "apps": apps, "layout": {"kind": FREE if legacy else GRID}, "legacy": legacy}


def migrate_workspaces(version: int, data: Any) -> Any:
    """JsonStore migration hook. Each workspace is migrated on its own: a damaged one is dropped, never the file."""
    if version == 1 and isinstance(data, dict) and isinstance(data.get("workspaces"), list):
        migrated = []
        for raw in data["workspaces"]:
            try:
                if isinstance(raw, dict):
                    migrated.append(_migrate_v1_workspace(raw))
            except Exception:  # noqa: BLE001
                log.exception("Dropped a version 1 workspace that could not be migrated")
        log.info("Migrated %d workspace(s) from the Playground format", len(migrated))
        return {"workspaces": migrated}
    raise ValueError(f"Cannot migrate workspaces from version {version}")


def legacy_slots(legacy: dict[str, dict[str, Any]], monitors: list[Monitor]) -> dict[str, FreeSlot]:
    """Absolute rectangles of a version 1 file as monitor-relative slots (rectangles on no monitor are dropped)."""
    slots = {}
    for entry_id, placement in legacy.items():
        try:
            rect = tuple(int(v) for v in placement["rect"])
        except (KeyError, TypeError, ValueError):
            continue
        monitor = monitor_for_rect(rect, monitors)
        if monitor is not None:
            slots[entry_id] = FreeSlot(to_fraction(rect, monitor.work), monitor.ref(), bool(placement.get("maximized")))
    return slots


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

    def __init__(self, settings: SettingsManager, registry: ApplicationRegistry,
                 monitors: Callable[[], list[Monitor]] | None = None) -> None:
        super().__init__()
        self._settings = settings
        self._registry = registry
        self._rules: dict[tuple, MatchRule] = {}  # catalog application -> its rule (reading an alias touches disk)
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
        self._convert_legacy(monitors)

    def _convert_legacy(self, monitors: Callable[[], list[Monitor]] | None) -> None:
        pending = [w for w in self._workspaces if w.legacy]
        if not pending:
            return
        try:
            if monitors is None:
                from cirana.wm.monitors import enum_monitors as monitors
            current = monitors()
        except Exception:  # noqa: BLE001 - keep the rectangles for the next start
            log.exception("Monitors unavailable; migrated window positions are kept for later")
            return
        if not current:
            return
        for workspace in pending:
            workspace.layout.free.update(legacy_slots(workspace.legacy, current))
            workspace.legacy = {}
        self._save()

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
        return [w for w in self._workspaces if w.has_app(app_id)]

    def resolve(self, item: WorkspaceApp) -> AppEntry | None:
        return self._registry.get(item.app_id) if item.app_id else None

    def display_name(self, item: WorkspaceApp) -> str:
        app = self.resolve(item)
        return app.name if app else item.name

    def app_rule(self, app: AppEntry) -> MatchRule:
        """The rule that finds a catalog application's windows (cached; do not modify the result)."""
        key = (app.id, app.launch_type, app.target, app.exe_path)
        rule = self._rules.get(key)
        if rule is None:
            if len(self._rules) > 2048:
                self._rules.clear()
            rule = self._rules[key] = MatchRule.for_app(app)
        return rule

    def rule_for(self, item: WorkspaceApp) -> MatchRule:
        """The application's own rule (from the catalog) combined with what the entry stores or learned."""
        app = self.resolve(item)
        base = self.app_rule(app) if app else MatchRule()
        own = item.rule
        return MatchRule(exe=own.exe or base.exe, aumid=own.aumid or base.aumid, class_name=own.class_name,
                         title=own.title, alt_exes=list(own.alt_exes))

    # -- workspace CRUD ---------------------------------------------------------------------------
    def create(self, name: str, app_ids: list[str] | None = None) -> Workspace:
        name = self._unique_name(clean_name(name))
        now = time.time()
        workspace = Workspace(f"ws-{uuid.uuid4().hex[:8]}", name, created=now, updated=now)
        self._workspaces.append(workspace)
        for app_id in app_ids or []:
            self._add_app(workspace, app_id)
        self._save()
        return workspace

    def rename(self, workspace_id: str, name: str) -> None:
        workspace = self._require(workspace_id)
        name = clean_name(name)
        if any(w.id != workspace_id and w.name.lower() == name.lower() for w in self._workspaces):
            raise ValueError(f"A workspace named '{name}' already exists.")
        workspace.name = name
        self._touch_save(workspace)

    def delete(self, workspace_id: str) -> None:
        """Forget the workspace. Its applications and windows are not touched."""
        workspace = self._require(workspace_id)
        self._workspaces.remove(workspace)
        if self._active == workspace_id:
            self._set_active("")
        self._save()

    # -- entries -----------------------------------------------------------------------------------------
    def add_app(self, workspace_id: str, app_id: str) -> WorkspaceApp | None:
        """Add an application from the catalog; None when it is unknown or already in the workspace."""
        workspace = self._require(workspace_id)
        entry = self._add_app(workspace, app_id)
        if entry is not None:
            self._touch_save(workspace)
        return entry

    def _add_app(self, workspace: Workspace, app_id: str) -> WorkspaceApp | None:
        app = self._registry.get(app_id)
        if app is None or workspace.has_app(app_id):
            return None
        entry = WorkspaceApp(new_entry_id(), app.name, app.id)
        workspace.apps.append(entry)
        return entry

    def add_window(self, workspace_id: str, window: TrackedWindow) -> WorkspaceApp:
        """Add an open window. Several windows of one application are separate entries."""
        workspace = self._require(workspace_id)
        rule = MatchRule.for_window(window)
        app = self.app_for_window(window)
        entry = WorkspaceApp(new_entry_id(), app.name if app else window.display_title, app.id if app else "",
                             MatchRule() if app and self.app_rule(app).matches(window) else rule,
                             launch=app is not None, hint_title=window.title)
        workspace.apps.append(entry)
        self._touch_save(workspace)
        return entry

    def app_for_window(self, window: TrackedWindow) -> AppEntry | None:
        """The catalog application a window belongs to, when exactly one claims it by executable or package."""
        claims = [a for a in self._registry.all_apps() if not a.arguments and not self.app_rule(a).empty
                  and self.app_rule(a).owns(window)]
        exact = [a for a in claims if self.app_rule(a).exe == window.app_exe]
        chosen = exact if len(exact) == 1 else claims
        return chosen[0] if len(chosen) == 1 else None

    def remove_entry(self, workspace_id: str, entry_id: str) -> None:
        workspace = self._require(workspace_id)
        before = len(workspace.apps)
        workspace.apps = [a for a in workspace.apps if a.id != entry_id]
        workspace.layout.free.pop(entry_id, None)
        if len(workspace.apps) != before:
            self._touch_save(workspace)

    def move_entry(self, workspace_id: str, entry_id: str, index: int) -> None:
        """Put an entry at position ``index`` (drag and drop); its window takes that place in tiled layouts."""
        workspace = self._require(workspace_id)
        current = next((i for i, a in enumerate(workspace.apps) if a.id == entry_id), -1)
        index = max(0, min(index, len(workspace.apps) - 1))
        if current < 0 or current == index:
            return
        workspace.apps.insert(index, workspace.apps.pop(current))
        self._touch_save(workspace)

    def set_launch(self, workspace_id: str, entry_id: str, launch: bool) -> None:
        workspace = self._require(workspace_id)
        entry = workspace.entry(entry_id)
        if entry is not None and entry.launch != launch:
            entry.launch = launch
            self._touch_save(workspace)

    def remember_window(self, workspace_id: str, entry_id: str, window: TrackedWindow) -> None:
        """A window was bound to the entry: keep its title as a hint and learn the program that showed it."""
        workspace = self.get(workspace_id)
        entry = workspace.entry(entry_id) if workspace else None
        if entry is None:
            return
        learned = False
        if not self.rule_for(entry).owns(window) and not (entry.rule.class_name or entry.rule.title):
            learned = entry.rule.learn(window)
        if learned or entry.hint_title != window.title:
            entry.hint_title = window.title
            self._save(touch=workspace if learned else None)

    # -- layout -----------------------------------------------------------------------------------------------
    def set_layout(self, workspace_id: str, layout: LayoutSpec) -> None:
        workspace = self._require(workspace_id)
        layout.free = layout.free or workspace.layout.free  # switching to a tiled layout keeps saved positions
        workspace.layout = layout
        self._touch_save(workspace)

    def save_positions(self, workspace_id: str, slots: dict[str, FreeSlot]) -> None:
        """Store where the windows are now as the workspace's "saved positions" layout."""
        workspace = self._require(workspace_id)
        workspace.layout.free.update(slots)
        workspace.layout.kind = FREE
        self._touch_save(workspace)

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

    def _touch_save(self, workspace: Workspace) -> None:
        self._save(touch=workspace)

    def _save(self, touch: Workspace | None = None) -> None:
        if touch is not None:
            touch.updated = time.time()
        self._settings.workspaces_store.save({"workspaces": [w.to_dict() for w in self._workspaces]})
        self.changed.emit()
