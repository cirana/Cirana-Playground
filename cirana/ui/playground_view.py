"""Playground page: real applications running inside draggable, closable CIRANA tabs.

Rules this page upholds:
* closing a tab detaches the window and leaves the application running;
* a host is never deleted while it still holds a window (detach first, then remove);
* only an explicit, confirmed request ever asks an application to close, and only apps CIRANA started.

Layout: with tabs open the page is one slim strip (tabs, status line, actions) above an edge-to-edge host. The strip
has a fixed height and the status lives inside it, so messages never resize the hosted window.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QIcon, QKeySequence, QPainter, QPalette, QShortcut
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QMenu, QMessageBox, QPushButton, QSizePolicy, QStackedWidget, QTabBar, QToolButton,
    QVBoxLayout, QWidget,
)

from cirana.core.playground import PlaygroundSession, SessionState
from cirana.native import win32_api as w
from cirana.native import window_embedder as embedder
from cirana.native.window_discovery import WindowInfo
from cirana.ui.common import EmptyState, label
from cirana.ui.context import AppContext
from cirana.ui.dialogs import AppPickerDialog, WindowPickerDialog
from cirana.ui.icons import icon
from cirana.ui.theme import BAD, ON_ACCENT, TEXT, TEXT2, WARN
from cirana.widgets.session_page import SessionPage

log = logging.getLogger(__name__)

STRIP_HEIGHT = 42


class _TabBar(QTabBar):
    """QTabBar that reports middle clicks, so a middle click can close (detach) a tab like in a browser."""

    middle_clicked = Signal(int)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.MiddleButton:
            index = self.tabAt(event.position().toPoint())
            if index >= 0:
                self.middle_clicked.emit(index)
                return
        super().mouseReleaseEvent(event)


class _ElidedLabel(QLabel):
    """One-line label that elides instead of growing, so a long message can never push the strip's buttons."""

    def __init__(self) -> None:
        super().__init__()
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def minimumSizeHint(self) -> QSize:
        return QSize(0, super().minimumSizeHint().height())

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        rect = self.contentsRect()
        text = self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight, rect.width())
        self.style().drawItemText(painter, rect, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight),
                                  self.palette(), self.isEnabled(), text, QPalette.ColorRole.WindowText)


def _tool(glyph: str, tip: str, name: str = "IconButton", color: str = TEXT2) -> QToolButton:
    button = QToolButton()
    button.setObjectName(name)
    button.setIcon(icon(glyph, color, 16))
    button.setIconSize(QSize(16, 16))
    button.setToolTip(tip)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    return button


