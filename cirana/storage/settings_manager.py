"""Owns every JSON file CIRANA persists and exposes typed accessors."""
from __future__ import annotations

import logging
import logging.handlers
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
}


def _is_dict(value: Any) -> bool:
    return isinstance(value, dict)


def _has(kind: type, **fields: type):
    def check(data: Any) -> bool:
        return isinstance(data, kind) and all(isinstance(data.get(k), t) for k, t in fields.items())

    return check


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
        self.workspaces_store = JsonStore(
            self.root / "workspaces.json", deep_default({"workspaces": []}), 1, _has(dict, workspaces=list)
        )
        # Windows CIRANA currently hosts; used to report applications left without a window after a crash.
        self.playground_store = JsonStore(
            self.root / "playground_session.json", deep_default({"embedded": []}), 1, _has(dict, embedded=list)
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
