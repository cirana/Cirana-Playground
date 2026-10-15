"""Discovery of installed Windows applications.

Sources (no disk-wide crawling):
  * user + system Start Menu shortcuts (bounded walk of the Programs folders)
  * packaged / AUMID apps via ``Get-StartApps``
  * ``App Paths`` registry registrations
Pure Python (no Qt) so it can run in any thread or from a script.
"""
from __future__ import annotations

import gc
import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from cirana.core.category_engine import CategoryEngine
from cirana.core.models import AppEntry, LaunchType, Source, normalize_name, stable_app_id

log = logging.getLogger(__name__)

CREATE_NO_WINDOW = 0x08000000

# Shortcut names that are never "applications" the user wants to launch.
_NOISE_NAME = re.compile(
    r"\b(uninstall|uninstaller|remove|readme|read me|release notes|license|eula|documentation|"
    r"user guide|manual|website|web site|changelog|what'?s new|help|support|report a|"
    r"visit|online|feedback|repair|modify)\b",
    re.I,
)
_LAUNCHABLE_EXT = {".exe", ".bat", ".cmd", ".com", ".msc", ".cpl"}


@dataclass
class ScanReport:
    apps: list[AppEntry] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    merged: list[tuple[str, str]] = field(default_factory=list)  # (kept, dropped description)
    errors: list[str] = field(default_factory=list)
    failed_sources: set[str] = field(default_factory=set)  # AppEntry.source values whose discovery failed this time


def start_menu_roots() -> list[tuple[Path, Source]]:
    roots: list[tuple[Path, Source]] = []
    appdata = os.environ.get("APPDATA")
    programdata = os.environ.get("PROGRAMDATA")
    if appdata:
        roots.append((Path(appdata, "Microsoft", "Windows", "Start Menu", "Programs"), Source.START_MENU_USER))
    if programdata:
        roots.append((Path(programdata, "Microsoft", "Windows", "Start Menu", "Programs"), Source.START_MENU_SYSTEM))
    return roots


def _resolve_shortcuts(paths: list[Path]) -> list[dict]:
    """Resolve .lnk files. Needs COM, so initialize it for the calling thread."""
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    results: list[dict] = []
    link = shell = None
    try:
        shell = win32com.client.Dispatch("WScript.Shell")
        for path in paths:
            try:
                link = shell.CreateShortCut(str(path))
                results.append(
                    {
                        "path": path,
                        "target": link.TargetPath or "",
                        "args": link.Arguments or "",
                        "cwd": link.WorkingDirectory or "",
                    }
                )
            except Exception as exc:  # noqa: BLE001 - one broken .lnk must not abort the scan
                log.debug("Cannot resolve %s: %s", path, exc)
                results.append({"path": path, "target": "", "args": "", "cwd": ""})
    finally:
        # COM wrappers must be released before CoUninitialize, otherwise pywin32 logs IUnknown errors.
        link = shell = None
        gc.collect()
        pythoncom.CoUninitialize()
    return results


def scan_start_menu(report: ScanReport) -> list[AppEntry]:
    entries: list[AppEntry] = []
    for root, source in start_menu_roots():
        if not root.is_dir():
            continue
        lnks: list[Path] = []
        for dirpath, _dirs, files in os.walk(root):
            lnks.extend(Path(dirpath, f) for f in files if f.lower().endswith(".lnk"))
        for info in _resolve_shortcuts(lnks):
            path: Path = info["path"]
            name = path.stem
            if _NOISE_NAME.search(name):
                continue
            target = os.path.expandvars(info["target"])
            ext = os.path.splitext(target)[1].lower()
            # Empty targets are MSI-advertised shortcuts: they can only be launched via the .lnk itself.
            if target and ext not in _LAUNCHABLE_EXT:
                continue
            if target and os.path.basename(target).lower().startswith("unins"):
                continue
            rel = path.parent.relative_to(root)
            group = "" if str(rel) == "." else str(rel)
            entries.append(
                AppEntry(
                    id=stable_app_id(name),
                    name=name,
                    launch_type=LaunchType.SHORTCUT.value,
                    target=str(path),
                    source=source.value,
                    arguments=info["args"],
                    exe_path=target if target and os.path.exists(target) else "",
                    working_dir=info["cwd"],
                    group=group,
                )
            )
    return entries


