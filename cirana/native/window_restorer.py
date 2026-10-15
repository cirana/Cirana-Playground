"""Capturing and restoring the native state of a window that was re-parented into CIRANA."""
from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import wintypes
from dataclasses import dataclass

from cirana.native import win32_api as w

log = logging.getLogger(__name__)

_STYLE_MASK = 0xFFFFFFFF


@dataclass(frozen=True)
class WindowState:
    hwnd: int
    parent: int  # 0 = desktop
    style: int
    ex_style: int
    rect: tuple[int, int, int, int]  # left, top, right, bottom in screen coordinates (normal position)
    maximized: bool
    minimized: bool


def capture_state(hwnd: int) -> WindowState:
    placement = w.WINDOWPLACEMENT()
    placement.length = ctypes.sizeof(placement)
    w.GetWindowPlacement(hwnd, ctypes.byref(placement))
    normal = placement.rcNormalPosition
    return WindowState(
        hwnd=hwnd,
        parent=int(w.GetParent(hwnd) or 0),
        style=int(w.GetWindowLongPtrW(hwnd, w.GWL_STYLE)) & _STYLE_MASK,
        ex_style=int(w.GetWindowLongPtrW(hwnd, w.GWL_EXSTYLE)) & _STYLE_MASK,
        rect=(normal.left, normal.top, normal.right, normal.bottom),
        maximized=bool(w.IsZoomed(hwnd)),
        minimized=bool(w.IsIconic(hwnd)),
    )


def _rect_is_on_screen(rect: tuple[int, int, int, int]) -> bool:
    left, top, right, bottom = rect
    box = wintypes.RECT(left, top, right, bottom)
    return bool(w.MonitorFromRect(ctypes.byref(box), w.MONITOR_DEFAULTTONULL))


def _restore_now(state: WindowState, activate: bool) -> None:
    hwnd = state.hwnd
    # Order matters (see SetParent remarks): re-parent first, then restore WS_CHILD/WS_POPUP and the frame styles.
    w.SetParent(hwnd, state.parent or None)
    w.SetWindowLongPtrW(hwnd, w.GWL_STYLE, state.style)
    w.SetWindowLongPtrW(hwnd, w.GWL_EXSTYLE, state.ex_style)
    left, top, right, bottom = state.rect
    flags = w.SWP_NOZORDER | w.SWP_NOACTIVATE | w.SWP_FRAMECHANGED | w.SWP_SHOWWINDOW | w.SWP_ASYNCWINDOWPOS
    if _rect_is_on_screen(state.rect):
        w.SetWindowPos(hwnd, None, left, top, right - left, bottom - top, flags)
    else:  # the monitor it came from is gone: keep the size, let Windows pick a visible position
        w.SetWindowPos(hwnd, None, 0, 0, right - left, bottom - top, flags | w.SWP_NOMOVE)
    if state.maximized:
        w.ShowWindow(hwnd, w.SW_SHOWMAXIMIZED)
    elif state.minimized:
        w.ShowWindow(hwnd, w.SW_MINIMIZE)
    else:
        w.ShowWindow(hwnd, w.SW_SHOWNORMAL)
    if activate and not state.minimized:
        w.SetForegroundWindow(hwnd)


def restore_state(state: WindowState, activate: bool = False, hung_timeout: float = 0.4) -> bool:
    """Put a window back where it came from. Returns False if the window no longer exists.

    A hung application can block cross-process calls such as SetParent, so for a hung window the work is
    done on a helper thread and abandoned after ``hung_timeout`` seconds instead of freezing CIRANA.
    """
    hwnd = state.hwnd
    if not w.IsWindow(hwnd):
        return False
    if not w.IsHungAppWindow(hwnd):
        _restore_now(state, activate)
        return True
    worker = threading.Thread(target=_restore_now, args=(state, False), daemon=True, name="cirana-restore")
    worker.start()
    worker.join(hung_timeout)
    if worker.is_alive():
        log.warning("Window %#x is not responding; its restore was left pending", hwnd)
        return False
    return True


def place_window(hwnd: int, rect: tuple[int, int, int, int], maximized: bool) -> bool:
    """Move an ordinary top-level window to a saved position. Refuses rectangles that are on no monitor."""
    if not w.IsWindow(hwnd) or not _rect_is_on_screen(rect):
        return False
    placement = w.WINDOWPLACEMENT()
    placement.length = ctypes.sizeof(placement)
    if not w.GetWindowPlacement(hwnd, ctypes.byref(placement)):
        return False
    placement.rcNormalPosition = wintypes.RECT(*rect)
    placement.showCmd = w.SW_SHOWMAXIMIZED if maximized else w.SW_SHOWNORMAL
    return bool(w.SetWindowPlacement(hwnd, ctypes.byref(placement)))
