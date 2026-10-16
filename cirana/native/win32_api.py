"""Typed ctypes bindings for the Win32 calls the window manager uses.

Every function gets explicit ``argtypes``/``restype`` so that 64-bit handles and LONG_PTR values are never truncated
to 32-bit ints. Deliberately absent: anything that re-parents a window or changes its styles (SetParent,
SetWindowLongPtr), attaches input queues or injects input. tests/test_forbidden_apis.py keeps it that way.
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
SWP_NOOWNERZORDER = 0x0200
SWP_ASYNCWINDOWPOS = 0x4000  # post the request to the owning thread instead of sending it (never blocks on a hung app)

SW_SHOWNORMAL = 1
SW_SHOWMAXIMIZED = 3
SW_SHOWNOACTIVATE = 4
SW_MINIMIZE = 6
SW_SHOWMINNOACTIVE = 7
SW_RESTORE = 9

GA_PARENT = 1
GW_HWNDPREV = 3  # the window just above in z-order
GW_OWNER = 4

WM_CLOSE = 0x0010

DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, HWND, wintypes.LPARAM)


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


WPF_RESTORETOMAXIMIZED = 0x0002


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
GetAncestor = _bind(user32, "GetAncestor", HWND, HWND, wintypes.UINT)
GetWindow = _bind(user32, "GetWindow", HWND, HWND, wintypes.UINT)
GetDesktopWindow = _bind(user32, "GetDesktopWindow", HWND)
GetWindowRect = _bind(user32, "GetWindowRect", wintypes.BOOL, HWND, ctypes.POINTER(wintypes.RECT))
SetWindowPos = _bind(user32, "SetWindowPos", wintypes.BOOL, HWND, HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                     ctypes.c_int, wintypes.UINT)
ShowWindowAsync = _bind(user32, "ShowWindowAsync", wintypes.BOOL, HWND, ctypes.c_int)  # posted: never blocks
GetWindowPlacement = _bind(user32, "GetWindowPlacement", wintypes.BOOL, HWND, ctypes.POINTER(WINDOWPLACEMENT))
SetForegroundWindow = _bind(user32, "SetForegroundWindow", wintypes.BOOL, HWND)
GetForegroundWindow = _bind(user32, "GetForegroundWindow", HWND)
PostMessageW = _bind(user32, "PostMessageW", wintypes.BOOL, HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
IsHungAppWindow = _bind(user32, "IsHungAppWindow", wintypes.BOOL, HWND)

# GetWindowLongPtr exists only on 64-bit; 32-bit exports the plain GetWindowLongW. (Read-only: styles are never set.)
_is_64 = sys.maxsize > 2**32
GetWindowLongPtrW = _bind(user32, "GetWindowLongPtrW" if _is_64 else "GetWindowLongW", LONG_PTR, HWND, ctypes.c_int)

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


TokenIntegrityLevel = 25
GetSidSubAuthorityCount = _bind(advapi32, "GetSidSubAuthorityCount", ctypes.POINTER(ctypes.c_ubyte), ctypes.c_void_p)
GetSidSubAuthority = _bind(advapi32, "GetSidSubAuthority", ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
                           wintypes.DWORD)


def process_integrity(pid: int) -> int | None:
    """The process's mandatory integrity level (0x2000 medium, 0x3000 high, 0x4000 system; uiAccess adds 0x10), or
    None when its token cannot be inspected. User Interface Privilege Isolation (UIPI) refuses every window change
    (styles, parent, position, show state) from a process with a *lower* level: measured as Win32 error 5."""
    handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    token = wintypes.HANDLE()
    try:
        if not OpenProcessToken(handle, TOKEN_QUERY, ctypes.byref(token)):
            return None
        try:
            buffer = ctypes.create_string_buffer(64)  # TOKEN_MANDATORY_LABEL: a SID pointer followed by the SID
            returned = wintypes.DWORD(0)
            if not GetTokenInformation(token, TokenIntegrityLevel, buffer, len(buffer), ctypes.byref(returned)):
                return None
            sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
            count = GetSidSubAuthorityCount(sid)[0]
            return int(GetSidSubAuthority(sid, count - 1)[0]) if count else None
        finally:
            CloseHandle(token)
    finally:
        CloseHandle(handle)


ERROR_ACCESS_DENIED = 5
ERROR_INVALID_WINDOW_HANDLE = 1400


def explain_error(code: int, higher_rights: bool | None = None) -> str:
    """A readable cause for the Win32 errors moving a window runs into. ``higher_rights``: whether the application's
    integrity level is above CIRANA's (None: its token could not be inspected)."""
    if code == ERROR_ACCESS_DENIED:
        if higher_rights:
            return ("Windows denied access (Win32 error 5): the application runs with higher rights than CIRANA (for "
                    "example as administrator), and Windows (UIPI) does not let a lower-rights program change its "
                    "window.")
        if higher_rights is None:
            return ("Windows denied access (Win32 error 5). CIRANA cannot inspect the application's rights: it most "
                    "likely runs with higher rights or under another user account, and Windows (UIPI) does not let "
                    "CIRANA change its window.")
        return ("Windows denied access (Win32 error 5) although the application runs with the same rights as CIRANA; "
                "its window refuses to be changed by other programs.")
    if code == ERROR_INVALID_WINDOW_HANDLE:
        return "The window was closed while CIRANA was arranging it (Win32 error 1400)."
    if code:
        return f"Windows refused the change ({ctypes.FormatError(code).strip().rstrip('.')}; Win32 error {code})."
    return "Windows did not apply the change and reported no error code."