def scan_packaged(report: ScanReport) -> list[AppEntry]:
    script = (
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
        "Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress"
    )
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True,
            timeout=45,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        report.errors.append(f"Get-StartApps failed: {exc}")
        return []
    if proc.returncode != 0 or not proc.stdout.strip():
        report.errors.append(f"Get-StartApps exit {proc.returncode}: {proc.stderr.decode('utf-8', 'replace')[:200]}")
        return []
    try:
        parsed = json.loads(proc.stdout.decode("utf-8-sig"))
    except ValueError as exc:
        report.errors.append(f"Get-StartApps returned invalid JSON: {exc}")
        return []
    if isinstance(parsed, dict):  # PowerShell emits a bare object for a single result
        parsed = [parsed]
    entries: list[AppEntry] = []
    for item in parsed:
        name, app_id = (item.get("Name") or "").strip(), (item.get("AppID") or "").strip()
        if not name or not app_id or _NOISE_NAME.search(name):
            continue
        exe_path = _known_folder_exe(app_id)
        if ("\\" in app_id or "/" in app_id) and not exe_path:
            ext = os.path.splitext(app_id)[1].lower()
            if ext not in _LAUNCHABLE_EXT:
                continue  # documents, .url files etc. that happen to be pinned in the Start menu
        entries.append(
            AppEntry(
                id=stable_app_id(name),
                name=name,
                launch_type=LaunchType.AUMID.value,
                target=app_id,
                source=Source.PACKAGED.value,
                exe_path=exe_path,
            )
        )
    return entries


_KNOWN_FOLDERS = {
    "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}": lambda: os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32"),
    "{D65231B0-B2F1-4857-A4CE-A8E7C6EA7D27}": lambda: os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "SysWOW64"),
    "{F38BF404-1D43-42F2-9305-67DE0B28FC23}": lambda: os.environ.get("WINDIR", r"C:\Windows"),
    "{6D809377-6AF0-444B-8957-A3773F02200E}": lambda: os.environ.get("ProgramW6432", r"C:\Program Files"),
    "{7C5A40EF-A0FB-4BFC-874A-C0F2E0B9FA8E}": lambda: os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
}


def _known_folder_exe(app_id: str) -> str:
    """Resolve known-folder-GUID AppIDs (``{GUID}\\tool.exe``) to a real file (icons / classification)."""
    match = re.match(r"^(\{[0-9A-Fa-f-]{36}\})\\(.+)$", app_id)
    if not match or match.group(1).upper() not in _KNOWN_FOLDERS:
        return ""
    candidate = os.path.join(_KNOWN_FOLDERS[match.group(1).upper()](), match.group(2))
    return candidate if candidate.lower().endswith(".exe") and os.path.isfile(candidate) else ""


def scan_registry(report: ScanReport) -> list[AppEntry]:
    import winreg

    windir = os.environ.get("WINDIR", r"C:\Windows").lower()
    entries: list[AppEntry] = []
    seen: set[str] = set()
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            key = winreg.OpenKey(hive, r"Software\Microsoft\Windows\CurrentVersion\App Paths")
        except OSError:
            continue
        with key:
            index = 0
            while True:
                try:
                    sub = winreg.EnumKey(key, index)
                except OSError:
                    break
                index += 1
                try:
                    with winreg.OpenKey(key, sub) as subkey:
                        value, _ = winreg.QueryValueEx(subkey, "")
                except OSError:
                    continue
                exe = os.path.expandvars(str(value).strip().strip('"'))
                if not exe.lower().endswith(".exe") or not os.path.isfile(exe):
                    continue
                low = exe.lower()
                if low.startswith(windir) or "\\windowsapps\\" in low or low in seen:
                    continue
                seen.add(low)
                name = _file_description(exe) or Path(exe).stem
                if _NOISE_NAME.search(name) or Path(exe).stem.lower().startswith(("unins", "setup", "update")):
                    continue
                entries.append(
                    AppEntry(
                        id=stable_app_id(name),
                        name=name,
                        launch_type=LaunchType.EXECUTABLE.value,
                        target=exe,
                        source=Source.REGISTRY.value,
                        exe_path=exe,
                    )
                )
    return entries


