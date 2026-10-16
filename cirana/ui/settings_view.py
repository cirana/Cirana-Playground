"""Settings: scanning, categories, custom applications, hidden applications, data location."""
from __future__ import annotations

import os
import time

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QFrame, QHBoxLayout, QInputDialog, QListWidget, QListWidgetItem, QMessageBox,
    QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from cirana import __version__
from cirana.core.dynamic_tiling import BEHIND, MINIMIZE
from cirana.core.models import LaunchType
from cirana.ui.common import label, make_scroll
from cirana.ui.context import AppContext
from cirana.ui.wm_dialogs import RuleDialog, ShortcutsDialog
from cirana.wm.rules import ACTION_NAMES, WORKSPACE


def _card(title: str, hint: str = "") -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("Card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(20, 16, 20, 18)
    layout.setSpacing(10)
    layout.addWidget(label(title, "SectionTitle"))
    if hint:
        layout.addWidget(label(hint, "Muted", wrap=True))
    return frame, layout


def _button(text: str, handler, name: str = "SecondaryButton") -> QPushButton:
    button = QPushButton(text)
    button.setObjectName(name)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.clicked.connect(handler)
    return button


class SettingsView(QWidget):
    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        body = QWidget()
        root = QVBoxLayout(body)
        root.setContentsMargins(28, 24, 28, 28)
        root.setSpacing(14)
        root.addWidget(label("Settings", "PageTitle"))
        root.addWidget(label(f"CIRANA {__version__} · everything stays on this computer", "PageSubtitle"))

        # General / scanning
        card, lay = _card("Application discovery",
                          "Reads the Start Menu, packaged apps and App Paths registrations. No disk-wide scan.")
        self.scan_on_start = QCheckBox("Scan for applications when CIRANA starts")
        self.scan_on_start.setChecked(bool(ctx.settings.get("scan_on_startup")))
        self.scan_on_start.toggled.connect(lambda v: ctx.settings.set("scan_on_startup", bool(v)))
        lay.addWidget(self.scan_on_start)
        row = QHBoxLayout()
        self.scan_button = _button("Scan now", ctx.scanner.start, "PrimaryButton")
        row.addWidget(self.scan_button)
        self.scan_status = label("", "Muted", wrap=True)
        row.addWidget(self.scan_status, 1)
        lay.addLayout(row)
        root.addWidget(card)

        # Window management: shortcuts, stacks, focus follows the mouse
        card, lay = _card("Keyboard and focus",
                          "Global shortcuts move the focus, swap and resize tiles, switch monocle and stacks. They "
                          "work in every application while CIRANA runs.")
        row = QHBoxLayout()
        self.hotkeys_on = QCheckBox("Use global shortcuts")
        self.hotkeys_on.toggled.connect(lambda on: None if self._syncing else ctx.hotkeys.set_enabled(on))
        row.addWidget(self.hotkeys_on)
        row.addWidget(_button("Edit shortcuts…", self._edit_shortcuts))
        row.addStretch(1)
        lay.addLayout(row)
        self.hotkey_status = label("", "Muted", wrap=True)
        lay.addWidget(self.hotkey_status)
        row = QHBoxLayout()
        row.addWidget(label("Stacked windows not shown:", ""))
        self.stack_mode = QComboBox()
        self.stack_mode.addItem("Under the shown one", BEHIND)
        self.stack_mode.addItem("Minimized", MINIMIZE)
        self.stack_mode.setToolTip("Under the shown one: nothing is hidden. Minimized: their taskbar buttons stay, "
                                   "and they are shown again when they leave the stack or CIRANA closes.")
        self.stack_mode.currentIndexChanged.connect(
            lambda _i: None if self._syncing else ctx.tiler.set_stack_mode(self.stack_mode.currentData()))
        row.addWidget(self.stack_mode)
        row.addStretch(1)
        lay.addLayout(row)
        row = QHBoxLayout()
        self.ffm = QCheckBox("Focus follows the mouse")
        self.ffm.setToolTip("Uses Windows' own “activate a window by hovering over it” for this session (all "
                            "windows, not only tiled ones). Your own setting comes back when CIRANA closes.")
        self.ffm.toggled.connect(self._ffm_edited)
        row.addWidget(self.ffm)
        self.ffm_delay = QSpinBox()
        self.ffm_delay.setRange(0, 2000)
        self.ffm_delay.setSingleStep(50)
        self.ffm_delay.setSuffix(" ms delay")
        self.ffm_delay.valueChanged.connect(self._ffm_edited)
        row.addWidget(self.ffm_delay)
        self.ffm_raise = QCheckBox("Bring it to the front too")
        self.ffm_raise.toggled.connect(self._ffm_edited)
        row.addWidget(self.ffm_raise)
        row.addStretch(1)
        lay.addLayout(row)
        root.addWidget(card)

        card, lay = _card("Window rules",
                          "What CIRANA does with a program's windows: float them, ignore them completely, or "
                          "make them part of a workspace. The first matching rule wins.")
        self.rule_list = QListWidget()
        self.rule_list.setFixedHeight(130)
        self.rule_list.itemDoubleClicked.connect(lambda _item: self._edit_rule())
        lay.addWidget(self.rule_list)
        row = QHBoxLayout()
        row.addWidget(_button("New rule…", self._add_rule))
        row.addWidget(_button("Edit…", self._edit_rule))
        row.addWidget(_button("Up", lambda: self._move_rule(-1)))
        row.addWidget(_button("Down", lambda: self._move_rule(1)))
        row.addWidget(_button("Delete", self._delete_rule, "DangerButton"))
        row.addStretch(1)
        lay.addLayout(row)
        root.addWidget(card)
        self._syncing = False
        ctx.hotkeys.status_changed.connect(self._sync_window_management)

        # Categories
        card, lay = _card("Categories", "Rename any category or add your own. Changes to individual apps are never overwritten by a rescan.")
        self.category_list = QListWidget()
        self.category_list.setFixedHeight(190)
        lay.addWidget(self.category_list)
        row = QHBoxLayout()
        row.addWidget(_button("Rename…", self._rename_category))
        row.addWidget(_button("New category…", self._add_category))
        row.addWidget(_button("Delete", self._delete_category, "DangerButton"))
        row.addStretch(1)
        lay.addLayout(row)
        root.addWidget(card)

        # Custom apps
        card, lay = _card("Custom applications", "Executables or commands you added yourself.")
        self.custom_list = QListWidget()
        self.custom_list.setFixedHeight(110)
        lay.addWidget(self.custom_list)
        row = QHBoxLayout()
        row.addWidget(_button("Add executable…", self._add_exe))
        row.addWidget(_button("Add command…", self._add_command))
        row.addWidget(_button("Remove", self._remove_custom, "DangerButton"))
        row.addStretch(1)
        lay.addLayout(row)
        root.addWidget(card)

        # Hidden
        card, lay = _card("Hidden applications", "Applications you removed from CIRANA's views.")
        self.hidden_list = QListWidget()
        self.hidden_list.setFixedHeight(110)
        lay.addWidget(self.hidden_list)
        row = QHBoxLayout()
        row.addWidget(_button("Restore selected", self._unhide))
        row.addStretch(1)
        lay.addLayout(row)
        root.addWidget(card)

        # Data
        card, lay = _card("Your data")
        lay.addWidget(label(str(ctx.settings.root), "Muted"))
        row = QHBoxLayout()
        row.addWidget(_button("Open data folder", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(ctx.settings.root)))))
        row.addWidget(_button("Open log folder", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(ctx.settings.root / "logs")))))
        row.addStretch(1)
        lay.addLayout(row)
        root.addWidget(card)
        root.addStretch(1)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(make_scroll(body))

    # -- refresh -------------------------------------------------------------
    def _sync_window_management(self) -> None:
        ctx = self.ctx
        self._syncing = True
        try:
            self.hotkeys_on.setChecked(ctx.hotkeys.enabled)
            self.stack_mode.setCurrentIndex(max(0, self.stack_mode.findData(ctx.tiler.stack_mode)))
            self.ffm.setChecked(ctx.ffm.enabled)
            self.ffm_delay.setValue(ctx.ffm.delay)
            self.ffm_raise.setChecked(ctx.ffm.raises)
        finally:
            self._syncing = False
        self.ffm_delay.setEnabled(ctx.ffm.enabled)
        self.ffm_raise.setEnabled(ctx.ffm.enabled)
        bindings = ctx.hotkeys.bindings()
        used = sum(1 for text in bindings.values() if text)
        problems = ctx.hotkeys.problems
        if not ctx.hotkeys.enabled:
            text = "Global shortcuts are off."
        elif not ctx.hotkeys.running:
            text = f"{used} shortcuts set; they are registered while CIRANA runs."
        else:
            text = f"{used - len(problems)} of {used} shortcuts active."
        if problems:
            text += " Not working: " + " ".join(problems.values())
        self.hotkey_status.setText(text)
        names = {w.id: w.name for w in ctx.workspaces.all()}
        self._fill(self.rule_list, [
            (f"{'' if rule.enabled else '(off) '}{rule.label} — {ACTION_NAMES[rule.action]}"
             + (f": {names.get(rule.workspace_id, 'missing workspace')}" if rule.action == WORKSPACE else ""),
             rule.id) for rule in ctx.tiler.rules()])

    def _ffm_edited(self, _value: object = None) -> None:
        if not self._syncing:
            self.ctx.ffm.set_enabled(self.ffm.isChecked(), self.ffm_delay.value(), self.ffm_raise.isChecked())
            self._sync_window_management()

    def _edit_shortcuts(self) -> None:
        ShortcutsDialog(self.ctx, self).exec()
        self._sync_window_management()

    def _rule_index(self) -> int:
        rule_id = self._selected(self.rule_list)
        return next((i for i, r in enumerate(self.ctx.tiler.rules()) if r.id == rule_id), -1)

    def _add_rule(self) -> None:
        dialog = RuleDialog(self.ctx, parent=self)
        if dialog.exec():
            self.ctx.tiler.set_rules(self.ctx.tiler.rules() + [dialog.current()])
            self._sync_window_management()

    def _edit_rule(self) -> None:
        index = self._rule_index()
        if index < 0:
            return
        rules = self.ctx.tiler.rules()
        dialog = RuleDialog(self.ctx, rules[index], self)
        if dialog.exec():
            rules[index] = dialog.current()
            self.ctx.tiler.set_rules(rules)
            self._sync_window_management()

    def _move_rule(self, step: int) -> None:
        index = self._rule_index()
        rules = self.ctx.tiler.rules()
        if index < 0 or not 0 <= index + step < len(rules):
            return
        rules[index], rules[index + step] = rules[index + step], rules[index]
        self.ctx.tiler.set_rules(rules)
        self._sync_window_management()

    def _delete_rule(self) -> None:
        index = self._rule_index()
        if index >= 0:
            rules = self.ctx.tiler.rules()
            del rules[index]
            self.ctx.tiler.set_rules(rules)
            self._sync_window_management()

    def refresh(self) -> None:
        self._sync_window_management()
        reg = self.ctx.registry
        self.scan_button.setEnabled(not self.ctx.scanner.scanning)
        report = self.ctx.scanner.last_report
        if self.ctx.scanner.scanning:
            self.scan_status.setText("Scanning…")
        elif report is not None:
            counts = report.counts
            extra = f" · {len(report.errors)} source error(s), see log" if report.errors else ""
            self.scan_status.setText(
                f"Found {counts.get('total', 0)} applications "
                f"(Start Menu {counts.get('start_menu', 0)}, packaged {counts.get('packaged', 0)}, "
                f"registry {counts.get('registry', 0)}){extra}"
            )
        elif reg.scanned_at:
            self.scan_status.setText("Last scan: " + time.strftime("%Y-%m-%d %H:%M", time.localtime(reg.scanned_at)))
        else:
            self.scan_status.setText("No scan has run yet.")

        self._fill(self.category_list, [(f"{name}  ({len(reg.apps_in_category(cid))})", cid) for cid, name in reg.categories()])
        self._fill(self.custom_list, [(a.name, a.id) for a in reg.all_apps(include_hidden=True) if a.custom])
        self._fill(self.hidden_list, [(a.name, a.id) for a in reg.hidden_apps()])

    @staticmethod
    def _fill(widget: QListWidget, rows: list[tuple[str, str]]) -> None:
        selected = widget.currentItem().data(Qt.ItemDataRole.UserRole) if widget.currentItem() else None
        widget.clear()
        for text, key in rows:
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, key)
            widget.addItem(item)
            if key == selected:
                widget.setCurrentItem(item)

    @staticmethod
    def _selected(widget: QListWidget) -> str | None:
        item = widget.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    # -- actions --------------------------------------------------------------
    def _rename_category(self) -> None:
        cid = self._selected(self.category_list)
        if not cid:
            return
        name, ok = QInputDialog.getText(self, "Rename category", "New name:", text=self.ctx.registry.category_name(cid))
        if ok:
            self._try(lambda: self.ctx.registry.rename_category(cid, name))

    def _add_category(self) -> None:
        name, ok = QInputDialog.getText(self, "New category", "Category name:")
        if ok:
            self._try(lambda: self.ctx.registry.add_category(name))

    def _delete_category(self) -> None:
        cid = self._selected(self.category_list)
        if cid:
            self._try(lambda: self.ctx.registry.delete_category(cid))

    def _add_exe(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose an executable", "", "Applications (*.exe *.bat *.cmd)")
        if not path:
            return
        name, ok = QInputDialog.getText(self, "Add application", "Display name:", text=os.path.splitext(os.path.basename(path))[0])
        if ok:
            self._try(lambda: self.ctx.registry.add_custom_app(name, path, LaunchType.EXECUTABLE.value))

    def _add_command(self) -> None:
        name, ok = QInputDialog.getText(self, "Add command", "Display name:")
        if not ok or not name.strip():
            return
        command, ok = QInputDialog.getText(self, "Add command", "Command line (runs exactly as typed, without a shell):")
        if ok:
            self._try(lambda: self.ctx.registry.add_custom_app(name, command, LaunchType.COMMAND.value))

    def _remove_custom(self) -> None:
        app_id = self._selected(self.custom_list)
        if app_id:
            self.ctx.registry.remove_custom_app(app_id)

    def _unhide(self) -> None:
        app_id = self._selected(self.hidden_list)
        if app_id:
            self.ctx.registry.unhide(app_id)

    def _try(self, func) -> None:
        try:
            func()
        except ValueError as exc:
            QMessageBox.warning(self, "CIRANA", str(exc))
