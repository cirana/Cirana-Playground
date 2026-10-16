"""Windows page: every application window that is open right now (live), with quick actions, and the switch for
tiling all of them (cirana.core.dynamic_tiling, all-window scope).

The list comes from the window tracker, so it follows windows as they open, close, move and change their title,
and building it never waits for an application.
"""
from __future__ import annotations

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QMenu, QSizePolicy, QSpinBox, QToolButton,
    QVBoxLayout, QWidget,
)

from cirana.core.background import run_async
from cirana.ui.common import EmptyState, label, make_scroll
from cirana.ui.context import AppContext
from cirana.ui.dialogs import window_icon_app, window_subtitle
from cirana.ui.icons import icon
from cirana.ui.theme import TEXT2
from cirana.ui.wm_dialogs import AutoLayoutPicker, RuleDialog
from cirana.wm.identity import TrackedWindow
from cirana.wm.layout import AUTO, BSP, GRID, MAIN_SIDE, SIDE_BY_SIDE, LayoutSpec
from cirana.wm.monitors import monitor_for_rect
from cirana.wm.rules import FLOAT, WindowRule

TILING_CHOICES = [("Side by side", SIDE_BY_SIDE, 0), ("Main + side panel", MAIN_SIDE, 0), ("Grid 2×2", GRID, 2),
                  ("Dynamic grid", GRID, 0), ("BSP (split per window)", BSP, 0),
                  ("Automatic (by window count)", AUTO, 0)]


def _tool(glyph: str, tip: str) -> QToolButton:
    button = QToolButton()
    button.setObjectName("IconButton")
    button.setIcon(icon(glyph, TEXT2, 16))
    button.setToolTip(tip)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    return button


class WindowRow(QFrame):
    def __init__(self, view: "WindowsView", window: TrackedWindow, where: str, groups: list[str]) -> None:
        super().__init__()
        ctx = view.ctx
        self.setObjectName("WorkspaceAppRow")
        self.setMinimumHeight(60)
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 8, 10, 8)
        row.setSpacing(10)
        tile = QLabel()
        tile.setObjectName("IconTile")
        tile.setFixedSize(40, 40)
        tile.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ctx.icons.request(window_icon_app(ctx, window), 28, tile, tile.setPixmap)
        row.addWidget(tile)
        text = QVBoxLayout()
        text.setSpacing(0)
        title = label(window.display_title, "AppName")
        title.setToolTip(window.title)
        title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)  # long titles never widen it
        text.addWidget(title)
        meta = window_subtitle(window) + (f" · {where}" if where else "")
        if groups:
            meta += " · in " + ", ".join(groups)
        state, detail = ctx.tiler.status(window)  # tiled / stacked / floating / ignored, and why
        if state:
            meta += f" · {state}" + (f" ({detail})" if detail else "")
        elif ctx.tiler.ignores(window):
            meta += " · ignored (window rule)"
        sub = label(meta, "AppCategory")
        sub.setToolTip(window.app_exe)
        sub.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        text.addWidget(sub)
        row.addLayout(text, 1)
        for glyph, tip, handler in (
            ("external", "Bring to front", lambda: view.run(lambda c: c.bring_to_front([window]))),
            ("minimize", "Minimize", lambda: view.run(lambda c: [c.minimize(window)])),
            ("maximize", "Restore" if window.maximized else "Maximize",
             lambda: view.run(lambda c: [c.restore(window) if window.maximized else c.maximize(window)])),
        ):
            button = _tool(glyph, tip)
            button.clicked.connect(handler)
            row.addWidget(button)
        floating = window.exe_name.lower() in ctx.tiler.exclusions()
        float_button = _tool("link" if floating else "unlink",
                             f"Tile {window.exe_name} again" if floating else
                             f"Leave {window.exe_name} floating (all-window tiling does not arrange it)")
        float_button.clicked.connect(lambda: ctx.tiler.set_floating(window.exe_name, not floating))
        float_button.setEnabled(bool(window.exe_name))
        row.addWidget(float_button)
        add = _tool("plus", "Add to a workspace")
        add.clicked.connect(lambda: view.show_add_menu(window, add.mapToGlobal(add.rect().bottomLeft())))
        row.addWidget(add)
        more = _tool("list", "Tiling: monocle, stacks, float, window rule")
        more.clicked.connect(lambda: view.show_tiling_menu(window, more.mapToGlobal(more.rect().bottomLeft())))
        row.addWidget(more)


