"""The Win32 side of the window manager, behind one object.

The tracker and the controller only talk to a backend, so the tests run them against a simulated desktop
(``tests/fake_desktop.py``). Rules for this module:

* reading uses calls that send no message to the window (``GetWindowLongPtr``, ``GetWindowRect``, DWM attributes,
  ``GetWindowText`` on another process's window reads a cached copy);
* changing uses only ``SetWindowPos`` / ``ShowWindowAsync`` with asynchronous flags (posted to the window's own thread,
  so a hung application can never block CIRANA) and ``SetForegroundWindow``;
* never: re-parenting, style changes, input injection, thread-input attachment, DLL injection.
"""
from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes

from cirana.native import win32_api as w
from cirana.wm.filters import WindowFacts, manageable
from cirana.wm.identity import CREATE_TIME_TOLERANCE, Rect, TrackedWindow
from cirana.wm.monitors import Monitor, enum_monitors

log = logging.getLogger(__name__)

SHOW_RESTORE = w.SW_RESTORE
SHOW_MAXIMIZE = w.SW_SHOWMAXIMIZED
SHOW_MINIMIZE = w.SW_SHOWMINNOACTIVE
MOVE_FLAGS = w.SWP_NOZORDER | w.SWP_NOACTIVATE | w.SWP_NOOWNERZORDER | w.SWP_ASYNCWINDOWPOS