def _file_description(exe: str) -> str:
    try:
        import win32api

        lang, codepage = win32api.GetFileVersionInfo(exe, "\\VarFileInfo\\Translation")[0]
        return (win32api.GetFileVersionInfo(exe, f"\\StringFileInfo\\{lang:04x}{codepage:04x}\\FileDescription") or "").strip()
    except Exception:  # noqa: BLE001 - version info is optional
        return ""


_PRIORITY = {
    LaunchType.SHORTCUT.value: 0,
    LaunchType.EXECUTABLE.value: 1,
    LaunchType.AUMID.value: 2,
    LaunchType.COMMAND.value: 3,
}


def deduplicate(entries: list[AppEntry], report: ScanReport | None = None) -> list[AppEntry]:
    """Merge entries sharing a normalized name (or executable); prefer shortcuts, then user-scope."""
    ranked = sorted(
        entries,
        key=lambda e: (_PRIORITY.get(e.launch_type, 9), 0 if e.source == Source.START_MENU_USER.value else 1),
    )
    by_name: dict[str, AppEntry] = {}
    by_exe: dict[str, AppEntry] = {}
    for entry in ranked:
        key = normalize_name(entry.name)
        exe_key = os.path.normcase(entry.exe_path) if entry.exe_path else ""
        existing = by_name.get(key)
        if existing is None and exe_key and entry.source == Source.REGISTRY.value:
            existing = by_exe.get(exe_key)  # registry entries only ever add apps, never split one
        if existing is not None:
            if report is not None:
                report.merged.append((existing.name, f"{entry.name} [{entry.launch_type}/{entry.source}]"))
            if not existing.exe_path and entry.exe_path:
                existing.exe_path = entry.exe_path
            continue
        by_name[key] = entry
        if exe_key:
            by_exe.setdefault(exe_key, entry)
    return list(by_name.values())


# Scan loop label -> the AppEntry.source values that loop produces.
SOURCES_BY_LABEL: dict[str, tuple[str, ...]] = {
    "start_menu": (Source.START_MENU_USER.value, Source.START_MENU_SYSTEM.value),
    "packaged": (Source.PACKAGED.value,),
    "registry": (Source.REGISTRY.value,),
}


def scan_applications(engine: CategoryEngine | None = None) -> ScanReport:
    """Run all discovery sources and return a deduplicated, categorized report."""
    report = ScanReport()
    engine = engine or CategoryEngine()
    collected: list[AppEntry] = []
    for label, func in (("start_menu", scan_start_menu), ("packaged", scan_packaged), ("registry", scan_registry)):
        errors_before = len(report.errors)
        try:
            found = func(report)
        except Exception as exc:  # noqa: BLE001 - a failing source must not hide the others
            log.exception("Scan source %s failed", label)
            report.errors.append(f"{label}: {exc}")
            found = []
        if not found and len(report.errors) > errors_before:  # raised, or reported its own failure
            report.failed_sources.update(SOURCES_BY_LABEL[label])
        report.counts[label] = len(found)
        collected.extend(found)
    report.apps = deduplicate(collected, report)
    for app in report.apps:
        app.category = engine.classify(app)
    report.apps.sort(key=lambda a: a.name.lower())
    report.counts["total"] = len(report.apps)
    return report


if __name__ == "__main__":  # manual check: python -m cirana.core.application_scanner
    import time

    t0 = time.perf_counter()
    rep = scan_applications()
    print(f"{rep.counts}  in {time.perf_counter() - t0:.1f}s")
    for item in rep.apps[:20]:
        print(f"{item.name:40s} {item.launch_type:10s} {item.category:14s} {item.target[:70]}")
    print("merged:", len(rep.merged), rep.merged[:8])
    print("errors:", rep.errors)
