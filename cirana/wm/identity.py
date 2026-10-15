"""Who a window belongs to, and rules that recognise an application's windows again later.

A window handle (HWND) and a process id are both recycled by Windows, so neither is ever stored. At runtime a window
is identified by (hwnd, pid, process creation time): if any of the three changes, it is a different window. On disk,
workspaces keep a :class:`MatchRule` (executable, AppUserModelID, window class, title) that finds the application's
windows again after CIRANA or the application restarted.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import psutil

from cirana.core.models import AppEntry, LaunchType
from cirana.native import win32_api as w

# Start-menu entries whose AppUserModelID names a classic program.
_AUMID_PROGRAMS = {"microsoft.windows.remotedesktop": "mstsc.exe"}
CREATE_TIME_TOLERANCE = 0.01  # seconds; the same process always reports the same creation time


def norm_exe(path: str) -> str:
    return os.path.normcase(os.path.normpath(path)).lower() if path else ""


def _aumid_program(app: AppEntry) -> str:
    """The executable behind an AppUserModelID launch, when there is one.

    Get-StartApps reports classic programs without an explicit AUMID by their full path (``C:\\WINDOWS\\System32\\
    charmap.exe``); those are ordinary Win32 programs, not packaged apps."""
    target = app.target.strip()
    if os.path.isabs(target) and target.lower().endswith(".exe"):
        return target
    program = _AUMID_PROGRAMS.get(target.lower())
    return os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32", program) if program else ""


def app_exe(app: AppEntry) -> str:
    """The application's executable (normalized), or "" when it is unknown (packaged apps, commands)."""
    if app.exe_path:
        return norm_exe(app.exe_path)
    if app.launch_type == LaunchType.EXECUTABLE.value:
        return norm_exe(app.target)
    if app.launch_type == LaunchType.AUMID.value:
        return norm_exe(_aumid_program(app))
    return ""


def app_aumid(app: AppEntry) -> str:
    """The packaged app's AppUserModelID (``Family!App``), or ""."""
    if app.launch_type == LaunchType.AUMID.value and "!" in app.target:
        return app.target.strip()
    return ""


def family_package(family: str) -> tuple[str, str] | None:
    """(name, publisher id) of a package family name ``Name_PublisherId``."""
    if "_" not in family:
        return None
    name, publisher = family.rsplit("_", 1)
    return (name.lower(), publisher.lower()) if name and publisher else None


def aumid_package(aumid: str) -> tuple[str, str] | None:
    """(package name, publisher id) of an AUMID ``Name_PublisherId!AppId``, else None."""
    return family_package(aumid.split("!", 1)[0]) if "!" in aumid else None


def alias_aumid(exe: str) -> str:
    """The AppUserModelID behind an app execution alias (``...\\Microsoft\\WindowsApps\\wt.exe``). The alias only
    hands the request over: the window then belongs to another image of the package (WindowsTerminal.exe)."""
    if not exe or "\\microsoft\\windowsapps\\" not in exe.lower():
        return ""
    fields = w.read_app_alias(exe)
    return fields[1] if len(fields) > 1 and "!" in fields[1] else ""


def in_package(exe: str, package: tuple[str, str] | None) -> bool:
    """True if ``exe`` lives in the package's install folder ``...\\<Name>_<version>_<arch>__<PublisherId>\\``."""
    if not package or not exe:
        return False
    name, publisher = package
    return any(part.startswith(name + "_") and part.endswith("__" + publisher) for part in exe.lower().split("\\"))


def process_started(pid: int) -> float:
    """Creation time of process ``pid`` (0.0 when it is gone or cannot be inspected)."""
    try:
        return psutil.Process(pid).create_time() if pid else 0.0
    except psutil.Error:
        return 0.0


@dataclass(frozen=True)
class ProcessFacts:
    created: float
    exe: str  # lower-case image path ("" when it cannot be read)
    parent: int  # the pid recorded as its parent when it was started (that process may be gone by now)


def process_facts(pid: int) -> ProcessFacts | None:
    """Creation time, image path and parent pid of a process (None when it is gone or cannot be inspected)."""
    try:
        proc = psutil.Process(pid)
        with proc.oneshot():
            created, parent = proc.create_time(), proc.ppid()
    except psutil.Error:
        return None
    return ProcessFacts(created, w.process_image_path(pid).lower(), parent)


Rect = tuple[int, int, int, int]  # left, top, right, bottom (physical pixels, virtual-screen coordinates)


