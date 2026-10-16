"""Global keyboard shortcuts for the window manager (focus, swap, resize, monocle, stacks, workspaces).

Registered with ``RegisterHotKey`` on a thread of CIRANA's own (no keyboard hook, nothing loaded into other
processes): Windows posts ``WM_HOTKEY`` to that thread's queue and the action is handed to the GUI thread. Windows
lets the process that received the hotkey hand the keyboard focus on, so focus commands work without any trick.

Shortcuts are stored in ``config.json`` (``hotkeys``: action -> "Ctrl+Alt+Left", "" = none). Before a shortcut is
used it is checked against the shortcuts Windows and most applications already use; a shortcut Windows refuses to
register (another program has it) is reported per shortcut and the others keep working.

Defaults avoid Ctrl+Alt with letters, digits and punctuation: on keyboards with an AltGr key (Turkish Q, German, …)
Ctrl+Alt+Q is AltGr+Q, which types "@".
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Protocol

from PySide6.QtCore import QObject, Signal, Slot

log = logging.getLogger(__name__)

# (action id, what it does, default shortcut)
ACTIONS: list[tuple[str, str, str]] = [
    ("focus_left", "Focus the window on the left", "Ctrl+Alt+Left"),
    ("focus_right", "Focus the window on the right", "Ctrl+Alt+Right"),
    ("focus_up", "Focus the window above", "Ctrl+Alt+Up"),
    ("focus_down", "Focus the window below", "Ctrl+Alt+Down"),
    ("swap_left", "Move the window left (or to the monitor on the left)", "Ctrl+Alt+Shift+Left"),
    ("swap_right", "Move the window right (or to the monitor on the right)", "Ctrl+Alt+Shift+Right"),
    ("swap_up", "Move the window up (or to the monitor above)", "Ctrl+Alt+Shift+Up"),
    ("swap_down", "Move the window down (or to the monitor below)", "Ctrl+Alt+Shift+Down"),
    ("grow_width", "Make the tile wider", "Ctrl+Alt+Win+Right"),
    ("shrink_width", "Make the tile narrower", "Ctrl+Alt+Win+Left"),
    ("grow_height", "Make the tile taller", "Ctrl+Alt+Win+Down"),
    ("shrink_height", "Make the tile lower", "Ctrl+Alt+Win+Up"),
    ("monocle", "Monocle: the window fills the screen / back to the layout", "Ctrl+Alt+Enter"),
    ("toggle_float", "Float the window / tile it again", "Ctrl+Alt+Space"),
    ("stack_left", "Stack the window with the tile on the left", ""),
    ("stack_right", "Stack the window with the tile on the right", ""),
    ("stack_up", "Stack the window with the tile above", ""),
    ("stack_down", "Stack the window with the tile below", ""),
    ("unstack", "Take the window out of its stack", "Ctrl+Alt+Backspace"),
    ("stack_next", "Show the next window of the stack", "Ctrl+Alt+PageDown"),
    ("stack_previous", "Show the previous window of the stack", "Ctrl+Alt+PageUp"),
    ("move_workspace_next", "Move the window to the next workspace", "Ctrl+Alt+Shift+PageDown"),
    ("move_workspace_previous", "Move the window to the previous workspace", "Ctrl+Alt+Shift+PageUp"),
    ("workspace_next", "Activate the next workspace", ""),
    ("workspace_previous", "Activate the previous workspace", ""),
]
DEFAULTS = {action: default for action, _label, default in ACTIONS}
LABELS = {action: label for action, label, _default in ACTIONS}

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x0001, 0x0002, 0x0004, 0x0008, 0x4000
MODIFIERS = (("Ctrl", MOD_CONTROL), ("Alt", MOD_ALT), ("Shift", MOD_SHIFT), ("Win", MOD_WIN))

KEYS: dict[str, int] = {
    **{chr(c): c for c in range(ord("A"), ord("Z") + 1)},
    **{str(d): 0x30 + d for d in range(10)},
    **{f"F{n}": 0x6F + n for n in range(1, 25)},
    **{f"Num{d}": 0x60 + d for d in range(10)},
    "Left": 0x25, "Up": 0x26, "Right": 0x27, "Down": 0x28, "Home": 0x24, "End": 0x23, "PageUp": 0x21,
    "PageDown": 0x22, "Insert": 0x2D, "Delete": 0x2E, "Enter": 0x0D, "Space": 0x20, "Backspace": 0x08, "Tab": 0x09,
    "Esc": 0x1B, "PrintScreen": 0x2C, "Pause": 0x13,
    "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD, ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF, "\\": 0xDC,
    "`": 0xC0,
}
KEY_NAMES = list(KEYS)
_TYPING = {k for k in KEYS if len(k) == 1}  # letters, digits, punctuation: produce characters


@dataclass(frozen=True)
class Hotkey:
    modifiers: int
    vk: int
    key: str

    @property
    def text(self) -> str:
        return "+".join([name for name, flag in MODIFIERS if self.modifiers & flag] + [self.key])


def parse(text: str) -> Hotkey | None:
    """"Ctrl+Alt+Left" -> Hotkey; None when it is not a valid shortcut (unknown key, no Ctrl / Alt / Win)."""
    parts = [p.strip() for p in (text or "").split("+")]
    if text and text.strip().endswith("++"):  # "Ctrl+Alt++" is not a key name; "=" is
        return None
    if not parts or not parts[-1]:
        return None
    modifiers = 0
    for part in parts[:-1]:
        flag = next((f for name, f in MODIFIERS if name.lower() == part.lower()), None)
        if flag is None:
            return None
        modifiers |= flag
    key = next((k for k in KEYS if k.lower() == parts[-1].lower()), None)
    if key is None or not modifiers & (MOD_CONTROL | MOD_ALT | MOD_WIN):
        return None  # a shortcut without Ctrl, Alt or Win would swallow ordinary typing
    return Hotkey(modifiers, KEYS[key], key)


def normalize(text: str) -> str:
    hotkey = parse(text)
    return hotkey.text if hotkey is not None else ""


# Shortcuts Windows itself, almost every application, or CIRANA's own window already use.
_RESERVED = {
    "Alt+Tab": "switches windows (Windows)", "Alt+Shift+Tab": "switches windows (Windows)",
    "Alt+F4": "closes the window (Windows)", "Alt+Esc": "cycles windows (Windows)",
    "Alt+Space": "opens the window menu (Windows)", "Ctrl+Esc": "opens Start (Windows)",
    "Ctrl+Shift+Esc": "opens Task Manager (Windows)", "Ctrl+Alt+Delete": "is the secure attention sequence",
    "Ctrl+Alt+Tab": "switches windows (Windows)", "Alt+Left": "means Back in browsers and Explorer",
    "Alt+Right": "means Forward in browsers and Explorer", "Alt+Enter": "opens properties / full screen in many "
                                                                       "applications",
    "Ctrl+Tab": "switches tabs in most applications", "Ctrl+Shift+Tab": "switches tabs in most applications",
    "Ctrl+Alt+Shift+Win+Space": "is reserved for the Office key",
    "Alt+Win+R": "records the screen (Game Bar)", "Alt+Win+G": "records the last moments (Game Bar)",
    "Alt+Win+B": "switches HDR (Windows)", "Alt+Win+K": "mutes the microphone (Windows)",
    "Alt+Win+D": "shows the date and time (Windows)", "Alt+Win+PrintScreen": "takes a screenshot (Game Bar)",
    "Ctrl+Shift+Win+B": "restarts the graphics driver (Windows)",
}
_APP_KEYS = set("ACVXZYSFNOPWTRLKB") | {"1", "2", "3", "4", "5", "6"}  # Ctrl+… in nearly every program (and CIRANA)


def conflict(text: str) -> str:
    """Why this shortcut should not be used ("" when there is no known conflict)."""
    hotkey = parse(text)
    if hotkey is None:
        return "Not a valid shortcut: it needs Ctrl, Alt or Win and one key."
    name = hotkey.text
    if name in _RESERVED:
        return f"{name} {_RESERVED[name]}."
    mods = hotkey.modifiers
    if mods & MOD_WIN and not mods & MOD_ALT and not (mods & MOD_CONTROL and mods & MOD_ALT):
        return f"{name}: Windows uses most Win+… shortcuts (snapping, desktops, Start, settings)."
    if mods == MOD_CONTROL and hotkey.key in _APP_KEYS:
        return f"{name} is used by almost every application (copy, paste, save, …) or by CIRANA."
    if mods == MOD_CONTROL | MOD_SHIFT and hotkey.key in set("TNZPS"):
        return f"{name} is used by many applications (reopen tab, new window, redo, …)."
    if mods in (MOD_ALT, MOD_ALT | MOD_SHIFT) and hotkey.key in _TYPING:
        return f"{name} opens menus or runs commands in many applications."
    return ""


def warning(text: str) -> str:
    """A softer remark: the shortcut works, but may get in the way on some keyboards."""
    hotkey = parse(text)
    if hotkey is None:
        return ""
    if hotkey.modifiers & (MOD_CONTROL | MOD_ALT) == MOD_CONTROL | MOD_ALT and not hotkey.modifiers & MOD_WIN \
            and hotkey.key in _TYPING:
        return (f"{hotkey.text} is AltGr+{hotkey.key} on keyboards with an AltGr key (for example Turkish Q, where "
                "AltGr types @, €, [, ] …): it would stop typing that character.")
    return ""


class Registrar(Protocol):
    """Registers shortcuts (the real one uses RegisterHotKey on its own thread; tests use a stand-in)."""

    def register(self, ident: int, modifiers: int, vk: int) -> int: ...  # 0, or the Win32 error code
    def unregister(self, ident: int) -> None: ...


def explain(code: int) -> str:
    if code == 1409:  # ERROR_HOTKEY_ALREADY_REGISTERED
        return "another program (or Windows) already uses it"
    return f"Windows refused it (error {code})"


class _Relay(QObject):
    """Lives on the GUI thread; the hotkey thread's signals are queued to it."""

    pressed = Signal(int)
    registered = Signal(object)  # {ident: error code}


