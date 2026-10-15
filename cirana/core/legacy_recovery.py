"""One-time clean-up after the Playground (window embedding) versions of CIRANA.

Those versions kept ``playground_session.json`` while they hosted windows. If one of them ended unexpectedly, two
things could be left behind, and this module deals with them once, at the next start:

* ``docked`` records: windows whose taskbar button the old version had removed (``ITaskbarList::DeleteTab``). The
  button is given back (``AddTab``) for every window that still belongs to the same process (pid and start time).
* ``embedded`` records: applications whose re-parented window was destroyed with the old CIRANA window. They still run,
  without any window. They are only listed; ending one is the user's explicit choice (see the main window).

The file is removed afterwards, unless the CIRANA instance that wrote it is still running. The current version never
re-parents, docks or hides anything, so it never writes this file.
"""
from __future__ import annotations

import ctypes
import json
import logging
import os
import uuid
from dataclasses import dataclass
from ctypes import wintypes
from pathlib import Path

import psutil

from cirana.native import win32_api as w

log = logging.getLogger(__name__)

FILE_NAME = "playground_session.json"
_CREATE_TIME_TOLERANCE = 2.0  # seconds; protects against a recycled pid


@dataclass(frozen=True)
class Orphan:
    pid: int
    title: str
    exe: str
    created: float


def _alive_with_identity(pid: int, created: float) -> psutil.Process | None:
    try:
        proc = psutil.Process(pid)
        if created and abs(proc.create_time() - created) > _CREATE_TIME_TOLERANCE:
            return None  # the pid now belongs to a different process
        return proc if proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE else None
    except psutil.Error:
        return None


def _owner_running(entry: dict) -> bool:
    owner = entry.get("owner") if isinstance(entry.get("owner"), dict) else {}
    try:
        pid, created = int(owner.get("pid") or 0), float(owner.get("created") or 0)
    except (TypeError, ValueError):
        return False
    return bool(pid) and pid != os.getpid() and _alive_with_identity(pid, created) is not None


def _has_visible_window(pid: int) -> bool:
    found = []

    @w.WNDENUMPROC
    def collect(hwnd, _lparam):  # noqa: ANN001 - ctypes callback
        if w.window_pid(hwnd)[0] == pid and w.IsWindowVisible(hwnd):
            found.append(hwnd)
            return False
        return True

    w.EnumWindows(collect, 0)
    return bool(found)


def read_records(root: Path) -> dict | None:
    """The old file's data ({"embedded": [...], "docked": [...]}), or None when there is none (or it is unreadable)."""
    path = root / FILE_NAME
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        data = raw.get("data") if isinstance(raw, dict) else None
    except (OSError, ValueError):
        log.warning("Could not read %s; it is left in place", FILE_NAME)
        return None
    if not isinstance(data, dict):
        return {"embedded": [], "docked": []}
    return {key: [e for e in data.get(key, []) if isinstance(e, dict)] if isinstance(data.get(key), list) else []
            for key in ("embedded", "docked")}


def find_orphans(entries: list[dict]) -> list[Orphan]:
    """Recorded hosted processes that still run, have no visible window, and whose CIRANA is gone."""
    found: list[Orphan] = []
    for entry in entries:
        try:
            pid, created = int(entry["pid"]), float(entry.get("created") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if _owner_running(entry):
            continue
        proc = _alive_with_identity(pid, created)
        if proc is None or _has_visible_window(pid):
            continue
        try:
            title = str(entry.get("title") or proc.name())
        except psutil.Error:
            continue
        found.append(Orphan(pid, title, str(entry.get("exe") or ""), created))
    return found


def end_orphan(orphan: Orphan) -> str:
    """Terminate one orphan (only after the user chose it), re-checking that the pid is still the same process."""
    proc = _alive_with_identity(orphan.pid, orphan.created)
    if proc is None:
        return f"{orphan.title} is no longer running."
    if _has_visible_window(orphan.pid):
        return f"{orphan.title} has a window again; it was left running."
    try:
        proc.terminate()
        proc.wait(timeout=3)
    except psutil.TimeoutExpired:
        return f"{orphan.title} did not exit when asked; it was left running."
    except psutil.Error as exc:
        log.warning("Could not end %s (%s): %s", orphan.title, orphan.pid, exc)
        return f"Could not end {orphan.title}: {exc}"
    return f"Ended {orphan.title}."


# -- giving taskbar buttons back (ITaskbarList::AddTab) ---------------------------------------------------------------
_CLSID_TASKBAR_LIST = uuid.UUID("56FDF344-FD6D-11D0-958A-006097C9A090")
_IID_ITASKBAR_LIST = uuid.UUID("56FDF342-FD6D-11D0-958A-006097C9A090")
_RELEASE, _HR_INIT, _ADD_TAB = 2, 3, 4  # vtable slots: IUnknown (3), HrInit, AddTab, ...


def _add_tabs(hwnds: list[int]) -> int:
    """AddTab for each window (GUI thread: Qt has initialised COM there). Returns how many the shell accepted."""
    if not hwnds:
        return 0
    try:
        ole32 = ctypes.OleDLL("ole32")
        try:
            ole32.CoInitializeEx(None, 0x2)  # S_FALSE when Qt already did it
        except OSError:
            pass  # initialised in another mode: works as well
        ptr = ctypes.c_void_p()
        guid = lambda value: (ctypes.c_ubyte * 16).from_buffer_copy(value.bytes_le)  # noqa: E731
        ole32.CoCreateInstance(ctypes.byref(guid(_CLSID_TASKBAR_LIST)), None, 0x1,
                               ctypes.byref(guid(_IID_ITASKBAR_LIST)), ctypes.byref(ptr))
    except (OSError, AttributeError) as exc:
        log.warning("The taskbar cannot be reached to give buttons back: %s", exc)
        return 0
    vtable = ctypes.cast(ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
    unary = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)
    tab = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, wintypes.HWND)
    restored = 0
    try:
        if unary(vtable[_HR_INIT])(ptr) >= 0:
            restored = sum(1 for hwnd in hwnds if tab(vtable[_ADD_TAB])(ptr, hwnd) >= 0)
    finally:
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[_RELEASE])(ptr)
    return restored


def restore_taskbar_buttons(entries: list[dict]) -> int:
    """Give the old version's docked windows their taskbar button back (same process only). Returns the count."""
    hwnds = []
    for entry in entries:
        try:
            hwnd, pid, created = int(entry["hwnd"]), int(entry["pid"]), float(entry.get("created") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if _owner_running(entry) or not w.IsWindow(hwnd) or w.window_pid(hwnd)[0] != pid:
            continue
        if _alive_with_identity(pid, created) is not None and w.IsWindowVisible(hwnd):
            hwnds.append(hwnd)
    return _add_tabs(hwnds)


def recover(root: Path) -> list[Orphan]:
    """Run the one-time clean-up. Returns windowless applications for the user to decide about."""
    records = read_records(root)
    if records is None:
        return []
    restored = restore_taskbar_buttons(records["docked"])
    if restored:
        log.info("Gave %d window(s) docked by an earlier CIRANA their taskbar button back", restored)
    orphans = find_orphans(records["embedded"])
    if not any(_owner_running(entry) for entry in records["embedded"] + records["docked"]):
        try:
            for path in (root / FILE_NAME, root / (FILE_NAME + ".bak")):
                path.unlink(missing_ok=True)
        except OSError:
            log.warning("Could not remove %s", FILE_NAME)
    return orphans
