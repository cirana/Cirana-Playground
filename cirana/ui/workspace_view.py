"""Workspaces page: create, rename and delete groups of windows, fill them (catalog or open windows, drag and drop),
choose and preview their layout, and activate them with one click."""
from __future__ import annotations

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QDrag
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFrame, QHBoxLayout, QInputDialog, QLabel, QMenu, QMessageBox, QPushButton,
    QSizePolicy, QSpinBox, QToolButton, QVBoxLayout, QWidget,
)

from cirana.core.workspace_controller import EntryState, EntryStatus
from cirana.core.workspaces import Workspace
from cirana.ui.common import EmptyState, label, make_scroll
from cirana.ui.context import AppContext
from cirana.ui.dialogs import AppPickerDialog, WindowPickerDialog, choose_window
from cirana.ui.icons import icon
from cirana.ui.theme import ON_ACCENT, TEXT, TEXT2
from cirana.widgets.dnd import app_id_from, entry_from, entry_mime
from cirana.widgets.layout_preview import LayoutPreview
from cirana.widgets.workspace_card import STATE_TEXT, WorkspaceCard, entry_icon_app, repolish
from cirana.ui.wm_dialogs import AutoLayoutPicker
from cirana.wm.layout import AUTO, BSP, FREE, GRID, MAIN_SIDE, SIDE_BY_SIDE, LayoutSpec
from cirana.wm.monitors import resolve

# (label, kind, grid columns): "Grid 2×2" and "Dynamic grid" are the same layout with fixed / automatic columns.
LAYOUT_CHOICES = [
    ("Side by side", SIDE_BY_SIDE, 0),
    ("Main + side panel", MAIN_SIDE, 0),
    ("Grid 2×2", GRID, 2),
    ("Dynamic grid", GRID, 0),
    ("Saved positions", FREE, 0),
    ("BSP (split per window)", BSP, 0),
    ("Automatic (by window count)", AUTO, 0),
]
STATE_KIND = {EntryState.RUNNING: "good", EntryState.LAUNCHING: "busy", EntryState.AMBIGUOUS: "warn",
              EntryState.UNAVAILABLE: "bad"}


def _icon_button(glyph: str, tip: str) -> QToolButton:
    button = QToolButton()
    button.setObjectName("IconButton")
    button.setIcon(icon(glyph, TEXT2, 16))
    button.setToolTip(tip)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    return button


def _button(text: str, glyph: str, name: str = "SecondaryButton", tip: str = "") -> QPushButton:
    button = QPushButton(f" {text}")
    button.setObjectName(name)
    button.setIcon(icon(glyph, ON_ACCENT if name == "PrimaryButton" else TEXT, 16))
    button.setToolTip(tip)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    return button


