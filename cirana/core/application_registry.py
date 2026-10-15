"""Single source of truth for applications: discovered data merged with user overrides.

Discovered data (applications.json) is only ever replaced wholesale by a scan.
Everything the user decides (categories, favorites, hidden apps, custom apps) lives in
separate files keyed by stable app ids, so a rescan never loses manual changes.
"""
from __future__ import annotations

import logging
import re
import time
import uuid

from PySide6.QtCore import QObject, Signal

from cirana.core.category_engine import CategoryEngine
from cirana.core.models import BUILTIN_CATEGORIES, DEFAULT_CATEGORY, AppEntry, LaunchType, Source
from cirana.storage.settings_manager import SettingsManager

log = logging.getLogger(__name__)

MAX_RECENTS = 30


class ApplicationRegistry(QObject):
    changed = Signal()  # any change to apps, categories, favorites or recents

    def __init__(self, settings: SettingsManager, engine: CategoryEngine | None = None) -> None:
        super().__init__()
        self.settings = settings
        self.engine = engine or CategoryEngine()
        self._discovered: dict[str, AppEntry] = {}
        self._custom: dict[str, AppEntry] = {}
        self._cats = settings.categories_store.load()
        self._favs = settings.favorites_store.load()
        self._over = settings.overrides_store.load()
        self.scanned_at: float = 0.0
        self._load_apps()

    # -- loading / scanning ------------------------------------------------
    def _load_apps(self) -> None:
        raw = self.settings.applications_store.load()
        self.scanned_at = float(raw.get("scanned_at") or 0)
        self._discovered = {}
        for item in raw.get("applications", []):
            entry = AppEntry.from_dict(item) if isinstance(item, dict) else None
            if entry:
                self._discovered[entry.id] = entry
        self._custom = {}
        for item in self._over["custom_apps"]:
            entry = AppEntry.from_dict(item) if isinstance(item, dict) else None
            if entry:
                entry.custom = True
                self._custom[entry.id] = entry

    def apply_scan(self, apps: list[AppEntry]) -> None:
        self._discovered = {a.id: a for a in apps}
        self.scanned_at = time.time()
        self.settings.applications_store.save(
            {"scanned_at": self.scanned_at, "applications": [a.to_dict() for a in apps]}
        )
        self.changed.emit()

    # -- queries ----------------------------------------------------------------
    def all_apps(self, include_hidden: bool = False) -> list[AppEntry]:
        hidden = set(self._over["hidden"])
        apps = [*self._custom.values(), *(a for a in self._discovered.values() if a.id not in self._custom)]
        if not include_hidden:
            apps = [a for a in apps if a.id not in hidden]
        return sorted(apps, key=lambda a: a.name.lower())

    def get(self, app_id: str) -> AppEntry | None:
        return self._custom.get(app_id) or self._discovered.get(app_id)

    def hidden_apps(self) -> list[AppEntry]:
        hidden = set(self._over["hidden"])
        return [a for a in self.all_apps(include_hidden=True) if a.id in hidden]

    def category_of(self, app: AppEntry) -> str:
        chosen = self._cats["assignments"].get(app.id)
        if chosen and self.category_exists(chosen):
            return chosen
        return app.category if self.category_exists(app.category) else DEFAULT_CATEGORY

    def category_name(self, category_id: str) -> str:
        if category_id in self._cats["renames"]:
            return self._cats["renames"][category_id]
        if category_id in BUILTIN_CATEGORIES:
            return BUILTIN_CATEGORIES[category_id]
        for item in self._cats["custom"]:
            if item["id"] == category_id:
                return item["name"]
        return BUILTIN_CATEGORIES[DEFAULT_CATEGORY]

    def categories(self) -> list[tuple[str, str]]:
        ids = [*BUILTIN_CATEGORIES, *(c["id"] for c in self._cats["custom"])]
        return [(cid, self.category_name(cid)) for cid in ids]

    def category_exists(self, category_id: str) -> bool:
        return category_id in BUILTIN_CATEGORIES or any(c["id"] == category_id for c in self._cats["custom"])

    def is_builtin_category(self, category_id: str) -> bool:
        return category_id in BUILTIN_CATEGORIES

    def apps_in_category(self, category_id: str) -> list[AppEntry]:
        return [a for a in self.all_apps() if self.category_of(a) == category_id]

    def is_favorite(self, app_id: str) -> bool:
        return app_id in self._favs["favorites"]

    def favorites(self) -> list[AppEntry]:
        apps = [self.get(i) for i in self._favs["favorites"]]
        return [a for a in apps if a and a.id not in set(self._over["hidden"])]

    def recents(self, limit: int = 10) -> list[AppEntry]:
        hidden = set(self._over["hidden"])
        result: list[AppEntry] = []
        for item in self._favs["recents"]:
            app = self.get(item["id"])
            if app and app.id not in hidden:
                result.append(app)
            if len(result) >= limit:
                break
        return result

    def launch_mode(self, app_id: str) -> str:
        return self._over["launch_modes"].get(app_id, "external")

    # -- user mutations -----------------------------------------------------
    def set_category(self, app_id: str, category_id: str) -> None:
        if not self.category_exists(category_id):
            raise ValueError(f"Unknown category {category_id}")
        self._cats["assignments"][app_id] = category_id
        self._save_cats()

    def reset_category(self, app_id: str) -> None:
        if self._cats["assignments"].pop(app_id, None) is not None:
            self._save_cats()

    def add_category(self, name: str) -> str:
        name = name.strip()
        if not name:
            raise ValueError("Category name is empty")
        if any(name.lower() == n.lower() for _, n in self.categories()):
            raise ValueError(f"A category named '{name}' already exists")
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "category"
        category_id = f"custom-{slug}-{uuid.uuid4().hex[:4]}"
        self._cats["custom"].append({"id": category_id, "name": name})
        self._save_cats()
        return category_id

    def rename_category(self, category_id: str, name: str) -> None:
        name = name.strip()
        if not name:
            raise ValueError("Category name is empty")
        if any(cid != category_id and name.lower() == n.lower() for cid, n in self.categories()):
            raise ValueError(f"A category named '{name}' already exists")
        if category_id in BUILTIN_CATEGORIES:
            self._cats["renames"][category_id] = name
        else:
            for item in self._cats["custom"]:
                if item["id"] == category_id:
                    item["name"] = name
        self._save_cats()

    def delete_category(self, category_id: str) -> None:
        if category_id in BUILTIN_CATEGORIES:
            raise ValueError("Built-in categories cannot be deleted")
        self._cats["custom"] = [c for c in self._cats["custom"] if c["id"] != category_id]
        self._cats["assignments"] = {k: v for k, v in self._cats["assignments"].items() if v != category_id}
        self._save_cats()

    def toggle_favorite(self, app_id: str) -> bool:
        favs: list[str] = self._favs["favorites"]
        if app_id in favs:
            favs.remove(app_id)
            state = False
        else:
            favs.append(app_id)
            state = True
        self._save_favs()
        return state

    def hide(self, app_id: str) -> None:
        if app_id not in self._over["hidden"]:
            self._over["hidden"].append(app_id)
            self._save_over()

    def unhide(self, app_id: str) -> None:
        if app_id in self._over["hidden"]:
            self._over["hidden"].remove(app_id)
            self._save_over()

    def record_launch(self, app_id: str) -> None:
        recents = [r for r in self._favs["recents"] if r["id"] != app_id]
        recents.insert(0, {"id": app_id, "ts": time.time()})
        self._favs["recents"] = recents[:MAX_RECENTS]
        self._save_favs()

    def set_launch_mode(self, app_id: str, mode: str) -> None:
        self._over["launch_modes"][app_id] = mode
        self._save_over()

    def add_custom_app(self, name: str, target: str, launch_type: str, arguments: str = "") -> AppEntry:
        name, target = name.strip(), target.strip()
        if not name or not target:
            raise ValueError("Name and target are required")
        if launch_type not in (LaunchType.EXECUTABLE.value, LaunchType.COMMAND.value):
            raise ValueError("Custom apps must be executables or commands")
        entry = AppEntry(
            id=f"custom-{uuid.uuid4().hex[:10]}",
            name=name,
            launch_type=launch_type,
            target=target,
            source=Source.USER.value,
            arguments=arguments,
            exe_path=target if launch_type == LaunchType.EXECUTABLE.value else "",
            custom=True,
        )
        entry.category = self.engine.classify(entry)
        self._custom[entry.id] = entry
        self._over["custom_apps"].append(entry.to_dict())
        self._save_over()
        return entry

    def remove_custom_app(self, app_id: str) -> None:
        if app_id in self._custom:
            del self._custom[app_id]
            self._over["custom_apps"] = [c for c in self._over["custom_apps"] if c.get("id") != app_id]
            self._save_over()

    # -- persistence ------------------------------------------------------------
    def _save_cats(self) -> None:
        self.settings.categories_store.save(self._cats)
        self.changed.emit()

    def _save_favs(self) -> None:
        self.settings.favorites_store.save(self._favs)
        self.changed.emit()

    def _save_over(self) -> None:
        self.settings.overrides_store.save(self._over)
        self.changed.emit()
