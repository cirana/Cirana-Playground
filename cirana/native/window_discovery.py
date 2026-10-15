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

    @property
    def exe_name(self) -> str:
        return self.exe.rsplit("\\", 1)[-1] if self.exe else ""


def _describe(hwnd: int) -> WindowInfo:
    pid, tid = w.window_pid(hwnd)
    return WindowInfo(hwnd, pid, tid, w.window_text(hwnd), w.class_name(hwnd), w.process_image_path(pid).lower())


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


def list_top_level_windows(only_candidates: bool = True) -> list[WindowInfo]:
    found: list[int] = []

    @w.WNDENUMPROC
    def collect(hwnd, _lparam):  # noqa: ANN001 - ctypes callback
        found.append(int(hwnd))
        return True

    w.EnumWindows(collect, 0)
    return [_describe(h) for h in found if not only_candidates or is_main_window_candidate(h)]


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
    "windows_of_process", "windows_of_executable", "own_pid", "child_windows",
]
