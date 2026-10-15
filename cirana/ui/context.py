"""Bundle of services handed to every view (explicit dependency container, no globals)."""
from __future__ import annotations

from dataclasses import dataclass

from cirana.core.application_registry import ApplicationRegistry
from cirana.core.process_tracker import ProcessTracker
from cirana.core.scan_controller import ScanController
from cirana.core.workspace_controller import WorkspaceController
from cirana.core.workspaces import WorkspaceManager
from cirana.storage.settings_manager import SettingsManager
from cirana.ui.app_actions import AppActions
from cirana.ui.app_icons import AppIconProvider
from cirana.wm.controller import WindowController
from cirana.wm.tracker import WindowTracker


@dataclass
class AppContext:
    settings: SettingsManager
    registry: ApplicationRegistry
    scanner: ScanController
    tracker: ProcessTracker
    icons: AppIconProvider
    actions: AppActions
    workspaces: WorkspaceManager
    windows: WindowTracker  # live inventory of the desktop's application windows
    window_control: WindowController
    wm: WorkspaceController  # workspaces at runtime: bindings, activation, layouts

    @property
    def view_mode(self) -> str:
        return self.settings.get("view_mode")
