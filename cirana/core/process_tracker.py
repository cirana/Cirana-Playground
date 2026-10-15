"""Detects which known applications are currently running.

Polling runs on a worker thread at a modest interval and pauses while CIRANA is
hidden/minimized, so it stays cheap. Only applications whose executable path is
known can be matched (packaged AUMID apps without a resolvable exe cannot).
"""
from __future__ import annotations

import logging
import os

import psutil
from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot

from cirana.core.models import AppEntry

log = logging.getLogger(__name__)


def running_executables() -> dict[str, int]:
    """Map normalized exe path -> pid for every process whose exe path is readable.

    CIRANA's own process (and its parent, e.g. a venv launcher) is excluded, otherwise
    "Python 3.12" would always look running while CIRANA is started from source.
    """
    result: dict[str, int] = {}
    own = {os.getpid(), os.getppid()}
    for proc in psutil.process_iter(["pid", "exe"]):
        exe = proc.info.get("exe")
        if exe and proc.info["pid"] not in own:
            result.setdefault(os.path.normcase(exe), proc.info["pid"])
    return result


def _exe_of(app: AppEntry) -> str:
    return os.path.normcase(app.exe_path or (app.target if app.launch_type == "executable" else ""))


def match_running(apps: list[AppEntry], running: dict[str, int]) -> dict[str, int]:
    """Match apps to processes by executable path.

    Apps that share an executable (Chrome PWAs, pydoc, wscript...) or that are launched with
    arguments cannot be told apart from the bare process, so they are never reported as running.
    """
    shared: dict[str, int] = {}
    for app in apps:
        exe = _exe_of(app)
        if exe:
            shared[exe] = shared.get(exe, 0) + 1
    found: dict[str, int] = {}
    for app in apps:
        exe = _exe_of(app)
        if exe and shared[exe] == 1 and not app.arguments:
            pid = running.get(exe)
            if pid is not None:
                found[app.id] = pid
    return found


class _PollWorker(QObject):
    result = Signal(dict)

    @Slot(object)
    def poll(self, apps: object) -> None:
        try:
            self.result.emit(match_running(list(apps), running_executables()))  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001 - polling must never crash the thread
            log.exception("Process polling failed")
            self.result.emit({})


class ProcessTracker(QObject):
    """Emits ``running_changed(dict[app_id, pid])`` when the set of running apps changes."""

    running_changed = Signal(dict)
    _request = Signal(object)

    def __init__(self, interval_ms: int = 5000) -> None:
        super().__init__()
        self._apps: list[AppEntry] = []
        self._running: dict[str, int] = {}
        self._busy = False
        self._thread = QThread()
        self._worker = _PollWorker()
        self._worker.moveToThread(self._thread)
        self._request.connect(self._worker.poll)
        self._worker.result.connect(self._on_result)
        self._thread.start()
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self.refresh)

    @property
    def running(self) -> dict[str, int]:
        return dict(self._running)

    def set_apps(self, apps: list[AppEntry]) -> None:
        self._apps = [a for a in apps if a.exe_path or a.launch_type == "executable"]

    def start(self) -> None:
        self._timer.start()
        self.refresh()

    def set_active(self, active: bool) -> None:
        if active:
            self._timer.start()
            self.refresh()
        else:
            self._timer.stop()

    @Slot()
    def refresh(self) -> None:
        if self._busy or not self._apps:
            return
        self._busy = True
        self._request.emit(list(self._apps))

    def _on_result(self, found: dict) -> None:
        self._busy = False
        if found != self._running:
            self._running = found
            self.running_changed.emit(dict(found))

    def shutdown(self) -> None:
        self._timer.stop()
        self._thread.quit()
        self._thread.wait(3000)
