"""Owns every JSON file CIRANA persists and exposes typed accessors."""
from __future__ import annotations

import json
import logging
import logging.handlers
import shutil
from pathlib import Path
from typing import Any

from cirana.paths import data_dir
from cirana.storage.json_store import JsonStore, deep_default

DEFAULT_CONFIG: dict[str, Any] = {
    "sidebar_collapsed": False,
    "view_mode": "grid",
    "scan_on_startup": True,
    "last_page": "home",
    "window_geometry": "",
    "active_workspace": "",
    # Tiling of every application window (not only a workspace's): on / off, its layout, programs left floating.
    "global_tiling": False,
    "global_layout": {},
    "tiling_exclusions": [],
}


def _is_dict(value: Any) -> bool:
    return isinstance(value, dict)


def _has(kind: type, **fields: type):
    def check(data: Any) -> bool:
        return isinstance(data, kind) and all(isinstance(data.get(k), t) for k, t in fields.items())

    return check


def _migrate_workspaces(version: int, data: Any) -> Any:
    from cirana.core.workspaces import migrate_workspaces  # the model lives in core (imports this module)

    return migrate_workspaces(version, data)


def _keep_v1_copy(path: Path, copy: Path) -> None:
    """Before a version 1 workspaces file is migrated, keep it as it was (once): the migration is one-way."""
    if copy.exists() or not path.exists():
        return
    try:
        if json.loads(path.read_text(encoding="utf-8")).get("version") == 1:
            shutil.copyfile(path, copy)
    except (OSError, ValueError, AttributeError):
        logging.getLogger(__name__).warning("Could not keep a copy of the version 1 workspaces file")


class SettingsManager:
    """Central access point for all on-disk state."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root else data_dir()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "cache").mkdir(exist_ok=True)
        (self.root / "logs").mkdir(exist_ok=True)

        self.config_store = JsonStore(self.root / "config.json", deep_default(DEFAULT_CONFIG), 1, _is_dict)
        self.applications_store = JsonStore(
            self.root / "applications.json",
            deep_default({"scanned_at": 0, "applications": []}),
            1,
            _has(dict, applications=list),
        )
        self.categories_store = JsonStore(
            self.root / "categories.json",
            deep_default({"custom": [], "renames": {}, "assignments": {}}),
            1,
            _has(dict, custom=list, renames=dict, assignments=dict),
        )
        self.favorites_store = JsonStore(
            self.root / "favorites.json",
            deep_default({"favorites": [], "recents": []}),
            1,
            _has(dict, favorites=list, recents=list),
        )
        self.overrides_store = JsonStore(
            self.root / "overrides.json",
            deep_default({"hidden": [], "custom_apps": [], "launch_modes": {}}),
            1,
            _has(dict, hidden=list, custom_apps=list, launch_modes=dict),
        )
        _keep_v1_copy(self.root / "workspaces.json", self.root / "workspaces.v1.json")
        self.workspaces_store = JsonStore(
            self.root / "workspaces.json", deep_default({"workspaces": []}), 2, _has(dict, workspaces=list),
            migrate=_migrate_workspaces,
        )

        self.config: dict[str, Any] = {**DEFAULT_CONFIG, **self.config_store.load()}

    @property
    def icon_cache_dir(self) -> Path:
        path = self.root / "cache" / "icons"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def save_config(self) -> None:
        self.config_store.save(self.config)

    def get(self, key: str) -> Any:
        return self.config.get(key, DEFAULT_CONFIG.get(key))

    def set(self, key: str, value: Any) -> None:
        if self.config.get(key) != value:
            self.config[key] = value
            self.save_config()


def setup_logging(root: Path) -> None:
    logger = logging.getLogger()
    if any(getattr(h, "_cirana", False) for h in logger.handlers):
        return
    handler = logging.handlers.RotatingFileHandler(
        root / "logs" / "cirana.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    handler._cirana = True  # type: ignore[attr-defined]
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