class WindowsView(QWidget):
    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self._syncing = False
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(12)
        head = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(0)
        titles.addWidget(label("Open windows", "PageTitle"))
        self.subtitle = label("", "PageSubtitle")
        titles.addWidget(self.subtitle)
        head.addLayout(titles, 1)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter windows…")
        self.filter.setClearButtonEnabled(True)
        self.filter.setMaximumWidth(280)
        self.filter.textChanged.connect(lambda _t: self.refresh())
        head.addWidget(self.filter)
        root.addLayout(head)
        root.addWidget(self._tiling_panel())
        self.rows_host = QWidget()
        self.rows = QVBoxLayout(self.rows_host)
        self.rows.setContentsMargins(0, 0, 4, 0)
        self.rows.setSpacing(8)
        self.rows.addStretch(1)
        root.addWidget(make_scroll(self.rows_host), 1)
        self.empty = EmptyState("monitor", "No application windows",
                                "Windows you open appear here as they open. CIRANA lists ordinary application "
                                "windows; the taskbar, the desktop, menus and tool windows are left out.")
        root.addWidget(self.empty)
        self._visible = False
        self._timer = QTimer(self, singleShot=True, interval=250)  # coalesce bursts (a dragged window moves a lot)
        self._timer.timeout.connect(self.refresh)
        for signal in (ctx.windows.window_added, ctx.windows.window_removed, ctx.windows.window_changed):
            signal.connect(lambda _w: self._visible and self._timer.start())
        ctx.windows.inventory_ready.connect(lambda: self._visible and self._timer.start())
        ctx.tiler.state_changed.connect(lambda _s: self._visible and self._timer.start())
        ctx.tiler.reflowed.connect(lambda _s: self._visible and self._timer.start())

    def _tiling_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("Panel")
        rows = QVBoxLayout(panel)
        rows.setContentsMargins(16, 10, 16, 10)
        rows.setSpacing(8)
        row = QHBoxLayout()
        row.setSpacing(12)
        rows.addLayout(row)
        self.tile_all = QCheckBox("Tile all windows")
        self.tile_all.setToolTip(
            "Arrange every application window automatically, one layout per monitor, as windows open, close, "
            "minimize and restore. Fixed-size and full-screen windows, and programs you leave floating, are not "
            "touched. Drag a window onto another one to swap them, or onto another monitor to move it there.")
        self.tile_all.toggled.connect(self._tile_all_toggled)
        row.addWidget(self.tile_all)
        self.tiling_kind = QComboBox()
        for text, _kind, _columns in TILING_CHOICES:
            self.tiling_kind.addItem(text)
        self.tiling_kind.currentIndexChanged.connect(self._tiling_edited)
        self.tiling_gap = QSpinBox()
        self.tiling_gap.setRange(0, 64)
        self.tiling_gap.setSuffix(" px gap")
        self.tiling_gap.valueChanged.connect(self._tiling_edited)
        self.tiling_ratio = QSpinBox()
        self.tiling_ratio.setRange(20, 80)
        self.tiling_ratio.setSuffix(" % main")
        self.tiling_ratio.valueChanged.connect(self._tiling_edited)
        self.tiling_auto = AutoLayoutPicker()  # second line: only shown for the automatic layout
        self.tiling_auto.changed.connect(self._tiling_edited)
        for widget in (self.tiling_kind, self.tiling_gap, self.tiling_ratio):
            row.addWidget(widget)
        self.tiling_borders = QCheckBox("Borders")
        self.tiling_borders.setToolTip("A thin border around every tiled window: green for the focused one, grey for "
                                       "the others")
        self.tiling_borders.toggled.connect(
            lambda on: None if self._syncing else self.ctx.overlays.set_borders_enabled(on))
        row.addWidget(self.tiling_borders)
        row.addStretch(1)
        row = QHBoxLayout()
        row.setSpacing(12)
        rows.addLayout(row)
        row.addWidget(self.tiling_auto)
        self.tiling_state = label("", "TilingState")
        self.tiling_state.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        row.addWidget(self.tiling_state, 1)
        keys = _tool("settings", "Keyboard shortcuts, focus follows the mouse, stacks and window rules (Settings)")
        keys.clicked.connect(lambda: self.ctx.actions.navigate.emit("settings", None))
        row.addWidget(keys)
        return panel

    def _sync_tiling(self) -> None:
        tiler = self.ctx.tiler
        spec = tiler.global_layout()
        self._syncing = True
        try:
            self.tile_all.setChecked(tiler.global_on)
            index = next((i for i, (_t, kind, columns) in enumerate(TILING_CHOICES)
                          if kind == spec.kind and (kind != GRID or columns == (2 if spec.columns == 2 else 0))), 1)
            self.tiling_kind.setCurrentIndex(index)
            self.tiling_auto.set_auto(spec.auto)
            self.tiling_gap.setValue(spec.gap)
            self.tiling_ratio.setValue(round(spec.ratio * 100))
            self.tiling_borders.setChecked(self.ctx.overlays.borders_enabled)
        finally:
            self._syncing = False
        self.tiling_ratio.setVisible(spec.kind in (MAIN_SIDE, AUTO))
        self.tiling_auto.setVisible(spec.kind == AUTO)
        if not tiler.global_on:
            state, kind = "Off: windows stay where you put them", "idle"
        elif tiler.paused():
            state, kind = "Paused while you move a window", "warn"
        else:
            tiled = tiler.tiled_windows()
            monitors = len({tiler._home.get(w.key) for w in tiled})
            stacks = tiler.stack_count()
            state = (f"On: {len(tiled)} window{'s' if len(tiled) != 1 else ''} tiled"
                     + (f" on {monitors} monitors" if monitors > 1 else "")
                     + (f" · {stacks} stack{'s' if stacks != 1 else ''}" if stacks else "")
                     + (" · monocle" if tiler.monocle_on() else "")
                     + (" · layout adjusted to minimum sizes" if any(g.fallback for g in tiler.shown_groups())
                        else ""))
            kind = "good"
        self.tiling_state.setText(state)
        self.tiling_state.setProperty("kind", kind)
        self.tiling_state.style().unpolish(self.tiling_state)
        self.tiling_state.style().polish(self.tiling_state)

    def _tile_all_toggled(self, on: bool) -> None:
        if self._syncing:
            return
        self.ctx.tiler.set_global(on)
        self.ctx.actions.message.emit("Tiling all windows: they follow the layout by themselves" if on else
                                      "Tiling all windows is off: every window stays where it is")
        self.refresh()

    def _tiling_edited(self, _value: object = None) -> None:
        if self._syncing:
            return
        _text, kind, columns = TILING_CHOICES[max(0, self.tiling_kind.currentIndex())]
        self.ctx.tiler.set_global_layout(LayoutSpec(kind, gap=self.tiling_gap.value(),
                                                    ratio=self.tiling_ratio.value() / 100, columns=columns,
                                                    auto=self.tiling_auto.auto()))
        self.tiling_ratio.setVisible(kind in (MAIN_SIDE, AUTO))
        self.tiling_auto.setVisible(kind == AUTO)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._visible = True

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._visible = False

    def refresh(self) -> None:
        ctx = self.ctx
        self._sync_tiling()
        windows = ctx.windows.windows()
        needle = self.filter.text().strip().lower()
        shown = [w for w in windows if not needle or needle in f"{w.title} {w.exe_name}".lower()]
        monitors = ctx.windows.backend.monitors()
        monitors = monitors if len(monitors) > 1 else []  # name the monitor only when there is a choice
        groups: dict[str, list[str]] = {}
        for workspace in ctx.workspaces.all():
            for status in ctx.wm.statuses(workspace.id):
                if status.window is not None:
                    groups.setdefault(status.window.key, []).append(workspace.name)
        while self.rows.count() > 1:
            item = self.rows.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for window in shown:
            monitor = monitor_for_rect(window.rect, monitors) if monitors and not window.minimized else None
            where = monitor.label.split(" · ")[0] if monitor else ""
            self.rows.insertWidget(self.rows.count() - 1, WindowRow(self, window, where, groups.get(window.key, [])))
        if not ctx.windows.ready:
            self.subtitle.setText("Looking for windows…")
        else:
            self.subtitle.setText(f"{len(windows)} application window{'s' if len(windows) != 1 else ''} open"
                                  + (f" · {len(shown)} shown" if needle else ""))
        self.empty.setVisible(ctx.windows.ready and not windows)

    def run(self, action) -> None:  # noqa: ANN001 - callable(WindowController) -> list[Result]
        controller = self.ctx.window_control
        run_async(lambda _cancel: action(controller),
                  lambda results: [self.ctx.actions.message.emit(r.message) for r in results if not r.ok])

    def show_tiling_menu(self, window: TrackedWindow, pos) -> None:  # noqa: ANN001
        """What dynamic tiling can do with this window: monocle, stacks, floating, a window rule."""
        tiler = self.ctx.tiler
        menu = QMenu(self)
        found = tiler.tile_of(window.key)
        if found is not None:
            group, unit = found
            menu.addAction("Monocle off" if group.monocle == unit.id else "Monocle (fill the screen)",
                           lambda: tiler.toggle_monocle(window))
            others = [u.window for u in group.units if u.id != unit.id]
            if others:
                stack_menu = menu.addMenu("Stack with")
                for other in others:
                    stack_menu.addAction(other.display_title, lambda o=other: tiler.stack_with(window, o))
            if unit.stacked:
                if unit.window.key != window.key:
                    menu.addAction("Show in its stack", lambda: tiler.show_stacked(window))
                menu.addAction("Take out of the stack", lambda: tiler.unstack(window))
            menu.addSeparator()
        floating = window.key in tiler._user_float
        menu.addAction("Tile this window again" if floating else "Float this window (until it closes)",
                       lambda: tiler.toggle_float(window))
        menu.addAction("Window rule for this program…", lambda: self._new_rule(window))
        menu.exec(pos)
        self.refresh()

    def _new_rule(self, window: TrackedWindow) -> None:
        dialog = RuleDialog(self.ctx, WindowRule.for_window(window, FLOAT), self)
        if dialog.exec():
            self.ctx.tiler.set_rules(self.ctx.tiler.rules() + [dialog.current()])

    def show_add_menu(self, window: TrackedWindow, pos) -> None:  # noqa: ANN001
        menu = QMenu(self)
        for workspace in self.ctx.workspaces.all():
            action = QAction(workspace.name, menu)
            action.triggered.connect(lambda _=False, wid=workspace.id: self._add(wid, window))
            menu.addAction(action)
        if self.ctx.workspaces.all():
            menu.addSeparator()
        menu.addAction("New workspace…", lambda: self._add_new(window))
        menu.exec(pos)

    def _add(self, workspace_id: str, window: TrackedWindow) -> None:
        workspace = self.ctx.workspaces.get(workspace_id)
        if workspace is None or self.ctx.windows.get(window.key) is None:
            return
        entry = self.ctx.workspaces.add_window(workspace_id, window)
        self.ctx.wm.choose(workspace_id, entry.id, window.key, place=False)
        self.ctx.actions.message.emit(f"Added {window.display_title} to {workspace.name}")

    def _add_new(self, window: TrackedWindow) -> None:
        from PySide6.QtWidgets import QInputDialog, QMessageBox

        name, ok = QInputDialog.getText(self, "New workspace", "Workspace name:")
        if not ok:
            return
        try:
            workspace = self.ctx.workspaces.create(name)
        except ValueError as exc:
            QMessageBox.warning(self, "Cannot create workspace", str(exc))
            return
        self._add(workspace.id, window)
