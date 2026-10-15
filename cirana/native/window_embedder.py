"""Experimental hosting of foreign top-level windows inside a CIRANA native container.

Two mechanisms, chosen per window by :func:`hosting_mode`:

* **child** (classic Win32 apps): the foreign window is converted to a WS_CHILD and re-parented with ``SetParent``
  onto the HWND of a native Qt widget. Qt's ``createWindowContainer`` is *not* mixed in.
* **docked** (packaged / Store / WinUI / UWP apps, consoles, Explorer, Chromium browsers): re-parenting crashes or
  breaks these, so the window stays an ordinary top-level window and CIRANA only keeps it positioned exactly over the
  tab. Nothing about the window is changed except its position, size and show state, always through posted / async
  calls; its owner, parent and styles are never touched. It keeps its own title bar, taskbar button and Alt+Tab entry.

Nothing here kills a process. Failure paths restore whatever was changed.
"""
from __future__ import annotations

import ctypes
import logging
from dataclasses import dataclass, field
from enum import Enum

from cirana.native import taskbar
from cirana.native import win32_api as w
from cirana.native.window_discovery import WindowInfo, is_main_window_candidate, own_pid
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
# Chromium browsers only let their address bar take the keyboard while ::GetActiveWindow() is their own window. A
# re-parented (WS_CHILD) window is the active window only as a leftover of the moment it was embedded: the first click
# on CIRANA or on the page makes CIRANA's window active, and Windows never makes a child window active again
# (SetActiveWindow / SetForegroundWindow / SetFocus / WM_ACTIVATE all measured without effect; splitting the shared
# input queue with AttachThreadInput left no window with the keyboard). Typing in the address bar then went to the
# web page (measured with Chrome and Brave). Docked, they stay real top-level windows and get real activation.
# Electron apps share the window class but host only web content, which works as a child, so they are not listed.
DOCKED_EXECUTABLES = {"chrome.exe", "msedge.exe", "brave.exe"}
NEVER_HOSTED_CLASSES = {"Progman", "Shell_TrayWnd", "WorkerW"}
DOCKED_NOTE = ("Docked: this application cannot live inside CIRANA safely, so it stays its own window and is kept "
               "exactly over this tab. It keeps its own title bar and taskbar button.")

CHILD = "child"
DOCKED = "docked"


def _packaged_image(exe: str) -> bool:
    return any(part in exe for part in ("\\windowsapps\\", "\\systemapps\\", "\\immersivecontrolpanel\\"))


def hosting_mode(info: WindowInfo) -> str:
    """How a window would be hosted: re-parented (child) or docked over the tab. Inspects, never changes."""
    # An owned window is docked too: re-parenting would drop its owner, and the restore could not give it back.
    docked = (info.class_name in DOCKED_CLASSES or _packaged_image(info.exe) or info.exe_name in DOCKED_EXECUTABLES
              or bool(info.owner))
    return DOCKED if docked else CHILD


class EmbedStatus(str, Enum):
    OK = "ok"
    BLOCKED = "blocked"  # known-incompatible, nothing was touched
    FAILED = "failed"  # an attempt was made and rolled back


@dataclass
class EmbedResult:
    status: EmbedStatus
    reason: str = ""
    warnings: list[str] = field(default_factory=list)
    # FAILED only: False when the window could not verifiably be put back as it was (e.g. a hung application whose
    # restore is still pending). Nothing else may then be tried on it.
    rolled_back: bool = True
    # FAILED, not rolled back: the window, which may still be parented to the host (see NativeHost.restore_pending).
    stranded: EmbeddedWindow | None = None

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
    taskbar_hidden: bool = False  # docked: its taskbar button was removed (ITaskbarList) and must be given back

    @property
    def docked(self) -> bool:
        return self.mode == DOCKED


