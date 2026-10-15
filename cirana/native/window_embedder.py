"""Experimental hosting of foreign top-level windows inside a CIRANA native container.

Two mechanisms, chosen per window by :func:`hosting_mode`:

* **child** (classic Win32 apps): the foreign window is converted to a WS_CHILD and re-parented with ``SetParent``
  onto the HWND of a native Qt widget. Qt's ``createWindowContainer`` is *not* mixed in.
* **docked** (packaged / Store / WinUI / UWP apps, consoles, Explorer): re-parenting crashes or breaks these, so the
  window stays an ordinary top-level window and CIRANA only keeps it positioned exactly over the tab. Nothing about
  the window is changed except its position, size and show state, always through posted / async calls; its owner,
  parent and styles are never touched. It keeps its own title bar, taskbar button and Alt+Tab entry.

Nothing here kills a process. Failure paths restore whatever was changed.
"""
from __future__ import annotations

import ctypes
import logging
from dataclasses import dataclass, field
from enum import Enum

from cirana.native import win32_api as w
from cirana.native.window_discovery import WindowInfo, own_pid
from cirana.native.window_restorer import WindowState, capture_state, restore_state

log = logging.getLogger(__name__)

# Frame styles that make no sense for a child window.
_FRAME_STYLES = (w.WS_CAPTION | w.WS_THICKFRAME | w.WS_SYSMENU | w.WS_MINIMIZEBOX | w.WS_MAXIMIZEBOX
                 | w.WS_POPUP | w.WS_MINIMIZE | w.WS_MAXIMIZE)

# Window classes known to break (or be meaningless) when re-parented.
BLOCKED_CLASSES: dict[str, str] = {
    "ApplicationFrameWindow": "Packaged (UWP) apps are hosted by the system frame process and cannot be re-parented.",
    "Windows.UI.Core.CoreWindow": "Packaged (UWP) apps cannot be re-parented.",
    "CabinetWClass": "File Explorer windows are shell-owned and unstable when re-parented.",
    "Progman": "The desktop window cannot be embedded.",
    "Shell_TrayWnd": "The taskbar cannot be embedded.",
    "ConsoleWindowClass": "Console windows are owned by conhost.exe and cannot be re-parented reliably.",
    "CASCADIA_HOSTING_WINDOW_CLASS": "Windows Terminal draws its own frame and does not survive re-parenting.",
}
# Classes that usually *work* but have caveats the user should know about.
WARN_CLASSES: dict[str, str] = {
    "Chrome_WidgetWin_1": "Chromium/Electron apps use GPU rendering and custom frames; rendering and input may glitch.",
    "SDL_app": "Games and SDL apps render with the GPU and often fight for the window.",
    "UnrealWindow": "Unreal Engine applications are not designed to be embedded.",
    "UnityWndClass": "Unity applications are not designed to be embedded.",
}


# Windows that must never be re-parented but can be docked (kept over the tab as a top-level window).
DOCKED_CLASSES = {"ApplicationFrameWindow", "Windows.UI.Core.CoreWindow", "CabinetWClass", "ConsoleWindowClass",
                  "CASCADIA_HOSTING_WINDOW_CLASS"}
NEVER_HOSTED_CLASSES = {"Progman", "Shell_TrayWnd", "WorkerW"}
DOCKED_NOTE = ("Docked: this application cannot live inside CIRANA safely, so it stays its own window and is kept "
               "exactly over this tab. It keeps its own title bar and taskbar button.")

CHILD = "child"
DOCKED = "docked"


def _packaged_image(exe: str) -> bool:
    return any(part in exe for part in ("\\windowsapps\\", "\\systemapps\\", "\\immersivecontrolpanel\\"))


def hosting_mode(info: WindowInfo) -> str:
    """How a window would be hosted: re-parented (child) or docked over the tab. Inspects, never changes."""
    return DOCKED if info.class_name in DOCKED_CLASSES or _packaged_image(info.exe) else CHILD


