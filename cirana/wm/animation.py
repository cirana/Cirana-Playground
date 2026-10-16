"""Window animation: a purely visual layer between "where the window is" and "where the layout wants it".

A re-tile that moves windows first glides them to their targets (``run_animation``), then the normal placement
(``WindowController.place_all``) puts every window at its exact target and verifies it. So the final coordinates, the
minimum-size learning (``CONSTRAINED``) and every decision of the layout code are the same with and without
animation; a window that cannot be animated (minimized, maximized, full screen, on a monitor with another scale,
refused, not responding, gone) simply is not part of it and is placed at once.

Frames are time based (progress = elapsed / duration, eased), at about ``FPS`` per second: a late frame jumps to
where the clock says the window should be instead of queueing the frames it missed, and a frame equal to the
previous one is not posted. It runs on the worker thread of the re-tile (never the GUI thread); posting a frame is
the controller's asynchronous ``SetWindowPos``, so a hung application cannot block it. Cancelling (a newer layout,
a drag, quitting) stops it between two frames; the next animation starts from wherever the windows are.

The interpolation (``run_animation``) is pure apart from the callbacks it is given (no Win32, no Qt), so it is
tested with a fake clock; ``glide_placements`` / ``snap_to_targets`` are the thin layer that connects it to the
controller. It knows nothing about layouts: it moves windows between two rectangles.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from cirana.wm.controller import TOLERANCE
from cirana.wm.identity import Rect, TrackedWindow
from cirana.wm.monitors import monitor_for_rect

log = logging.getLogger(__name__)

FPS = 30
DEFAULT_MS = 180
MAX_MS = 350
BORDER_MS = 120  # focus border colour change
PREVIEW_MS = 100  # drop preview moving between targets
RECHECK = 1  # every frame the windows are checked again (closed, handle reused, not responding)


def ease_out_cubic(t: float) -> float:
    t = min(1.0, max(0.0, t))
    return 1.0 - (1.0 - t) ** 3


def lerp_rect(start: Rect, end: Rect, progress: float) -> Rect:
    return tuple(round(a + (b - a) * progress) for a, b in zip(start, end))  # type: ignore[return-value]


def duration_ms(settings: Any) -> int:
    """The animation length from the settings: 0 = off (the old, immediate placement)."""
    if settings is None or settings.get("animations") is False:
        return 0
    value = settings.get("animation_ms")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return DEFAULT_MS
    return int(min(MAX_MS, max(0, value)))


def flag(settings: Any, key: str, ms: int = 1) -> int:
    """``ms`` when the animation ``key`` ("animation_border" / "animation_preview") is on, else 0."""
    if settings is None or settings.get("animations") is False or settings.get(key) is False:
        return 0
    return ms if duration_ms(settings) > 0 else 0


@dataclass
class Move:
    window: TrackedWindow
    start: Rect  # outer window rectangle now
    end: Rect  # outer window rectangle at the target


def run_animation(moves: list[Move], post: Callable[[Move, Rect], bool], cancel: threading.Event, duration: float,
                  check: Callable[[Move], bool] | None = None, fps: int = FPS,
                  clock: Callable[[], float] = time.monotonic) -> dict[str, int]:
    """Glide ``moves`` for ``duration`` seconds. ``post(move, rect)`` sends one frame (False: the window refused or is
    gone - it leaves the animation); ``check(move)`` says whether it may still be touched. The last frame (the exact
    target) is not sent here: the placement that follows does that. Returns ``{"frames", "posts", "dropped",
    "cancelled"}``."""
    stats = {"frames": 0, "posts": 0, "dropped": 0, "cancelled": 0}
    active = list(moves)
    last: dict[int, Rect] = {}
    interval = 1.0 / max(1, fps)
    begin = clock()
    tick = begin
    while active and duration > 0:
        if cancel.is_set():
            stats["cancelled"] = 1
            break
        elapsed = clock() - begin
        if elapsed >= duration:
            break
        progress = ease_out_cubic(elapsed / duration)
        for move in list(active):
            if check is not None and stats["frames"] % RECHECK == RECHECK - 1 and not check(move):
                active.remove(move)
                stats["dropped"] += 1
                continue
            rect = lerp_rect(move.start, move.end, progress)
            if rect == last.get(move.window.hwnd):
                continue  # nothing new to say
            if cancel.is_set():  # never post a frame after a newer layout took over
                stats["cancelled"] = 1
                return stats
            if not post(move, rect):
                active.remove(move)
                stats["dropped"] += 1
                continue
            last[move.window.hwnd] = rect
            stats["posts"] += 1
        stats["frames"] += 1
        tick += interval
        now = clock()
        if tick < now:
            tick = now  # late: do not try to catch up frame by frame
        if cancel.wait(max(0.0, tick - now)):
            stats["cancelled"] = 1
            break
    return stats


def glide_placements(backend, controller, placements, cancel: threading.Event, milliseconds: int,  # noqa: ANN001
                     animated: dict[int, tuple[TrackedWindow, Rect]]) -> None:
    """Glide the windows of ``placements`` (``Placement`` of window + target frame) towards their targets - the worker
    thread part of a re-tile, before ``WindowController.place_all`` sets the exact positions. Windows that cannot be
    animated safely are left out (minimized, maximized, off screen, full screen, on a monitor with another scale where
    the application resizes itself, already there); the ones that move are noted in ``animated`` (window, outer
    target) until the placement has confirmed them. An animation problem never costs the placement."""
    try:
        monitors = backend.monitors()
        moves = []
        for placement in placements:
            window, hwnd = placement.window, placement.window.hwnd
            if backend.is_minimized(hwnd) or backend.is_maximized(hwnd):
                continue
            start = backend.frame_rect(hwnd)
            here, there = monitor_for_rect(start, monitors), monitor_for_rect(placement.rect, monitors)
            if here is None or there is None or here.dpi != there.dpi or start == here.rect \
                    or max(abs(a - b) for a, b in zip(start, placement.rect)) <= TOLERANCE:
                continue
            moves.append(Move(window, backend.window_rect(hwnd), controller.outer_frame(window, placement.rect)))
        if moves:
            for move in moves:
                animated[move.window.hwnd] = (move.window, move.end)
            run_animation(moves, lambda m, rect: controller.nudge(m.window, rect), cancel, milliseconds / 1000.0,
                          lambda m: backend.still_same(m.window) and not backend.is_hung(m.window.hwnd))
    except Exception:  # noqa: BLE001
        log.debug("Window animation failed", exc_info=True)


def snap_to_targets(controller, animated: dict[int, tuple[TrackedWindow, Rect]]) -> None:  # noqa: ANN001
    """Windows an animation left half way get their exact places at once (tiling stops, CIRANA quits). Only windows
    that are still the same ones and may be touched."""
    for window, outer in list(animated.values()):
        try:
            if controller.check(window) is None:
                controller.nudge(window, outer)
        except Exception:  # noqa: BLE001 - one window's problem never keeps the others half way
            log.debug("Could not finish a window animation", exc_info=True)
    animated.clear()