class EntryRow(QFrame):
    """One window of the workspace. Drag it to reorder (its place in tiled layouts) or onto the drop zone to remove
    it; drop an application card on it to insert the application here."""

    def __init__(self, view: "WorkspaceView", workspace: Workspace, status: EntryStatus, index: int) -> None:
        super().__init__()
        ctx = view.ctx
        self._view = view
        self.workspace_id = workspace.id
        self.entry_id = status.entry.id
        self.index = index
        self._press: QPoint | None = None
        self.setObjectName("WorkspaceAppRow")
        self.setAcceptDrops(True)
        self.setMinimumHeight(64)
        row = QHBoxLayout(self)
        row.setContentsMargins(8, 8, 10, 8)
        row.setSpacing(10)

        grip = QLabel()
        grip.setPixmap(icon("grip", TEXT2, 16).pixmap(16, 16))
        grip.setToolTip("Drag to reorder; drop on the area below to remove")
        grip.setCursor(Qt.CursorShape.OpenHandCursor)
        row.addWidget(grip)
        number = label(str(index + 1), "Muted")
        number.setFixedWidth(16)
        row.addWidget(number)
        tile = QLabel()
        tile.setObjectName("IconTile")
        tile.setFixedSize(40, 40)
        tile.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ctx.icons.request(entry_icon_app(ctx, status), 28, tile, tile.setPixmap)
        row.addWidget(tile)

        text = QVBoxLayout()
        text.setSpacing(1)
        top = QHBoxLayout()
        top.setSpacing(8)
        name = label(ctx.workspaces.display_name(status.entry), "AppName")
        name.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        name.setMinimumWidth(40)
        top.addWidget(name, 0)
        pill = label(STATE_TEXT[status.state], "StatePill")
        pill.setProperty("kind", STATE_KIND.get(status.state, "idle"))
        pill.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        top.addWidget(pill, 0, Qt.AlignmentFlag.AlignVCenter)
        top.addStretch(1)
        text.addLayout(top)
        detail = status.window.display_title if status.window is not None else (
            f"{len(status.candidates)} open windows fit" if status.state is EntryState.AMBIGUOUS else
            status.entry.rule.exe.rsplit("\\", 1)[-1] if not status.entry.app_id else "")
        if detail:
            sub = label(detail, "AppCategory")
            sub.setToolTip(detail)
            sub.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)  # a long title never widens it
            text.addWidget(sub)
        state, why = ctx.tiler.status(status.window) if status.window is not None else ("", "")
        if state:
            shown = state + (f" ({why})" if why else "")
            text.addWidget(label(shown[0].upper() + shown[1:], "Muted"))
        if status.message:
            note = label(status.message, "RowWarning", wrap=True)
            text.addWidget(note)
        text.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        row.addLayout(text, 1)

        if status.entry.app_id:
            start = QCheckBox("Start")
            start.setToolTip("Start this application when the workspace is activated and it is not open")
            start.setChecked(status.entry.launch)
            start.toggled.connect(lambda on: ctx.workspaces.set_launch(workspace.id, status.entry.id, on))
            row.addWidget(start)
        if status.state is not EntryState.LAUNCHING:
            choose = _icon_button("link", "Choose which open window belongs here")
            choose.clicked.connect(lambda: view.choose_window_for(workspace.id, status.entry.id))
            row.addWidget(choose)
        if status.window is not None:
            show = _icon_button("external", "Bring this window to the front")
            show.clicked.connect(lambda: view.show_window(status.window))
            row.addWidget(show)
        remove = _icon_button("x", "Remove from workspace (the application keeps running)")
        remove.clicked.connect(lambda: ctx.workspaces.remove_entry(workspace.id, status.entry.id))
        row.addWidget(remove)

    # -- dragging this row -----------------------------------------------------------------------------------
    def mousePressEvent(self, event) -> None:
        self._press = event.position().toPoint() if event.button() == Qt.MouseButton.LeftButton else None
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if (event.buttons() & Qt.MouseButton.LeftButton and self._press is not None
                and (event.position().toPoint() - self._press).manhattanLength() >= QApplication.startDragDistance()):
            self._press = None
            drag = QDrag(self)
            drag.setMimeData(entry_mime(self.workspace_id, self.entry_id, ""))
            pixmap = self.grab()
            drag.setPixmap(pixmap)
            drag.setHotSpot(QPoint(24, pixmap.height() // 2))
            self._view.set_entry_drag(True)
            try:
                drag.exec(Qt.DropAction.MoveAction)
            finally:
                self._view.set_entry_drag(False)
            return
        super().mouseMoveEvent(event)

    # -- dropping onto this row ------------------------------------------------------------------------------
    def _accepts(self, mime) -> bool:  # noqa: ANN001
        workspace_id, entry_id = entry_from(mime)
        return bool(app_id_from(mime)) or (workspace_id == self.workspace_id and entry_id != self.entry_id)

    def dragEnterEvent(self, event) -> None:
        if self._accepts(event.mimeData()):
            event.acceptProposedAction()
            self.setProperty("dropTarget", "true")
            repolish(self)

    def dragLeaveEvent(self, event) -> None:
        self.setProperty("dropTarget", "false")
        repolish(self)

    def dropEvent(self, event) -> None:
        self.dragLeaveEvent(event)
        mime = event.mimeData()
        if not self._accepts(mime):
            return
        event.acceptProposedAction()
        workspaces = self._view.ctx.workspaces
        app_id = app_id_from(mime)
        if app_id:
            entry = workspaces.add_app(self.workspace_id, app_id)
            if entry is None:
                self._view.ctx.actions.message.emit("That application is already in this workspace")
                return
            workspaces.move_entry(self.workspace_id, entry.id, self.index)
        else:
            workspaces.move_entry(self.workspace_id, entry_from(mime)[1], self.index)


class WorkspaceView(QWidget):
    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self._selected = ctx.workspaces.active_id
        self._syncing = False
        self.setAcceptDrops(True)  # dropping on the page adds to the selected workspace

        # -- left: the workspaces ---------------------------------------------------------------------------
        left = QVBoxLayout()
        left.setSpacing(10)
        left.addWidget(label("Workspaces", "PageTitle"))
        left.addWidget(label("Groups of windows you arrange and bring up together.", "PageSubtitle", wrap=True))
        new_button = _button("New workspace", "plus", "PrimaryButton")
        new_button.clicked.connect(self.create_workspace)
        left.addWidget(new_button)
        self.list_host = QWidget()
        self.list_layout = QVBoxLayout(self.list_host)
        self.list_layout.setContentsMargins(0, 0, 4, 0)
        self.list_layout.setSpacing(8)
        self.list_layout.addStretch(1)
        left.addWidget(make_scroll(self.list_host), 1)
        left_box = QWidget()
        left_box.setLayout(left)
        left_box.setFixedWidth(300)

        # -- right: the selected workspace ------------------------------------------------------------------
        self.empty = QWidget()
        empty_layout = QVBoxLayout(self.empty)
        empty_layout.setContentsMargins(0, 36, 0, 0)
        empty_layout.addWidget(EmptyState(
            "workspaces", "No workspace yet",
            "Create a workspace, then add applications from the catalog or windows that are already open. "
            "Activating it opens what is missing, arranges the windows in the layout you choose and brings them "
            "to the front. CIRANA never embeds, hides or closes your windows."), 1)

        self.detail = QWidget()
        detail = QVBoxLayout(self.detail)
        detail.setContentsMargins(0, 0, 0, 0)
        detail.setSpacing(12)
        self.title = label("", "PageTitle")
        self.subtitle = label("", "PageSubtitle")
        for text in (self.title, self.subtitle):
            text.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)  # long names never squeeze
        detail.addWidget(self.title)
        detail.addWidget(self.subtitle)
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        self.front_button = _button("Bring to front", "external",
                                    tip="Show this workspace's open windows on top, without moving them")
        self.front_button.clicked.connect(lambda: ctx.wm.bring_to_front(self._selected))
        self.apply_button = _button("Apply layout", "apps", tip="Arrange this workspace's open windows now")
        self.apply_button.clicked.connect(lambda: ctx.wm.apply_layout(self._selected))
        self.activate_button = _button("Activate", "play", "PrimaryButton",
                                       "Open what is missing, arrange the windows and bring them to the front")
        self.activate_button.clicked.connect(lambda: self.activate(self._selected))
        more = _icon_button("list", "More")
        more.clicked.connect(lambda: self.show_item_menu(self._selected, more.mapToGlobal(more.rect().bottomLeft())))
        for widget in (self.activate_button, self.apply_button, self.front_button, more):
            title_row.addWidget(widget)
        title_row.addStretch(1)
        detail.addLayout(title_row)

        # layout picker + preview
        layout_panel = QFrame()
        layout_panel.setObjectName("Panel")
        panel = QHBoxLayout(layout_panel)
        panel.setContentsMargins(16, 12, 16, 12)
        panel.setSpacing(16)
        controls = QVBoxLayout()
        controls.setSpacing(8)
        controls.addWidget(label("Layout", "SectionTitle"))
        self.kind_combo = QComboBox()
        for text, _kind, _columns in LAYOUT_CHOICES:
            self.kind_combo.addItem(text)
        self.kind_combo.currentIndexChanged.connect(self._layout_edited)
        self.monitor_combo = QComboBox()
        self.monitor_combo.setToolTip("The monitor tiled layouts fill. If it is unplugged, the primary one is used.")
        self.monitor_combo.currentIndexChanged.connect(self._layout_edited)
        self.gap_spin = QSpinBox()
        self.gap_spin.setRange(0, 64)
        self.gap_spin.setSuffix(" px gap")
        self.gap_spin.valueChanged.connect(self._layout_edited)
        self.ratio_spin = QSpinBox()
        self.ratio_spin.setRange(20, 80)
        self.ratio_spin.setSuffix(" % main")
        self.ratio_spin.setToolTip("Width of the main (first) window")
        self.ratio_spin.valueChanged.connect(self._layout_edited)
        self.auto_picker = AutoLayoutPicker()
        self.auto_picker.changed.connect(self._layout_edited)
        for widget in (self.kind_combo, self.auto_picker, self.monitor_combo):
            controls.addWidget(widget)
        spins = QHBoxLayout()
        spins.addWidget(self.gap_spin)
        spins.addWidget(self.ratio_spin)
        controls.addLayout(spins)
        self.dynamic_check = QCheckBox("Dynamic tiling")
        self.dynamic_check.setToolTip(
            "While this workspace is active, re-tile its windows automatically whenever one opens, closes, is "
            "minimized or restored. Only windows that clearly belong to the workspace are arranged.")
        self.dynamic_check.toggled.connect(self._dynamic_toggled)
        controls.addWidget(self.dynamic_check)
        self.tiling_state = label("", "TilingState")
        controls.addWidget(self.tiling_state)
        self.layout_hint = label("", "Muted", wrap=True)
        controls.addWidget(self.layout_hint)
        controls.addStretch(1)
        panel.addLayout(controls, 1)
        self.preview = LayoutPreview()
        panel.addWidget(self.preview, 1)
        detail.addWidget(layout_panel)

        # entries
        self.rows_host = QWidget()
        self.rows_layout = QVBoxLayout(self.rows_host)
        self.rows_layout.setContentsMargins(0, 0, 4, 0)
        self.rows_layout.setSpacing(8)
        self.drop_zone = QFrame()
        self.drop_zone.setObjectName("DropZone")
        self.drop_zone.setAcceptDrops(True)
        zone = QVBoxLayout(self.drop_zone)
        zone.setContentsMargins(18, 14, 18, 14)
        self.drop_hint = label("", "Muted", wrap=True)
        self.drop_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        add_apps = _button("Add applications…", "plus", tip="From the application catalog")
        add_apps.clicked.connect(lambda: self.add_applications(self._selected))
        add_windows = _button("Add open windows…", "apps", tip="Windows that are open right now")
        add_windows.clicked.connect(lambda: self.add_windows(self._selected))
        buttons.addWidget(add_apps)
        buttons.addWidget(add_windows)
        buttons.addStretch(1)
        zone.addWidget(self.drop_hint)
        zone.addLayout(buttons)
        self.rows_layout.addWidget(self.drop_zone)
        self.rows_layout.addStretch(1)
        detail.addWidget(make_scroll(self.rows_host), 1)
        self._entry_drag = False

        right = QVBoxLayout()
        right.addWidget(self.empty)
        right.addWidget(self.detail)
        root = QHBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(24)
        root.addWidget(left_box)
        root.addLayout(right, 1)

    # -- rendering ---------------------------------------------------------------------------------
    def refresh(self) -> None:
        ctx = self.ctx
        workspaces = ctx.workspaces.all()
        if not any(w.id == self._selected for w in workspaces):
            self._selected = ctx.workspaces.active_id or (workspaces[0].id if workspaces else "")
        while self.list_layout.count() > 1:
            item = self.list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for workspace in workspaces:
            card = WorkspaceCard(ctx, workspace, workspace.id == ctx.workspaces.active_id, False,
                                 workspace.id == self._selected)
            card.selected.connect(self.select)
            card.activate_requested.connect(self.activate)
            card.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            card.customContextMenuRequested.connect(
                lambda pos, c=card: self.show_item_menu(c.workspace_id, c.mapToGlobal(pos)))
            self.list_layout.insertWidget(self.list_layout.count() - 1, card)
        current = ctx.workspaces.get(self._selected)
        self.empty.setVisible(current is None)
        self.detail.setVisible(current is not None)
        if current is None:
            return
        statuses = ctx.wm.statuses(current.id)
        active = current.id == ctx.workspaces.active_id
        busy = ctx.wm.busy(current.id)
        self.title.setText(current.name)
        open_count = sum(1 for s in statuses if s.window is not None)
        count = len(statuses)
        tiling = " · tiling" if ctx.tiler.manages(current.id) else ""
        self.subtitle.setText(f"{count} window{'s' if count != 1 else ''} · {open_count} open"
                              + (" · active" if active else "") + tiling + (" · working…" if busy else ""))
        self.activate_button.setEnabled(bool(statuses))
        self.apply_button.setEnabled(open_count > 0)
        self.front_button.setEnabled(open_count > 0)
        self._sync_layout_controls(current, statuses)
        while self.rows_layout.count() > 2:  # drop zone + stretch stay
            item = self.rows_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for index, status in enumerate(statuses):
            self.rows_layout.insertWidget(self.rows_layout.count() - 2, EntryRow(self, current, status, index))
        self._update_drop_hint()

    def _sync_layout_controls(self, workspace: Workspace, statuses: list[EntryStatus]) -> None:
        spec = workspace.layout
        monitors = self.ctx.windows.backend.monitors()
        self._syncing = True
        try:
            index = next((i for i, (_t, kind, columns) in enumerate(LAYOUT_CHOICES)
                          if kind == spec.kind and (kind != GRID or columns == (2 if spec.columns == 2 else 0))), 3)
            self.kind_combo.setCurrentIndex(index)
            self.auto_picker.set_auto(spec.auto)
            self.monitor_combo.clear()
            self.monitor_combo.addItem("Primary monitor", None)
            for monitor in monitors:
                self.monitor_combo.addItem(monitor.label, monitor.ref())
            chosen = 0
            if spec.monitor is not None:
                found = resolve(spec.monitor, monitors)
                chosen = next((i + 1 for i, m in enumerate(monitors) if found is not None and m.device == found.device),
                              0)
            self.monitor_combo.setCurrentIndex(chosen)
            self.gap_spin.setValue(spec.gap)
            self.ratio_spin.setValue(round(spec.ratio * 100))
            self.dynamic_check.setChecked(spec.dynamic)
        finally:
            self._syncing = False
        free = spec.kind == FREE
        self.ratio_spin.setVisible(spec.kind in (MAIN_SIDE, AUTO))
        self.auto_picker.setVisible(spec.kind == AUTO)
        self.monitor_combo.setEnabled(not free)
        self.gap_spin.setEnabled(not free)
        self.dynamic_check.setEnabled(not free)
        tiler = self.ctx.tiler
        if tiler.global_on:
            state, kind = "All windows are tiled (Windows page); that layout is in charge", "good"
        elif free:
            state, kind = ("Dynamic tiling works with tiled layouts only" if spec.dynamic else ""), "idle"
        elif not spec.dynamic:
            state, kind = "", "idle"
        elif tiler.paused(workspace.id):
            state, kind = "Tiling paused while you move a window", "warn"
        elif tiler.manages(workspace.id):
            stacks = tiler.stack_count()
            state = ("Tiling: on, following this workspace's windows"
                     + (f" · {stacks} stack{'s' if stacks != 1 else ''}" if stacks else "")
                     + (" · monocle" if tiler.monocle_on() else ""))
            kind = "good"
        else:
            state, kind = "Tiling: on, starts when this workspace is activated", "idle"
        self.tiling_state.setText(state)
        self.tiling_state.setVisible(bool(state))
        self.tiling_state.setProperty("kind", kind)
        repolish(self.tiling_state)
        if free:
            hint = "Windows go back where they were when you chose “Save current positions” (⋯ menu)."
        elif spec.dynamic:
            hint = ("Minimized and maximized windows are left out until restored. Drag a window onto another one "
                    "to swap them; keyboard shortcuts focus, swap, resize, stack and switch monocle.")
        else:
            hint = "Applied only when you activate or press Apply layout; move windows freely afterwards."
        self.layout_hint.setText(hint)
        if tiler.manages(workspace.id):  # what dynamic tiling lays out right now
            tiled = {entry_id for entry_id, _window in tiler.tile_set(workspace.id)}
            shown = [s for s in statuses if s.entry.id in tiled]
        else:  # what activating would lay out
            shown = [s for s in statuses if s.placeable]
        monitor = resolve(spec.monitor, monitors)
        entries = [(s.entry.id, self.ctx.workspaces.display_name(s.entry)) for s in shown]
        preview_spec = spec
        if tiler.manages(workspace.id):  # stacks share a tile: one tile per stack, named after its windows
            group = next((g for g in tiler.shown_groups() if g.id == workspace.id), None)
            if group is not None:
                preview_spec = group.spec
                names = dict(entries)
                entries = [(u.id, " + ".join(names.get(s, w.display_title) for s, w in u.members))
                           for u in group.units]
        self.preview.set_layout(preview_spec, entries, monitor.work if monitor else None)

    def _layout_edited(self, _value: object = None) -> None:
        if self._syncing:
            return
        workspace = self.ctx.workspaces.get(self._selected)
        if workspace is None:
            return
        _text, kind, columns = LAYOUT_CHOICES[max(0, self.kind_combo.currentIndex())]
        old = workspace.layout
        spec = LayoutSpec(kind=kind, gap=self.gap_spin.value(), ratio=self.ratio_spin.value() / 100,
                          columns=columns, weights=list(old.weights), monitor=self.monitor_combo.currentData(),
                          free=dict(old.free), dynamic=old.dynamic, heights=list(old.heights), bsp=dict(old.bsp),
                          auto=self.auto_picker.auto(), stacks=[dict(s) for s in old.stacks])
        if spec.to_dict() != old.to_dict():
            self.ctx.workspaces.set_layout(workspace.id, spec)

    def _dynamic_toggled(self, on: bool) -> None:
        if self._syncing or not self.ctx.workspaces.get(self._selected):
            return
        self.ctx.workspaces.set_dynamic(self._selected, on)
        if on and self.ctx.tiler.reflow(self._selected):
            self.ctx.actions.message.emit("Dynamic tiling is on: the windows follow the layout by themselves")
        elif on:
            self.ctx.actions.message.emit("Dynamic tiling is on; it takes over when this workspace is activated")
        else:
            self.ctx.actions.message.emit("Dynamic tiling is off: windows stay where they are")

    def select(self, workspace_id: str) -> None:
        if workspace_id != self._selected and self.ctx.workspaces.get(workspace_id):
            self._selected = workspace_id
            self.refresh()

    @property
    def selected_id(self) -> str:
        return self._selected

    # -- actions ----------------------------------------------------------------------------------------
    def create_workspace(self) -> None:
        name, ok = QInputDialog.getText(self, "New workspace", "Workspace name:")
        if not ok:
            return
        try:
            workspace = self.ctx.workspaces.create(name)
        except ValueError as exc:
            QMessageBox.warning(self, "Cannot create workspace", str(exc))
            return
        self._selected = workspace.id
        self.refresh()

    def rename(self, workspace_id: str) -> None:
        workspace = self.ctx.workspaces.get(workspace_id)
        if workspace is None:
            return
        name, ok = QInputDialog.getText(self, "Rename workspace", "Workspace name:", text=workspace.name)
        if not ok:
            return
        try:
            self.ctx.workspaces.rename(workspace_id, name)
        except ValueError as exc:
            QMessageBox.warning(self, "Cannot rename workspace", str(exc))

    def delete(self, workspace_id: str) -> None:
        workspace = self.ctx.workspaces.get(workspace_id)
        if workspace is None:
            return
        answer = QMessageBox.question(
            self, "Delete workspace",
            f"Delete “{workspace.name}”?\n\nOnly the workspace is removed. Its applications stay installed and "
            "every window stays open.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.ctx.wm.close(workspace_id)
            self.ctx.workspaces.delete(workspace_id)

    def activate(self, workspace_id: str) -> None:
        workspace = self.ctx.workspaces.get(workspace_id)
        if workspace is None:
            return
        if not workspace.apps:
            self.ctx.actions.message.emit(f"{workspace.name} is empty. Add applications or open windows first")
            return
        self.ctx.wm.activate(workspace_id)

    def add_applications(self, workspace_id: str) -> None:
        workspace = self.ctx.workspaces.get(workspace_id)
        if workspace is None:
            return
        dialog = AppPickerDialog(self.ctx, f"Add to {workspace.name}", ok_text="Add", multi=True,
                                 exclude={a.app_id for a in workspace.apps if a.app_id}, parent=self)
        if dialog.exec():
            for app_id in dialog.selected_ids():
                self.ctx.workspaces.add_app(workspace_id, app_id)

    def add_windows(self, workspace_id: str) -> None:
        workspace = self.ctx.workspaces.get(workspace_id)
        if workspace is None:
            return
        bound = {s.window.key for s in self.ctx.wm.statuses(workspace_id) if s.window is not None}
        dialog = WindowPickerDialog(self.ctx, f"Add open windows to {workspace.name}", exclude=bound, parent=self)
        if dialog.exec():
            for window in dialog.selected_windows():
                entry = self.ctx.workspaces.add_window(workspace_id, window)
                self.ctx.wm.choose(workspace_id, entry.id, window.key, place=False)

    def choose_window_for(self, workspace_id: str, entry_id: str) -> None:
        workspace = self.ctx.workspaces.get(workspace_id)
        entry = workspace.entry(entry_id) if workspace else None
        if entry is None:
            return
        name = self.ctx.workspaces.display_name(entry)
        candidates = self.ctx.wm.candidates(workspace_id, entry_id)
        hint = ""
        if not candidates:  # e.g. a program that showed its window through another one (a launcher, a tray app)
            taken = {s.window.key for s in self.ctx.wm.statuses(workspace_id) if s.window is not None}
            candidates = [w for w in self.ctx.windows.windows() if w.key not in taken]
            hint = (f"None of the open windows was recognised as {name}. Pick its window: CIRANA remembers which "
                    "program shows it and recognises it by itself from then on.")
        if not candidates:
            self.ctx.actions.message.emit("No application window is open")
            return
        window = choose_window(self.ctx, name, candidates, self, hint)
        if window is not None:
            self.ctx.wm.choose(workspace_id, entry_id, window.key)

    def show_window(self, window) -> None:  # noqa: ANN001
        from cirana.core.background import run_async

        controller = self.ctx.window_control
        run_async(lambda _cancel: controller.bring_to_front([window]),
                  lambda results: [self.ctx.actions.message.emit(r.message) for r in results if not r.ok])

    def show_item_menu(self, workspace_id: str, pos: QPoint) -> None:
        workspace = self.ctx.workspaces.get(workspace_id)
        if workspace is None:
            return
        wm = self.ctx.wm
        menu = QMenu(self)
        menu.addAction("Activate", lambda: self.activate(workspace_id))
        menu.addAction("Apply layout", lambda: wm.apply_layout(workspace_id))
        menu.addAction("Bring to front", lambda: wm.bring_to_front(workspace_id))
        menu.addAction("Minimize its windows", lambda: wm.minimize(workspace_id))
        menu.addSeparator()
        menu.addAction("Save current positions", lambda: wm.save_positions(workspace_id))
        close = menu.addAction("Close workspace (windows stay open)", lambda: wm.close(workspace_id))
        close.setEnabled(workspace_id == self.ctx.workspaces.active_id or wm.busy(workspace_id))
        menu.addSeparator()
        menu.addAction("Rename…", lambda: self.rename(workspace_id))
        menu.addAction("Delete…", lambda: self.delete(workspace_id))
        menu.exec(pos)

    # -- drag & drop onto the page ------------------------------------------------------------------------------
    def set_entry_drag(self, on: bool) -> None:
        self._entry_drag = on
        self._update_drop_hint()

    def _update_drop_hint(self) -> None:
        workspace = self.ctx.workspaces.get(self._selected)
        if self._entry_drag:
            text = "Drop here to remove it from the workspace (the application keeps running)"
        elif workspace is not None and workspace.apps:
            text = "Drag applications here from Applications, Favorites or Home, or add open windows"
        else:
            text = "This workspace is empty. Drag applications here, or add windows that are open right now"
        self.drop_hint.setText(text)
        self.drop_zone.setProperty("removing", "true" if self._entry_drag else "false")
        repolish(self.drop_zone)

    def dragEnterEvent(self, event) -> None:
        mime = event.mimeData()
        workspace_id, _entry = entry_from(mime)
        if (app_id_from(mime) or workspace_id == self._selected) and self.ctx.workspaces.get(self._selected):
            event.acceptProposedAction()
            self.drop_zone.setProperty("active", "true")
            repolish(self.drop_zone)

    def dragLeaveEvent(self, event) -> None:
        self.drop_zone.setProperty("active", "false")
        repolish(self.drop_zone)

    def dropEvent(self, event) -> None:
        self.dragLeaveEvent(event)
        mime = event.mimeData()
        app_id = app_id_from(mime)
        workspace_id, entry_id = entry_from(mime)
        if app_id and self._selected:
            event.acceptProposedAction()
            self.ctx.actions.add_to_workspace(app_id, self._selected)
        elif entry_id and workspace_id == self._selected:  # dragged out of the list: remove
            event.acceptProposedAction()
            self.ctx.workspaces.remove_entry(workspace_id, entry_id)
