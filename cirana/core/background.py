"""Tiny helper to run a blocking function on the global thread pool and deliver the result on the GUI thread.

Used for work that must never freeze the UI but does not deserve its own long-lived QThread: waiting for an
application window to appear, listing running windows, placing windows.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal, Slot

log = logging.getLogger(__name__)

_live: set["Job"] = set()  # keeps jobs (and their relays) alive until their result was delivered


class _Relay(QObject):
    """Lives in the GUI thread; queued signal delivery moves the result across threads."""

    done = Signal(object, object)  # result, error text (or None)

    def __init__(self, job: "Job") -> None:
        super().__init__()
        self._job = job
        self.done.connect(self._deliver)

    @Slot(object, object)
    def _deliver(self, result: Any, error: str | None) -> None:
        self._job._finish(result, error)


class Job(QRunnable):
    def __init__(self, fn: Callable[[threading.Event], Any], on_done: Callable[[Any], None] | None,
                 on_error: Callable[[str], None] | None) -> None:
        super().__init__()
        self.setAutoDelete(False)
        self.cancelled = threading.Event()
        self._fn = fn
        self._on_done = on_done
        self._on_error = on_error
        self._relay = _Relay(self)

    def cancel(self) -> None:
        """Ask the function to stop (it receives the event) and drop its result."""
        self.cancelled.set()

    def run(self) -> None:  # worker thread
        try:
            result, error = self._fn(self.cancelled), None
        except Exception as exc:  # noqa: BLE001 - a background job must never take the process down
            log.exception("Background job failed")
            result, error = None, str(exc) or exc.__class__.__name__
        self._relay.done.emit(result, error)

    def _finish(self, result: Any, error: str | None) -> None:  # GUI thread
        QTimer.singleShot(0, lambda: _live.discard(self))  # not while the relay's own slot is still running
        if self.cancelled.is_set():
            return
        if error is not None:
            if self._on_error:
                self._on_error(error)
        elif self._on_done:
            self._on_done(result)


def run_async(
    fn: Callable[[threading.Event], Any],
    on_done: Callable[[Any], None] | None = None,
    on_error: Callable[[str], None] | None = None,
) -> Job:
    """Run ``fn(cancel_event)`` off the GUI thread; callbacks run on the GUI thread (not after ``cancel()``)."""
    job = Job(fn, on_done, on_error)
    _live.add(job)
    QThreadPool.globalInstance().start(job)
    return job
