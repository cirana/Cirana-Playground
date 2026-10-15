"""Application icon provider: shell icons, cached on disk and in memory, with a letter-tile fallback."""
from __future__ import annotations

import logging
import os
from pathlib import Path

import time
from collections import deque
from typing import Callable

import shiboken6
from PySide6.QtCore import QFileInfo, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QApplication, QFileIconProvider

from cirana.core.models import AppEntry, LaunchType
from cirana.native.shell_icons import aumid_icon_bgra, file_icon_bgra
from cirana.ui.theme import TEXT

log = logging.getLogger(__name__)

# Letter-tile fallbacks: muted, dark fills so a missing icon never shouts louder than a real one.
_TILE_COLORS = ["#3A4A2C", "#2C3A52", "#22463F", "#4E3329", "#43304D", "#4A4026", "#28414F"]
QObjectLike = object
_BASE = 96  # cached icons are stored at 64px and scaled down by the card


class AppIconProvider:
    """Must be used from the GUI thread (QPixmap / QFileIconProvider)."""

    def __init__(self, cache_dir: Path) -> None:
        self._dir = cache_dir
        self._memory: dict[str, QPixmap] = {}
        self._provider = QFileIconProvider()
        self._queue: deque[tuple[AppEntry, int, QObjectLike, Callable[[QPixmap], None]]] = deque()
        self._timer = QTimer()
        self._timer.setInterval(0)
        self._timer.timeout.connect(self._drain)

    def request(self, app: AppEntry, size: int, owner, setter: Callable[[QPixmap], None]) -> None:
        """Deliver an icon to ``setter``: immediately when cached, otherwise in time-boxed batches
        so that extracting hundreds of icons never freezes the UI."""
        if app.id in self._memory:
            setter(self.pixmap(app, size))
            return
        self._queue.append((app, size, owner, setter))
        if not self._timer.isActive():
            self._timer.start()

    def _drain(self) -> None:
        deadline = time.perf_counter() + 0.012
        while self._queue and time.perf_counter() < deadline:
            app, size, owner, setter = self._queue.popleft()
            if shiboken6.isValid(owner):
                setter(self.pixmap(app, size))
        if not self._queue:
            self._timer.stop()

    def pixmap(self, app: AppEntry, size: int = 40) -> QPixmap:
        base = self._memory.get(app.id)
        if base is None:
            base = self._load(app)
            self._memory[app.id] = base
        dpr = QApplication.primaryScreen().devicePixelRatio() if QApplication.primaryScreen() else 1.0
        device = int(size * max(dpr, 1.0))
        scaled = base.scaled(device, device, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        scaled.setDevicePixelRatio(max(dpr, 1.0))
        return scaled

    def invalidate(self, app_id: str) -> None:
        self._memory.pop(app_id, None)
        (self._dir / f"{app_id}.png").unlink(missing_ok=True)

    # -- internals ---------------------------------------------------------
    def _cache_file(self, app: AppEntry) -> Path:
        return self._dir / f"{app.id}.png"

    def _load(self, app: AppEntry) -> QPixmap:
        cached = self._cache_file(app)
        source = app.icon_source
        # Reuse the disk cache only if the source file has not changed since it was written.
        if cached.exists() and not (source and os.path.exists(source) and os.path.getmtime(source) > cached.stat().st_mtime):
            pix = QPixmap(str(cached))
            if not pix.isNull():
                return pix
        pix = self._extract(app)
        if pix is None or pix.isNull():
            return self._letter_tile(app.name, app.id)  # not cached: retried next session
        try:
            pix.save(str(cached), "PNG")
        except Exception:  # noqa: BLE001
            log.debug("Could not cache icon for %s", app.name)
        return pix

    def _extract(self, app: AppEntry) -> QPixmap | None:
        source = app.icon_source
        raw = None
        if source and os.path.exists(source):
            raw = file_icon_bgra(source)
        elif app.launch_type == LaunchType.AUMID.value:
            raw = aumid_icon_bgra(app.target)
        if raw:
            pixels, width, height = raw
            image = self._crop_to_content(QImage(pixels, width, height, width * 4, QImage.Format.Format_ARGB32).copy())
            return QPixmap.fromImage(image).scaled(
                _BASE, _BASE, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
            )
        if source and os.path.exists(source):  # Qt fallback when the shell image list is unavailable
            icon = self._provider.icon(QFileInfo(source))
            if not icon.isNull():
                pix = icon.pixmap(_BASE, _BASE)
                if not pix.isNull():
                    return pix
        return None

    @staticmethod
    def _crop_to_content(image: QImage) -> QImage:
        """Some apps only ship tiny icons that the shell centers on a large transparent canvas; zoom those in."""
        grid = 32
        small = image.scaled(grid, grid, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
        xs, ys = [], []
        for y in range(grid):
            for x in range(grid):
                if small.pixelColor(x, y).alpha() > 24:
                    xs.append(x)
                    ys.append(y)
        if not xs:
            return image
        left, right, top, bottom = min(xs), max(xs) + 1, min(ys), max(ys) + 1
        if (right - left) > grid * 0.62 and (bottom - top) > grid * 0.62:
            return image
        scale = image.width() / grid
        side = max(right - left, bottom - top) * scale * 1.25
        cx, cy = (left + right) / 2 * scale, (top + bottom) / 2 * scale
        x0 = max(0, min(image.width() - side, cx - side / 2))
        y0 = max(0, min(image.height() - side, cy - side / 2))
        return image.copy(int(x0), int(y0), int(min(side, image.width())), int(min(side, image.height())))

    @staticmethod
    def _letter_tile(name: str, key: str) -> QPixmap:
        pix = QPixmap(_BASE, _BASE)
        pix.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor(_TILE_COLORS[sum(map(ord, key)) % len(_TILE_COLORS)])
        path = QPainterPath()
        path.addRoundedRect(QRectF(2, 2, _BASE - 4, _BASE - 4), 14, 14)
        painter.fillPath(path, color)
        font = QFont("Segoe UI")
        font.setPixelSize(32)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(TEXT))
        letter = next((c for c in name if c.isalnum()), "?").upper()
        painter.drawText(QRectF(0, 0, _BASE, _BASE), Qt.AlignmentFlag.AlignCenter, letter)
        painter.end()
        return pix