IO_REPARSE_TAG_APPEXECLINK = 0x8000001B
_FSCTL_GET_REPARSE_POINT = 0x000900A8
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_OPEN_EXISTING = 3
_INVALID_HANDLE = wintypes.HANDLE(-1).value
CreateFileW = _bind(kernel32, "CreateFileW", wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                    ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
DeviceIoControl = _bind(kernel32, "DeviceIoControl", wintypes.BOOL, wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p,
                        wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p)


def parse_app_alias(raw: bytes) -> list[str]:
    """The strings of an app execution alias reparse buffer: [package family, AppUserModelID, target exe, ...].
    Anything else (another reparse tag, a truncated buffer) gives []."""
    if len(raw) < 12 or int.from_bytes(raw[:4], "little") != IO_REPARSE_TAG_APPEXECLINK:
        return []
    try:
        return raw[12:].decode("utf-16-le").split("\0")[:3]
    except UnicodeDecodeError:
        return []


def read_app_alias(path: str) -> list[str]:
    """Read an app execution alias (e.g. ``%LOCALAPPDATA%/Microsoft/WindowsApps/wt.exe``): the file is a reparse point
    that names the packaged app it starts. Opening it with FILE_FLAG_OPEN_REPARSE_POINT reads the link itself."""
    handle = CreateFileW(path, 0, 7, None, _OPEN_EXISTING, _FILE_FLAG_OPEN_REPARSE_POINT | _FILE_FLAG_BACKUP_SEMANTICS,
                         None)
    if not handle or handle == _INVALID_HANDLE:
        return []
    try:
        buffer = ctypes.create_string_buffer(16384)
        size = wintypes.DWORD(0)
        if not DeviceIoControl(handle, _FSCTL_GET_REPARSE_POINT, None, 0, buffer, len(buffer), ctypes.byref(size), None):
            return []
        return parse_app_alias(buffer.raw[:size.value])
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


# -- window events, monitors, process identity (window manager) -----------------------------------------------------
# Out-of-context WinEvent hooks are delivered to the installing thread's message loop: nothing is injected into other
# processes. Every call below only reads, or (SetWindowPos / ShowWindowAsync) asks the owning thread to move a window.
WINEVENTPROC = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD, HWND, wintypes.LONG, wintypes.LONG,
                                  wintypes.DWORD, wintypes.DWORD)
WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002
EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_SYSTEM_MOVESIZESTART = 0x000A  # the user starts dragging / resizing a window (modal move loop)
EVENT_SYSTEM_MOVESIZEEND = 0x000B
EVENT_SYSTEM_MINIMIZESTART = 0x0016
EVENT_SYSTEM_MINIMIZEEND = 0x0017
EVENT_OBJECT_CREATE = 0x8000
EVENT_OBJECT_DESTROY = 0x8001
EVENT_OBJECT_SHOW = 0x8002
EVENT_OBJECT_HIDE = 0x8003
EVENT_OBJECT_LOCATIONCHANGE = 0x800B
EVENT_OBJECT_NAMECHANGE = 0x800C
EVENT_OBJECT_CLOAKED = 0x8017
EVENT_OBJECT_UNCLOAKED = 0x8018
OBJID_WINDOW = 0
CHILDID_SELF = 0
WM_QUIT = 0x0012
HWND_TOP = 0
HWND_TOPMOST = -1
MONITORINFOF_PRIMARY = 0x1
MDT_EFFECTIVE_DPI = 0

SetWinEventHook = _bind(user32, "SetWinEventHook", wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE,
                        WINEVENTPROC, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD)
UnhookWinEvent = _bind(user32, "UnhookWinEvent", wintypes.BOOL, wintypes.HANDLE)
GetMessageW = _bind(user32, "GetMessageW", wintypes.BOOL, ctypes.POINTER(wintypes.MSG), HWND, wintypes.UINT,
                    wintypes.UINT)
TranslateMessage = _bind(user32, "TranslateMessage", wintypes.BOOL, ctypes.POINTER(wintypes.MSG))
DispatchMessageW = _bind(user32, "DispatchMessageW", LONG_PTR, ctypes.POINTER(wintypes.MSG))
PostThreadMessageW = _bind(user32, "PostThreadMessageW", wintypes.BOOL, wintypes.DWORD, wintypes.UINT, wintypes.WPARAM,
                           wintypes.LPARAM)
GetCurrentThreadId = _bind(kernel32, "GetCurrentThreadId", wintypes.DWORD)


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD), ("szDevice", wintypes.WCHAR * 32)]


MONITORENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HANDLE, wintypes.HDC, ctypes.POINTER(wintypes.RECT),
                                     wintypes.LPARAM)
EnumDisplayMonitors = _bind(user32, "EnumDisplayMonitors", wintypes.BOOL, wintypes.HDC, ctypes.POINTER(wintypes.RECT),
                            MONITORENUMPROC, wintypes.LPARAM)
GetMonitorInfoW = _bind(user32, "GetMonitorInfoW", wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(MONITORINFOEXW))


def _optional(lib_name: str, name: str, restype, *argtypes):
    """A binding that may not exist on older Windows builds (None then)."""
    try:
        return _bind(ctypes.WinDLL(lib_name, use_last_error=True), name, restype, *argtypes)
    except (OSError, AttributeError):
        return None


GetDpiForMonitor = _optional("shcore", "GetDpiForMonitor", ctypes.c_long, wintypes.HANDLE, ctypes.c_int,
                             ctypes.POINTER(wintypes.UINT), ctypes.POINTER(wintypes.UINT))
GetApplicationUserModelId = _optional("kernel32", "GetApplicationUserModelId", ctypes.c_long, wintypes.HANDLE,
                                      ctypes.POINTER(wintypes.UINT), wintypes.LPWSTR)
GetThreadDpiAwarenessContext = _optional("user32", "GetThreadDpiAwarenessContext", ctypes.c_void_p)
GetAwarenessFromDpiAwarenessContext = _optional("user32", "GetAwarenessFromDpiAwarenessContext", ctypes.c_int,
                                                ctypes.c_void_p)
GetProcessTimes = _bind(kernel32, "GetProcessTimes", wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
                        ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
                        ctypes.POINTER(wintypes.FILETIME))


