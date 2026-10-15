"""Run by tests/test_wm_live.py (CIRANA_LIVE=1) on the real Windows Qt platform: tiles two helper windows with
all-window tiling, shows the focus borders and the drop preview, and prints what Win32 reports about them as JSON.

    python -m tests.live_overlay_script <data dir>
"""
import ctypes
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes
from dataclasses import replace
from pathlib import Path

from PySide6.QtWidgets import QApplication

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindow.restype = wintypes.HWND
user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
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
    from cirana.native import win32_api as w
    from cirana.storage.settings_manager import SettingsManager
    from cirana.ui.tiling_overlay import TilingOverlays
    from cirana.wm.controller import WindowController
    from cirana.wm.layout import SIDE_BY_SIDE, LayoutSpec
    from cirana.wm.tracker import WindowTracker

    def wait(condition, timeout=8.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            app.processEvents()
            if condition():
                return True
            time.sleep(0.02)
        return False

    settings = SettingsManager(home)
    tracker = WindowTracker(hooks=True, interval=60)
    tracker.start()
    wait(lambda: tracker.ready)
    helper = os.path.basename(sys.executable).lower()
    settings.set("tiling_exclusions", sorted({x.exe_name.lower() for x in tracker.windows()} - {helper, ""}))
    helpers = []
    for _ in range(2):
        proc = subprocess.Popen([sys.executable, "-m", "tests.hang_window_helper", str(home / "x"), "1"],
                                stdout=subprocess.PIPE, text=True, cwd=root)
        helpers.append((proc, int(proc.stdout.readline().split()[1])))
    out = {}
    try:
        wait(lambda: all(tracker.inventory.by_hwnd(h) for _p, h in helpers))
        registry = ApplicationRegistry(settings)
        manager = WorkspaceManager(settings, registry, monitors=tracker.backend.monitors)
        wm = WorkspaceController(manager, registry, tracker, WindowController(tracker.backend),
                                 WorkspaceLauncher(registry, tracker))
        tiler = DynamicTiler(wm, settings)
        overlays = TilingOverlays(tiler, settings)
        tiler.set_global_layout(LayoutSpec(SIDE_BY_SIDE, gap=12))
        tiler.set_global(True)
        keys = [tracker.inventory.by_hwnd(h).key for _p, h in helpers]
        wait(lambda: all(k in overlays.borders for k in keys) and all(
            tiler.target_of(k) == tracker.backend.frame_rect(h) for k, (_p, h) in zip(keys, helpers)))
        wait(lambda: False, timeout=0.6)  # location events, border placement
        out["helpers"] = []
        for key, (_proc, hwnd) in zip(keys, helpers):
            border = overlays.borders[key]
            own = int(border.winId())
            frame = tracker.backend.frame_rect(hwnd)
            out["helpers"].append({
                "frame": frame, "border": list(w.window_rect(own)), "thickness": border.thickness,
                "ex_style": int(user32.GetWindowLongPtrW(own, -20)) & 0xFFFFFFFF,
                "above": int(user32.GetWindow(hwnd, 3) or 0) == own,  # GW_HWNDPREV: the border is right above it
                "in_inventory": tracker.inventory.by_hwnd(own) is not None,
                "active": border.active,
            })
        # focus the second helper: its border turns green (foreground events through the real hook)
        user32.SetForegroundWindow(helpers[1][1])
        wait(lambda: overlays.borders[keys[1]].active, timeout=4)
        out["focus"] = [overlays.borders[k].active for k in keys]
        # drop preview (as a drag over the other tile would show it)
        tiler._dragging.add(keys[0])
        dragged = replace(tracker.inventory.by_hwnd(helpers[0][1]), rect=tracker.backend.frame_rect(helpers[1][1]))
        tiler._update_preview(dragged)  # its centre is over the other tile
        app.processEvents()
        overlay_handles = {int(b.winId()) for b in overlays.borders.values()} | {int(overlays.preview.winId())}
        out["foreground_is_overlay"] = int(user32.GetForegroundWindow() or 0) in overlay_handles
        preview = int(overlays.preview.winId())
        out["preview"] = {"visible": overlays.preview.isVisible(), "rect": list(w.window_rect(preview)),
                          "target": tiler.target_of(keys[1]),
                          "ex_style": int(user32.GetWindowLongPtrW(preview, -20)) & 0xFFFFFFFF}
        tiler._dragging.clear()
        tiler._set_preview(None)
        app.processEvents()
        out["preview_hidden"] = not overlays.preview.isVisible()
        tiler.set_global(False)
        wait(lambda: not overlays.borders, timeout=2)
        out["cleared"] = not overlays.borders
        overlays.stop()
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
