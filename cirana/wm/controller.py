"""Moving, sizing, restoring and raising real top-level windows - and saying honestly what happened.

Every request goes to the window's own thread asynchronously (``SetWindowPos`` with ``SWP_ASYNCWINDOWPOS``,
``ShowWindowAsync``), so a hung application cannot block CIRANA. Because an asynchronous call says nothing about the
outcome, the controller reads the window back after a moment and reports per window: placed, kept a minimum size,
refused by Windows (an elevated application), not responding, or gone. One window's failure never stops the others.

The methods that wait (``place_all``, ``bring_to_front``) are meant for a worker thread (``core.background.run_async``).
Nothing here changes a window's parent, owner or styles.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from enum import Enum

from cirana.native import win32_api as w
from cirana.wm.identity import Rect, TrackedWindow
from cirana.wm.monitors import monitor_for_rect

log = logging.getLogger(__name__)

TOLERANCE = 2  # pixels a window may be off (rounding of DPI-scaled sizes)
SETTLE = 0.25  # seconds between posting the changes and reading the windows back
ATTEMPTS = 2  # the second pass corrects frame margins measured on a minimized window and DPI-change resizes


class Status(str, Enum):
    PLACED = "placed"
    CONSTRAINED = "constrained"  # moved, but the application kept a minimum (or fixed) size
    DENIED = "denied"  # Windows refused (higher rights / UIPI)
    HUNG = "hung"  # not responding: the request waits in its queue
    GONE = "gone"  # closed, or the handle now names another window
    FAILED = "failed"  # anything else (the application moved itself back, an unknown error)


@dataclass(frozen=True)
class Placement:
    window: TrackedWindow
    rect: Rect  # target visible frame (physical pixels)
    maximized: bool = False  # maximize on the monitor that contains ``rect``


@dataclass(frozen=True)
class Result:
    key: str
    title: str
    status: Status
    message: str = ""
    actual: Rect | None = None

    @property
    def ok(self) -> bool:
        return self.status in (Status.PLACED, Status.CONSTRAINED)


def _off_by(a: Rect, b: Rect) -> int:
    return max(abs(x - y) for x, y in zip(a, b))


class WindowController:
    def __init__(self, backend, settle: float = SETTLE) -> None:  # noqa: ANN001 - Win32Backend or a test double
        self.backend = backend
        self.settle = settle

    # -- guards ------------------------------------------------------------------------------------------------
    def check(self, window: TrackedWindow) -> Result | None:
        """Why the window must not (or cannot) be touched now; None when it may be."""
        backend, name = self.backend, window.display_title
        if not backend.still_same(window):
            return Result(window.key, name, Status.GONE, f"{name} was closed (or its window was replaced).")
        if backend.higher_rights(window.pid):
            return Result(window.key, name, Status.DENIED, f"{name}: {w.explain_error(w.ERROR_ACCESS_DENIED, True)}")
        if backend.is_hung(window.hwnd):
            return Result(window.key, name, Status.HUNG, f"{name} is not responding; CIRANA left it alone.")
        return None

    def _refused(self, window: TrackedWindow, code: int) -> Result:
        name = window.display_title
        if code == w.ERROR_ACCESS_DENIED:
            higher = self.backend.higher_rights(window.pid)
            return Result(window.key, name, Status.DENIED, f"{name}: {w.explain_error(code, higher)}")
        if code == w.ERROR_INVALID_WINDOW_HANDLE:
            return Result(window.key, name, Status.GONE, f"{name} was closed.")
        return Result(window.key, name, Status.FAILED, f"{name}: {w.explain_error(code)}")

    # -- placing -----------------------------------------------------------------------------------------------
    def _outer(self, hwnd: int, frame: Rect) -> Rect:
        """The window rectangle whose visible frame is ``frame`` (adds the invisible resize borders back)."""
        if self.backend.is_minimized(hwnd):
            return frame  # its borders cannot be measured while minimized; the second pass corrects it
        outer, visible = self.backend.window_rect(hwnd), self.backend.frame_rect(hwnd)
        margins = (visible[0] - outer[0], visible[1] - outer[1], outer[2] - visible[2], outer[3] - visible[3])
        if any(m < 0 or m > 64 for m in margins):  # nonsense while the window is changing state
            margins = (0, 0, 0, 0)
        return frame[0] - margins[0], frame[1] - margins[1], frame[2] + margins[2], frame[3] + margins[3]

    def outer_frame(self, window: TrackedWindow, frame: Rect) -> Rect:
        """The window rectangle whose visible frame is ``frame`` (for animations, which glide outer rectangles)."""
        return self._outer(window.hwnd, frame)

    def nudge(self, window: TrackedWindow, outer: Rect) -> bool:
        """One animation frame: the asynchronous request only, nothing is verified (``place_all`` does that at the
        end). False when Windows refused it, so the window leaves the animation."""
        ok, _code = self.backend.set_pos(window.hwnd, outer)
        return ok

    def _post(self, placement: Placement, monitors: list) -> Result | None:  # noqa: ANN001
        """Send one window on its way. A Result when it was refused right away."""
        backend, hwnd = self.backend, placement.window.hwnd
        before = monitor_for_rect(backend.frame_rect(hwnd), monitors)
        after = monitor_for_rect(placement.rect, monitors)
        if placement.maximized and backend.is_maximized(hwnd) and before == after:
            return None  # already maximized where it belongs
        if backend.is_maximized(hwnd):
            backend.show_async(hwnd, w.SW_RESTORE)
        elif backend.is_minimized(hwnd):
            backend.show_async(hwnd, w.SW_SHOWNOACTIVATE)  # shown again without taking the focus
            if backend.restores_maximized(hwnd):
                backend.show_async(hwnd, w.SW_RESTORE)  # it came back maximized: restore it to a normal window
        ok, code = backend.set_pos(hwnd, self._outer(hwnd, placement.rect))
        if not ok:
            return self._refused(placement.window, code)
        if before is not None and after is not None and before.device != after.device and before.dpi != after.dpi:
            # Moving to a monitor with another scale makes per-monitor aware applications resize themselves
            # (WM_DPICHANGED) after the move: the same request once more, queued behind it, wins.
            backend.set_pos(hwnd, self._outer(hwnd, placement.rect))
        if placement.maximized:
            backend.show_async(hwnd, w.SW_SHOWMAXIMIZED)
        return None

    def _verify(self, placement: Placement, final: bool) -> Result | None:
        """The outcome, or None when another attempt should be made."""
        backend, window = self.backend, placement.window
        name = window.display_title
        if not backend.still_same(window):
            return Result(window.key, name, Status.GONE, f"{name} closed while it was being arranged.")
        actual = backend.frame_rect(window.hwnd)
        if placement.maximized:
            if backend.is_maximized(window.hwnd):
                return Result(window.key, name, Status.PLACED, actual=actual)
        elif not (backend.is_minimized(window.hwnd) or backend.is_maximized(window.hwnd)) \
                and _off_by(actual, placement.rect) <= TOLERANCE:
            return Result(window.key, name, Status.PLACED, actual=actual)
        if backend.is_hung(window.hwnd):
            return Result(window.key, name, Status.HUNG, f"{name} stopped responding; it moves once it responds.",
                          actual)
        if not final:
            return None
        target = placement.rect
        wider = actual[2] - actual[0] > target[2] - target[0] + TOLERANCE
        taller = actual[3] - actual[1] > target[3] - target[1] + TOLERANCE
        if wider or taller:
            return Result(window.key, name, Status.CONSTRAINED,
                          f"{name} keeps a minimum size of {actual[2] - actual[0]}×{actual[3] - actual[1]} pixels, so "
                          "it is larger than its place in the layout.", actual)
        if abs(actual[0] - target[0]) <= TOLERANCE and abs(actual[1] - target[1]) <= TOLERANCE:
            return Result(window.key, name, Status.CONSTRAINED,
                          f"{name} has a fixed size; it was moved but not resized.", actual)
        return Result(window.key, name, Status.FAILED, f"{name} did not accept the new position (the application "
                                                       "positioned its window itself).", actual)

    def place_all(self, placements: list[Placement], cancel: threading.Event | None = None) -> list[Result]:
        """Arrange several windows. Worker thread: waits ``settle`` seconds (up to twice) to verify."""
        cancel = cancel or threading.Event()
        results: dict[int, Result] = {}
        monitors = self.backend.monitors()
        pending: list[int] = []
        for index, placement in enumerate(placements):
            refused = self.check(placement.window) or self._post(placement, monitors)
            if refused is not None:
                results[index] = refused
            else:
                pending.append(index)
        for attempt in range(ATTEMPTS):
            if not pending or cancel.wait(self.settle):
                break
            final = attempt == ATTEMPTS - 1
            again = []
            for index in pending:
                outcome = self._verify(placements[index], final)
                if outcome is None:
                    again.append(index)
                    refused = self._post(placements[index], monitors)  # re-measured margins, settled DPI
                    if refused is not None:
                        results[index] = refused
                        again.pop()
                else:
                    results[index] = outcome
            pending = again
        for index in pending:  # cancelled while verifying
            window = placements[index].window
            results[index] = Result(window.key, window.display_title, Status.FAILED, "Cancelled.")
        return [results[i] for i in range(len(placements))]

    # -- show state and z-order --------------------------------------------------------------------------------
    def _show(self, window: TrackedWindow, command: int) -> Result:
        refused = self.check(window)
        if refused is not None:
            return refused
        if not self.backend.show_async(window.hwnd, command):
            return self._refused(window, w.last_error())
        return Result(window.key, window.display_title, Status.PLACED)

    def minimize(self, window: TrackedWindow, quiet: bool = False) -> Result:
        """``quiet``: without activating another window (a stack's hidden window)."""
        return self._show(window, w.SW_SHOWMINNOACTIVE if quiet else w.SW_MINIMIZE)

    def show(self, window: TrackedWindow) -> Result:
        """Show a minimized window again where it was, without taking the focus."""
        refused = self.check(window)
        if refused is not None:
            return refused
        if self.backend.is_minimized(window.hwnd):
            self.backend.show_async(window.hwnd, w.SW_SHOWNOACTIVATE)
        return Result(window.key, window.display_title, Status.PLACED)

    def maximize(self, window: TrackedWindow) -> Result:
        return self._show(window, w.SW_SHOWMAXIMIZED)

    def restore(self, window: TrackedWindow) -> Result:
        return self._show(window, w.SW_RESTORE)

    def bring_to_front(self, windows: list[TrackedWindow]) -> list[Result]:
        """Show minimized windows again and raise the group, ``windows[0]`` on top with the keyboard focus."""
        results: list[Result] = []
        usable: list[TrackedWindow] = []
        for window in windows:
            refused = self.check(window)
            if refused is not None:
                results.append(refused)
                continue
            if self.backend.is_minimized(window.hwnd):
                self.backend.show_async(window.hwnd, w.SW_SHOWNOACTIVATE)
            usable.append(window)
        for window in reversed(usable):
            self.backend.raise_window(window.hwnd)
            results.append(Result(window.key, window.display_title, Status.PLACED))
        if usable and not self.backend.set_foreground(usable[0].hwnd):
            log.info("Windows did not let CIRANA hand the foreground to %s", usable[0].display_title)
        return results

    def focus(self, window: TrackedWindow) -> Result:
        """Raise the window and give it the keyboard focus, once. Windows only lets the process that received the
        last input (a CIRANA hotkey or click) hand the foreground on; when it refuses, that is reported and nothing
        is retried or forced."""
        refused = self.check(window)
        if refused is not None:
            return refused
        name = window.display_title
        if self.backend.is_minimized(window.hwnd):
            self.backend.show_async(window.hwnd, w.SW_RESTORE)
        self.backend.raise_window(window.hwnd)
        if not self.backend.set_foreground(window.hwnd):
            return Result(window.key, name, Status.FAILED, f"Windows did not let CIRANA give {name} the focus (it "
                                                           "only allows that right after a CIRANA shortcut or click).")
        return Result(window.key, name, Status.PLACED)
