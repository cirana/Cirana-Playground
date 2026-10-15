"""A simulated desktop implementing the window-manager backend interface (cirana.wm.backend.Win32Backend).

Windows have a visible frame plus invisible resize borders, optional minimum sizes, can be hung, elevated (UIPI
refuses changes), minimized / maximized, closed, or have their handle recycled by another process. Changes are
applied immediately, as if the application processed the posted request at once.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from cirana.native import win32_api as w
from cirana.wm.identity import Rect, TrackedWindow
from cirana.wm.monitors import Monitor, monitor_for_rect

MINIMIZED_RECT = (-32000, -32000, -31840, -31972)


@dataclass
class FakeWindow:
    hwnd: int
    pid: int
    created: float
    exe: str = ""
    title: str = ""
    class_name: str = "AppWindow"
    aumid: str = ""
    rect: Rect = (100, 100, 900, 700)  # visible frame
    margins: tuple[int, int, int, int] = (7, 0, 7, 7)  # invisible borders: left, top, right, bottom
    min_size: tuple[int, int] = (0, 0)
    fixed_size: bool = False
    minimized: bool = False
    maximized: bool = False
    was_maximized: bool = False  # restore target of a minimized window
    normal_rect: Rect = (100, 100, 900, 700)
    hung: bool = False
    elevated: bool = False
    manageable: bool = True
    dpi_aware: bool = True  # resizes itself after moving to a monitor with another scale
    content_exe: str = ""
    content_aumid: str = ""
    shows_pids: frozenset[int] = frozenset()  # a terminal window showing the console of these processes


@dataclass
class FakeDesktop:
    monitors_: list[Monitor] = field(default_factory=lambda: [
        Monitor("\\\\.\\DISPLAY1", (0, 0, 1920, 1080), (0, 0, 1920, 1040), True, 96)])
    own_pid: int = 1
    windows: dict[int, FakeWindow] = field(default_factory=dict)
    calls: list[tuple] = field(default_factory=list)
    foreground: int = 0
    _next_hwnd: int = 0x1000

    # -- building the scene ---------------------------------------------------------------------------------------
    def add(self, exe: str = "c:\\apps\\app.exe", title: str = "App", pid: int | None = None, created: float = 100.0,
            **kwargs) -> FakeWindow:
        self._next_hwnd += 0x10
        hwnd = kwargs.pop("hwnd", self._next_hwnd)
        window = FakeWindow(hwnd, pid if pid is not None else hwnd // 0x10, created, exe, title, **kwargs)
        if "normal_rect" not in kwargs:
            window.normal_rect = window.rect
        self.windows[hwnd] = window
        return window

    def close(self, hwnd: int) -> None:
        self.windows.pop(hwnd, None)

    def tracked(self, hwnd: int) -> TrackedWindow:
        window = self.describe(hwnd)
        assert window is not None
        return window

    # -- backend interface: reading ---------------------------------------------------------------------------------
    def enum_windows(self) -> list[int]:
        return list(self.windows)

    def is_top_level(self, hwnd: int) -> bool:
        return hwnd in self.windows

    def describe(self, hwnd: int) -> TrackedWindow | None:
        win = self.windows.get(hwnd)
        if win is None or not win.manageable:
            return None
        return TrackedWindow(win.hwnd, win.pid, win.created, win.exe, win.aumid, win.title, win.class_name, 0,
                             self.frame_rect(hwnd), win.minimized, win.maximized, win.content_exe, win.content_aumid,
                             not win.fixed_size)

    def hosts_of(self, pids: set[int]) -> set[int]:
        return {win.hwnd for win in self.windows.values() if win.shows_pids & pids and win.pid not in pids}

    def process_created(self, pid: int) -> float:
        return next((win.created for win in self.windows.values() if win.pid == pid), 0.0)

    def still_same(self, window: TrackedWindow) -> bool:
        win = self.windows.get(window.hwnd)
        return win is not None and win.pid == window.pid and win.created == window.process_created

    def frame_rect(self, hwnd: int) -> Rect:
        win = self.windows[hwnd]
        return MINIMIZED_RECT if win.minimized else win.rect

    def window_rect(self, hwnd: int) -> Rect:
        win = self.windows[hwnd]
        if win.minimized:
            return MINIMIZED_RECT
        left, top, right, bottom = win.rect
        ml, mt, mr, mb = win.margins
        return left - ml, top - mt, right + mr, bottom + mb

    def is_minimized(self, hwnd: int) -> bool:
        return self.windows[hwnd].minimized

    def is_maximized(self, hwnd: int) -> bool:
        win = self.windows[hwnd]
        return win.maximized and not win.minimized

    def restores_maximized(self, hwnd: int) -> bool:
        win = self.windows[hwnd]
        return win.minimized and win.was_maximized

    def is_hung(self, hwnd: int) -> bool:
        return self.windows[hwnd].hung

    def higher_rights(self, pid: int) -> bool | None:
        return any(win.elevated for win in self.windows.values() if win.pid == pid)

    def monitors(self) -> list[Monitor]:
        return list(self.monitors_)

    # -- backend interface: changing ------------------------------------------------------------------------------
    def _work_of(self, rect: Rect) -> Rect:
        monitor = monitor_for_rect(rect, self.monitors_) or self.monitors_[0]
        return monitor.work

    def show_async(self, hwnd: int, command: int) -> bool:
        self.calls.append(("show", hwnd, command))
        win = self.windows.get(hwnd)
        if win is None or win.hung or win.elevated:
            return win is not None and not win.elevated
        if command in (w.SW_MINIMIZE, w.SW_SHOWMINNOACTIVE):
            if not win.minimized:
                win.was_maximized = win.maximized
            win.minimized = True
        elif command in (w.SW_RESTORE, w.SW_SHOWNOACTIVATE):
            if win.minimized:
                win.minimized = False
                win.maximized = win.was_maximized
            elif win.maximized and command == w.SW_RESTORE:
                win.maximized = False
                win.rect = win.normal_rect
        elif command == w.SW_SHOWMAXIMIZED:
            win.minimized = False
            if not win.maximized:
                win.normal_rect = win.rect
            win.maximized = True
            win.rect = self._work_of(win.normal_rect)
        return True

    def set_pos(self, hwnd: int, rect: Rect) -> tuple[bool, int]:
        self.calls.append(("pos", hwnd, rect))
        win = self.windows.get(hwnd)
        if win is None:
            return False, w.ERROR_INVALID_WINDOW_HANDLE
        if win.elevated:
            return False, w.ERROR_ACCESS_DENIED
        if win.hung:
            return True, 0  # queued; never processed while it hangs
        ml, mt, mr, mb = win.margins
        left, top, right, bottom = rect[0] + ml, rect[1] + mt, rect[2] - mr, rect[3] - mb
        if win.fixed_size:
            right, bottom = left + win.rect[2] - win.rect[0], top + win.rect[3] - win.rect[1]
        right, bottom = max(right, left + win.min_size[0]), max(bottom, top + win.min_size[1])
        old_monitor = monitor_for_rect(win.rect, self.monitors_)
        win.rect = win.normal_rect = (left, top, right, bottom)
        new_monitor = monitor_for_rect(win.rect, self.monitors_)
        if win.dpi_aware and old_monitor and new_monitor and old_monitor.dpi != new_monitor.dpi:
            factor = new_monitor.dpi / old_monitor.dpi  # WM_DPICHANGED: the app rescales itself after the move
            win.rect = win.normal_rect = (left, top, left + round((right - left) * factor),
                                          top + round((bottom - top) * factor))
        return True, 0

    def raise_window(self, hwnd: int) -> bool:
        self.calls.append(("raise", hwnd))
        return hwnd in self.windows

    def set_foreground(self, hwnd: int) -> bool:
        self.calls.append(("foreground", hwnd))
        self.foreground = hwnd
        return True