class Win32Backend:
    """Real desktop. Safe to use from any thread (each call is an independent, non-blocking Win32 call)."""

    def __init__(self) -> None:
        self._own_pid = int(w.GetCurrentProcessId())

    # -- enumeration and identity ------------------------------------------------------------------------------
    @property
    def own_pid(self) -> int:
        return self._own_pid

    def enum_windows(self) -> list[int]:
        found: list[int] = []

        @w.WNDENUMPROC
        def collect(hwnd, _lparam):  # noqa: ANN001 - ctypes callback
            found.append(int(hwnd))
            return True

        w.EnumWindows(collect, 0)
        return found

    def is_top_level(self, hwnd: int) -> bool:
        return int(w.GetAncestor(hwnd, w.GA_PARENT) or 0) == int(w.GetDesktopWindow() or 0)

    @staticmethod
    def _process(pid: int) -> tuple[float, str, str]:
        return w.process_identity(pid)  # one limited-rights handle: cheap enough to read every time

    def process_created(self, pid: int) -> float:
        return w.process_identity(pid)[0] if pid else 0.0

    def facts(self, hwnd: int) -> WindowFacts | None:
        if not w.IsWindow(hwnd):
            return None
        pid = w.window_pid(hwnd)[0]
        if pid == self._own_pid:
            # Never one of ours: and GetWindowText on a window of this process sends WM_GETTEXT to the GUI thread,
            # which would tie this (probe) thread to it.
            return None
        owner = int(w.GetWindow(hwnd, w.GW_OWNER) or 0)
        _created, exe, _aumid = self._process(pid)
        return WindowFacts(
            hwnd=hwnd, pid=pid, title=w.window_text(hwnd), class_name=w.class_name(hwnd), exe=exe,
            style=int(w.GetWindowLongPtrW(hwnd, w.GWL_STYLE)) & 0xFFFFFFFF,
            ex_style=int(w.GetWindowLongPtrW(hwnd, w.GWL_EXSTYLE)) & 0xFFFFFFFF,
            owner=owner, owner_visible=bool(owner and w.IsWindowVisible(owner)),
            visible=bool(w.IsWindowVisible(hwnd)), cloaked=w.is_cloaked(hwnd), minimized=bool(w.IsIconic(hwnd)),
            rect=w.window_rect(hwnd),
        )

    def describe(self, hwnd: int) -> TrackedWindow | None:
        """The window as CIRANA tracks it, or None when it is gone or not an application window."""
        facts = self.facts(hwnd)
        if facts is None or not manageable(facts, self._own_pid)[0]:
            return None
        created, exe, aumid = self._process(facts.pid)
        if not created:
            return None  # its process cannot be identified: nothing could ever be verified about it
        content_exe = content_aumid = ""
        if facts.class_name == "ApplicationFrameWindow":  # a UWP app: identify it by its CoreWindow, not the frame
            for child in self._children(hwnd):
                if w.class_name(child) == "Windows.UI.Core.CoreWindow":
                    child_pid = w.window_pid(child)[0]
                    if child_pid and child_pid != facts.pid:
                        _c, content_exe, content_aumid = self._process(child_pid)
                        break
        return TrackedWindow(
            hwnd=hwnd, pid=facts.pid, process_created=created, exe=exe, aumid=aumid, title=facts.title,
            class_name=facts.class_name, owner=facts.owner, rect=self.frame_rect(hwnd), minimized=facts.minimized,
            maximized=bool(w.IsZoomed(hwnd)), content_exe=content_exe, content_aumid=content_aumid,
            resizable=bool(facts.style & w.WS_THICKFRAME),
        )

    @staticmethod
    def _children(hwnd: int) -> list[int]:
        result: list[int] = []

        @w.WNDENUMPROC
        def collect(child, _lparam):  # noqa: ANN001
            result.append(int(child))
            return True

        w.EnumChildWindows(hwnd, collect, 0)
        return result

    def still_same(self, window: TrackedWindow) -> bool:
        """The handle still names the same window: it exists, belongs to the same pid, and that pid still is the
        same process (a recycled pid has another creation time)."""
        if not w.IsWindow(window.hwnd) or w.window_pid(window.hwnd)[0] != window.pid:
            return False
        return abs(self.process_created(window.pid) - window.process_created) <= CREATE_TIME_TOLERANCE

    def hosts_of(self, pids: set[int]) -> set[int]:
        """Windows of other processes that own a window of a process in ``pids``: a terminal owns the console window
        of each shell it shows (PowerShell / CMD inside Windows Terminal). GetWindow / GetWindowThreadProcessId only:
        no process is opened and no message is sent."""
        hosts = set()
        for hwnd in self.enum_windows():
            owner = int(w.GetWindow(hwnd, w.GW_OWNER) or 0)
            if owner and w.window_pid(hwnd)[0] in pids and w.window_pid(owner)[0] not in pids:
                hosts.add(owner)
        return hosts

    # -- state ------------------------------------------------------------------------------------------------
    def frame_rect(self, hwnd: int) -> Rect:
        return w.visible_frame_rect(hwnd)

    def window_rect(self, hwnd: int) -> Rect:
        return w.window_rect(hwnd)

    def is_minimized(self, hwnd: int) -> bool:
        return bool(w.IsIconic(hwnd))

    def is_maximized(self, hwnd: int) -> bool:
        return bool(w.IsZoomed(hwnd))

    def restores_maximized(self, hwnd: int) -> bool:
        """A minimized window that comes back maximized when restored (GetWindowPlacement)."""
        placement = w.WINDOWPLACEMENT()
        placement.length = ctypes.sizeof(placement)
        return bool(w.GetWindowPlacement(hwnd, ctypes.byref(placement))
                    and placement.flags & w.WPF_RESTORETOMAXIMIZED)

    def is_hung(self, hwnd: int) -> bool:
        return bool(w.IsHungAppWindow(hwnd))

    def higher_rights(self, pid: int) -> bool | None:
        """True when the process runs at a higher integrity level than CIRANA: Windows (UIPI) then refuses every
        change to its windows. None when its token cannot be read (often: it is elevated)."""
        theirs, ours = w.process_integrity(pid), w.process_integrity(self._own_pid)
        if theirs is None or ours is None:
            return None
        return theirs > ours

    def monitors(self) -> list[Monitor]:
        return enum_monitors()

    def monitor_of(self, hwnd: int, monitors: list[Monitor]) -> Monitor | None:
        from cirana.wm.monitors import monitor_for_rect

        return monitor_for_rect(self.frame_rect(hwnd), monitors)

    # -- changes (asynchronous: posted to the window's own thread) --------------------------------------------
    def show_async(self, hwnd: int, command: int) -> bool:
        return bool(w.ShowWindowAsync(hwnd, command))

    def set_pos(self, hwnd: int, rect: Rect) -> tuple[bool, int]:
        """Move and size (outer window rectangle). (accepted, Win32 error code)."""
        left, top, right, bottom = rect
        ctypes.set_last_error(0)
        ok = bool(w.SetWindowPos(hwnd, None, left, top, right - left, bottom - top, MOVE_FLAGS))
        return ok, 0 if ok else w.last_error()

    def raise_window(self, hwnd: int) -> bool:
        flags = w.SWP_NOMOVE | w.SWP_NOSIZE | w.SWP_NOACTIVATE | w.SWP_ASYNCWINDOWPOS
        return bool(w.SetWindowPos(hwnd, wintypes.HWND(w.HWND_TOP), 0, 0, 0, 0, flags))

    def set_foreground(self, hwnd: int) -> bool:
        """Windows only lets the foreground process hand the foreground on; CIRANA is it right after a click."""
        return bool(w.SetForegroundWindow(hwnd))
