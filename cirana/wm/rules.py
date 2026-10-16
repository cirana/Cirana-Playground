"""Window rules: what CIRANA does with the windows of a program, chosen by the user.

A rule names a program precisely (executable, window class and / or AppUserModelID; a title filter can narrow it
further) and says what to do with its windows:

* ``float``: dynamic tiling (both scopes) leaves them where they are;
* ``ignore``: CIRANA does not manage them at all: no tiling, stacking, keyboard focus, border, and no workspace entry
  binds them automatically;
* ``workspace``: they belong to a workspace: while it is active its dynamic tiling arranges them with its own windows,
  and activating it raises them too (nothing is stored per window, and nothing is started).

Every field that is set must match (AND), comparisons ignore case, and a title alone never matches anything: a rule
without an executable, class or AUMID is refused, so a rule can never catch unrelated windows by their title. The
first enabled rule that matches wins (the list order is the priority).

Pure data and matching (no Qt, no Win32); stored in ``config.json`` as ``window_rules``.
"""
from __future__ import annotations

import ntpath
import uuid
from dataclasses import dataclass
from typing import Any

from cirana.wm.identity import TrackedWindow

FLOAT, IGNORE, WORKSPACE = "float", "ignore", "workspace"
ACTIONS = (FLOAT, IGNORE, WORKSPACE)
ACTION_NAMES = {FLOAT: "Float (never tiled)", IGNORE: "Ignore (never managed)", WORKSPACE: "Belongs to a workspace"}


def new_rule_id() -> str:
    return f"r-{uuid.uuid4().hex[:8]}"


@dataclass
class WindowRule:
    id: str
    name: str = ""
    exe: str = ""  # file name (``code.exe``) or full path of the application's executable
    class_name: str = ""  # exact window class
    aumid: str = ""  # exact AppUserModelID of a packaged app
    title: str = ""  # optional: the title contains this text
    action: str = FLOAT
    workspace_id: str = ""  # action ``workspace``
    enabled: bool = True

    @property
    def label(self) -> str:
        return self.name.strip() or self.exe or self.class_name or self.aumid or "Rule"

    def problem(self) -> str:
        """Why the rule cannot be saved ("" when it is fine)."""
        if not (self.exe.strip() or self.class_name.strip() or self.aumid.strip()):
            return ("Name the program by its executable, window class or AppUserModelID; a title alone could match "
                    "unrelated windows.")
        if self.action not in ACTIONS:
            return "Choose what to do with the windows."
        if self.action == WORKSPACE and not self.workspace_id:
            return "Choose the workspace the windows belong to."
        return ""

    def matches(self, window: TrackedWindow) -> bool:
        if not self.enabled or self.problem():
            return False
        if self.exe.strip():
            wanted = self.exe.strip().casefold()
            images = {e.casefold() for e in (window.app_exe, window.exe) if e}
            if "\\" in wanted or "/" in wanted:
                if ntpath.normcase(ntpath.normpath(wanted)) not in {ntpath.normcase(ntpath.normpath(i))
                                                                     for i in images}:
                    return False
            elif wanted not in {ntpath.basename(i) for i in images}:
                return False
        if self.class_name.strip() and window.class_name.casefold() != self.class_name.strip().casefold():
            return False
        if self.aumid.strip() and self.aumid.strip().casefold() not in {
                a.casefold() for a in (window.app_aumid, window.aumid) if a}:
            return False
        if self.title.strip() and self.title.strip().casefold() not in window.title.casefold():
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "exe": self.exe, "class_name": self.class_name, "aumid": self.aumid,
                "title": self.title, "action": self.action, "workspace_id": self.workspace_id,
                "enabled": self.enabled}

    @classmethod
    def from_dict(cls, raw: Any) -> "WindowRule | None":
        if not isinstance(raw, dict):
            return None

        def text(key: str) -> str:
            value = raw.get(key)
            return value.strip() if isinstance(value, str) else ""

        rule = cls(text("id") or new_rule_id(), text("name"), text("exe"), text("class_name"), text("aumid"),
                   text("title"), text("action") or FLOAT, text("workspace_id"), raw.get("enabled") is not False)
        return None if rule.problem() else rule

    @classmethod
    def for_window(cls, window: TrackedWindow, action: str = FLOAT) -> "WindowRule":
        """A rule that catches this window's program (and only windows of its class)."""
        return cls(new_rule_id(), window.exe_name or window.class_name, exe=window.exe_name,
                   class_name=window.class_name, aumid=window.app_aumid, action=action)


def read_rules(raw: Any) -> list[WindowRule]:
    rules, seen = [], set()
    for item in raw if isinstance(raw, list) else ():
        rule = WindowRule.from_dict(item)
        if rule is not None and rule.id not in seen:
            seen.add(rule.id)
            rules.append(rule)
    return rules


def first_match(rules: list[WindowRule], window: TrackedWindow) -> WindowRule | None:
    return next((rule for rule in rules if rule.matches(window)), None)