def higher_rights(pid: int) -> bool | None:
    """True when the process's integrity level is above CIRANA's: Windows (UIPI) then refuses every change to its
    windows (measured: styles, parent, position and show state all fail with Win32 error 5). None: unknown."""
    theirs, ours = w.process_integrity(pid), w.process_integrity(own_pid())
    if theirs is None or ours is None:
        return None
    return theirs > ours


def _rights_problem(pid: int) -> tuple[str, bool]:
    """(reason the window cannot be changed, or ''; whether its rights could not be inspected at all)."""
    higher = higher_rights(pid)
    elevated = w.process_is_elevated(pid)
    if higher is None and elevated:  # the integrity level is unreadable, but the token says administrator
        higher = not w.process_is_elevated(own_pid())
    if higher:
        if elevated:
            return ("The application runs with administrator rights; Windows (UIPI) does not let a normal-rights "
                    "CIRANA change or move its window."), False
        return ("The application runs with higher rights than CIRANA (a higher integrity level, e.g. an "
                "accessibility or system tool); Windows (UIPI) blocks CIRANA from changing its window."), False
    return "", higher is None and elevated is None


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
    refused, unknown = _rights_problem(info.pid)
    if refused:
        return EmbedResult(EmbedStatus.BLOCKED, refused)
    if unknown:
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
        # Every step is checked before the next one runs: a refused style change must not be followed by SetParent.
        # SetParent remarks: when making a top-level window a child, set WS_CHILD / clear WS_POPUP *before* SetParent.
        style = int(w.GetWindowLongPtrW(hwnd, w.GWL_STYLE)) & 0xFFFFFFFF
        style = (style & ~_FRAME_STYLES) | w.WS_CHILD | w.WS_VISIBLE | w.WS_CLIPSIBLINGS | w.WS_CLIPCHILDREN
        _set_long(hwnd, w.GWL_STYLE, style, "Changing the window style")
        # A window that asks for its own taskbar button keeps one in some shells even as a child: drop the request
        # while it is hosted. The captured ex-style (with WS_EX_APPWINDOW) is put back by restore_state.
        if state.ex_style & w.WS_EX_APPWINDOW:
            _set_long(hwnd, w.GWL_EXSTYLE, state.ex_style & ~w.WS_EX_APPWINDOW, "Changing the extended window style")
        ctypes.set_last_error(0)
        w.SetParent(hwnd, host_hwnd)
        error = ctypes.get_last_error()
        if int(w.GetParent(hwnd) or 0) != host_hwnd:
            if not w.IsWindow(hwnd):
                error = w.ERROR_INVALID_WINDOW_HANDLE
            raise OSError(error, f"SetParent was refused. {w.explain_error(error, higher_rights(info.pid))}")
        w.SetWindowPos(hwnd, None, 0, 0, width, height,
                       w.SWP_NOZORDER | w.SWP_NOACTIVATE | w.SWP_FRAMECHANGED | w.SWP_SHOWWINDOW | w.SWP_ASYNCWINDOWPOS)
    except OSError as exc:
        log.warning("Embedding %s (%s) failed: %s", info.exe_name, info.class_name, exc)
        restore_state(state)
        reason = exc.strerror or str(exc)
        if not w.IsWindow(hwnd) or is_restored(state):  # (a closed window has nothing left to restore)
            return EmbedResult(EmbedStatus.FAILED, reason, compat.warnings), None
        log.warning("%s (%#x) could not be fully restored after the failed embedding", info.exe_name, hwnd)
        stranded = EmbeddedWindow(hwnd, host_hwnd, state, info.pid, launched_by_cirana)
        return EmbedResult(EmbedStatus.FAILED, reason + " CIRANA could not verify that the window is back as it was; "
                           "it was left alone.", compat.warnings, rolled_back=False, stranded=stranded), None

    embedded = EmbeddedWindow(hwnd, host_hwnd, state, info.pid, launched_by_cirana)
    return EmbedResult(EmbedStatus.OK, warnings=compat.warnings), embedded


