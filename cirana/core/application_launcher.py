"""Starts applications according to their launch type. Never uses a shell."""
from __future__ import annotations

import logging
import os
import subprocess

from cirana.core.models import AppEntry, LaunchResult, LaunchType

log = logging.getLogger(__name__)

DETACHED_PROCESS = 0x00000008
CREATE_NEW_CONSOLE = 0x00000010
CREATE_NEW_PROCESS_GROUP = 0x00000200
ERROR_ELEVATION_REQUIRED = 740


def launch(app: AppEntry) -> LaunchResult:
    """Launch ``app``. The returned pid is only set when CIRANA started the process itself."""
    try:
        kind = LaunchType(app.launch_type)
        if kind is LaunchType.SHORTCUT:
            return _launch_shortcut(app)
        if kind is LaunchType.EXECUTABLE:
            return _launch_executable(app)
        if kind is LaunchType.AUMID:
            return _launch_aumid(app)
        return _launch_command(app)
    except (OSError, ValueError) as exc:
        log.warning("Launch of %s failed: %s", app.name, exc)
        return LaunchResult(False, f"Could not start {app.name}: {exc}")


def _launch_shortcut(app: AppEntry) -> LaunchResult:
    if not os.path.exists(app.target):
        return LaunchResult(False, f"Shortcut no longer exists: {app.target}")
    os.startfile(app.target)  # type: ignore[attr-defined]  # ShellExecute handles MSI shortcuts + elevation
    return LaunchResult(True, f"Launched {app.name}")


def _launch_executable(app: AppEntry) -> LaunchResult:
    if not os.path.isfile(app.target):
        return LaunchResult(False, f"Executable not found: {app.target}")
    cwd = app.working_dir if app.working_dir and os.path.isdir(app.working_dir) else os.path.dirname(app.target)
    cmdline = subprocess.list2cmdline([app.target]) + (f" {app.arguments}" if app.arguments else "")
    try:
        # CREATE_NEW_CONSOLE: console programs / .bat files get their own window; GUI programs ignore the flag.
        proc = subprocess.Popen(
            cmdline, cwd=cwd or None, creationflags=CREATE_NEW_CONSOLE | CREATE_NEW_PROCESS_GROUP, close_fds=True
        )
        return LaunchResult(True, f"Launched {app.name}", pid=proc.pid)
    except OSError as exc:
        if getattr(exc, "winerror", None) == ERROR_ELEVATION_REQUIRED:
            os.startfile(app.target, arguments=app.arguments)  # type: ignore[attr-defined]  # shows the UAC prompt
            return LaunchResult(True, f"Launched {app.name} (elevation requested)")
        raise


def _launch_aumid(app: AppEntry) -> LaunchResult:
    # The pid of explorer.exe says nothing about the app, so it is deliberately not returned.
    subprocess.Popen(
        ["explorer.exe", f"shell:AppsFolder\\{app.target}"],
        creationflags=DETACHED_PROCESS,
        close_fds=True,
    )
    return LaunchResult(True, f"Launched {app.name}")


def _launch_command(app: AppEntry) -> LaunchResult:
    command = app.target.strip()
    if not command:
        return LaunchResult(False, "Empty command")
    cwd = app.working_dir if app.working_dir and os.path.isdir(app.working_dir) else None
    proc = subprocess.Popen(
        command, cwd=cwd, creationflags=CREATE_NEW_CONSOLE | CREATE_NEW_PROCESS_GROUP, close_fds=True
    )
    return LaunchResult(True, f"Launched {app.name}", pid=proc.pid)
