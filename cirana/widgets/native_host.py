"""A native Qt widget whose HWND hosts one foreign window."""
from __future__ import annotations

from PySide6.QtCore import QEvent, QTimer, Qt, Signal
from PySide6.QtWidgets import QApplication, QWidget

from cirana.native import win32_api as w
from cirana.native import window_embedder as embedder
from cirana.native.window_discovery import WindowInfo


class NativeHost(QWidget):
    """Owns the embedded window's lifetime: sizing, liveness tracking and guaranteed detach.

    A host must never be destroyed (or re-parented to another top-level window) while it still holds a
    foreign window: Windows destroys child windows together with their parent. Always call :meth:`detach` first.
    """

    window_lost = Signal(str)  # "closed" (app closed its window) or "escaped" (app re-parented itself)
    embedded_changed = Signal()
    hung_changed = Signal(bool)  # the hosted application stopped / resumed responding

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAutoFillBackground(False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumSize(200, 150)
        self._embedded: embedder.EmbeddedWindow | None = None
        self._released: embedder.EmbeddedWindow | None = None  # last window given back (its restore may still be pending)
        self._hung = False
        # One cheap IsWindow / GetParent / IsHungAppWindow check twice a second, only while embedded.
        # None of these send a message to the foreign window, so they cannot block on a stuck application.
        self._watch = QTimer(self)
        self._watch.setInterval(500)
        self._watch.timeout.connect(self._check_alive)
        # Keyboard focus follow-up. A click inside the hosted window goes straight to the foreign HWND: Qt never
        # sees it and Windows does not move keyboard focus there while CIRANA is already the active window, so
        # typing kept going to CIRANA. This 50 ms check uses only calls that send nothing (GetForegroundWindow,
        # GetAsyncKeyState, GetCursorPos, GetWindowRect, GetFocus) and posts the app's activation state.
        # Focus is handed over as soon as the pointer *enters* the app, i.e. before the click: giving it after the
        # click made browsers restore their previous focus (the page) over the field just clicked (the address bar).
        self._focus_watch = QTimer(self)
        self._focus_watch.setInterval(50)
        self._focus_watch.timeout.connect(self._check_focus)
        self._button_was_down = False
        self._pointer_inside = False
        self._app_active = False  # what the hosted application was last told about its activation
        self._docked_minimized = False  # a docked window was minimized together with CIRANA

    @property
    def embedded(self) -> embedder.EmbeddedWindow | None:
        return self._embedded

    @property
    def hung(self) -> bool:
        return self._hung

    def _device_size(self) -> tuple[int, int]:
        ratio = self.devicePixelRatioF()
        return max(1, round(self.width() * ratio)), max(1, round(self.height() * ratio))

    def attach(self, info: WindowInfo, launched_by_cirana: bool, allow_dock: bool = True) -> embedder.EmbedResult:
        """``allow_dock=False`` (the re-parenting proof of concept): windows that cannot be re-parented are refused
        by ``check_compatibility`` instead of being docked."""
        if self._embedded is not None:
            return embedder.EmbedResult(embedder.EmbedStatus.BLOCKED, "This host already contains a window.")
        if allow_dock and embedder.hosting_mode(info) == embedder.DOCKED:  # never re-parent these: dock instead
            result, embedded = embedder.dock(info, int(self.winId()), launched_by_cirana)
        else:
            width, height = self._device_size()
            result, embedded = embedder.embed(info, int(self.winId()), width, height, launched_by_cirana)
        if embedded is not None:
            self._embedded = embedded
            self._released = None
            self._set_hung(False)
            self._watch.start()
            self._app_active = True  # it was the foreground window a moment ago
            self._docked_minimized = False
            self._focus_watch.start()
            if embedded.docked:
                self.window().installEventFilter(self)  # follow the main window while it is dragged
                self.sync_size()
            self.embedded_changed.emit()
        return result

    def detach(self, activate: bool = False) -> bool:
        """Release the window back to the desktop. The application keeps running."""
        embedded, self._embedded = self._embedded, None
        self._stop_watching()
        if embedded is None:
            return False
        self._released = embedded
        restored = embedder.detach(embedded, activate=activate)
        self.embedded_changed.emit()
        return restored

    def restore_pending(self) -> bool:
        """True while a released window is still parented to this host (a hung application answers late).

        Deleting the host in that state would destroy the application's window with it, so owners keep the host
        alive until this turns False.
        """
        released = self._released
        if released is None or released.docked:  # a docked window was never parented to the host
            return False
        return bool(w.IsWindow(released.hwnd) and int(w.GetParent(released.hwnd) or 0) == released.host)

    def request_application_close(self) -> bool:
        """Release the window, then politely ask the application to close (WM_CLOSE).

        Only for applications CIRANA launched itself (raises PermissionError otherwise). The application may
        still prompt to save, so this is a request, never a kill.
        """
        embedded = self._embedded
        if embedded is None:
            return False
        if not embedded.launched_by_cirana:
            raise PermissionError("CIRANA only closes applications it launched itself.")
        self._embedded = None
        self._stop_watching()
        sent = embedder.request_close(embedded)
        self.embedded_changed.emit()
        return sent

    def focus_embedded(self, explicit: bool = False) -> None:
        """``explicit``: the user picked this tab. A docked window is only brought to the front then, never on a
        plain re-activation of CIRANA (that would close a CIRANA menu the user just opened)."""
        embedded = self._embedded
        if embedded and not self._hung and (explicit or not embedded.docked):
            embedder.focus(embedded)
            self._app_active = True

    def _stop_watching(self) -> None:
        self._watch.stop()
        self._focus_watch.stop()
        self.window().removeEventFilter(self)
        self._button_was_down = False
        self._pointer_inside = False
        self._app_active = False
        self._set_hung(False)

    def _set_app_active(self, active: bool) -> None:
        if self._embedded and active != self._app_active:
            embedder.set_active(self._embedded, active)
            self._app_active = active

    def _check_focus(self) -> None:
        embedded = self._embedded
        if embedded is None or self._hung:
            return
        if embedded.docked:
            self._tick_docked(embedded)
            return
        down = w.mouse_button_down()
        pressed, self._button_was_down = down and not self._button_was_down, down
        if not self.isVisible():  # another tab or page is showing
            self._pointer_inside = False
            self._set_app_active(False)
            return
        root = int(w.GetAncestor(int(self.winId()), w.GA_ROOT) or 0)
        foreground = int(w.GetForegroundWindow() or 0)
        if foreground != root:
            # Another program is in front. The app's own menus and dialogs do not count as leaving it.
            self._pointer_inside = False
            if not embedder.owns_window(embedded, foreground):
                self._set_app_active(False)
            return
        if not self._app_active:  # CIRANA came back to the front
            self.focus_embedded()
            return
        inside = embedder.contains_point(embedded, *w.cursor_pos())
        entered, self._pointer_inside = inside and not self._pointer_inside, inside
        # Entering the app (before any click lands) or, as a fallback, a click that was too quick for the
        # enter check. Never while a CIRANA menu is open: moving focus would close it under the user's click.
        if (entered or (inside and pressed)) and QApplication.activePopupWidget() is None \
                and not embedder.has_focus(embedded):
            self.focus_embedded()

    # -- docked mode ----------------------------------------------------------------------------------
    def _root(self) -> int:
        return int(w.GetAncestor(int(self.winId()), w.GA_ROOT) or 0)

    def _tick_docked(self, embedded: embedder.EmbeddedWindow) -> None:
        """Keep a docked window glued to the tab: same rectangle, above CIRANA, minimized together with CIRANA."""
        root = self._root()
        if w.IsIconic(root):
            if not self._docked_minimized:
                self._docked_minimized = True
                embedder.show_docked(embedded, minimized=True)
            return
        if self._docked_minimized:
            self._docked_minimized = False
            if self.isVisible():
                embedder.show_docked(embedded, minimized=False)
        if not self.isVisible():
            return  # another tab or page: it was sent behind CIRANA when the host was hidden
        self.sync_size()  # also snaps it back if the user dragged it away
        # Raise it over CIRANA only while CIRANA is in front and none of CIRANA's own menus or dialogs is open:
        # those are owned by CIRANA's window and would end up behind the docked app.
        if int(w.GetForegroundWindow() or 0) == root and QApplication.activePopupWidget() is None \
                and QApplication.activeModalWidget() is None:
            embedder.keep_docked_above(embedded, root)

    def _host_screen_rect(self) -> tuple[int, int, int, int]:
        return w.window_rect(int(self.winId()))

    def sync_size(self) -> None:
        embedded = self._embedded
        if not embedded:
            return
        if embedded.docked:
            if self.isVisible() and not self._hung:
                embedder.place_docked(embedded, self._host_screen_rect())
        else:
            embedder.resize(embedded, *self._device_size())

    def eventFilter(self, obj, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Move and obj is self.window():
            self.sync_size()
        return super().eventFilter(obj, event)

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        embedded = self._embedded
        if embedded is not None and embedded.docked and not w.IsIconic(self._root()):
            embedder.send_docked_behind(embedded, self._root())

    # -- events -------------------------------------------------------------------
    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.sync_size()

    def showEvent(self, event) -> None:
        # Hidden stack pages do not receive resize events, so the size is re-applied whenever the host appears.
        super().showEvent(event)
        embedded = self._embedded
        if embedded is not None and embedded.docked and not self._hung and w.IsIconic(embedded.hwnd) \
                and not w.IsIconic(self._root()):
            embedder.show_docked(embedded, minimized=False)  # minimized with CIRANA while this tab was hidden
        self.sync_size()

    def event(self, event: QEvent) -> bool:
        handled = super().event(event)
        if event.type() == QEvent.Type.DevicePixelRatioChange:  # moved to a monitor with a different scale
            self.sync_size()
        return handled

    def mousePressEvent(self, event) -> None:
        self.focus_embedded()
        super().mousePressEvent(event)

    def _set_hung(self, hung: bool) -> None:
        if hung != self._hung:
            self._hung = hung
            self.hung_changed.emit(hung)

    def _check_alive(self) -> None:
        embedded = self._embedded
        if embedded is None:
            return
        if not embedder.is_alive(embedded):
            self._lose("closed")
        elif not embedder.is_still_embedded(embedded):
            self._lose("escaped")
        else:
            self._set_hung(embedder.is_hung(embedded))

    def _lose(self, reason: str) -> None:
        self._embedded = None
        self._stop_watching()
        self.window_lost.emit(reason)
        self.embedded_changed.emit()

    def closeEvent(self, event) -> None:
        self.detach()
        super().closeEvent(event)
