"""Focus follows the mouse (optional, off by default): the window under the pointer gets the keyboard focus.

Windows does not let a background program take the focus for another window when the user merely moves the mouse
(the foreground lock), and CIRANA does not get around it (no input injection, no attached input queues). It uses
Windows' own documented setting instead, *active window tracking* (``SystemParametersInfo`` with
``SPI_SETACTIVEWINDOWTRACKING``, the "activate a window by hovering over it with the mouse" option of the Ease of
Access settings), with a short delay and without raising the window (both adjustable):

* it is changed for this session only (never written to the user profile) and put back to the user's own values
  when CIRANA closes or the option is switched off;
* the values found before are kept in ``config.json`` (``ffm_restore``) while CIRANA has changed them, so a crash is
  repaired at the next start;
* Windows applies it to every window, not only tiled ones; menus, drags and the pointer resting on the desktop are
  handled by Windows itself, so it cannot cause focus loops.
"""
from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes
from typing import Callable

log = logging.getLogger(__name__)

DEFAULT_DELAY_MS = 150
MAX_DELAY_MS = 2000


class SystemSettings:
    """The three Windows values (real ``SystemParametersInfoW``; tests use a stand-in with the same methods)."""

    def read(self) -> dict[str, int]:
        from cirana.native import win32_api as w

        tracking, raise_ = wintypes.BOOL(0), wintypes.BOOL(0)
        delay = wintypes.DWORD(0)
        w.SystemParametersInfoW(w.SPI_GETACTIVEWINDOWTRACKING, 0, ctypes.byref(tracking), 0)
        w.SystemParametersInfoW(w.SPI_GETACTIVEWNDTRKZORDER, 0, ctypes.byref(raise_), 0)
        w.SystemParametersInfoW(w.SPI_GETACTIVEWNDTRKTIMEOUT, 0, ctypes.byref(delay), 0)
        return {"tracking": int(bool(tracking.value)), "raise": int(bool(raise_.value)), "delay": int(delay.value)}

    def write(self, values: dict[str, int]) -> bool:
        from cirana.native import win32_api as w

        ok = True
        # fWinIni = 0: for this session only, nothing written to the profile and no broadcast to other windows
        for action, value in ((w.SPI_SETACTIVEWNDTRKTIMEOUT, values["delay"]),
                              (w.SPI_SETACTIVEWNDTRKZORDER, values["raise"]),
                              (w.SPI_SETACTIVEWINDOWTRACKING, values["tracking"])):
            ok = bool(w.SystemParametersInfoW(action, 0, ctypes.c_void_p(int(value)), 0)) and ok
        return ok


class FocusFollowsMouse:
    def __init__(self, settings, system: SystemSettings | None = None,  # noqa: ANN001 - SettingsManager
                 notify: Callable[[str], None] | None = None) -> None:
        self.settings = settings
        self.system = system or SystemSettings()
        self.notify = notify or (lambda _text: None)

    @property
    def enabled(self) -> bool:
        return self.settings.get("focus_follows_mouse") is True

    @property
    def delay(self) -> int:
        value = self.settings.get("ffm_delay")
        return min(MAX_DELAY_MS, max(0, int(value))) if isinstance(value, int) and not isinstance(value, bool) \
            else DEFAULT_DELAY_MS

    @property
    def raises(self) -> bool:
        return self.settings.get("ffm_raise") is True

    def start(self) -> None:
        """At start: repair what a crashed CIRANA left behind, then apply the option."""
        if self.settings.get("ffm_restore") and not self.enabled:
            self._restore()
        self.apply()

    def set_enabled(self, on: bool, delay: int | None = None, raises: bool | None = None) -> None:
        self.settings.set("focus_follows_mouse", bool(on))
        if delay is not None:
            self.settings.set("ffm_delay", min(MAX_DELAY_MS, max(0, int(delay))))
        if raises is not None:
            self.settings.set("ffm_raise", bool(raises))
        self.apply()

    def apply(self) -> None:
        if not self.enabled:
            self._restore()
            return
        try:
            if not self.settings.get("ffm_restore"):
                self.settings.set("ffm_restore", self.system.read())  # the user's own values, put back later
            if not self.system.write({"tracking": 1, "raise": int(self.raises), "delay": self.delay}):
                self.notify("Windows did not accept the focus-follows-mouse setting.")
        except Exception:  # noqa: BLE001
            log.warning("Focus follows the mouse could not be switched on", exc_info=True)
            self.notify("Focus follows the mouse could not be switched on (see log).")

    def _restore(self) -> None:
        saved = self.settings.get("ffm_restore")
        if not isinstance(saved, dict) or not saved:
            return
        try:
            values = {key: int(saved.get(key, 0)) for key in ("tracking", "raise", "delay")}
            self.system.write(values)
        except Exception:  # noqa: BLE001
            log.warning("Could not put the active window tracking setting back", exc_info=True)
            return
        self.settings.set("ffm_restore", {})

    def stop(self) -> None:
        """CIRANA closes: the user's own values come back (the option stays on for the next start)."""
        self._restore()
