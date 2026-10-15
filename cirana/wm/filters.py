"""Which top-level windows are application windows a user would want to arrange.

Pure functions over :class:`WindowFacts`, so the rules are testable without a desktop. Roughly the rules Windows uses
for taskbar buttons and Alt+Tab, plus a list of shell surfaces (desktop, taskbar, Start, flyouts) that must never be
touched.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from cirana.native import win32_api as w
from cirana.wm.identity import Rect

# The desktop, the taskbars, Start / Search / notification flyouts, menus, tooltips and shell helper windows.
SYSTEM_CLASSES = frozenset({
    "Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "NotifyIconOverflowWindow",
    "TopLevelWindowForOverflowXamlIsland", "Windows.UI.Core.CoreWindow", "XamlExplorerHostIslandWindow",
    "MultitaskingViewFrame", "ForegroundStaging", "TaskListThumbnailWnd", "Shell_InputSwitchTopLevelWindow",
    "EdgeUiInputTopWndClass", "ApplicationManager_ImmersiveShellWindow", "ApplicationManager_DesktopShellWindow",
    "Internet Explorer_Hidden", "tooltips_class32", "#32768", "SysShadow", "DummyDWMListenerWindow",
    "Windows.Internal.Shell.TabProxyWindow", "PseudoConsoleWindow", "IME", "MSCTFIME UI",
})
SYSTEM_EXECUTABLES = frozenset({
    "shellexperiencehost.exe", "startmenuexperiencehost.exe", "searchhost.exe", "searchapp.exe", "textinputhost.exe",
    "lockapp.exe", "shellhost.exe", "widgets.exe", "dwm.exe", "csrss.exe", "winlogon.exe", "logonui.exe",
})
MIN_WIDTH, MIN_HEIGHT = 100, 60


@dataclass(frozen=True)
class WindowFacts:
    """Everything the filter looks at, read from the window without sending it a message."""

    hwnd: int
    pid: int
    title: str
    class_name: str
    exe: str
    style: int
    ex_style: int
    owner: int
    owner_visible: bool
    visible: bool
    cloaked: bool
    minimized: bool
    rect: Rect


def manageable(facts: WindowFacts, own_pid: int) -> tuple[bool, str]:
    """(is it an application window CIRANA may arrange, the reason when not)."""
    if facts.pid == own_pid:
        return False, "CIRANA's own window"
    if not facts.visible:
        return False, "hidden"
    if facts.cloaked:
        return False, "cloaked (another virtual desktop, or a suspended app)"
    if facts.style & w.WS_CHILD:
        return False, "child window"
    if facts.class_name in SYSTEM_CLASSES or os.path.basename(facts.exe) in SYSTEM_EXECUTABLES:
        return False, "part of the Windows shell"
    app_window = bool(facts.ex_style & w.WS_EX_APPWINDOW)
    if facts.ex_style & w.WS_EX_TOOLWINDOW and not app_window:
        return False, "tool window"
    if facts.ex_style & w.WS_EX_NOACTIVATE and not app_window:
        return False, "never activated (popup, notification)"
    caption = facts.style & w.WS_CAPTION == w.WS_CAPTION
    if facts.owner and not app_window and not (caption and not facts.owner_visible):
        return False, "owned window (dialog, palette or popup of another window)"
    if facts.style & w.WS_DISABLED:
        return False, "disabled"
    if not (facts.title.strip() or caption or app_window or facts.style & w.WS_THICKFRAME):
        return False, "untitled popup"
    if not facts.minimized:
        left, top, right, bottom = facts.rect
        if right - left < MIN_WIDTH or bottom - top < MIN_HEIGHT:
            return False, "too small"
    return True, ""
