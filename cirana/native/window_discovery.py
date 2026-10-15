"""Finding the top-level window that belongs to an application CIRANA just started."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from cirana.native import win32_api as w


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    pid: int
    tid: int
    title: str
    class_name: str
    exe: str  # lower-case image path of the owning process ("" if inaccessible)
    owner: int = 0  # GW_OWNER: the window this one belongs to (0 = un-owned)

    @property
    def exe_name(self) -> str:
        return self.exe.rsplit("\\", 1)[-1] if self.exe else ""


def _describe(hwnd: int) -> WindowInfo:
    pid, tid = w.window_pid(hwnd)
    return WindowInfo(hwnd, pid, tid, w.window_text(hwnd), w.class_name(hwnd), w.process_image_path(pid).lower(),
                      int(w.GetWindow(hwnd, w.GW_OWNER) or 0))


def describe_window(hwnd: int) -> WindowInfo:
    return _describe(hwnd)


def is_main_window_candidate(hwnd: int) -> bool:
    """Visible, un-owned, non-tool, non-cloaked windows with a real size: what a user sees as 'the app window'."""
    if not w.IsWindow(hwnd) or not w.IsWindowVisible(hwnd) or w.is_cloaked(hwnd):
        return False
    if w.GetWindow(hwnd, w.GW_OWNER):
        return False
    ex_style = w.GetWindowLongPtrW(hwnd, w.GWL_EXSTYLE)
    if ex_style & w.WS_EX_TOOLWINDOW and not ex_style & w.WS_EX_APPWINDOW:
        return False
    left, top, right, bottom = w.window_rect(hwnd)
    return (right - left) > 80 and (bottom - top) > 60


def is_interactive_owned_window(hwnd: int) -> bool:
    """An owned top-level window a user really works with (not a menu, tooltip, drop-down or splash).

    Some programs make their main window owned by a hidden helper window (Delphi / VCL, some .NET apps), and tool
    palettes or dialogs are owned by the main window. Accepted only when it is visible, not cloaked, enabled (not
    blocked by a modal dialog), activatable, not a tool window, has a real title bar (or asks for a taskbar button)
    and a real size. GetWindow / GetWindowLongPtr / GetWindowRect send nothing to the window.
    """
    if not w.IsWindow(hwnd) or not w.GetWindow(hwnd, w.GW_OWNER):
        return False
    if not w.IsWindowVisible(hwnd) or w.is_cloaked(hwnd):
        return False
    style = int(w.GetWindowLongPtrW(hwnd, w.GWL_STYLE)) & 0xFFFFFFFF
    ex_style = int(w.GetWindowLongPtrW(hwnd, w.GWL_EXSTYLE)) & 0xFFFFFFFF
    if style & (w.WS_CHILD | w.WS_DISABLED) or ex_style & w.WS_EX_NOACTIVATE:
        return False
    if ex_style & w.WS_EX_TOOLWINDOW and not ex_style & w.WS_EX_APPWINDOW:
        return False
    if style & w.WS_CAPTION != w.WS_CAPTION and not ex_style & w.WS_EX_APPWINDOW:
        return False  # menus, tooltips, address-bar suggestions, splash screens: popups without a title bar
    left, top, right, bottom = w.window_rect(hwnd)
    return (right - left) > 80 and (bottom - top) > 60


def list_top_level_windows(only_candidates: bool = True, include_owned: bool = False) -> list[WindowInfo]:
    """``include_owned``: also interactive owned windows (the Running window picker). Launch detection and crash
    recovery keep using main window candidates only."""
    found: list[int] = []

    @w.WNDENUMPROC
    def collect(hwnd, _lparam):  # noqa: ANN001 - ctypes callback
        found.append(int(hwnd))
        return True

    w.EnumWindows(collect, 0)
    return [_describe(h) for h in found if not only_candidates or is_main_window_candidate(h)
            or (include_owned and is_interactive_owned_window(h))]


def snapshot_handles() -> set[int]:
    """Handles of every current top-level window (cheap: no per-window process lookups)."""
    found: set[int] = set()

    @w.WNDENUMPROC
    def collect(hwnd, _lparam):  # noqa: ANN001 - ctypes callback
        found.add(int(hwnd))
        return True

    w.EnumWindows(collect, 0)
    return found


def wait_for_new_window(
    before: set[int],
    accept: Callable[[WindowInfo], bool],
    timeout: float = 10.0,
    poll: float = 0.1,
) -> WindowInfo | None:
    """Poll until a candidate window that did not exist in ``before`` and satisfies ``accept`` appears."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for info in list_top_level_windows():
            if info.hwnd not in before and accept(info):
                return info
        time.sleep(poll)
    return None


def windows_of_executable(exe_path: str) -> list[WindowInfo]:
    """Candidate main windows whose process image is ``exe_path`` (compared case-insensitively)."""
    wanted = exe_path.lower()
    if not wanted:  # windows of processes that cannot be inspected also have exe == ""
        return []
    return [info for info in list_top_level_windows() if info.exe == wanted]


def windows_of_process(pid: int) -> list[WindowInfo]:
    return [info for info in list_top_level_windows() if info.pid == pid]


def own_pid() -> int:
    return int(w.GetCurrentProcessId())


def child_windows(hwnd: int) -> list[int]:
    result: list[int] = []

    @w.WNDENUMPROC
    def collect(child, _lparam):  # noqa: ANN001
        result.append(int(child))
        return True

    w.EnumChildWindows(hwnd, collect, 0)
    return result


__all__ = [
    "WindowInfo", "describe_window", "is_main_window_candidate", "list_top_level_windows", "snapshot_handles", "wait_for_new_window",
    "is_interactive_owned_window", "windows_of_process", "windows_of_executable", "own_pid", "child_windows",
]