class EmbedStatus(str, Enum):
    OK = "ok"
    BLOCKED = "blocked"  # known-incompatible, nothing was touched
    FAILED = "failed"  # an attempt was made and rolled back


@dataclass
class EmbedResult:
    status: EmbedStatus
    reason: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status is EmbedStatus.OK


@dataclass
class EmbeddedWindow:
    hwnd: int
    host: int
    state: WindowState
    pid: int
    launched_by_cirana: bool
    mode: str = CHILD

    @property
    def docked(self) -> bool:
        return self.mode == DOCKED


def check_compatibility(info: WindowInfo) -> EmbedResult:
    """Cheap, side-effect-free checks run before re-parenting a foreign window (child mode)."""
    warnings: list[str] = []
    if not w.IsWindow(info.hwnd):
        return EmbedResult(EmbedStatus.BLOCKED, "The window no longer exists.")
    if info.pid == own_pid():
        return EmbedResult(EmbedStatus.BLOCKED, "That window belongs to CIRANA itself.")
    if info.class_name in BLOCKED_CLASSES:
        return EmbedResult(EmbedStatus.BLOCKED, BLOCKED_CLASSES[info.class_name])
    if "\\windowsapps\\" in info.exe:
        return EmbedResult(
            EmbedStatus.BLOCKED,
            "Packaged (Microsoft Store / WinUI) applications crash when their window is re-parented. "
            "Verified on this system: Windows 11 Notepad 11.2607 terminates about two seconds after SetParent.",
        )
    elevated = w.process_is_elevated(info.pid)
    if elevated:
        return EmbedResult(
            EmbedStatus.BLOCKED,
            "The application runs with administrator rights; Windows (UIPI) blocks a normal-rights CIRANA from hosting it.",
        )
    if elevated is None:
        warnings.append("Could not inspect the application's privilege level; embedding may be refused by Windows.")
    if w.IsHungAppWindow(info.hwnd):
        return EmbedResult(EmbedStatus.BLOCKED, "The application is not responding.")
    mismatch = w.dpi_awareness_mismatch(info.hwnd)
    if mismatch:
        warnings.append(mismatch)
    if info.class_name in WARN_CLASSES:
        warnings.append(WARN_CLASSES[info.class_name])
    return EmbedResult(EmbedStatus.OK, warnings=warnings)


def embed(info: WindowInfo, host_hwnd: int, width: int, height: int, launched_by_cirana: bool) -> tuple[EmbedResult, EmbeddedWindow | None]:
    """Re-parent ``info.hwnd`` into ``host_hwnd`` and size it to ``width`` x ``height`` device pixels."""
    compat = check_compatibility(info)
    if not compat.ok:
        return compat, None

    hwnd = info.hwnd
    state = capture_state(hwnd)
    if state.maximized or state.minimized:
        w.ShowWindow(hwnd, w.SW_RESTORE)  # a maximized/minimized window has no meaningful child geometry
    try:
        # SetParent remarks: when making a top-level window a child, set WS_CHILD / clear WS_POPUP *before* SetParent.
        style = int(w.GetWindowLongPtrW(hwnd, w.GWL_STYLE)) & 0xFFFFFFFF
        style = (style & ~_FRAME_STYLES) | w.WS_CHILD | w.WS_VISIBLE | w.WS_CLIPSIBLINGS | w.WS_CLIPCHILDREN
        ctypes.set_last_error(0)
        w.SetWindowLongPtrW(hwnd, w.GWL_STYLE, style)
        style_error = ctypes.get_last_error()
        ctypes.set_last_error(0)
        w.SetParent(hwnd, host_hwnd)
        error = ctypes.get_last_error()
        if int(w.GetParent(hwnd) or 0) != host_hwnd:
            raise OSError(
                error,
                f"SetParent was refused (Win32 error {error}; style error {style_error}; "
                f"app window valid={bool(w.IsWindow(hwnd))}, host valid={bool(w.IsWindow(host_hwnd))}).",
            )
        w.SetWindowPos(hwnd, None, 0, 0, width, height,
                       w.SWP_NOZORDER | w.SWP_NOACTIVATE | w.SWP_FRAMECHANGED | w.SWP_SHOWWINDOW | w.SWP_ASYNCWINDOWPOS)
    except OSError as exc:
        log.warning("Embedding %s (%s) failed: %s", info.exe_name, info.class_name, exc)
        restore_state(state)
        return EmbedResult(EmbedStatus.FAILED, str(exc), compat.warnings), None

    embedded = EmbeddedWindow(hwnd, host_hwnd, state, info.pid, launched_by_cirana)
    return EmbedResult(EmbedStatus.OK, warnings=compat.warnings), embedded