def _set_long(hwnd: int, index: int, value: int, what: str) -> None:
    """SetWindowLongPtrW that raises when Windows refuses. It returns the previous value, so 0 alone is no error;
    0 together with a last error is (e.g. 5 under UIPI)."""
    ctypes.set_last_error(0)
    previous = w.SetWindowLongPtrW(hwnd, index, value)
    error = ctypes.get_last_error()
    if not previous and error:
        pid = w.window_pid(hwnd)[0]
        raise OSError(error, f"{what} was refused. {w.explain_error(error, higher_rights(pid) if pid else None)}")


# What embed() changes on the window (the show state aside): its parent, these style bits and WS_EX_APPWINDOW.
_CHANGED_STYLES = (_FRAME_STYLES | w.WS_CHILD) & ~(w.WS_MINIMIZE | w.WS_MAXIMIZE)


def is_restored(state: WindowState) -> bool:
    """True when the window is verifiably back as captured in everything CIRANA changes: its parent, its frame /
    child styles and WS_EX_APPWINDOW. GetParent / GetWindowLongPtr send nothing, so this is safe on a hung app."""
    hwnd = state.hwnd
    if not w.IsWindow(hwnd) or int(w.GetParent(hwnd) or 0) != state.parent:
        return False
    style = int(w.GetWindowLongPtrW(hwnd, w.GWL_STYLE)) & 0xFFFFFFFF
    ex_style = int(w.GetWindowLongPtrW(hwnd, w.GWL_EXSTYLE)) & 0xFFFFFFFF
    return not (style ^ state.style) & _CHANGED_STYLES and not (ex_style ^ state.ex_style) & w.WS_EX_APPWINDOW


# -- docked mode ------------------------------------------------------------------------------------------
def dock_compatibility(info: WindowInfo) -> EmbedResult:
    """Checks before docking. Docking only moves the window, so far less is refused than for re-parenting."""
    if not w.IsWindow(info.hwnd):
        return EmbedResult(EmbedStatus.BLOCKED, "The window no longer exists.")
    if info.pid == own_pid():
        return EmbedResult(EmbedStatus.BLOCKED, "That window belongs to CIRANA itself.")
    if info.class_name in NEVER_HOSTED_CLASSES:
        return EmbedResult(EmbedStatus.BLOCKED, "The desktop and the taskbar cannot be hosted.")
    refused, _unknown = _rights_problem(info.pid)
    if refused:
        return EmbedResult(EmbedStatus.BLOCKED, refused)
    if w.IsHungAppWindow(info.hwnd):
        return EmbedResult(EmbedStatus.BLOCKED, "The application is not responding.")
    # Docking only ever moves the window, so ask Windows whether that is allowed, with a move that changes nothing
    # (posted: never waits for the application). Under UIPI it fails at once with error 5.
    ctypes.set_last_error(0)
    if not w.SetWindowPos(info.hwnd, None, 0, 0, 0, 0, w.SWP_NOMOVE | w.SWP_NOSIZE | w.SWP_NOZORDER | w.SWP_NOACTIVATE
                          | w.SWP_NOOWNERZORDER | w.SWP_ASYNCWINDOWPOS):
        error = ctypes.get_last_error()
        return EmbedResult(EmbedStatus.BLOCKED, "CIRANA is not allowed to move this window. "
                           + w.explain_error(error, higher_rights(info.pid)))
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
    # While it lives over a tab, CIRANA's own button stands for it. Only Explorer's button list changes (the window,
    # its styles and its Alt+Tab entry are untouched); if the shell refuses, the button simply stays.
    # Only a window that has a button of its own (an owned palette has none: undocking must not invent one).
    has_button = is_main_window_candidate(info.hwnd) \
        or bool(int(w.GetWindowLongPtrW(info.hwnd, w.GWL_EXSTYLE)) & w.WS_EX_APPWINDOW)
    embedded.taskbar_hidden = has_button and taskbar.hide_button(info.hwnd)
    return EmbedResult(EmbedStatus.OK, warnings=compat.warnings), embedded


