"""Runs application discovery on a worker thread."""
from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QThread, Signal, Slot

from cirana.core.application_registry import ApplicationRegistry
from cirana.core.application_scanner import ScanReport, scan_applications

log = logging.getLogger(__name__)


class _ScanWorker(QObject):
    finished = Signal(object)

    def __init__(self, registry: ApplicationRegistry) -> None:
        super().__init__()
        self._engine = registry.engine

    @Slot()
    def run(self) -> None:
        try:
            report = scan_applications(self._engine)
        except Exception as exc:  # noqa: BLE001
            log.exception("Scan crashed")
            report = ScanReport(errors=[str(exc)])
            report.counts["failed"] = 1
        self.finished.emit(report)


class ScanController(QObject):
    started = Signal()
    finished = Signal(object)  # ScanReport, after the registry has been updated

    def __init__(self, registry: ApplicationRegistry) -> None:
        super().__init__()
        self._registry = registry
        self._thread: QThread | None = None
        self._worker: _ScanWorker | None = None
        self.last_report: ScanReport | None = None

    @property
    def scanning(self) -> bool:
        return self._thread is not None

    def start(self) -> bool:
        if self._thread is not None:
            return False  # a scan is already running
        self._thread = QThread()
        self._worker = _ScanWorker(self._registry)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._on_finished)
        self._thread.start()
        self.started.emit()
        return True

    def _on_finished(self, report: ScanReport) -> None:
        self.last_report = report
        # A failed scan (no apps at all) must not wipe a good cache.
        if report.apps:
            self._registry.apply_scan(report.apps)
        self._cleanup_thread()
        self.finished.emit(report)

    def _cleanup_thread(self) -> None:
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(5000)
            self._thread.deleteLater()
        self._thread = None
        self._worker = None

    def shutdown(self) -> None:
        self._cleanup_thread()
