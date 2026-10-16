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

Pure apart from the callbacks it is given (no Win32, no Qt), so it is tested with a fake clock.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from cirana.wm.identity import Rect, TrackedWindow

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
