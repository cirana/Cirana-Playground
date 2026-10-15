"""Runs application discovery on a worker thread."""
from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QThread, Signal, Slot

from cirana.core.application_registry import ApplicationRegistry
from cirana.core.application_scanner import SOURCES_BY_LABEL, ScanReport, deduplicate, scan_applications
from cirana.core.models import AppEntry

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
            report.failed_sources.update(s for sources in SOURCES_BY_LABEL.values() for s in sources)
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
            self._registry.apply_scan(self._merge_failed_sources(report))
        self._cleanup_thread()
        self.finished.emit(report)

    def _merge_failed_sources(self, report: ScanReport) -> list[AppEntry]:
        """Keep the previously found applications of every source that failed this time.

        Sources that succeeded are authoritative (an application they no longer list is gone); a failed source says
        nothing, so its earlier results stay instead of vanishing from the list.
        """
        if not report.failed_sources:
            return report.apps
        kept = [a for a in self._registry.discovered_apps() if a.source in report.failed_sources]
        if not kept:
            return report.apps
        log.warning("Kept %d application(s) from failed scan source(s) %s", len(kept), sorted(report.failed_sources))
        # The same rules as a full scan decide between a kept entry and a new one for the same application.
        report.apps = sorted(deduplicate([*report.apps, *kept], report), key=lambda a: a.name.lower())
        report.counts["total"] = len(report.apps)
        return report.apps

    def _cleanup_thread(self) -> None:
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(5000)
            self._thread.deleteLater()
        self._thread = None
        self._worker = None

    def shutdown(self) -> None:
        self._cleanup_thread()
