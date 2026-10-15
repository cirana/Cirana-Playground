"""Plain data types shared across CIRANA."""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class LaunchType(str, Enum):
    EXECUTABLE = "executable"
    SHORTCUT = "shortcut"
    AUMID = "aumid"
    COMMAND = "command"


class Source(str, Enum):
    START_MENU_USER = "start_menu_user"
    START_MENU_SYSTEM = "start_menu_system"
    PACKAGED = "packaged"
    REGISTRY = "registry"
    USER = "user"


# Category ids are stable; display names live in categories.json and can be renamed.
BUILTIN_CATEGORIES: dict[str, str] = {
    "development": "Development",
    "gaming": "Gaming",
    "browsers": "Browsers",
    "communication": "Communication",
    "multimedia": "Multimedia",
    "design": "Design",
    "productivity": "Productivity",
    "utilities": "Utilities",
    "other": "Other",
}
DEFAULT_CATEGORY = "other"


def normalize_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


def stable_app_id(name: str) -> str:
    """Identifier derived from the display name so it survives a change of launch method."""
    return "app-" + hashlib.sha1(normalize_name(name).encode("utf-8")).hexdigest()[:12]


@dataclass
class AppEntry:
    id: str
    name: str
    launch_type: str
    target: str
    source: str
    category: str = DEFAULT_CATEGORY  # automatic suggestion; user override lives in categories.json
    arguments: str = ""
    exe_path: str = ""  # resolved executable, when known (icon + running detection)
    working_dir: str = ""
    group: str = ""  # Start Menu sub-folder, used as a categorization hint
    custom: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AppEntry | None":
        try:
            known = {k: raw[k] for k in cls.__dataclass_fields__ if k in raw}
            entry = cls(**known)
        except TypeError:
            return None
        if not (entry.id and entry.name and entry.target):
            return None
        return entry

    @property
    def icon_source(self) -> str:
        """File whose shell icon represents this app (may be empty for packaged apps)."""
        if self.exe_path:
            return self.exe_path
        if self.launch_type in (LaunchType.SHORTCUT.value, LaunchType.EXECUTABLE.value):
            return self.target
        return ""


@dataclass
class LaunchResult:
    ok: bool
    message: str = ""
    pid: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)