class _HotkeyThread(threading.Thread):
    """Owns the registrations and their message loop (RegisterHotKey binds a shortcut to the calling thread)."""

    def __init__(self, relay: _Relay) -> None:
        super().__init__(name="cirana-hotkeys", daemon=True)
        self._relay = relay
        self._thread_id = 0
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._wanted: dict[int, tuple[int, int]] = {}
        self._active: set[int] = set()

    def request(self, wanted: dict[int, tuple[int, int]]) -> None:
        from cirana.native import win32_api as w

        with self._lock:
            self._wanted = dict(wanted)
        self._ready.wait(2.0)
        if self._thread_id:
            w.PostThreadMessageW(self._thread_id, w.WM_APP + 1, 0, 0)

    def _apply(self) -> None:
        from cirana.native import win32_api as w

        for ident in list(self._active):
            w.UnregisterHotKey(None, ident)
        self._active.clear()
        with self._lock:
            wanted = dict(self._wanted)
        errors = {}
        for ident, (modifiers, vk) in wanted.items():
            if w.RegisterHotKey(None, ident, modifiers | MOD_NOREPEAT, vk):
                self._active.add(ident)
                errors[ident] = 0
            else:
                errors[ident] = w.last_error() or 1409
        self._relay.registered.emit(errors)

    def run(self) -> None:
        import ctypes

        from cirana.native import win32_api as w

        self._thread_id = int(w.GetCurrentThreadId())
        msg = w.wintypes.MSG()
        w.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)  # make sure the thread has a message queue
        self._ready.set()
        self._apply()
        try:
            while w.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == w.WM_HOTKEY:
                    self._relay.pressed.emit(int(msg.wParam))
                elif msg.message == w.WM_APP + 1:
                    self._apply()
        finally:
            for ident in self._active:
                w.UnregisterHotKey(None, ident)  # on the thread that registered them

    def stop(self, timeout: float = 2.0) -> None:
        from cirana.native import win32_api as w

        self._ready.wait(timeout)
        if self._thread_id:
            w.PostThreadMessageW(self._thread_id, w.WM_QUIT, 0, 0)
        self.join(timeout)


