"""Hiding a hosted window's taskbar button with the shell's ITaskbarList (DeleteTab / AddTab).

Only Explorer's list of buttons changes: the window itself (styles, owner, parent, Alt+Tab entry, activation) is not
touched, so this is safe for docked windows, whose styles CIRANA never changes. The calls go to Explorer, never to the
hosted application, so a hung application cannot block them. If the shell object is unavailable (no Explorer, COM
refused) every call reports False and the button simply stays: nothing riskier is attempted instead.

Must be called on the GUI thread (a single-threaded COM apartment; Qt initialises OLE there).
"""
from __future__ import annotations

import ctypes
import logging
import uuid
from ctypes import wintypes

log = logging.getLogger(__name__)

_CLSID_TASKBAR_LIST = uuid.UUID("56FDF344-FD6D-11D0-958A-006097C9A090")
_IID_ITASKBAR_LIST = uuid.UUID("56FDF342-FD6D-11D0-958A-006097C9A090")
_CLSCTX_INPROC_SERVER = 0x1
_COINIT_APARTMENTTHREADED = 0x2
# ITaskbarList vtable: IUnknown (QueryInterface, AddRef, Release), HrInit, AddTab, DeleteTab, ActivateTab, SetActiveAlt
_HR_INIT, _ADD_TAB, _DELETE_TAB = 3, 4, 5

_HrInitProto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)
_TabProto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, wintypes.HWND)

_instance: ctypes.c_void_p | None = None
_unavailable = False


def _guid(value: uuid.UUID) -> ctypes.Array:
    return (ctypes.c_ubyte * 16).from_buffer_copy(value.bytes_le)


def _vtable(ptr: ctypes.c_void_p) -> ctypes.Array:
    return ctypes.cast(ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))


def _taskbar() -> ctypes.c_void_p | None:
    """The process-wide ITaskbarList, created on first use; None (and never retried) if the shell refuses."""
    global _instance, _unavailable
    if _instance is not None or _unavailable:
        return _instance
    try:
        ole32 = ctypes.OleDLL("ole32")
        try:
            ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)  # S_FALSE when Qt already did it
        except OSError:
            pass  # RPC_E_CHANGED_MODE: COM is already initialised in another mode, which works as well
        ptr = ctypes.c_void_p()
        ole32.CoCreateInstance(ctypes.byref(_guid(_CLSID_TASKBAR_LIST)), None, _CLSCTX_INPROC_SERVER,
                               ctypes.byref(_guid(_IID_ITASKBAR_LIST)), ctypes.byref(ptr))
        if _HrInitProto(_vtable(ptr)[_HR_INIT])(ptr) < 0:
            raise OSError("ITaskbarList::HrInit failed")
        _instance = ptr
    except (OSError, AttributeError) as exc:
        log.warning("Taskbar buttons cannot be hidden on this system: %s", exc)
        _unavailable = True
    return _instance


def _tab_call(index: int, hwnd: int) -> bool:
    ptr = _taskbar()
    if ptr is None or not hwnd:
        return False
    return _TabProto(_vtable(ptr)[index])(ptr, hwnd) >= 0


def hide_button(hwnd: int) -> bool:
    """Remove ``hwnd``'s taskbar button. True if the shell accepted it."""
    return _tab_call(_DELETE_TAB, hwnd)


def show_button(hwnd: int) -> bool:
    """Give ``hwnd`` its taskbar button back (undoes :func:`hide_button`)."""
    return _tab_call(_ADD_TAB, hwnd)
