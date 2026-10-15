"""Live inventory of the application windows on the desktop.

Three parts, so nothing ever runs on (or blocks) the GUI thread except bookkeeping:

* a **hook thread** installs out-of-context WinEvent hooks (``SetWinEventHook`` with ``WINEVENT_OUTOFCONTEXT``: the
  events are delivered to this thread's message loop, nothing is loaded into other processes) and only notes which
  window changed;
* a **probe thread** coalesces those notes (a dragged window reports dozens of moves a second), describes the windows
  through the backend and, every few seconds, re-enumerates everything with ``EnumWindows`` to catch missed events;
* :class:`WindowInventory` (GUI thread) turns the results into added / changed / removed signals. A handle that now
  belongs to another process (or a reused pid) is reported as the old window removed and a new one added.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Protocol

from PySide6.QtCore import QObject, Signal, Slot

from cirana.wm.identity import TrackedWindow

log = logging.getLogger(__name__)

DEBOUNCE = 0.04  # seconds a burst of events is collected before the windows are described
RECONCILE_INTERVAL = 3.0  # full EnumWindows pass (missed or coalesced-away events)


class Backend(Protocol):  # what the tracker needs from cirana.wm.backend.Win32Backend (or a test double)
    def enum_windows(self) -> list[int]: ...
    def describe(self, hwnd: int) -> TrackedWindow | None: ...
    def is_top_level(self, hwnd: int) -> bool: ...


class WindowInventory:
    """The known windows by handle, in z-order of the last full pass. Pure bookkeeping (no Win32, no Qt)."""

    def __init__(self) -> None:
        self._by_hwnd: dict[int, TrackedWindow] = {}
        self._order: list[int] = []

    def windows(self) -> list[TrackedWindow]:
        ranked = {hwnd: i for i, hwnd in enumerate(self._order)}
        return sorted(self._by_hwnd.values(), key=lambda win: ranked.get(win.hwnd, -1))

    def get(self, key: str) -> TrackedWindow | None:
        hwnd = int(key.split(":", 1)[0], 16) if key else 0
        window = self._by_hwnd.get(hwnd)
        return window if window is not None and window.key == key else None

    def by_hwnd(self, hwnd: int) -> TrackedWindow | None:
        return self._by_hwnd.get(hwnd)

    def hwnds(self) -> frozenset[int]:
        return frozenset(self._by_hwnd)

    def apply(self, hwnd: int, window: TrackedWindow | None) -> list[tuple[str, TrackedWindow]]:
        """One window's new state (None: gone or no longer an application window). Returns the changes as
        ("added" | "removed" | "changed", window) pairs; "removed" carries the last known snapshot."""
        old = self._by_hwnd.get(hwnd)
        if window is None:
            if old is None:
                return []
            del self._by_hwnd[hwnd]
            return [("removed", old)]
        self._by_hwnd[hwnd] = window
        if old is None:
            if hwnd not in self._order:
                self._order.insert(0, hwnd)  # new windows open on top
            return [("added", window)]
        if old.key != window.key:  # the handle (or the pid) was recycled: a different window
            return [("removed", old), ("added", window)]
        return [("changed", window)] if old != window else []

    def reconcile(self, order: list[int], found: dict[int, TrackedWindow | None]) -> list[tuple[str, TrackedWindow]]:
        """A full pass: ``order`` is every top-level handle in z-order, ``found`` their states."""
        changes: list[tuple[str, TrackedWindow]] = []
        for hwnd in list(self._by_hwnd):
            if found.get(hwnd) is None:
                changes += self.apply(hwnd, None)
        for hwnd in order:
            window = found.get(hwnd)
            if window is not None:
                changes += self.apply(hwnd, window)
        self._order = [hwnd for hwnd in order if hwnd in self._by_hwnd]
        return changes


class _Relay(QObject):
    """Lives on the GUI thread; signals emitted from the probe thread are queued to it."""

    batch = Signal(object)
    move_size = Signal(int, bool)  # hwnd, started (True) / ended (False)


class _HookThread(threading.Thread):
    """Owns the WinEvent hooks and their message loop. ``sink(event, hwnd)`` must be fast and thread-safe."""

    def __init__(self, sink: Callable[[int, int], None], wants: Callable[[int, int], bool]) -> None:
        super().__init__(name="cirana-winevents", daemon=True)
        self._sink = sink
        self._wants = wants
        self._thread_id = 0
        self._ready = threading.Event()
        self._callback = None  # the ctypes callback must stay referenced while the hooks exist

    def run(self) -> None:
        import ctypes

        from cirana.native import win32_api as w

        self._thread_id = int(w.GetCurrentThreadId())

        def on_event(_hook, event, hwnd, id_object, id_child, _thread, _time):  # noqa: ANN001 - ctypes callback
            try:
                if hwnd and id_object == w.OBJID_WINDOW and id_child == w.CHILDID_SELF and self._wants(event, hwnd):
                    self._sink(int(event), int(hwnd))
            except Exception:  # noqa: BLE001 - an exception must never unwind into Windows
                log.exception("WinEvent callback failed")

        self._callback = w.WINEVENTPROC(on_event)
        hooks = []
        for low, high in ((w.EVENT_SYSTEM_FOREGROUND, w.EVENT_SYSTEM_FOREGROUND),
                          (w.EVENT_SYSTEM_MOVESIZESTART, w.EVENT_SYSTEM_MOVESIZEEND),
                          (w.EVENT_SYSTEM_MINIMIZESTART, w.EVENT_SYSTEM_MINIMIZEEND),
                          (w.EVENT_OBJECT_CREATE, w.EVENT_OBJECT_HIDE),
                          (w.EVENT_OBJECT_LOCATIONCHANGE, w.EVENT_OBJECT_NAMECHANGE),
                          (w.EVENT_OBJECT_CLOAKED, w.EVENT_OBJECT_UNCLOAKED)):
            hook = w.SetWinEventHook(low, high, None, self._callback, 0, 0,
                                     w.WINEVENT_OUTOFCONTEXT | w.WINEVENT_SKIPOWNPROCESS)
            if hook:
                hooks.append(hook)
            else:
                log.warning("SetWinEventHook(%#x-%#x) failed (error %d)", low, high, w.last_error())
        self._ready.set()
        msg = w.wintypes.MSG()
        try:
            while w.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                w.TranslateMessage(ctypes.byref(msg))
                w.DispatchMessageW(ctypes.byref(msg))
        finally:
            for hook in hooks:
                w.UnhookWinEvent(hook)  # on the thread that installed them

    def stop(self, timeout: float = 2.0) -> None:
        from cirana.native import win32_api as w

        self._ready.wait(timeout)
        if self._thread_id:
            w.PostThreadMessageW(self._thread_id, w.WM_QUIT, 0, 0)
        self.join(timeout)


class _ProbeThread(threading.Thread):
    """Describes changed windows (coalesced) and does the periodic full pass. Results go to ``emit``."""

    def __init__(self, backend: Backend, emit: Callable[[object], None], interval: float) -> None:
        super().__init__(name="cirana-window-probe", daemon=True)
        self._backend = backend
        self._emit = emit
        self._interval = interval
        self._cond = threading.Condition()
        self._dirty: set[int] = set()
        self._foreground = 0
        self._full = True  # the first pass builds the inventory
        self._stopping = False

    def mark(self, hwnd: int, foreground: bool = False) -> None:
        with self._cond:
            self._dirty.add(hwnd)
            if foreground:
                self._foreground = hwnd
            self._cond.notify()

    def request_full(self) -> None:
        with self._cond:
            self._full = True
            self._cond.notify()

    def stop(self, timeout: float = 2.0) -> None:
        with self._cond:
            self._stopping = True
            self._cond.notify()
        self.join(timeout)

    def run(self) -> None:
        next_full = 0.0
        while True:
            with self._cond:
                wait = max(0.0, next_full - time.monotonic())
                if not (self._dirty or self._full or self._stopping):
                    self._cond.wait(wait)
                if self._stopping:
                    return
                if self._dirty and not self._full:
                    self._cond.wait(DEBOUNCE)  # let the burst finish (a drag reports many moves)
                    if self._stopping:
                        return
                dirty, self._dirty = self._dirty, set()
                foreground, self._foreground = self._foreground, 0
                full = self._full or time.monotonic() >= next_full
                self._full = False
            try:
                if full:
                    order = self._backend.enum_windows()
                    self._emit(("full", order, {hwnd: self._describe(hwnd) for hwnd in order}, foreground))
                    next_full = time.monotonic() + self._interval
                elif dirty:
                    self._emit(("partial", None, {hwnd: self._describe(hwnd) for hwnd in dirty}, foreground))
            except Exception:  # noqa: BLE001 - the probe thread must survive anything a window does
                log.exception("Window probe pass failed")
                next_full = time.monotonic() + self._interval

    def _describe(self, hwnd: int) -> TrackedWindow | None:
        try:
            return self._backend.describe(hwnd)
        except Exception:  # noqa: BLE001
            log.debug("Could not describe window %#x", hwnd, exc_info=True)
            return None


class WindowTracker(QObject):
    """Signals carry :class:`TrackedWindow` snapshots and are always delivered on the GUI thread."""

    window_added = Signal(object)
    window_removed = Signal(object)  # the last known snapshot
    window_changed = Signal(object)
    foreground_changed = Signal(str)  # key of the new foreground application window ("" = something else)
    # The user started (True) or finished (False) dragging or resizing a window with the mouse. Only the user's
    # modal move loop reports these: SetWindowPos (CIRANA's own moves) never does.
    move_size = Signal(object, bool)
    inventory_ready = Signal()  # the first full pass is in

    def __init__(self, backend: Backend | None = None, hooks: bool = True,
                 interval: float = RECONCILE_INTERVAL) -> None:
        super().__init__()
        if backend is None:
            from cirana.wm.backend import Win32Backend

            backend = Win32Backend()
        self.backend = backend
        self.inventory = WindowInventory()
        self.foreground_key = ""
        self.ready = False
        self._use_hooks = hooks
        self._interval = interval
        self._relay = _Relay()
        self._relay.batch.connect(self._on_batch)
        self._relay.move_size.connect(self._on_move_size)
        self._known: frozenset[int] = frozenset()  # read by the hook thread (replaced, never mutated)
        self._probe: _ProbeThread | None = None
        self._hooks: _HookThread | None = None

    # -- lifecycle ---------------------------------------------------------------------------------------------
    def start(self) -> None:
        if self._probe is not None:
            return
        self._probe = _ProbeThread(self.backend, self._relay.batch.emit, self._interval)
        self._probe.start()
        if self._use_hooks:
            self._hooks = _HookThread(self._on_event, self._wants)
            self._hooks.start()

    def stop(self) -> None:
        if self._hooks is not None:
            self._hooks.stop()
            self._hooks = None
        if self._probe is not None:
            self._probe.stop()
            self._probe = None

    def refresh(self) -> None:
        """Re-enumerate everything soon (e.g. after launching an application)."""
        if self._probe is not None:
            self._probe.request_full()

    # -- queries (GUI thread) ----------------------------------------------------------------------------------
    def windows(self) -> list[TrackedWindow]:
        return self.inventory.windows()

    def get(self, key: str) -> TrackedWindow | None:
        return self.inventory.get(key)

    # -- hook thread -------------------------------------------------------------------------------------------
    def _wants(self, event: int, hwnd: int) -> bool:
        from cirana.native import win32_api as w

        if event == w.EVENT_OBJECT_DESTROY:
            return hwnd in self._known  # child windows die by the thousand; only known windows matter
        return event == w.EVENT_SYSTEM_FOREGROUND or hwnd in self._known or self.backend.is_top_level(hwnd)

    def _on_event(self, event: int, hwnd: int) -> None:
        from cirana.native import win32_api as w

        if event in (w.EVENT_SYSTEM_MOVESIZESTART, w.EVENT_SYSTEM_MOVESIZEEND):
            self._relay.move_size.emit(hwnd, event == w.EVENT_SYSTEM_MOVESIZESTART)  # queued to the GUI thread
        if self._probe is not None:
            self._probe.mark(hwnd, foreground=event == w.EVENT_SYSTEM_FOREGROUND)

    @Slot(int, bool)
    def _on_move_size(self, hwnd: int, started: bool) -> None:
        window = self.inventory.by_hwnd(hwnd)
        if window is not None:
            self.move_size.emit(window, started)

    # -- results (GUI thread) ----------------------------------------------------------------------------------
    @Slot(object)
    def _on_batch(self, batch: object) -> None:
        kind, order, found, foreground = batch  # type: ignore[misc]
        if kind == "full":
            changes = self.inventory.reconcile(order, found)
        else:
            changes = [change for hwnd, window in found.items() for change in self.inventory.apply(hwnd, window)]
        self.apply_changes(changes)
        if foreground:
            window = self.inventory.by_hwnd(foreground)
            key = window.key if window is not None else ""
            if key != self.foreground_key:
                self.foreground_key = key
                self.foreground_changed.emit(key)
        if kind == "full" and not self.ready:
            self.ready = True
            self.inventory_ready.emit()

    def apply_changes(self, changes: list[tuple[str, TrackedWindow]]) -> None:
        self._known = self.inventory.hwnds()
        for kind, window in changes:
            {"added": self.window_added, "removed": self.window_removed, "changed": self.window_changed}[kind].emit(window)

    # -- direct feeding (tests, or a caller that already described windows) ------------------------------------
    def feed(self, hwnd: int, window: TrackedWindow | None) -> None:
        self.apply_changes(self.inventory.apply(hwnd, window))

    def feed_snapshot(self, windows: list[TrackedWindow]) -> None:
        self._on_batch(("full", [win.hwnd for win in windows], {win.hwnd: win for win in windows}, 0))