class HotkeyManager(QObject):
    triggered = Signal(str)  # action id
    status_changed = Signal()  # registrations changed (``problems`` tells which failed)
    failed = Signal(str)  # a summary for the status toast when shortcuts could not be registered

    def __init__(self, settings=None, registrar: Registrar | None = None) -> None:  # noqa: ANN001
        super().__init__()
        self.settings = settings
        self._registrar = registrar  # None: the real RegisterHotKey thread (started by start())
        self._thread: _HotkeyThread | None = None
        self._relay = _Relay()
        self._relay.pressed.connect(self._on_pressed)
        self._relay.registered.connect(self._on_registered)
        self._ids: dict[int, str] = {}
        self.problems: dict[str, str] = {}  # action -> why its shortcut does not work
        self.running = False

    # -- settings ------------------------------------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self.settings is None or self.settings.get("hotkeys_enabled") is not False

    def bindings(self) -> dict[str, str]:
        stored = self.settings.get("hotkeys") if self.settings is not None else None
        stored = stored if isinstance(stored, dict) else {}
        result = {}
        for action, default in DEFAULTS.items():
            value = stored.get(action, default)
            result[action] = normalize(value) if isinstance(value, str) else default
        return result

    def check(self, bindings: dict[str, str]) -> dict[str, str]:
        """Problems of a set of shortcuts before it is saved: invalid, known conflicts, used twice."""
        problems, seen = {}, {}
        for action, text in bindings.items():
            if not text:
                continue
            reason = conflict(text)
            name = normalize(text)
            if not reason and name in seen:
                reason = f"{name} is also the shortcut for “{LABELS.get(seen[name], seen[name])}”."
            if reason:
                problems[action] = reason
            seen.setdefault(name, action)
        return problems

    def set_bindings(self, bindings: dict[str, str]) -> dict[str, str]:
        """Save the shortcuts that have no problem and register them; returns the problems (those are not used)."""
        problems = self.check(bindings)
        clean = {action: ("" if action in problems else normalize(text)) for action, text in bindings.items()
                 if action in DEFAULTS}
        if self.settings is not None:
            self.settings.set("hotkeys", {**self.bindings(), **clean})
        self.apply()
        return problems

    def set_enabled(self, on: bool) -> None:
        if self.settings is not None:
            self.settings.set("hotkeys_enabled", bool(on))
        self.apply()

    def reset(self) -> None:
        if self.settings is not None:
            self.settings.set("hotkeys", dict(DEFAULTS))
        self.apply()

    # -- registration --------------------------------------------------------------------------------------------
    def start(self) -> None:
        self.running = True
        if self._registrar is None and self._thread is None:
            self._thread = _HotkeyThread(self._relay)
            self._thread.start()
        self.apply()

    def stop(self) -> None:
        self.running = False
        if self._thread is not None:
            self._thread.stop()
            self._thread = None
        elif self._registrar is not None:
            for ident in list(self._ids):
                self._registrar.unregister(ident)
        self._ids.clear()

    def apply(self) -> None:
        """Register the current shortcuts (all of them again: the set is small)."""
        if not self.running:
            return
        bindings = self.bindings() if self.enabled else {}
        problems = self.check(bindings)
        wanted, ids = {}, {}
        for index, (action, text) in enumerate(sorted(bindings.items()), start=1):
            hotkey = parse(text) if text and action not in problems else None
            if hotkey is not None:
                wanted[index] = (hotkey.modifiers, hotkey.vk)
                ids[index] = action
        self.problems = problems
        if self._registrar is not None:
            for ident in list(self._ids):
                self._registrar.unregister(ident)
            self._ids = ids
            self._on_registered({ident: self._registrar.register(ident, mods | MOD_NOREPEAT, vk)
                                 for ident, (mods, vk) in wanted.items()})
        elif self._thread is not None:
            self._ids = ids
            self._thread.request(wanted)
        self.status_changed.emit()

    @Slot(object)
    def _on_registered(self, errors: dict[int, int]) -> None:
        failed = []
        for ident, code in errors.items():
            action = self._ids.get(ident)
            if action is not None and code:
                text = self.bindings().get(action, "")
                self.problems[action] = f"{text}: {explain(code)}."
                failed.append(text)
        if failed:
            log.warning("Shortcuts not registered: %s", ", ".join(failed))
            self.failed.emit(f"Keyboard shortcut{'s' if len(failed) != 1 else ''} {', '.join(failed)} could not be "
                             "registered: another program (or Windows) already uses "
                             f"{'them' if len(failed) != 1 else 'it'}. Change them in Settings → Keyboard and focus.")
        self.status_changed.emit()

    @Slot(int)
    def _on_pressed(self, ident: int) -> None:
        action = self._ids.get(ident)
        if action is not None:
            self.triggered.emit(action)

    def press(self, action: str) -> None:
        """Act as if the shortcut of ``action`` was pressed (tests, and the buttons in the window lists)."""
        ident = next((i for i, a in self._ids.items() if a == action), None)
        if ident is not None:
            self._on_pressed(ident)

