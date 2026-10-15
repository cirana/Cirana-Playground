"""Monitors, their work areas (the screen minus the taskbar) and DPI, and finding a saved monitor again.

Coordinates are physical pixels of the virtual screen, the same space SetWindowPos uses in a per-monitor DPI aware
process (Qt 6 makes CIRANA one). QScreen is deliberately not used: it reports logical pixels, which give wrong
positions when monitors have different scale factors.
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
from typing import Any

from cirana.wm.identity import Rect


@dataclass(frozen=True)
class Monitor:
    device: str  # \\.\DISPLAY1 ... (stable while the monitor stays on the same output)
    rect: Rect
    work: Rect  # without the taskbar and docked app bars
    primary: bool = False
    dpi: int = 96

    @property
    def scale(self) -> float:
        return self.dpi / 96.0

    @property
    def label(self) -> str:
        left, top, right, bottom = self.rect
        name = self.device.rsplit("\\", 1)[-1] or self.device
        return f"{name} · {right - left}×{bottom - top}{' · primary' if self.primary else ''}"

    def ref(self) -> "MonitorRef":
        return MonitorRef(self.device, self.rect, self.primary)


@dataclass(frozen=True)
class MonitorRef:
    """A monitor as saved in a workspace. Resolved against the monitors present now (see :func:`resolve`)."""

    device: str = ""
    rect: Rect = (0, 0, 0, 0)
    primary: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"device": self.device, "rect": list(self.rect), "primary": self.primary}

    @classmethod
    def from_dict(cls, raw: Any) -> "MonitorRef | None":
        if not isinstance(raw, dict):
            return None
        rect = raw.get("rect")
        if not (isinstance(rect, list) and len(rect) == 4 and all(isinstance(v, int) and not isinstance(v, bool)
                                                                   for v in rect)):
            rect = [0, 0, 0, 0]
        device = raw.get("device")
        return cls(device if isinstance(device, str) else "", tuple(rect), bool(raw.get("primary")))


def resolve(ref: MonitorRef | None, monitors: list[Monitor]) -> Monitor | None:
    """The monitor a saved reference means now: the same output with the same geometry, then the same output, then
    the same geometry, then the primary monitor (the saved one was unplugged or rearranged), then any."""
    if not monitors:
        return None
    primary = next((m for m in monitors if m.primary), monitors[0])
    if ref is None:
        return primary
    for test in (lambda m: m.device == ref.device and m.rect == ref.rect,
                 lambda m: bool(ref.device) and m.device == ref.device,
                 lambda m: m.rect == ref.rect and ref.rect != (0, 0, 0, 0)):
        found = next((m for m in monitors if test(m)), None)
        if found is not None:
            return found
    return primary


def intersection_area(a: Rect, b: Rect) -> int:
    width = min(a[2], b[2]) - max(a[0], b[0])
    height = min(a[3], b[3]) - max(a[1], b[1])
    return width * height if width > 0 and height > 0 else 0


def monitor_for_rect(rect: Rect, monitors: list[Monitor]) -> Monitor | None:
    """The monitor showing most of ``rect`` (None when it is on none of them)."""
    best = max(monitors, key=lambda m: intersection_area(rect, m.rect), default=None)
    return best if best is not None and intersection_area(rect, best.rect) > 0 else None


def enum_monitors() -> list[Monitor]:
    """The monitors attached right now (Win32: EnumDisplayMonitors + GetMonitorInfoW + GetDpiForMonitor)."""
    from cirana.native import win32_api as w

    handles: list[int] = []

    @w.MONITORENUMPROC
    def collect(handle, _hdc, _rect, _lparam):  # noqa: ANN001 - ctypes callback
        handles.append(handle)
        return True

    w.EnumDisplayMonitors(None, None, collect, 0)
    found: list[Monitor] = []
    for handle in handles:
        info = w.MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(info)
        if not w.GetMonitorInfoW(handle, ctypes.byref(info)):
            continue
        r, k = info.rcMonitor, info.rcWork
        found.append(Monitor(info.szDevice, (r.left, r.top, r.right, r.bottom), (k.left, k.top, k.right, k.bottom),
                             bool(info.dwFlags & w.MONITORINFOF_PRIMARY), w.monitor_dpi(handle)))
    found.sort(key=lambda m: (not m.primary, m.rect[0], m.rect[1]))
    return found
