"""A tiny REAL Win32 window with its own message loop that can be told to stop responding (for the live tests).

    python -m tests.hang_window_helper <trigger file> <hang seconds> [<min width>x<min height>]

Prints ``READY <hwnd>`` once the window exists. When the trigger file appears, the message loop is blocked for the
given number of seconds (the window turns "not responding"), then it carries on. WM_CLOSE closes it normally.
With a minimum size the window answers WM_GETMINMAXINFO with it (ptMinTrackSize, in its own pixels), as many
applications do: Windows then refuses to make it any smaller, also when CIRANA asks for it.
"""
import ctypes
import os
import sys
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

WM_DESTROY, WM_CLOSE, WM_GETMINMAXINFO = 0x0002, 0x0010, 0x0024
WS_OVERLAPPEDWINDOW, WS_VISIBLE = 0x00CF0000, 0x10000000
PM_REMOVE = 1


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HANDLE), ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HANDLE), ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
    ]


class MINMAXINFO(ctypes.Structure):
    _fields_ = [("ptReserved", wintypes.POINT), ("ptMaxSize", wintypes.POINT), ("ptMaxPosition", wintypes.POINT),
                ("ptMinTrackSize", wintypes.POINT), ("ptMaxTrackSize", wintypes.POINT)]


MIN_SIZE = (0, 0)
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, wintypes.HWND, wintypes.HANDLE, wintypes.HINSTANCE, wintypes.LPVOID,
]
user32.CreateWindowExW.restype = wintypes.HWND
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.PostQuitMessage.argtypes = [ctypes.c_int]
kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE


@WNDPROC
def wnd_proc(hwnd, msg, wparam, lparam):
    if msg == WM_CLOSE:
        user32.DestroyWindow(hwnd)
        return 0
    if msg == WM_DESTROY:
        user32.PostQuitMessage(0)
        return 0
    if msg == WM_GETMINMAXINFO and MIN_SIZE != (0, 0):
        info = ctypes.cast(lparam, ctypes.POINTER(MINMAXINFO)).contents
        info.ptMinTrackSize.x, info.ptMinTrackSize.y = MIN_SIZE
        return 0
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def main() -> None:
    global MIN_SIZE
    trigger, hang_seconds = sys.argv[1], float(sys.argv[2])
    if len(sys.argv) > 3:
        MIN_SIZE = tuple(int(v) for v in sys.argv[3].lower().split("x"))
    instance = kernel32.GetModuleHandleW(None)
    window_class = WNDCLASSW()
    window_class.lpfnWndProc = wnd_proc
    window_class.hInstance = instance
    window_class.hbrBackground = 6  # COLOR_WINDOW + 1
    window_class.lpszClassName = "CiranaHangTestWindow"
    user32.RegisterClassW(ctypes.byref(window_class))
    hwnd = user32.CreateWindowExW(0, "CiranaHangTestWindow", "CIRANA hang test window", WS_OVERLAPPEDWINDOW | WS_VISIBLE,
                                  200, 200, 560, 340, None, None, instance, None)
    if sys.stdout is not None:  # pythonw.exe (no console) has no stdout
        print(f"READY {int(hwnd)}", flush=True)
    msg = wintypes.MSG()
    while True:
        if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
            if msg.message == 0x0012:  # WM_QUIT
                return
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        else:
            time.sleep(0.02)
        if os.path.exists(trigger):
            os.remove(trigger)
            time.sleep(hang_seconds)  # the message loop is blocked: the window is "not responding"


if __name__ == "__main__":
    main()