def monitor_dpi(monitor: int) -> int:
    """Effective DPI of a monitor (96 = 100 %); 96 when Windows cannot say."""
    if GetDpiForMonitor is None:
        return 96
    x, y = wintypes.UINT(0), wintypes.UINT(0)
    if GetDpiForMonitor(monitor, MDT_EFFECTIVE_DPI, ctypes.byref(x), ctypes.byref(y)) == 0 and x.value:
        return int(x.value)
    return 96


def process_dpi_awareness() -> str:
    """This thread's DPI awareness, for the log ('per-monitor' is what window placement needs)."""
    if GetThreadDpiAwarenessContext is None or GetAwarenessFromDpiAwarenessContext is None:
        return "unknown"
    value = GetAwarenessFromDpiAwarenessContext(GetThreadDpiAwarenessContext())
    return {0: "unaware", 1: "system", 2: "per-monitor"}.get(value, f"unknown ({value})")


def _filetime_seconds(value: wintypes.FILETIME) -> float:
    """A FILETIME as Unix seconds (the same clock psutil's create_time() uses)."""
    ticks = (value.dwHighDateTime << 32) | value.dwLowDateTime
    return ticks / 10_000_000 - 11_644_473_600 if ticks else 0.0


def process_identity(pid: int) -> tuple[float, str, str]:
    """(creation time, lower-case image path, AppUserModelID) of a process from one limited-rights handle.

    The creation time tells a reused pid apart from the process it named before. Fields that cannot be read are
    0.0 / "" (protected and elevated processes allow less)."""
    handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid) if pid else None
    if not handle:
        return 0.0, "", ""
    try:
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        stamp = _filetime_seconds(created) if GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                                              ctypes.byref(kernel), ctypes.byref(user)) else 0.0
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(1024)
        exe = buffer.value.lower() if QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)) else ""
        aumid = ""
        if GetApplicationUserModelId is not None:
            length = wintypes.UINT(512)
            name = ctypes.create_unicode_buffer(512)
            if GetApplicationUserModelId(handle, ctypes.byref(length), name) == 0:  # ERROR_SUCCESS: packaged process
                aumid = name.value
        return stamp, exe, aumid
    finally:
        CloseHandle(handle)


def last_error() -> int:
    return ctypes.get_last_error()


# -- global hotkeys (cirana.core.hotkeys) ---------------------------------------------------------------------------
# RegisterHotKey(NULL, ...) posts WM_HOTKEY to the registering thread's queue: no hook, nothing in other processes.
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x0001, 0x0002, 0x0004, 0x0008, 0x4000
WM_HOTKEY = 0x0312
WM_APP = 0x8000
ERROR_HOTKEY_ALREADY_REGISTERED = 1409
RegisterHotKey = _bind(user32, "RegisterHotKey", wintypes.BOOL, HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT)
UnregisterHotKey = _bind(user32, "UnregisterHotKey", wintypes.BOOL, HWND, ctypes.c_int)
PeekMessageW = _bind(user32, "PeekMessageW", wintypes.BOOL, ctypes.POINTER(wintypes.MSG), HWND, wintypes.UINT,
                     wintypes.UINT, wintypes.UINT)

# -- focus follows the mouse (cirana.core.focus_follows_mouse): Windows' own "activate a window by hovering over it"
# (active window tracking), changed for this session only (never written to the user profile).
SPI_GETACTIVEWINDOWTRACKING = 0x1000
SPI_SETACTIVEWINDOWTRACKING = 0x1001
SPI_GETACTIVEWNDTRKZORDER = 0x100C
SPI_SETACTIVEWNDTRKZORDER = 0x100D
SPI_GETACTIVEWNDTRKTIMEOUT = 0x2002
SPI_SETACTIVEWNDTRKTIMEOUT = 0x2003
SystemParametersInfoW = _bind(user32, "SystemParametersInfoW", wintypes.BOOL, wintypes.UINT, wintypes.UINT,
                              ctypes.c_void_p, wintypes.UINT)