# -- docked mode ------------------------------------------------------------------------------------------
def dock_compatibility(info: WindowInfo) -> EmbedResult:
    """Checks before docking. Docking only moves the window, so far less is refused than for re-parenting."""
    if not w.IsWindow(info.hwnd):
        return EmbedResult(EmbedStatus.BLOCKED, "The window no longer exists.")
    if info.pid == own_pid():
        return EmbedResult(EmbedStatus.BLOCKED, "That window belongs to CIRANA itself.")
    if info.class_name in NEVER_HOSTED_CLASSES:
        return EmbedResult(EmbedStatus.BLOCKED, "The desktop and the taskbar cannot be hosted.")
    if w.process_is_elevated(info.pid):
        return EmbedResult(
            EmbedStatus.BLOCKED,
            "The application runs with administrator rights; Windows (UIPI) does not let a normal-rights CIRANA move it.",
        )
    if w.IsHungAppWindow(info.hwnd):
        return EmbedResult(EmbedStatus.BLOCKED, "The application is not responding.")
    return EmbedResult(EmbedStatus.OK, warnings=[DOCKED_NOTE])


def dock(info: WindowInfo, host_hwnd: int, launched_by_cirana: bool) -> tuple[EmbedResult, EmbeddedWindow | None]:
    """Start docking ``info.hwnd`` over a tab. Only reads its state here; the host positions it afterwards."""
    compat = dock_compatibility(info)
    if not compat.ok:
        return compat, None
    state = capture_state(info.hwnd)  # read-only calls
    if state.maximized or state.minimized:
        w.ShowWindowAsync(info.hwnd, w.SW_RESTORE)  # a maximized window cannot be sized to the tab
    embedded = EmbeddedWindow(info.hwnd, host_hwnd, state, info.pid, launched_by_cirana, mode=DOCKED)
    return EmbedResult(EmbedStatus.OK, warnings=compat.warnings), embedded


def _docked_ours(embedded: EmbeddedWindow) -> bool:
    """The handle still names the same application's window (handles are recycled after a window closes)."""
    return bool(w.IsWindow(embedded.hwnd)) and w.window_pid(embedded.hwnd)[0] == embedded.pid


def place_docked(embedded: EmbeddedWindow, rect: tuple[int, int, int, int]) -> bool:
    """Keep a docked window's visible frame exactly on ``rect`` (screen device pixels). Async; never activates it."""
    if not _docked_ours(embedded) or w.IsIconic(embedded.hwnd):
        return False
    hwnd = embedded.hwnd
    if w.IsZoomed(hwnd):  # the user maximized it with its own button: un-maximize; the next tick places it
        w.ShowWindowAsync(hwnd, w.SW_RESTORE)
        return False
    # Windows 10/11 windows carry invisible resize borders; size the *visible* frame to the tab.
    outer, visible = w.window_rect(hwnd), w.visible_frame_rect(hwnd)
    left = rect[0] - (visible[0] - outer[0])
    top = rect[1] - (visible[1] - outer[1])
    right = rect[2] + (outer[2] - visible[2])
    bottom = rect[3] + (outer[3] - visible[3])
    if (left, top, right, bottom) == outer:
        return True
    return bool(w.SetWindowPos(hwnd, None, left, top, max(right - left, 1), max(bottom - top, 1),
                               w.SWP_NOZORDER | w.SWP_NOACTIVATE | w.SWP_NOOWNERZORDER | w.SWP_ASYNCWINDOWPOS))