def embed_or_dock(info: WindowInfo, host_hwnd: int, width: int, height: int,
                  launched_by_cirana: bool) -> tuple[EmbedResult, EmbeddedWindow | None]:
    """Child mode, with docking as the fallback when Windows refuses it.

    The fallback runs only after the failed attempt was verifiably undone (``rolled_back``): a window left half
    changed is never touched again. Docking re-checks on its own whether CIRANA may move the window at all; when it
    may not either (UIPI), the window stays a normal window and the result says why both failed."""
    result, embedded = embed(info, host_hwnd, width, height, launched_by_cirana)
    if embedded is not None or result.status is not EmbedStatus.FAILED or not result.rolled_back:
        return result, embedded
    docked, embedded = dock(info, host_hwnd, launched_by_cirana)
    if embedded is None:
        return EmbedResult(EmbedStatus.FAILED, f"{result.reason} Docking it over the tab was not possible either: "
                           f"{docked.reason}", result.warnings), None
    note = f"It could not be placed inside the tab, so it was docked over the tab instead. {result.reason}"
    log.info("%s (%s): child mode failed, docked instead: %s", info.exe_name, info.class_name, result.reason)
    return EmbedResult(EmbedStatus.OK, warnings=[note, *docked.warnings]), embedded


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
        if embedded.taskbar_hidden:  # closed, or the handle now names another window: nothing to give back
            taskbar.forget(embedded.hwnd)
            embedded.taskbar_hidden = False
        return False
    state, hwnd = embedded.state, embedded.hwnd
    left, top, right, bottom = state.rect
    w.SetWindowPos(hwnd, None, left, top, right - left, bottom - top,
                   w.SWP_NOZORDER | w.SWP_NOACTIVATE | w.SWP_NOOWNERZORDER | w.SWP_ASYNCWINDOWPOS)
    w.ShowWindowAsync(hwnd, w.SW_SHOWMAXIMIZED if state.maximized else w.SW_SHOWNOACTIVATE)
    if embedded.taskbar_hidden:  # a normal window again: its own taskbar button comes back
        # On failure the taskbar module keeps the record: retried later and kept for crash recovery.
        embedded.taskbar_hidden = not taskbar.show_button(hwnd)
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


def misplaced(embedded: EmbeddedWindow, width: int, height: int) -> bool:
    """True if a child-mode window left its slot in the host (top-left corner, ``width`` x ``height``).

    Chromium draws its own title bar and answers WM_NCHITTEST with HTCAPTION there, so dragging it runs the system
    move loop on the (now child) window and moves it inside the host. Measured with Chrome on Windows 11: a drag
    moved it by the drag distance and nothing put it back until the next resize. GetWindowRect sends
    nothing, so this is safe to poll even for a hung application.
    """
    if embedded.docked or not w.IsWindow(embedded.hwnd):
        return False
    left, top, right, bottom = w.window_rect(embedded.hwnd)
    host_left, host_top, _, _ = w.window_rect(embedded.host)
    return (left, top, right - left, bottom - top) != (host_left, host_top, max(width, 1), max(height, 1))


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


def is_top_level(hwnd: int) -> bool:
    return int(w.GetAncestor(hwnd, w.GA_PARENT) or 0) == int(w.GetDesktopWindow() or 0)


def give_back_escaped(embedded: EmbeddedWindow) -> bool:
    """A child-mode window that left the host on its own still carries CIRANA's changes (WS_CHILD, no frame). Put
    them back when it is now a top-level window of the same process; a window the application moved into one of its
    own windows is left alone. True when it was given back."""
    if embedded.docked or not _docked_ours(embedded) or not is_top_level(embedded.hwnd):
        return False
    return restore_state(embedded.state)


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
