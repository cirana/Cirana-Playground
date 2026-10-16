"""Komorebi-style visual aids for dynamic tiling: a border around every tiled window (green for the focused one,
grey for the others) and a grey drop preview while a tiled window is dragged.

Both are CIRANA's own small top-level windows, drawn next to (never inside) the applications:

* frameless *tool* windows (no taskbar button, not in Alt+Tab), transparent for the mouse and the keyboard
  (``Qt.WindowTransparentForInput``: WS_EX_TRANSPARENT | WS_EX_LAYERED), never activated
  (``WindowDoesNotAcceptFocus`` / ``WA_ShowWithoutActivating``), so they cannot take a click or the focus;
* they belong to CIRANA's process, which the window tracker skips (its hooks use WINEVENT_SKIPOWNPROCESS and its
  filter drops CIRANA's own windows): they never enter the inventory and cannot trigger tiling;
* only CIRANA's own overlay windows are positioned (SetWindowPos on their own handle, in physical pixels, so mixed
  DPI monitors line up); the applications' windows, styles and parents are never touched.

A border sits directly above its window in the z-order (inserted after the window above it), so a window placed
over a tiled one also covers its border. Colours follow WindowTracker.foreground_key / foreground_changed. The shown
window of a stack carries one small tab per stacked window along its top edge (the shown one green), and a window in
monocle gets a border twice as thick, so both modes are visible at a glance.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPainterPath
from PySide6.QtWidgets import QWidget

from cirana.ui.theme import COLORS
from cirana.wm.identity import Rect, TrackedWindow
from cirana.wm.monitors import Monitor, monitor_for_rect

log = logging.getLogger(__name__)

ACTIVE_COLOR = COLORS["accent"]  # #C3E85B
INACTIVE_COLOR = COLORS["border"]  # #2A2D34, the theme's neutral grey
STACK_COLOR = COLORS["text2"]  # the other windows of a stack (a lighter grey than the border)
BORDER_PX = 2  # logical pixels; scaled with the monitor's DPI
PREVIEW_FILL = QColor(143, 148, 157, 72)  # text2 grey, translucent
PREVIEW_LINE = QColor(143, 148, 157, 170)


def _native() -> bool:
    """Real Win32 windows (positioned by handle) - not the offscreen platform of the tests."""
    return QGuiApplication.platformName() == "windows"


class _Overlay(QWidget):
    def __init__(self, on_top: bool = False) -> None:
        flags = (Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowTransparentForInput
                 | Qt.WindowType.WindowDoesNotAcceptFocus | Qt.WindowType.NoDropShadowWindowHint)
        if on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        super().__init__(None, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.rect_px: Rect | None = None  # where it is, in physical screen pixels
        self._scale = 1.0

    def place(self, rect: Rect, scale: float = 1.0, above: int | None = None) -> None:
        """Move to ``rect`` (physical pixels). ``above``: the application window this overlay must sit right above
        in the z-order (None: keep the z-order)."""
        moved = rect != self.rect_px
        rescaled = scale != self._scale
        self.rect_px, self._scale = rect, scale
        if not self.isVisible():
            self.show()
            moved = True
        if not _native():
            if moved:
                self.setGeometry(QRect(rect[0], rect[1], rect[2] - rect[0], rect[3] - rect[1]))
            if rescaled:
                self.update()
            return
        from cirana.native import win32_api as w

        own = int(self.winId())
        insert_after, flags = None, w.SWP_NOACTIVATE | w.SWP_NOOWNERZORDER
        if above is not None:
            previous = int(w.GetWindow(above, w.GW_HWNDPREV) or 0)
            if previous == own:
                flags |= w.SWP_NOZORDER  # already right above it
            else:
                insert_after = previous or w.HWND_TOP
        else:
            flags |= w.SWP_NOZORDER
        if not moved and flags & w.SWP_NOZORDER:
            return
        for _ in range(2 if rescaled else 1):  # a monitor with another scale: Qt resizes once for WM_DPICHANGED
            w.SetWindowPos(own, insert_after, rect[0], rect[1], rect[2] - rect[0], rect[3] - rect[1], flags)
        if rescaled:
            self.update()

    def _px(self, value: float) -> float:
        """Physical pixels in Qt's logical units of this window."""
        return value / max(1.0, self.devicePixelRatioF())