def keep_docked_above(embedded: EmbeddedWindow, root: int) -> None:
    """Make sure the docked window is above CIRANA's window by moving CIRANA (our own window), never the app."""
    if not _docked_ours(embedded):
        return
    hwnd, above = embedded.hwnd, int(w.GetWindow(root, w.GW_HWNDPREV) or 0)
    for _ in range(64):  # walk up the z-order from CIRANA: is the app somewhere above it?
        if not above or above == hwnd:
            break
        above = int(w.GetWindow(above, w.GW_HWNDPREV) or 0)
    if above != hwnd:
        w.SetWindowPos(root, hwnd, 0, 0, 0, 0, w.SWP_NOMOVE | w.SWP_NOSIZE | w.SWP_NOACTIVATE)


def send_docked_behind(embedded: EmbeddedWindow, root: int) -> None:
    """Hide a docked window behind CIRANA (tab switch, other page). No minimize animation, no suspension."""
    if _docked_ours(embedded):
        w.SetWindowPos(embedded.hwnd, root, 0, 0, 0, 0,
                       w.SWP_NOMOVE | w.SWP_NOSIZE | w.SWP_NOACTIVATE | w.SWP_NOOWNERZORDER | w.SWP_ASYNCWINDOWPOS)


def show_docked(embedded: EmbeddedWindow, minimized: bool) -> None:
    """Minimize a docked window with CIRANA, and bring it back without activating it."""
    if _docked_ours(embedded):
        w.ShowWindowAsync(embedded.hwnd, w.SW_SHOWMINNOACTIVE if minimized else w.SW_SHOWNOACTIVATE)


def _undock(embedded: EmbeddedWindow, activate: bool) -> bool:
    if not _docked_ours(embedded):
        return False
    state, hwnd = embedded.state, embedded.hwnd
    left, top, right, bottom = state.rect
    w.SetWindowPos(hwnd, None, left, top, right - left, bottom - top,
                   w.SWP_NOZORDER | w.SWP_NOACTIVATE | w.SWP_NOOWNERZORDER | w.SWP_ASYNCWINDOWPOS)
    w.ShowWindowAsync(hwnd, w.SW_SHOWMAXIMIZED if state.maximized else w.SW_SHOWNOACTIVATE)
    if activate and not w.IsHungAppWindow(hwnd):
        w.SetForegroundWindow(hwnd)
    return True


# -- both modes ---------------------------------------------------------------------------------------------
def resize(embedded: EmbeddedWindow, width: int, height: int) -> bool:
    """Size a child-mode window. Posted asynchronously so a busy or hung application can never block the UI thread."""
    if embedded.docked or not w.IsWindow(embedded.hwnd):
        return False
    return bool(w.SetWindowPos(embedded.hwnd, None, 0, 0, max(width, 1), max(height, 1),
                               w.SWP_NOZORDER | w.SWP_NOACTIVATE | w.SWP_ASYNCWINDOWPOS))


def is_alive(embedded: EmbeddedWindow) -> bool:
    return bool(w.IsWindow(embedded.hwnd))


def is_hung(embedded: EmbeddedWindow) -> bool:
    """IsHungAppWindow only inspects the target thread's message-queue timestamps; it sends nothing."""
    return is_alive(embedded) and bool(w.IsHungAppWindow(embedded.hwnd))


