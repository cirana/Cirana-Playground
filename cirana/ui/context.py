"""Bundle of services handed to every view (explicit dependency container, no globals)."""
from __future__ import annotations

from dataclasses import dataclass

from cirana.core.application_registry import ApplicationRegistry
from cirana.core.playground import PlaygroundManager
from cirana.core.process_tracker import ProcessTracker
from cirana.core.scan_controller import ScanController
from cirana.core.workspace_launcher import WorkspaceLauncher
from cirana.core.workspaces import WorkspaceManager
from cirana.storage.settings_manager import SettingsManager
from cirana.ui.app_actions import AppActions
from cirana.ui.app_icons import AppIconProvider


@dataclass
class AppContext:
    settings: SettingsManager
    registry: ApplicationRegistry
    scanner: ScanController
    tracker: ProcessTracker
    icons: AppIconProvider
    actions: AppActions
    playground: PlaygroundManager
    workspaces: WorkspaceManager
    launcher: WorkspaceLauncher

    @property
    def view_mode(self) -> str:
        return self.settings.get("view_mode")