class PlaygroundView(QWidget):
    focus_mode_requested = Signal(bool)  # hide the sidebar too, so the hosted window gets the whole window
    compact_changed = Signal(bool)  # tabs are (not) open: the main window hides / restores its top bar
    workspace_menu_requested = Signal(object)  # anchor widget for the workspace menu
    search_requested = Signal()

    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self.manager = ctx.playground
        self._pages: dict[str, SessionPage] = {}
        self._suspended = False
        self._compact = False
        self._limbo: list[SessionPage] = []  # closed tabs whose hung application has not yet released its window
        self._limbo_timer = QTimer(self, interval=500)
        self._limbo_timer.timeout.connect(self._sweep_limbo)
        self._workspace_id = ctx.workspaces.active_id

        # -- header (only while no tab is open) --------------------------------------------
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        titles.addWidget(label("Playground", "PageTitle"))
        self.subtitle = label("Run compatible native applications inside CIRANA tabs.", "PageSubtitle")
        titles.addWidget(self.subtitle)
        head.addLayout(titles)
        head.addStretch(1)
        self.window_button = QPushButton(" Running window…")
        self.window_button.setObjectName("SecondaryButton")
        self.window_button.setIcon(icon("playground", TEXT, 16))
        self.window_button.setToolTip("Move a window that is already open into a tab")
        self.window_button.clicked.connect(self.pick_running_window)
        self.open_button = QPushButton(" Open application")
        self.open_button.setObjectName("PrimaryButton")
        self.open_button.setIcon(icon("plus", ON_ACCENT, 16))
        self.open_button.clicked.connect(self.pick_application)
        head.addWidget(self.window_button)
        head.addWidget(self.open_button)

        # -- the strip: tabs | status | actions -----------------------------------------------
        self.tabs = _TabBar()
        self.tabs.setObjectName("PlaygroundTabs")
        self.tabs.setMovable(True)
        self.tabs.setTabsClosable(True)
        self.tabs.setExpanding(False)
        self.tabs.setDrawBase(False)
        self.tabs.setUsesScrollButtons(True)
        self.tabs.setIconSize(QSize(16, 16))
        self.tabs.setElideMode(Qt.TextElideMode.ElideRight)
        self.tabs.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tabs.tabCloseRequested.connect(lambda index: self.close_tab(self._sid_at(index)))
        self.tabs.middle_clicked.connect(lambda index: self.close_tab(self._sid_at(index)))
        self.tabs.tabBarDoubleClicked.connect(lambda index: index >= 0 and self.set_focus_mode(not self.focus_button.isChecked()))
        self.tabs.currentChanged.connect(self._on_current_changed)
        self.tabs.tabMoved.connect(lambda _a, _b: self.manager.set_order(self.tab_order()))
        self.tabs.customContextMenuRequested.connect(self._tab_menu)
        self.plus = _tool("plus", "New tab")
        self.plus.clicked.connect(self._plus_menu)

        self.status = _ElidedLabel()
        self.status.setObjectName("StripStatus")
        self._status_timer = QTimer(self, singleShot=True, interval=6000)
        self._status_timer.timeout.connect(lambda: self.show_status(""))
        self.warn_button = _tool("alert", "", color=WARN)
        self.warn_button.clicked.connect(self._show_warnings)
        self.search_button = _tool("search", "Search applications (Ctrl+K)")
        self.search_button.clicked.connect(self.search_requested)
        self.workspace_button = QPushButton()
        self.workspace_button.setObjectName("StripWorkspace")
        self.workspace_button.setIcon(icon("workspaces", TEXT2, 16))
        self.workspace_button.setToolTip("Active workspace. Click to switch")
        self.workspace_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.workspace_button.clicked.connect(lambda: self.workspace_menu_requested.emit(self.workspace_button))
        self.divider = QFrame()
        self.divider.setObjectName("StripDivider")
        self.divider.setFixedSize(1, 18)
        self.detach_button = _tool("unlink", "Detach: give the window back to Windows. The application keeps running.")
        self.detach_button.clicked.connect(lambda: self.detach(self.current_session_id()))
        self.close_app_button = _tool("x", "Close application… (asks politely; it may prompt to save)", "StripDanger", BAD)
        self.close_app_button.clicked.connect(lambda: self.close_application(self.current_session_id()))
        self.focus_button = _tool("maximize", "Full-window mode: hide the sidebar too (F11, or double-click a tab)")
        self.focus_button.setCheckable(True)
        self.focus_button.toggled.connect(self.set_focus_mode)

        self.strip = QFrame()
        self.strip.setObjectName("PlaygroundStrip")
        self.strip.setFixedHeight(STRIP_HEIGHT)
        strip_row = QHBoxLayout(self.strip)
        strip_row.setContentsMargins(8, 0, 8, 0)
        strip_row.setSpacing(4)
        strip_row.addWidget(self.tabs, 0, Qt.AlignmentFlag.AlignVCenter)
        strip_row.addWidget(self.plus)
        strip_row.addSpacing(8)
        strip_row.addWidget(self.status, 1)
        for widget in (self.warn_button, self.search_button, self.workspace_button, self.divider,
                       self.detach_button, self.close_app_button, self.focus_button):
            strip_row.addWidget(widget, 0, Qt.AlignmentFlag.AlignVCenter)

        # -- body ----------------------------------------------------------------------------------
        self.stack = QStackedWidget()
        self.empty = self._build_empty()
        self.stack.addWidget(self.empty)

        root = QVBoxLayout(self)
        self.root_layout = root
        root.setContentsMargins(28, 24, 28, 18)
        root.setSpacing(0)
        self.header = QWidget()
        self.header.setLayout(head)
        root.addWidget(self.header)
        root.addWidget(self.strip)
        root.addWidget(self.stack, 1)

        self.manager.set_attacher(self._attach)
        self.manager.session_added.connect(self._on_session_added)
        self.manager.session_updated.connect(self._on_session_updated)
        self.manager.session_removed.connect(self._on_session_removed)
        self.manager.activate_requested.connect(self.activate)
        self.manager.order_requested.connect(self.apply_tab_order)
        ctx.workspaces.changed.connect(self._apply_workspace_filter)  # e.g. a deleted workspace must not strand its tabs
        # Shortcuts only fire while CIRANA itself has keyboard focus (never while the hosted application has it).
        for sequence, handler in (
            ("Ctrl+W", self._close_current),
            ("Ctrl+Tab", lambda: self._cycle(1)),
            ("Ctrl+Shift+Tab", lambda: self._cycle(-1)),
            ("Ctrl+Shift+Backtab", lambda: self._cycle(-1)),
        ):
            shortcut = QShortcut(QKeySequence(sequence), self, activated=handler)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._update_workspace_name()
        self._update_empty()

    def _build_empty(self) -> QWidget:
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 16, 0, 0)
        layout.addWidget(EmptyState(
            "playground",
            "Nothing is running in the Playground",
            "Open an application to run it inside a tab, or move a window that is already open into one. "
            "Classic Win32 programs move inside the tab. Store apps (Notepad, Calculator, Teams, Outlook...) are "
            "docked: they stay their own window, kept exactly over the tab. Closing a tab never closes the application.",
        ), 1)
        return holder

    # -- helpers ------------------------------------------------------------------------------
    def _sid_at(self, index: int) -> str:
        return self.tabs.tabData(index) or ""

    def _index_of(self, sid: str) -> int:
        for index in range(self.tabs.count()):
            if self._sid_at(index) == sid:
                return index
        return -1

    def tab_order(self) -> list[str]:
        return [self._sid_at(i) for i in range(self.tabs.count())]

    def current_session_id(self) -> str:
        index = self.tabs.currentIndex()
        return self._sid_at(index) if index >= 0 and self.tabs.isTabVisible(index) else ""

    def has_tabs(self) -> bool:
        return self._compact

    def _pixmap(self, session: PlaygroundSession):
        return self.ctx.icons.pixmap(session.icon_app, 24) if session.icon_app else None

    def show_status(self, text: str, kind: str = "") -> None:
        """Brief message in the strip. The strip's height is fixed, so this never resizes the hosted window."""
        self.status.setText(text)
        self.status.setToolTip(text)
        if self.status.property("kind") != kind:
            self.status.setProperty("kind", kind)
            self.status.style().unpolish(self.status)
            self.status.style().polish(self.status)
        self.status.update()
        if not self._compact:
            self.strip.setVisible(bool(text))  # nothing is hosted, so showing / hiding the strip costs nothing
        if text:
            self._status_timer.start()

    def _say(self, text: str) -> None:
        self.show_status(text)
        self.ctx.actions.message.emit(text)

    # -- manager callbacks ------------------------------------------------------------------
    def _on_session_added(self, sid: str) -> None:
        session = self.manager.get(sid)
        if session is None:
            return
        page = SessionPage(sid)
        page.reattach_clicked.connect(lambda: self.reattach(sid))
        page.show_window_clicked.connect(lambda: self.show_window(sid))
        page.relaunch_clicked.connect(lambda: self.relaunch(sid))
        page.close_tab_clicked.connect(lambda: self.close_tab(sid))
        page.lost.connect(lambda reason: self._on_lost(sid, reason))
        page.hung_changed.connect(lambda _hung: self._update_actions())
        self._pages[sid] = page
        self.stack.addWidget(page)
        index = self.tabs.addTab(session.title)
        self.tabs.setTabData(index, sid)
        self._refresh_tab(sid)
        self._apply_workspace_filter()
        if self.tabs.isTabVisible(index):
            self.tabs.setCurrentIndex(index)
        self._update_empty()

    def _on_session_updated(self, sid: str) -> None:
        self._refresh_tab(sid)
        self._update_empty()

    def _on_session_removed(self, sid: str) -> None:
        self._drop_tab(sid)

    def _refresh_tab(self, sid: str) -> None:
        session, page, index = self.manager.get(sid), self._pages.get(sid), self._index_of(sid)
        if session is None or page is None or index < 0:
            return
        page.apply(session, self._pixmap(session))
        self.tabs.setTabText(index, session.title)
        pix = self._pixmap(session)
        if pix is not None:
            self.tabs.setTabIcon(index, QIcon(pix))
        origin = "started by CIRANA" if session.launched_by_cirana else "adopted window"
        pid = f", pid {session.pid}" if session.pid else ""
        self.tabs.setTabToolTip(index, f"{session.title} ({session.state.value}, {origin}{pid})\n"
                                       "Double-click: full-window mode. Middle-click: close tab.")

    def _attach(self, session: PlaygroundSession, info: WindowInfo) -> embedder.EmbedResult:
        """Called by the manager on the GUI thread once the application's window is stable."""
        page = self._pages.get(session.id)
        if page is None:
            return embedder.EmbedResult(embedder.EmbedStatus.BLOCKED, "The tab was closed.")
        result = page.attach(info, session.launched_by_cirana)
        embedded = page.host.embedded
        session.docked = bool(embedded and embedded.docked)
        if result.ok and session.id == self.current_session_id():
            host = page.host
            QTimer.singleShot(80, host, lambda: host.focus_embedded(explicit=True))
        return result

    # -- tab operations ------------------------------------------------------------------------
    def activate(self, sid: str) -> None:
        index = self._index_of(sid)
        if index < 0:
            return
        session = self.manager.get(sid)
        if session is not None and not self.tabs.isTabVisible(index):
            session.workspace_id = self._workspace_id  # the tab follows the user into the active workspace
            self._apply_workspace_filter()
        self.tabs.setCurrentIndex(index)

    def close_tab(self, sid: str) -> None:
        """Detach (never close) the application, then remove the tab."""
        page, session = self._pages.get(sid), self.manager.get(sid)
        if page is None:
            return
        title = session.title if session else "The application"
        was_hosted = page.host.embedded is not None
        if was_hosted:
            page.detach(activate=True)
        self.manager.remove_session(sid)  # emits session_removed -> _drop_tab
        self._drop_tab(sid)
        if was_hosted:
            self._say(f"Closed the tab. {title} keeps running in its own window.")

    def _drop_tab(self, sid: str) -> None:
        page = self._pages.pop(sid, None)
        if page is None:
            return
        page.dispose()  # detaches if anything is still hosted
        self.stack.removeWidget(page)
        index = self._index_of(sid)
        if index >= 0:
            self.tabs.removeTab(index)
        if page.host.restore_pending():  # never delete a host that still holds a (hung) application's window
            page.hide()
            self._limbo.append(page)
            self._limbo_timer.start()
        else:
            page.deleteLater()
        self._update_empty()
        self._on_current_changed(self.tabs.currentIndex())

    def _sweep_limbo(self) -> None:
        waiting = [page for page in self._limbo if page.host.restore_pending()]
        for page in self._limbo:
            if page not in waiting:
                page.deleteLater()
        self._limbo = waiting
        if not waiting:
            self._limbo_timer.stop()

    def _close_current(self) -> None:
        sid = self.current_session_id()
        if sid:
            self.close_tab(sid)

    def _cycle(self, step: int) -> None:
        visible = [i for i in range(self.tabs.count()) if self.tabs.isTabVisible(i)]
        if len(visible) < 2:
            return
        current = self.tabs.currentIndex()
        position = visible.index(current) if current in visible else 0
        self.tabs.setCurrentIndex(visible[(position + step) % len(visible)])

    def detach(self, sid: str) -> None:
        page, session = self._pages.get(sid), self.manager.get(sid)
        if page is None or session is None:
            return
        was_hosted = page.host.embedded is not None
        restored = page.detach(activate=True)
        if was_hosted:
            self.manager.update_state(sid, SessionState.DETACHED, "")
            if restored:
                self._say(f"Detached {session.title}. It is a normal window again and is still running.")
            else:
                self._say(f"{session.title} is not responding: its window is handed back to Windows as soon as it "
                          "answers. Nothing was closed.")

    def reattach(self, sid: str) -> None:
        session = self.manager.get(sid)
        result = self.manager.reattach(sid)
        if result is not None and not result.ok and session is not None:
            self._say(f"Could not host {session.title} again: {result.reason}")

    def show_window(self, sid: str) -> None:
        session = self.manager.get(sid)
        if session is not None and session.hwnd:
            embedder.bring_to_front(session.hwnd)

    def close_application(self, sid: str) -> None:
        page, session = self._pages.get(sid), self.manager.get(sid)
        if page is None or session is None or page.host.embedded is None:
            return
        if not session.launched_by_cirana:
            QMessageBox.information(self, "Close application", "CIRANA only closes applications it started itself.")
            return
        answer = QMessageBox.question(
            self, "Close application",
            f"Ask {session.title} to close?\n\nThe application is asked politely and may prompt to save. "
            "Nothing is forced.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            page.host.request_application_close()
        except PermissionError as exc:
            self._say(str(exc))
            return
        self.manager.update_state(sid, SessionState.DETACHED, "")
        QTimer.singleShot(2000, lambda: self._after_close_request(sid))

    def _after_close_request(self, sid: str) -> None:
        session = self.manager.get(sid)
        if session is None:
            return
        if w.IsWindow(session.hwnd):
            self._say(f"{session.title} is still open. It may be asking whether to save.")
        else:
            self.manager.update_state(sid, SessionState.CLOSED, f"{session.title} was closed.")

    def relaunch(self, sid: str) -> None:
        session = self.manager.get(sid)
        if session is None or not session.app_id:
            return
        app_id = session.app_id
        self.close_tab(sid)
        self.ctx.actions.launch(app_id, "embedded")

    def _on_lost(self, sid: str, reason: str) -> None:
        session = self.manager.get(sid)
        if session is None:
            return
        if reason == "closed":
            self.manager.update_state(sid, SessionState.CLOSED, f"{session.title} closed its window.")
        else:
            self.manager.update_state(
                sid, SessionState.EXTERNAL,
                f"{session.title} moved its window out of CIRANA on its own; it is a normal window now.",
            )

    # -- selection / focus -------------------------------------------------------------------------
    def apply_tab_order(self, session_ids: list[str]) -> None:
        """Arrange tabs as a restored session asks; tabs not mentioned keep their relative order after them."""
        wanted = [sid for sid in session_ids if self._index_of(sid) >= 0]
        for target, sid in enumerate(wanted):
            current = self._index_of(sid)
            if current != target:
                self.tabs.moveTab(current, target)
        self.manager.set_order(self.tab_order())

    def _on_current_changed(self, index: int) -> None:
        sid = self._sid_at(index) if index >= 0 else ""
        page = self._pages.get(sid)
        if page is None or not self.tabs.isTabVisible(index):
            self.manager.current_id = ""
            self.stack.setCurrentWidget(self.empty)
            self._update_actions()
            return
        self.manager.current_id = sid
        self.stack.setCurrentWidget(page)
        self._update_actions()
        self.focus_current(explicit=True)

    def _update_actions(self) -> None:
        """The strip's per-tab actions always describe the current tab."""
        session = self.manager.get(self.current_session_id())
        embedded = session is not None and session.state is SessionState.EMBEDDED
        self.detach_button.setVisible(embedded)  # also while the app is hung: detaching is always allowed
        self.close_app_button.setVisible(embedded and session.launched_by_cirana)
        warnings = session.warnings if embedded else []
        self.warn_button.setVisible(bool(warnings))
        self.warn_button.setToolTip("\n".join(warnings))

    def _show_warnings(self) -> None:
        session = self.manager.get(self.current_session_id())
        if session is not None and session.warnings:
            self.show_status("; ".join(session.warnings), "warn")

    def focus_current(self, explicit: bool = False) -> None:
        """``explicit``: the user selected the tab (docked windows only come to the front then)."""
        page = self._pages.get(self.current_session_id())
        if page is not None and page.host.embedded is not None and self.isVisible() and not self._suspended:
            host = page.host  # the host is the timer's context: a tab closed in the meantime cancels the call
            QTimer.singleShot(60, host, lambda: host.focus_embedded(explicit=explicit))

    def set_suspended(self, suspended: bool) -> None:
        """Hide hosted windows while a Qt overlay is open (a native child window always paints above Qt widgets)."""
        self._suspended = suspended
        self.stack.setVisible(not suspended)
        if not suspended:
            self.focus_current()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.focus_current()

    # -- workspaces -----------------------------------------------------------------------------------
    def set_active_workspace(self, workspace_id: str) -> None:
        self._workspace_id = workspace_id or ""
        self._apply_workspace_filter()

    def _update_workspace_name(self) -> None:
        workspace = self.ctx.workspaces.get(self._workspace_id) if self._workspace_id else None
        self.workspace_button.setText(f" {workspace.name}" if workspace else " No workspace")

    def _apply_workspace_filter(self) -> None:
        """Only the active workspace's tabs are visible; the others keep running hidden inside their hosts."""
        known = {w.id for w in self.ctx.workspaces.all()}
        orphaned = 0
        for session in self.manager.sessions():
            if session.workspace_id and session.workspace_id not in known:  # its workspace was deleted
                session.workspace_id = ""  # the tab (and its application) lives on, outside any workspace
                orphaned += 1
        for index in range(self.tabs.count()):
            session = self.manager.get(self._sid_at(index))
            self.tabs.setTabVisible(index, session is not None and session.workspace_id == self._workspace_id)
        current = self.tabs.currentIndex()
        if current < 0 or not self.tabs.isTabVisible(current):
            visible = [i for i in range(self.tabs.count()) if self.tabs.isTabVisible(i)]
            if visible:
                self.tabs.setCurrentIndex(visible[0])
        self._on_current_changed(self.tabs.currentIndex())
        self._update_workspace_name()
        self._update_empty()
        if orphaned:
            self._say(f"{orphaned} tab{'s' if orphaned != 1 else ''} of a deleted workspace moved to \"No workspace\". "
                      "The applications keep running.")

    def visible_session_ids(self) -> list[str]:
        return [self._sid_at(i) for i in range(self.tabs.count()) if self.tabs.isTabVisible(i)]

    def set_focus_mode(self, on: bool) -> None:
        if self.focus_button.isChecked() != on:
            self.focus_button.setChecked(on)  # re-enters through toggled
            return
        self.focus_button.setIcon(icon("minimize" if on else "maximize", TEXT2, 16))
        self.focus_button.setToolTip("Leave full-window mode (F11)" if on
                                     else "Full-window mode: hide the sidebar too (F11, or double-click a tab)")
        self.focus_mode_requested.emit(on)

    def _update_empty(self) -> None:
        visible = self.visible_session_ids()
        compact = bool(visible)
        # With tabs open the page belongs to the hosted window: no title, no margins, one slim strip.
        self.header.setVisible(not compact)
        self.root_layout.setContentsMargins(*((0, 0, 0, 0) if compact else (28, 24, 28, 18)))
        for widget in (self.tabs, self.plus, self.search_button, self.workspace_button, self.divider, self.focus_button):
            widget.setVisible(compact)
        self.strip.setVisible(compact or bool(self.status.text()))
        hidden = [s for s in self.manager.sessions() if s.live and s.id not in visible]
        embedded = sum(1 for sid in visible if (s := self.manager.get(sid)) and s.state is SessionState.EMBEDDED)
        if not compact:
            self.stack.setCurrentWidget(self.empty)
            if self.focus_button.isChecked():
                self.set_focus_mode(False)  # the strip's exit button is gone, so never strand the user without a sidebar
        if compact:
            extra = f" · {len(hidden)} in other workspaces" if hidden else ""
            self.subtitle.setText(f"{len(visible)} tab{'s' if len(visible) != 1 else ''} · {embedded} hosted{extra}")
        elif hidden:
            self.subtitle.setText(f"{len(hidden)} application{'s' if len(hidden) != 1 else ''} running in other "
                                  "workspaces. Switch with the workspace button.")
        else:
            self.subtitle.setText("Run compatible native applications inside CIRANA tabs.")
        self._update_actions()
        if compact != self._compact:
            self._compact = compact
            self.compact_changed.emit(compact)

    # -- pickers / menus -------------------------------------------------------------------------------
    def pick_application(self) -> None:
        dialog = AppPickerDialog(self.ctx, "Open in Playground", ok_text="Open", show_hosting=True, parent=self)
        if dialog.exec() and dialog.selected_ids():
            self.ctx.actions.launch(dialog.selected_ids()[0], "embedded")

    def pick_running_window(self) -> None:
        dialog = WindowPickerDialog(self.manager.hosted_handles(), parent=self)
        if dialog.exec() and dialog.chosen is not None:
            outcome = self.manager.adopt_window(dialog.chosen, self._workspace_id)
            self.show_status(outcome.message)

    def _plus_menu(self) -> None:
        menu = QMenu(self)
        menu.addAction("Application…", self.pick_application)
        menu.addAction("Running window…", self.pick_running_window)
        menu.exec(self.plus.mapToGlobal(QPoint(0, self.plus.height())))

    def _tab_menu(self, pos: QPoint) -> None:
        index = self.tabs.tabAt(pos)
        sid = self._sid_at(index) if index >= 0 else ""
        session = self.manager.get(sid)
        if session is None:
            return
        menu = QMenu(self)
        if session.state is SessionState.EMBEDDED:
            menu.addAction("Detach to a normal window", lambda: self.detach(sid))
            if session.launched_by_cirana:
                menu.addAction("Close application…", lambda: self.close_application(sid))
        elif session.state is SessionState.DETACHED:
            menu.addAction("Re-attach", lambda: self.reattach(sid))
            menu.addAction("Show window", lambda: self.show_window(sid))
        elif session.state is SessionState.CLOSED and session.app_id:
            menu.addAction("Start again", lambda: self.relaunch(sid))
        if session.app_id and session.live:
            menu.addAction("Open another instance", lambda: self.ctx.actions.launch(session.app_id, "embedded", True))
        menu.addSeparator()
        full = self.focus_button.isChecked()
        menu.addAction("Leave full-window mode" if full else "Full-window mode", lambda: self.set_focus_mode(not full))
        menu.addAction("Close tab (application keeps running)", lambda: self.close_tab(sid))
        menu.exec(self.tabs.mapToGlobal(pos))

    # -- lifecycle ---------------------------------------------------------------------------------------
    def refresh(self) -> None:
        self._update_empty()

    def hosted_count(self) -> int:
        return sum(1 for page in self._pages.values() if page.host.embedded is not None)

    def shutdown(self) -> int:
        """Give every hosted window back to Windows. Safe to call more than once. Returns how many were restored."""
        restored = 0
        for page in list(self._pages.values()):
            if page.host.embedded is not None:
                restored += 1 if page.detach() else 0
        self.manager.shutdown()
        self.manager.clear_persisted_state()
        return restored