@dataclass(frozen=True)
class TrackedWindow:
    """What CIRANA knows about one top-level window at one moment (immutable snapshot)."""

    hwnd: int
    pid: int
    process_created: float
    exe: str = ""  # lower-case image path of the owning process ("" if it cannot be read)
    aumid: str = ""  # the process's AppUserModelID (packaged apps)
    title: str = ""
    class_name: str = ""
    owner: int = 0
    rect: Rect = (0, 0, 0, 0)  # visible frame (without the invisible DWM resize borders)
    minimized: bool = False
    maximized: bool = False
    # A UWP window is an ApplicationFrameWindow of ApplicationFrameHost.exe; the app itself is its CoreWindow child.
    content_exe: str = ""
    content_aumid: str = ""
    # It has a sizing border (WS_THICKFRAME). Fixed-size dialogs and borderless full-screen windows (games, videos)
    # have none; all-window tiling leaves them floating.
    resizable: bool = True

    @property
    def key(self) -> str:
        """Runtime identity: a recycled hwnd or pid gives a different key. Never persisted."""
        return f"{self.hwnd:x}:{self.pid}:{round(self.process_created * 1000)}"

    @property
    def exe_name(self) -> str:
        return os.path.basename(self.app_exe) if self.app_exe else ""

    @property
    def app_exe(self) -> str:
        """The application's own image (the UWP app inside a frame, not ApplicationFrameHost.exe)."""
        return self.content_exe or self.exe

    @property
    def app_aumid(self) -> str:
        return self.content_aumid or self.aumid

    @property
    def display_title(self) -> str:
        return self.title.strip() or self.exe_name or f"Window of process {self.pid}"

    def same_window(self, other: "TrackedWindow | None") -> bool:
        return other is not None and other.key == self.key


@dataclass
class MatchRule:
    """How a workspace entry recognises its application's windows. Stored on disk; never holds an HWND or a PID.

    A window matches when it belongs to the application (executable path or file name, AppUserModelID / package, or
    a program learned while launching it, ``alt_exes``) and passes the optional class and title filters."""

    exe: str = ""  # normalized full path of the executable
    aumid: str = ""
    class_name: str = ""  # exact window class, when set
    title: str = ""  # case-insensitive substring of the window title, when set
    alt_exes: list[str] = field(default_factory=list)  # file names of programs that show this app's windows

    @property
    def empty(self) -> bool:
        return not (self.exe or self.aumid or self.alt_exes or (self.class_name and self.title))

    def owns(self, window: TrackedWindow) -> bool:
        """The window belongs to the application (ignoring the class / title filters)."""
        exes = {e for e in (window.exe, window.content_exe) if e}
        names = {os.path.basename(e) for e in exes}
        if self.exe and (self.exe in exes or os.path.basename(self.exe) in names):
            return True  # the file name also catches launcher stubs (System32\notepad.exe -> the packaged Notepad)
        if self.alt_exes and names & {n.lower() for n in self.alt_exes}:
            return True
        if self.aumid:
            wanted = self.aumid.casefold()
            if wanted in {a.casefold() for a in (window.aumid, window.content_aumid) if a}:
                return True
            package = aumid_package(self.aumid)
            if package and any(in_package(e, package) for e in exes):
                return True
        return False

    def matches(self, window: TrackedWindow) -> bool:
        if self.empty:
            return False
        if self.class_name and window.class_name != self.class_name:
            return False
        if self.title and self.title.casefold() not in window.title.casefold():
            return False
        if self.exe or self.aumid or self.alt_exes:
            return self.owns(window)
        return True  # a class + title rule (an application whose image cannot be read)

    def learn(self, window: TrackedWindow) -> bool:
        """Remember the program that showed this app's window (PowerShell inside Windows Terminal, Discord.exe started
        by Update.exe) so the window is recognised again after a restart. True when something new was learned."""
        name = window.exe_name.lower()
        if not name or self.owns(window) or name in {n.lower() for n in self.alt_exes}:
            return False
        self.alt_exes.append(name)
        return True

    def to_dict(self) -> dict[str, Any]:
        return {"exe": self.exe, "aumid": self.aumid, "class_name": self.class_name, "title": self.title,
                "alt_exes": list(self.alt_exes)}

    @classmethod
    def from_dict(cls, raw: Any) -> "MatchRule":
        if not isinstance(raw, dict):
            return cls()

        def text(key: str) -> str:
            value = raw.get(key)
            return value if isinstance(value, str) else ""

        alt = raw.get("alt_exes")
        return cls(norm_exe(text("exe")), text("aumid"), text("class_name"), text("title"),
                   [a.lower() for a in alt if isinstance(a, str) and a] if isinstance(alt, list) else [])

    @classmethod
    def for_app(cls, app: AppEntry) -> "MatchRule":
        exe = app_exe(app)
        aumid = app_aumid(app) or alias_aumid(exe)
        return cls(exe=exe, aumid=aumid)

    @classmethod
    def for_window(cls, window: TrackedWindow) -> "MatchRule":
        """A rule for a window picked from the open-window list (its application, any of its windows)."""
        if window.app_exe or window.app_aumid:
            return cls(exe=window.app_exe, aumid=window.app_aumid)
        return cls(class_name=window.class_name, title=window.title.strip())
