"""Content of one Playground tab: a status panel and the native host for the application window.

The page has no chrome of its own. Detach / close / warnings live in the Playground's single strip, so the hosted
window gets every pixel of the tab.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QProgressBar, QPushButton, QStackedLayout, QVBoxLayout, QWidget

from cirana.core.playground import PlaygroundSession, SessionState
from cirana.native import win32_api as w
from cirana.native.window_discovery import WindowInfo
from cirana.native.window_embedder import EmbedResult
from cirana.ui.icons import icon
from cirana.ui.theme import TEXT
from cirana.widgets.native_host import NativeHost


def _button(text: str, name: str, glyph: str = "", color: str = TEXT) -> QPushButton:
    button = QPushButton(f" {text}" if glyph else text)
    button.setObjectName(name)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    if glyph:
        button.setIcon(icon(glyph, color, 16))
    return button


class SessionPage(QWidget):
    reattach_clicked = Signal()
    show_window_clicked = Signal()
    relaunch_clicked = Signal()
    close_tab_clicked = Signal()
    lost = Signal(str)  # "closed" | "escaped"
    hung_changed = Signal(bool)

    def __init__(self, session_id: str) -> None:
        super().__init__()
        self.session_id = session_id
        self.host = NativeHost()
        self.host.window_lost.connect(self.lost)
        self.host.hung_changed.connect(self._on_hung)
        self._session: PlaygroundSession | None = None

        # -- status panel (launching, detached, closed, not responding) ----------------------
        self.panel = QFrame()
        self.panel.setObjectName("HostPanel")
        panel_layout = QVBoxLayout(self.panel)
        panel_layout.setContentsMargins(32, 32, 32, 32)
        panel_layout.setSpacing(10)
        panel_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.icon_label = QLabel()
        self.icon_label.setFixedSize(40, 40)
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.panel_title = QLabel()
        self.panel_title.setObjectName("SectionTitle")
        self.panel_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.panel_text = QLabel()
        self.panel_text.setObjectName("Muted")
        self.panel_text.setWordWrap(True)
        self.panel_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.panel_text.setMaximumWidth(520)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setFixedWidth(240)
        self.progress.setFixedHeight(6)
        self.primary = _button("", "PrimaryButton")
        self.secondary = _button("", "SecondaryButton")
        self._handlers: dict[QPushButton, Signal | None] = {self.primary: None, self.secondary: None}
        for button in self._handlers:  # one permanent connection each; the action behind it changes with the state
            button.clicked.connect(lambda _=False, b=button: self._handlers[b].emit() if self._handlers[b] else None)
        self.tertiary = _button("Close tab", "SecondaryButton", "x")
        self.tertiary.clicked.connect(self.close_tab_clicked)
        buttons = QHBoxLayout()
        buttons.setAlignment(Qt.AlignmentFlag.AlignCenter)
        buttons.setSpacing(8)
        for button in (self.primary, self.secondary, self.tertiary):
            buttons.addWidget(button)
        panel_layout.addWidget(self.icon_label, 0, Qt.AlignmentFlag.AlignCenter)
        panel_layout.addWidget(self.panel_title)
        panel_layout.addWidget(self.panel_text, 0, Qt.AlignmentFlag.AlignCenter)
        panel_layout.addWidget(self.progress, 0, Qt.AlignmentFlag.AlignCenter)
        panel_layout.addSpacing(4)
        panel_layout.addLayout(buttons)

        self.body = QStackedLayout()
        self.body.addWidget(self.panel)
        self.body.addWidget(self.host)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addLayout(self.body, 1)

    # -- hosting ---------------------------------------------------------------------------
    def attach(self, info: WindowInfo, launched_by_cirana: bool) -> EmbedResult:
        self.body.setCurrentWidget(self.host)  # visible (and sized) before the window moves in
        result = self.host.attach(info, launched_by_cirana)
        if not result.ok:
            self.body.setCurrentWidget(self.panel)
        return result

    def detach(self, activate: bool = False) -> bool:
        return self.host.detach(activate=activate)

    def dispose(self) -> None:
        """Must be called before the page is deleted: a host that still holds a window would destroy it."""
        self.host.detach()

    # -- presentation ----------------------------------------------------------------------
    def apply(self, session: PlaygroundSession, pixmap: QPixmap | None) -> None:
        self._session = session
        if pixmap is not None:
            self.icon_label.setPixmap(pixmap)
        state = session.state
        if state is SessionState.EMBEDDED:
            if self.host.hung:
                self._on_hung(True)
            else:
                self.body.setCurrentWidget(self.host)
            return
        self.body.setCurrentWidget(self.panel)
        self.progress.setVisible(state is SessionState.LAUNCHING)
        for button in (self.primary, self.secondary):
            button.hide()
            self._handlers[button] = None
        self.tertiary.show()
        self.tertiary.setText(" Cancel" if state is SessionState.LAUNCHING else " Close tab")
        window_alive = bool(session.hwnd) and bool(w.IsWindow(session.hwnd))
        if state is SessionState.LAUNCHING:
            self.panel_title.setText(f"Starting {session.title}…")
            self.panel_text.setText("Waiting for the application window. The tab fills in as soon as it appears.")
        elif state is SessionState.DETACHED:
            self.panel_title.setText(f"{session.title} runs in its own window")
            self.panel_text.setText("It was detached from this tab and is still running. Re-attach it to host it here again.")
            self._action(self.primary, "Re-attach", self.reattach_clicked, enabled=window_alive)
            self._action(self.secondary, "Show window", self.show_window_clicked, enabled=window_alive)
        elif state is SessionState.CLOSED:
            self.panel_title.setText(f"{session.title} closed its window")
            self.panel_text.setText(session.message or "The application ended or closed its window.")
            if session.app_id:
                self._action(self.primary, "Start again", self.relaunch_clicked)
        else:  # EXTERNAL / FAILED
            self.panel_title.setText(f"{session.title} is not hosted in this tab")
            self.panel_text.setText(session.message)
            if window_alive:
                self._action(self.primary, "Show window", self.show_window_clicked)

    def _action(self, button: QPushButton, text: str, signal: Signal, enabled: bool = True) -> None:
        button.setText(text)
        self._handlers[button] = signal
        button.setEnabled(enabled)
        button.show()

    def _on_hung(self, hung: bool) -> None:
        """A hung hosted window must never be shown again by CIRANA: showing a window whose child does not
        answer WM_SHOWWINDOW blocks the UI thread until the application recovers (measured). So while it is
        not responding the host stays hidden behind a status panel; hiding is safe, re-showing waits for recovery."""
        session = self._session
        if session is None or session.state is not SessionState.EMBEDDED:
            return
        if hung:
            self.panel_title.setText(f"{session.title} is not responding")
            self.panel_text.setText(
                "CIRANA stays responsive. The window is hidden until the application recovers; it comes back "
                "here by itself. Detach (in the strip above) hands the window back to Windows at any time.")
            self.progress.show()
            for button in (self.primary, self.secondary, self.tertiary):
                button.hide()
                if button in self._handlers:
                    self._handlers[button] = None
            self.body.setCurrentWidget(self.panel)
        else:
            self.progress.hide()
            self.tertiary.show()
            self.body.setCurrentWidget(self.host)
        self.hung_changed.emit(hung)
