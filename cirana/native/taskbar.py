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

import psutil

from cirana.native import win32_api as w

log = logging.getLogger(__name__)

_CLSID_TASKBAR_LIST = uuid.UUID("56FDF344-FD6D-11D0-958A-006097C9A090")
_IID_ITASKBAR_LIST = uuid.UUID("56FDF342-FD6D-11D0-958A-006097C9A090")
_CLSCTX_INPROC_SERVER = 0x1
_COINIT_APARTMENTTHREADED = 0x2
# ITaskbarList vtable: IUnknown (QueryInterface, AddRef, Release), HrInit, AddTab, DeleteTab, ActivateTab, SetActiveAlt
_RELEASE, _HR_INIT, _ADD_TAB, _DELETE_TAB = 2, 3, 4, 5

_HrInitProto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)
_ReleaseProto = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
_TabProto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, wintypes.HWND)

_instance: ctypes.c_void_p | None = None
_warned = False
# Windows whose button CIRANA removed and has not given back yet: hwnd -> {"pid", "created", "hosted"}. "hosted":
# still docked (hidden on purpose); False: no longer hosted but AddTab failed, so it is retried and kept for recovery.
_records: dict[int, dict] = {}


def _guid(value: uuid.UUID) -> ctypes.Array:
    return (ctypes.c_ubyte * 16).from_buffer_copy(value.bytes_le)


def _vtable(ptr: ctypes.c_void_p) -> ctypes.Array:
    return ctypes.cast(ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))


def _taskbar() -> ctypes.c_void_p | None:
    """The process-wide ITaskbarList, created on first use. A failure is not final: the next call tries again (the
    shell may simply not be ready yet, e.g. right after logging in or while Explorer restarts)."""
    global _instance, _warned
    if _instance is not None:
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
            _ReleaseProto(_vtable(ptr)[_RELEASE])(ptr)
            raise OSError("ITaskbarList::HrInit failed")
        _instance = ptr
    except (OSError, AttributeError) as exc:
        if not _warned:
            log.warning("Taskbar buttons cannot be changed right now: %s", exc)
            _warned = True
    return _instance


def reset() -> None:
    """Drop the shell object (Explorer restarted: the old one talks to a taskbar that no longer exists)."""
    global _instance
    ptr, _instance = _instance, None
    if ptr is not None:
        try:
            _ReleaseProto(_vtable(ptr)[_RELEASE])(ptr)
        except OSError:
            pass


def _tab_call(index: int, hwnd: int) -> bool:
    if not hwnd:
        return False
    for attempt in range(2):  # a stale shell object (Explorer restarted) is recreated once
        ptr = _taskbar()
        if ptr is None:
            return False
        if _TabProto(_vtable(ptr)[index])(ptr, hwnd) >= 0:
            return True
        if attempt == 0:
            reset()
    return False


def _identity(hwnd: int) -> tuple[int, float]:
    pid = w.window_pid(hwnd)[0]
    try:
        return pid, psutil.Process(pid).create_time()
    except psutil.Error:
        return pid, 0.0


def _same_window(hwnd: int, record: dict) -> bool:
    """The handle still names the window of the same process (handles and pids are recycled)."""
    if not w.IsWindow(hwnd):
        return False
    pid, created = _identity(hwnd)
    return pid == record.get("pid") and (not record.get("created") or abs(created - record["created"]) < 2.0)


def hide_button(hwnd: int) -> bool:
    """Remove ``hwnd``'s taskbar button while it is hosted. True if the shell accepted it."""
    if not _tab_call(_DELETE_TAB, hwnd):
        return False
    pid, created = _identity(hwnd)
    _records[hwnd] = {"pid": pid, "created": created, "hosted": True}
    return True


def show_button(hwnd: int) -> bool:
    """Give ``hwnd`` its taskbar button back. On failure the record stays (retried later, kept for crash recovery)."""
    if _tab_call(_ADD_TAB, hwnd):
        _records.pop(hwnd, None)
        return True
    if hwnd in _records:
        _records[hwnd]["hosted"] = False
    log.warning("Could not give window %#x its taskbar button back; will retry", hwnd)
    return False


def adopt(records: list[dict]) -> None:
    """Take over buttons a previous CIRANA session hid and could not give back (validated by the caller)."""
    for record in records:
        _records.setdefault(int(record["hwnd"]), {"pid": int(record["pid"]), "created": float(record.get("created") or 0),
                                                  "hosted": False})


def retry_pending() -> int:
    """Try AddTab again for windows that are no longer hosted. Gone or recycled handles are forgotten."""
    restored = 0
    for hwnd, record in list(_records.items()):
        if record["hosted"]:
            continue
        if not _same_window(hwnd, record):
            _records.pop(hwnd, None)
        elif show_button(hwnd):
            restored += 1
    return restored


def records() -> list[dict]:
    """What must survive a crash: every window whose button is still missing because of CIRANA."""
    return [{"hwnd": hwnd, **record} for hwnd, record in _records.items() if _same_window(hwnd, record)]


def taskbar_restarted() -> int:
    """Explorer restarted and gave *every* window its button back: hide the hosted ones again, forget the pending
    ones (they have their button now). Returns how many were hidden again."""
    reset()
    hidden = 0
    for hwnd, record in list(_records.items()):
        if not record["hosted"] or not _same_window(hwnd, record):
            _records.pop(hwnd, None)
        elif _tab_call(_DELETE_TAB, hwnd):
            hidden += 1
        else:
            _records.pop(hwnd, None)  # it shows its button again; nothing to give back on detach
    return hidden


def forget(hwnd: int) -> None:
    _records.pop(hwnd, None)