def is_still_embedded(embedded: EmbeddedWindow) -> bool:
    """False if the application re-parented itself away (some apps do this on startup), or if a docked window's
    handle now belongs to someone else."""
    if embedded.docked:
        return _docked_ours(embedded)
    return is_alive(embedded) and int(w.GetParent(embedded.hwnd) or 0) == embedded.host


def focus(embedded: EmbeddedWindow) -> None:
    """Give keyboard focus to the hosted window (skipped for a hung app: SetFocus would wait for it).

    Keyboard focus alone is not enough for browsers: Chromium and Firefox only route typing to a page while they
    believe their window is *active*, and a window that became a child never receives WM_ACTIVATE again. So the
    activation is replayed first (posted, never sent). Focus already inside the app (e.g. on one of its own child
    windows) is left where it is. A docked window is a normal top-level window: it is simply brought to the front.
    """
    if not is_alive(embedded) or w.IsHungAppWindow(embedded.hwnd):
        return
    if embedded.docked:
        if _docked_ours(embedded):
            if w.IsIconic(embedded.hwnd):  # minimized with CIRANA, or by the user: its tab must not stay empty
                w.ShowWindowAsync(embedded.hwnd, w.SW_SHOWNOACTIVATE)
            w.SetForegroundWindow(embedded.hwnd)
        return
    set_active(embedded, True)
    if not has_focus(embedded):
        w.SetFocus(embedded.hwnd)


def set_active(embedded: EmbeddedWindow, active: bool) -> None:
    """Tell a child-mode application its (former top-level) window was (de)activated. Posted: never blocks.
    Docked windows are real top-level windows and get real activation from Windows, so they are left alone."""
    if not embedded.docked and is_still_embedded(embedded):  # only ever a window that is really ours to host
        w.PostMessageW(embedded.hwnd, w.WM_NCACTIVATE, int(active), 0)
        w.PostMessageW(embedded.hwnd, w.WM_ACTIVATE, w.WA_ACTIVE if active else w.WA_INACTIVE, 0)


def has_focus(embedded: EmbeddedWindow) -> bool:
    """True if keyboard focus is anywhere in the hosted application: its window, a child of it, or one of its own
    popups (address-bar suggestions, menus). GetFocus / IsChild / GetWindowThreadProcessId send nothing."""
    focused = int(w.GetFocus() or 0)
    return bool(focused) and (focused == embedded.hwnd or bool(w.IsChild(embedded.hwnd, focused))
                              or owns_window(embedded, focused))


def owns_window(embedded: EmbeddedWindow, hwnd: int) -> bool:
    """True if ``hwnd`` belongs to the hosted application's process (its menus, popups and dialogs)."""
    return bool(hwnd) and w.window_pid(hwnd)[0] == embedded.pid


def contains_point(embedded: EmbeddedWindow, x: int, y: int) -> bool:
    left, top, right, bottom = w.window_rect(embedded.hwnd)
    return left <= x < right and top <= y < bottom


def detach(embedded: EmbeddedWindow, activate: bool = False) -> bool:
    """Give the window back to the desktop with its original style and position. Never closes the app."""
    if embedded.docked:
        return _undock(embedded, activate)
    return restore_state(embedded.state, activate=activate)


def request_close(embedded: EmbeddedWindow) -> bool:
    """Politely ask the application to close (WM_CLOSE). Only allowed for apps CIRANA launched itself."""
    if not embedded.launched_by_cirana:
        raise PermissionError("CIRANA only closes applications it launched itself.")
    detach(embedded)
    return bool(w.PostMessageW(embedded.hwnd, w.WM_CLOSE, 0, 0))


def bring_to_front(hwnd: int) -> bool:
    """Restore (if minimized) and raise a normal top-level window. Used for 'already running' apps."""
    if not w.IsWindow(hwnd):
        return False
    if w.IsIconic(hwnd):
        w.ShowWindow(hwnd, w.SW_RESTORE)
    return bool(w.SetForegroundWindow(hwnd))
