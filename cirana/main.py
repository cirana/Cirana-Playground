"""CIRANA entry point."""
from __future__ import annotations

import ctypes
import logging
import signal
import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from cirana.core.application_registry import ApplicationRegistry
from cirana.core.dynamic_tiling import DynamicTiler
from cirana.core.focus_follows_mouse import FocusFollowsMouse
from cirana.core.hotkeys import HotkeyManager
from cirana.core.process_tracker import ProcessTracker
from cirana.core.scan_controller import ScanController
from cirana.core.window_commands import WindowCommands
from cirana.core.workspace_controller import WorkspaceController
from cirana.core.workspace_launcher import WorkspaceLauncher
from cirana.core.workspaces import WorkspaceManager
from cirana.native import win32_api
from cirana.storage.settings_manager import SettingsManager, setup_logging
from cirana.ui.app_actions import AppActions
from cirana.ui.app_icons import AppIconProvider
from cirana.ui.context import AppContext
from cirana.ui.main_window import MainWindow
from cirana.ui.theme import app_font, app_icon, build_stylesheet
from cirana.ui.tiling_overlay import TilingOverlays
from cirana.wm.controller import WindowController
from cirana.wm.tracker import WindowTracker

log = logging.getLogger("cirana")


def build_app(argv: list[str] | None = None, window_hooks: bool = True,
              window_backend=None) -> tuple[QApplication, MainWindow, AppContext]:  # noqa: ANN001
    """``window_hooks`` / ``window_backend``: the tests run the window manager on a simulated desktop."""
    app = QApplication.instance() or QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("CIRANA")
    app.setWindowIcon(app_icon())
    app.setFont(app_font())
    app.setStyleSheet(build_stylesheet())

    settings = SettingsManager()
    settings.apply_first_run_defaults()  # a new installation: all-window tiling is on; old settings stay as they are
    setup_logging(settings.root)
    log.info("DPI awareness: %s", win32_api.process_dpi_awareness())
    registry = ApplicationRegistry(settings)
    scanner = ScanController(registry)
    tracker = ProcessTracker()
    actions = AppActions(registry, tracker)
    windows = WindowTracker(window_backend, hooks=window_hooks)
    window_control = WindowController(windows.backend)
    workspaces = WorkspaceManager(settings, registry, monitors=windows.backend.monitors)
    actions.workspaces = workspaces
    wm = WorkspaceController(workspaces, registry, windows, window_control, WorkspaceLauncher(registry, windows))
    tiler = DynamicTiler(wm, settings)
    overlays = TilingOverlays(tiler, settings)
    commands = WindowCommands(tiler)
    hotkeys = HotkeyManager(settings)  # registers nothing until main() starts it (the tests never do)
    hotkeys.triggered.connect(commands.run)
    hotkeys.failed.connect(wm.message.emit)  # RegisterHotKey refused a shortcut: the user is told (toast)
    ffm = FocusFollowsMouse(settings, notify=wm.message.emit)
    ctx = AppContext(
        settings=settings,
        registry=registry,
        scanner=scanner,
        tracker=tracker,
        icons=AppIconProvider(settings.icon_cache_dir),
        actions=actions,
        workspaces=workspaces,
        windows=windows,
        window_control=window_control,
        wm=wm,
        tiler=tiler,
        overlays=overlays,
        commands=commands,
        hotkeys=hotkeys,
        ffm=ffm,
    )
    window = MainWindow(ctx)
    tracker.set_apps(registry.all_apps())
    registry.changed.connect(lambda: tracker.set_apps(registry.all_apps()))
    app.aboutToQuit.connect(tiler.stop)  # first: nothing is arranged any more while CIRANA shuts down
    app.aboutToQuit.connect(overlays.stop)
    app.aboutToQuit.connect(hotkeys.stop)
    app.aboutToQuit.connect(ffm.stop)
    app.aboutToQuit.connect(windows.stop)
    app.aboutToQuit.connect(tracker.shutdown)
    app.aboutToQuit.connect(scanner.shutdown)
    return app, window, ctx


def _install_safety_nets(app: QApplication) -> None:
    """Quit cleanly on Ctrl+C in a console run, and make sure unexpected errors reach the log."""
    previous = sys.excepthook

    def hook(kind, value, trace):  # noqa: ANN001
        log.error("Unhandled exception", exc_info=(kind, value, trace))
        previous(kind, value, trace)

    sys.excepthook = hook
    signal.signal(signal.SIGINT, lambda *_: app.quit())  # quit() runs aboutToQuit -> the hook threads stop
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
    ctx.windows.start()
    ctx.hotkeys.start()  # global shortcuts (RegisterHotKey on CIRANA's own thread)
    ctx.ffm.start()  # repairs a crashed session's setting, applies the option (off by default)
    # First run (empty cache) always scans; afterwards the cached list is shown instantly.
    if settings.get("scan_on_startup") or not registry.scanned_at:
        ctx.scanner.start()
    QTimer.singleShot(600, window.check_legacy_session)
    log.info("CIRANA started with %d cached applications", len(registry.all_apps()))
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
