"""Applications left running without a window after CIRANA died while hosting them.

Measured on Windows 11: when a host process is killed, the foreign child window is destroyed with it, but the
application process keeps running with no visible window. CIRANA records what it hosts (``playground_session.json``);
on the next start it reports those processes that are still alive. Ending one is always the user's explicit decision.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import psutil

from cirana.native import taskbar
from cirana.native import win32_api as w
from cirana.native.window_discovery import is_main_window_candidate, windows_of_process

log = logging.getLogger(__name__)

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


def find_orphans(entries: list[dict]) -> list[Orphan]:
    """Recorded hosted processes that are still running, no longer have a visible window, and whose host is gone."""
    found: list[Orphan] = []
    for entry in entries:
        try:
            pid, created = int(entry["pid"]), float(entry.get("created") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        owner = entry.get("owner") or {}
        if owner.get("pid") and owner["pid"] != os.getpid() \
                and _alive_with_identity(int(owner["pid"]), float(owner.get("created") or 0)):
            continue  # another CIRANA instance is still hosting it
        proc = _alive_with_identity(pid, created)
        if proc is None or windows_of_process(pid):
            continue
        found.append(Orphan(pid, str(entry.get("title") or proc.name()), str(entry.get("exe") or ""), created))
    return found


def restore_taskbar_buttons(entries: list[dict]) -> int:
    """Give docked windows their taskbar button back after CIRANA ended without undocking them (crash, kill).

    Only for a window that still belongs to the same process (pid and start time) and is an ordinary main window,
    and never while the CIRANA instance that docked it is still running. A button the shell refuses to give back now
    (Explorer not ready yet) stays recorded and is retried. Returns how many buttons came back.
    """
    eligible = []
    for entry in entries:
        try:
            hwnd, pid, created = int(entry["hwnd"]), int(entry["pid"]), float(entry.get("created") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        owner = entry.get("owner") or {}
        if owner.get("pid") and _alive_with_identity(int(owner["pid"]), float(owner.get("created") or 0)):
            continue  # that instance (possibly this one) still hosts it
        if _alive_with_identity(pid, created) is None or w.window_pid(hwnd)[0] != pid \
                or not is_main_window_candidate(hwnd):
            continue
        eligible.append({"hwnd": hwnd, "pid": pid, "created": created})
    taskbar.adopt(eligible)
    return taskbar.retry_pending()


def end_orphan(orphan: Orphan) -> str:
    """Terminate one orphan after re-checking that the pid still is the same process. Returns a status message."""
    proc = _alive_with_identity(orphan.pid, orphan.created)
    if proc is None:
        return f"{orphan.title} is no longer running."
    if windows_of_process(orphan.pid):
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
