"""Run by tests/test_wm_live.py (CIRANA_LIVE=1): all-window tiling of REAL windows that keep a minimum size
(tests/hang_window_helper.py answers WM_GETMINMAXINFO; only CIRANA's own test windows are asked anything), printed
as JSON: where each window was put, what Windows reports for it, and what the tiler said.

    python -m tests.live_minimum_script <data dir>
"""
import ctypes
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

from PySide6.QtWidgets import QApplication

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]


def main() -> None:
    home = Path(sys.argv[1])
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    app = QApplication([])

    from cirana.core.application_registry import ApplicationRegistry
    from cirana.core.dynamic_tiling import DynamicTiler
    from cirana.core.workspace_controller import WorkspaceController
    from cirana.core.workspace_launcher import WorkspaceLauncher
    from cirana.core.workspaces import WorkspaceManager
    from cirana.storage.settings_manager import SettingsManager
    from cirana.wm.controller import WindowController
    from cirana.wm.layout import SIDE_BY_SIDE, LayoutSpec
    from cirana.wm.monitors import monitor_for_rect
    from cirana.wm.tracker import WindowTracker

    def wait(condition, timeout=8.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            app.processEvents()
            if condition():
                return True
            time.sleep(0.02)
        return False

    def settled(tiler):
        quiet = [None]

        def check():
            if tiler._timer.isActive() or tiler._job is not None:
                quiet[0] = None
            elif quiet[0] is None:
                quiet[0] = time.monotonic()
            return quiet[0] is not None and time.monotonic() - quiet[0] > 0.8
        return wait(check, 15)

    settings = SettingsManager(home)
    tracker = WindowTracker(hooks=True, interval=60)
    tracker.start()
    wait(lambda: tracker.ready)
    helper = os.path.basename(sys.executable).lower()
    settings.set("tiling_exclusions", sorted({x.exe_name.lower() for x in tracker.windows()} - {helper, ""}))
    helpers = []

    def open_helper(minimum):
        args = [sys.executable, "-m", "tests.hang_window_helper", str(home / "x"), "1"] + ([minimum] if minimum else [])
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, text=True, cwd=root)
        helpers.append((proc, int(proc.stdout.readline().split()[1])))
        return helpers[-1][1]

    def report(hwnds):
        rows = []
        for hwnd in hwnds:
            client = wintypes.RECT()
            user32.GetClientRect(hwnd, ctypes.byref(client))
            window = tracker.inventory.by_hwnd(hwnd)
            rows.append({"frame": tracker.backend.frame_rect(hwnd), "outer": tracker.backend.window_rect(hwnd),
                         "client": [client.right, client.bottom],
                         "target": tiler.target_of(window.key) if window else None,
                         "reason": tiler.float_reason(window) if window else ""})
        return rows

    out = {}
    try:
        wide, plain1, plain2 = open_helper("900x300"), open_helper(""), open_helper("")
        wait(lambda: all(tracker.inventory.by_hwnd(h) for _p, h in helpers))
        registry = ApplicationRegistry(settings)
        manager = WorkspaceManager(settings, registry, monitors=tracker.backend.monitors)
        wm = WorkspaceController(manager, registry, tracker, WindowController(tracker.backend),
                                 WorkspaceLauncher(registry, tracker))
        messages = []
        wm.message.connect(messages.append)
        tiler = DynamicTiler(wm, settings)
        tiler.set_global_layout(LayoutSpec(SIDE_BY_SIDE, gap=12))
        tiler.set_global(True)
        settled(tiler)
        monitor = monitor_for_rect(tracker.backend.frame_rect(wide), tracker.backend.monitors())
        out["work"] = monitor.work
        out["tiled"] = report([wide, plain1, plain2])
        out["count"] = tiler.count
        # one that is wider than the whole monitor: it floats, its top-left corner on the screen
        huge = open_helper(f"{(monitor.work[2] - monitor.work[0]) * 2}x200")
        wait(lambda: tracker.inventory.by_hwnd(huge) is not None)
        settled(tiler)
        out["with_huge"] = report([wide, plain1, plain2, huge])
        out["messages"] = messages
        tiler.stop()
    finally:
        for proc, hwnd in helpers:
            user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE, our own helpers only
            proc.wait(10)
            proc.stdout.close()
        tracker.stop()
    print(json.dumps(out))


if __name__ == "__main__":
    main()