class BorderOverlay(_Overlay):
    def __init__(self) -> None:
        super().__init__()
        self.active = False
        self.thickness = BORDER_PX
        self.stack: tuple[int, int] | None = None  # (index of the shown window, windows in the stack)
        self.monocle = False

    def set_active(self, active: bool) -> None:
        if active != self.active:
            self.active = active
            self.update()  # repaint only when the colour really changes

    def set_mode(self, stack: tuple[int, int] | None, monocle: bool) -> None:
        if (stack, monocle) != (self.stack, self.monocle):
            self.stack, self.monocle = stack, monocle
            self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        width = self._px(self.thickness)
        outer = QRectF(self.rect())
        path = QPainterPath()
        path.addRoundedRect(outer, width * 2, width * 2)
        inner = QPainterPath()
        inner.addRect(outer.adjusted(width, width, -width, -width))
        painter.fillPath(path.subtracted(inner), QColor(ACTIVE_COLOR if self.active else INACTIVE_COLOR))
        if self.stack is not None:  # one tab per stacked window, centred on the top edge
            shown, count = self.stack
            tab, space = width * 10, width * 2
            left = (outer.width() - (count * tab + (count - 1) * space)) / 2
            for index in range(count):
                box = QRectF(left + index * (tab + space), 0, tab, width * 2)
                painter.fillRect(box, QColor(ACTIVE_COLOR if index == shown else STACK_COLOR))
        painter.end()


class PreviewOverlay(_Overlay):
    def __init__(self) -> None:
        super().__init__(on_top=True)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        line = self._px(2)
        box = QRectF(self.rect()).adjusted(line / 2, line / 2, -line / 2, -line / 2)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(PREVIEW_FILL)
        painter.drawRoundedRect(box, self._px(8), self._px(8))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        pen = painter.pen()
        pen.setStyle(Qt.PenStyle.SolidLine)
        pen.setColor(PREVIEW_LINE)
        pen.setWidthF(line)
        painter.setPen(pen)
        painter.drawRoundedRect(box, self._px(8), self._px(8))
        painter.end()


