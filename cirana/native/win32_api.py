"""Typed ctypes bindings for the Win32 calls used by window discovery and embedding.

Every function gets explicit ``argtypes``/``restype`` so that 64-bit handles and
LONG_PTR values are never truncated to 32-bit ints.
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)

LONG_PTR = ctypes.c_ssize_t  # pointer-sized signed integer on both 32 and 64-bit Python
HWND = wintypes.HWND

# GetWindowLong index values
GWL_STYLE = -16
GWL_EXSTYLE = -20
GWLP_HWNDPARENT = -8

# Window styles
WS_OVERLAPPED = 0x00000000
WS_POPUP = 0x80000000
WS_CHILD = 0x40000000
WS_MINIMIZE = 0x20000000
WS_DISABLED = 0x08000000
WS_VISIBLE = 0x10000000
WS_CLIPSIBLINGS = 0x04000000
WS_CLIPCHILDREN = 0x02000000
WS_MAXIMIZE = 0x01000000
WS_CAPTION = 0x00C00000
WS_BORDER = 0x00800000
WS_DLGFRAME = 0x00400000
WS_SYSMENU = 0x00080000
WS_THICKFRAME = 0x00040000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
WS_EX_DLGMODALFRAME = 0x00000001
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_WINDOWEDGE = 0x00000100
WS_EX_CLIENTEDGE = 0x00000200
WS_EX_APPWINDOW = 0x00040000
WS_EX_NOACTIVATE = 0x08000000
WS_EX_LAYERED = 0x00080000

# SetWindowPos flags
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
SWP_SHOWWINDOW = 0x0040
SWP_NOOWNERZORDER = 0x0200
SWP_ASYNCWINDOWPOS = 0x4000  # post the request to the owning thread instead of sending it (never blocks on a hung app)

SW_HIDE = 0
SW_SHOWNORMAL = 1
SW_SHOWMINIMIZED = 2
SW_SHOWMAXIMIZED = 3
SW_SHOWNOACTIVATE = 4
SW_SHOW = 5
SW_MINIMIZE = 6
SW_SHOWMINNOACTIVE = 7
SW_RESTORE = 9

MONITOR_DEFAULTTONULL = 0

GA_PARENT = 1
GA_ROOT = 2
GW_HWNDPREV = 3
GW_OWNER = 4

WM_CLOSE = 0x0010
WM_ACTIVATE = 0x0006
WM_NCACTIVATE = 0x0086
WA_INACTIVE = 0
WA_ACTIVE = 1

VK_LBUTTON = 0x01
VK_RBUTTON = 0x02
VK_MBUTTON = 0x04

DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TokenElevation = 20

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, HWND, wintypes.LPARAM)


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [
        ("length", wintypes.UINT), ("flags", wintypes.UINT), ("showCmd", wintypes.UINT),
        ("ptMinPosition", POINT), ("ptMaxPosition", POINT), ("rcNormalPosition", wintypes.RECT),
    ]


def _bind(lib, name: str, restype, *argtypes):
    func = getattr(lib, name)
    func.restype = restype
    func.argtypes = list(argtypes)
    return func


EnumWindows = _bind(user32, "EnumWindows", wintypes.BOOL, WNDENUMPROC, wintypes.LPARAM)
EnumChildWindows = _bind(user32, "EnumChildWindows", wintypes.BOOL, HWND, WNDENUMPROC, wintypes.LPARAM)
GetWindowThreadProcessId = _bind(user32, "GetWindowThreadProcessId", wintypes.DWORD, HWND, ctypes.POINTER(wintypes.DWORD))
IsWindow = _bind(user32, "IsWindow", wintypes.BOOL, HWND)
IsWindowVisible = _bind(user32, "IsWindowVisible", wintypes.BOOL, HWND)
IsIconic = _bind(user32, "IsIconic", wintypes.BOOL, HWND)
IsZoomed = _bind(user32, "IsZoomed", wintypes.BOOL, HWND)
GetWindowTextW = _bind(user32, "GetWindowTextW", ctypes.c_int, HWND, wintypes.LPWSTR, ctypes.c_int)
GetWindowTextLengthW = _bind(user32, "GetWindowTextLengthW", ctypes.c_int, HWND)
GetClassNameW = _bind(user32, "GetClassNameW", ctypes.c_int, HWND, wintypes.LPWSTR, ctypes.c_int)
GetParent = _bind(user32, "GetParent", HWND, HWND)
GetAncestor = _bind(user32, "GetAncestor", HWND, HWND, wintypes.UINT)
GetWindow = _bind(user32, "GetWindow", HWND, HWND, wintypes.UINT)
SetParent = _bind(user32, "SetParent", HWND, HWND, HWND)
GetWindowRect = _bind(user32, "GetWindowRect", wintypes.BOOL, HWND, ctypes.POINTER(wintypes.RECT))
GetClientRect = _bind(user32, "GetClientRect", wintypes.BOOL, HWND, ctypes.POINTER(wintypes.RECT))
SetWindowPos = _bind(user32, "SetWindowPos", wintypes.BOOL, HWND, HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                     ctypes.c_int, wintypes.UINT)
MoveWindow = _bind(user32, "MoveWindow", wintypes.BOOL, HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                   wintypes.BOOL)
ShowWindow = _bind(user32, "ShowWindow", wintypes.BOOL, HWND, ctypes.c_int)
ShowWindowAsync = _bind(user32, "ShowWindowAsync", wintypes.BOOL, HWND, ctypes.c_int)  # posted: never blocks
GetWindowPlacement = _bind(user32, "GetWindowPlacement", wintypes.BOOL, HWND, ctypes.POINTER(WINDOWPLACEMENT))
SetWindowPlacement = _bind(user32, "SetWindowPlacement", wintypes.BOOL, HWND, ctypes.POINTER(WINDOWPLACEMENT))
SetForegroundWindow = _bind(user32, "SetForegroundWindow", wintypes.BOOL, HWND)
SetFocus = _bind(user32, "SetFocus", HWND, HWND)
GetFocus = _bind(user32, "GetFocus", HWND)
IsChild = _bind(user32, "IsChild", wintypes.BOOL, HWND, HWND)
GetAsyncKeyState = _bind(user32, "GetAsyncKeyState", ctypes.c_short, ctypes.c_int)
GetCursorPos = _bind(user32, "GetCursorPos", wintypes.BOOL, ctypes.POINTER(POINT))
PostMessageW = _bind(user32, "PostMessageW", wintypes.BOOL, HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
IsHungAppWindow = _bind(user32, "IsHungAppWindow", wintypes.BOOL, HWND)
MonitorFromRect = _bind(user32, "MonitorFromRect", wintypes.HANDLE, ctypes.POINTER(wintypes.RECT), wintypes.DWORD)
GetForegroundWindow = _bind(user32, "GetForegroundWindow", HWND)
BringWindowToTop = _bind(user32, "BringWindowToTop", wintypes.BOOL, HWND)
RedrawWindow = _bind(user32, "RedrawWindow", wintypes.BOOL, HWND, ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT)

# GetWindowLongPtr / SetWindowLongPtr exist only on 64-bit; 32-bit exports the plain *Long names.
_is_64 = sys.maxsize > 2**32
GetWindowLongPtrW = _bind(user32, "GetWindowLongPtrW" if _is_64 else "GetWindowLongW", LONG_PTR, HWND, ctypes.c_int)
SetWindowLongPtrW = _bind(user32, "SetWindowLongPtrW" if _is_64 else "SetWindowLongW", LONG_PTR, HWND, ctypes.c_int, LONG_PTR)

OpenProcess = _bind(kernel32, "OpenProcess", wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
CloseHandle = _bind(kernel32, "CloseHandle", wintypes.BOOL, wintypes.HANDLE)
QueryFullProcessImageNameW = _bind(kernel32, "QueryFullProcessImageNameW", wintypes.BOOL, wintypes.HANDLE, wintypes.DWORD,
                                   wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
GetCurrentProcessId = _bind(kernel32, "GetCurrentProcessId", wintypes.DWORD)
OpenProcessToken = _bind(advapi32, "OpenProcessToken", wintypes.BOOL, wintypes.HANDLE, wintypes.DWORD,
                         ctypes.POINTER(wintypes.HANDLE))
GetTokenInformation = _bind(advapi32, "GetTokenInformation", wintypes.BOOL, wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
DwmGetWindowAttribute = _bind(dwmapi, "DwmGetWindowAttribute", ctypes.c_long, HWND, wintypes.DWORD, ctypes.c_void_p,
                              wintypes.DWORD)


def window_text(hwnd: int) -> str:
    length = GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def class_name(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    GetClassNameW(hwnd, buffer, 256)
    return buffer.value


def window_pid(hwnd: int) -> tuple[int, int]:
    """Return (pid, thread id) owning ``hwnd``; (0, 0) if the window is gone."""
    pid = wintypes.DWORD(0)
    tid = GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value), int(tid)


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    rect = wintypes.RECT()
    GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.left, rect.top, rect.right, rect.bottom


def cursor_pos() -> tuple[int, int]:
    point = POINT()
    GetCursorPos(ctypes.byref(point))
    return point.x, point.y


def mouse_button_down() -> bool:
    """Physical state of any mouse button right now (no message is sent to anyone)."""
    return any(GetAsyncKeyState(vk) & 0x8000 for vk in (VK_LBUTTON, VK_RBUTTON, VK_MBUTTON))


def process_image_path(pid: int) -> str:
    handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(1024)
        if QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value
        return ""
    finally:
        CloseHandle(handle)


def process_is_elevated(pid: int) -> bool | None:
    """True/False for elevation, None when the token cannot be inspected (often means higher integrity)."""
    handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    token = wintypes.HANDLE()
    try:
        if not OpenProcessToken(handle, TOKEN_QUERY, ctypes.byref(token)):
            return None
        try:
            elevation = wintypes.DWORD(0)
            returned = wintypes.DWORD(0)
            if GetTokenInformation(token, TokenElevation, ctypes.byref(elevation), ctypes.sizeof(elevation),
                                   ctypes.byref(returned)):
                return bool(elevation.value)
            return None
        finally:
            CloseHandle(token)
    finally:
        CloseHandle(handle)


def visible_frame_rect(hwnd: int) -> tuple[int, int, int, int]:
    """The window's visible bounds (without the invisible DWM resize borders); its window rect if DWM won't say."""
    rect = wintypes.RECT()
    if DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect), ctypes.sizeof(rect)) == 0:
        return rect.left, rect.top, rect.right, rect.bottom
    return window_rect(hwnd)


def is_cloaked(hwnd: int) -> bool:
    value = wintypes.DWORD(0)
    if DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(value), ctypes.sizeof(value)) == 0:
        return bool(value.value)
    return False


def dpi_awareness_mismatch(hwnd: int) -> str:
    """Describe a DPI-awareness difference between ``hwnd`` and this process ('' if equal or unknown)."""
    try:
        get_ctx = _bind(user32, "GetWindowDpiAwarenessContext", ctypes.c_void_p, HWND)
        get_self = _bind(user32, "GetThreadDpiAwarenessContext", ctypes.c_void_p)
        equal = _bind(user32, "AreDpiAwarenessContextsEqual", wintypes.BOOL, ctypes.c_void_p, ctypes.c_void_p)
    except AttributeError:  # Windows older than 10 1607
        return ""
    if equal(get_ctx(hwnd), get_self()):
        return ""
    return ("The application uses a different DPI-awareness mode than CIRANA; "
            "it may look blurry or mis-sized when hosted.")
