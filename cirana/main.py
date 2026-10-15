"""CIRANA entry point."""
from __future__ import annotations

import ctypes
import logging
import signal
import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from cirana.core.application_registry import ApplicationRegistry
from cirana.core.playground import PlaygroundManager
from cirana.core.process_tracker import ProcessTracker
from cirana.core.scan_controller import ScanController
from cirana.core.workspace_launcher import WorkspaceLauncher
from cirana.core.workspaces import WorkspaceManager
from cirana.storage.settings_manager import SettingsManager, setup_logging
from cirana.ui.app_actions import AppActions
from cirana.ui.app_icons import AppIconProvider
from cirana.ui.context import AppContext
from cirana.ui.main_window import MainWindow
from cirana.ui.theme import app_font, app_icon, build_stylesheet

log = logging.getLogger("cirana")


def build_app(argv: list[str] | None = None) -> tuple[QApplication, MainWindow, AppContext]:
    app = QApplication.instance() or QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("CIRANA")
    app.setWindowIcon(app_icon())
    app.setFont(app_font())
    app.setStyleSheet(build_stylesheet())

    settings = SettingsManager()
    setup_logging(settings.root)
    registry = ApplicationRegistry(settings)
    scanner = ScanController(registry)
    tracker = ProcessTracker()
    actions = AppActions(registry, tracker)
    playground = PlaygroundManager(registry, settings)
    actions.playground = playground
    workspaces = WorkspaceManager(settings, registry)
    actions.workspaces = workspaces
    actions.workspace_id = lambda: workspaces.active_id
    ctx = AppContext(
        settings=settings,
        registry=registry,
        scanner=scanner,
        tracker=tracker,
        icons=AppIconProvider(settings.icon_cache_dir),
        actions=actions,
        playground=playground,
        workspaces=workspaces,
        launcher=WorkspaceLauncher(registry, workspaces, playground),
    )
    window = MainWindow(ctx)
    tracker.set_apps(registry.all_apps())
    registry.changed.connect(lambda: tracker.set_apps(registry.all_apps()))
    app.aboutToQuit.connect(window.shutdown_playground)  # first: hosted windows go back to Windows before teardown
    app.aboutToQuit.connect(tracker.shutdown)
    app.aboutToQuit.connect(scanner.shutdown)
    return app, window, ctx


def _install_safety_nets(app: QApplication) -> None:
    """Make sure hosted windows are handed back even on Ctrl+C, and that unexpected errors reach the log."""
    previous = sys.excepthook

    def hook(kind, value, trace):  # noqa: ANN001
        log.error("Unhandled exception", exc_info=(kind, value, trace))
        previous(kind, value, trace)

    sys.excepthook = hook
    signal.signal(signal.SIGINT, lambda *_: app.quit())  # quit() runs aboutToQuit -> hosted windows are restored
    keepalive = QTimer(app)  # Python only runs signal handlers while the interpreter is active
    keepalive.setInterval(300)
    keepalive.timeout.connect(lambda: None)
    keepalive.start()


def _set_app_user_model_id() -> None:
    """Group CIRANA's windows under its own taskbar button and icon instead of python.exe's (source checkout)."""
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Cirana.Playground")
    except (AttributeError, OSError):
        log.debug("Could not set the AppUserModelID", exc_info=True)


def main(argv: list[str] | None = None) -> int:
    _set_app_user_model_id()  # before the first window exists
    app, window, ctx = build_app(argv)
    _install_safety_nets(app)
    window.show()
    tracker, settings, registry = ctx.tracker, ctx.settings, ctx.registry
    tracker.start()
    # First run (empty cache) always scans; afterwards the cached list is shown instantly.
    if settings.get("scan_on_startup") or not registry.scanned_at:
        ctx.scanner.start()
    QTimer.singleShot(600, window.check_orphans)
    log.info("CIRANA started with %d cached applications", len(registry.all_apps()))
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