class TilingOverlays(QObject):
    """Keeps one border per tiled window and the drop preview in step with the tiler and the tracker."""

    def __init__(self, tiler, settings=None) -> None:  # noqa: ANN001 - DynamicTiler, SettingsManager
        super().__init__()
        self.tiler = tiler
        self.tracker = tiler.wm.tracker
        self.settings = settings
        self.borders: dict[str, BorderOverlay] = {}
        self.preview = PreviewOverlay()
        self._stopped = False
        self._sync_timer = QTimer(self, singleShot=True, interval=30)  # coalesces bursts of changes
        self._sync_timer.timeout.connect(self.sync)
        tracker = self.tracker
        tracker.foreground_changed.connect(self._on_focus)
        tracker.window_changed.connect(self._on_window_changed)
        tracker.window_added.connect(lambda _w: self.schedule())
        tracker.window_removed.connect(self._on_removed)
        tiler.reflowed.connect(lambda _s: self.schedule())
        tiler.state_changed.connect(lambda _s: self.schedule())
        tiler.drop_preview.connect(self._on_preview)
        tiler.wm.workspaces.active_changed.connect(lambda _w: self.schedule())
        tiler.wm.workspaces.changed.connect(self.schedule)

    # -- settings -----------------------------------------------------------------------------------------------
    @property
    def borders_enabled(self) -> bool:
        return self.settings is None or self.settings.get("tiling_borders") is not False

    def set_borders_enabled(self, on: bool) -> None:
        if self.settings is not None:
            self.settings.set("tiling_borders", bool(on))
        self.sync()

    # -- borders ------------------------------------------------------------------------------------------------
    def schedule(self) -> None:
        if not self._stopped:
            self._sync_timer.start()

    def _monitors(self) -> list[Monitor]:
        try:
            return self.tracker.backend.monitors()
        except Exception:  # noqa: BLE001
            return []

    def _fullscreen(self, window: TrackedWindow, monitors: list[Monitor]) -> bool:
        """Covers a whole monitor, and not because tiling put it there (a tile can fill a monitor whose taskbar
        hides itself)."""
        return any(window.rect == monitor.rect for monitor in monitors) and             self.tiler.target_of(window.key) != window.rect

    def bordered(self) -> list[TrackedWindow]:
        """The windows that get a border: managed by dynamic tiling, not minimized, maximized or full screen."""
        if self._stopped or not self.borders_enabled:
            return []
        monitors = self._monitors()
        return [w for w in self.tiler.managed_windows()
                if not w.minimized and not w.maximized and not self._fullscreen(w, monitors)]

    def sync(self) -> None:
        wanted = {w.key: w for w in self.bordered()}
        for key in [k for k in self.borders if k not in wanted]:
            self._drop(key)
        monitors = self._monitors()
        focused = self.tracker.foreground_key
        for key, window in wanted.items():
            border = self.borders.get(key)
            if border is None:
                border = self.borders[key] = BorderOverlay()
            border.set_active(key == focused)
            found = self.tiler.tile_of(key)
            stack = monocle = None
            if found is not None:
                group, unit = found
                if unit.stacked:
                    stack = ([s for s, _w in unit.members].index(unit.active), len(unit.members))
                monocle = group.monocle == unit.id
            border.set_mode(stack, bool(monocle))
            self._place(border, window, monitors)

    def _place(self, border: BorderOverlay, window: TrackedWindow, monitors: list[Monitor]) -> None:
        monitor = monitor_for_rect(window.rect, monitors)
        scale = monitor.scale if monitor is not None else 1.0
        border.thickness = max(1, round(BORDER_PX * scale)) * (2 if border.monocle else 1)
        t = border.thickness
        left, top, right, bottom = window.rect
        try:
            border.place((left - t, top - t, right + t, bottom + t), scale, above=window.hwnd)
        except Exception:  # noqa: BLE001 - a window that vanished meanwhile: the next event removes its border
            log.debug("Could not place a border", exc_info=True)

    def _drop(self, key: str) -> None:
        border = self.borders.pop(key, None)
        if border is not None:
            border.hide()
            border.deleteLater()

    def _on_removed(self, window: TrackedWindow) -> None:
        self._drop(window.key)
        self.schedule()

    def _on_window_changed(self, window: TrackedWindow) -> None:
        border = self.borders.get(window.key)
        if border is None:
            return
        if window.minimized or window.maximized or self._fullscreen(window, self._monitors()):
            self._drop(window.key)  # at once: no border hanging around a minimized or full-screen window
            self.schedule()
            return
        self._place(border, window, self._monitors())  # follows moves and resizes

    def _on_focus(self, key: str) -> None:
        monitors = None
        for border_key, border in self.borders.items():
            border.set_active(border_key == key)
            if border_key == key:  # the focused window came to the front: its border goes right above it
                window = self.tracker.get(key)
                if window is not None:
                    monitors = monitors or self._monitors()
                    self._place(border, window, monitors)

    # -- drop preview -------------------------------------------------------------------------------------------
    def _on_preview(self, rect: Rect | None) -> None:
        if rect is None or self._stopped:
            self.preview.hide()
            self.preview.rect_px = None
            return
        monitor = monitor_for_rect(rect, self._monitors())
        self.preview.place(rect, monitor.scale if monitor is not None else 1.0)  # stays on top by itself

    # -- shutdown -----------------------------------------------------------------------------------------------
    def clear(self) -> None:
        for key in list(self.borders):
            self._drop(key)
        self.preview.hide()
        self.preview.rect_px = None

    def stop(self) -> None:
        self._stopped = True
        self._sync_timer.stop()
        self.clear()
