"""Window manager screens: editing the global shortcuts, creating and editing window rules, and the small picker for
automatic layouts. Same look as the other CIRANA dialogs (theme object names, no new styles)."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLineEdit, QListWidget,
    QListWidgetItem, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

from cirana.core.hotkeys import DEFAULTS, KEY_NAMES, LABELS, MODIFIERS, conflict, normalize, parse, warning
from cirana.ui.common import label
from cirana.ui.context import AppContext
from cirana.wm.layout import AUTO_KINDS, AUTO_STEPS, BSP, DEFAULT_AUTO, GRID, MAIN_SIDE, SIDE_BY_SIDE
from cirana.wm.rules import ACTION_NAMES, ACTIONS, WORKSPACE, WindowRule, new_rule_id

AUTO_NAMES = {SIDE_BY_SIDE: "Side by side", MAIN_SIDE: "Main + side panel", GRID: "Grid", BSP: "BSP"}
AUTO_LABELS = {"2": "2 windows", "3": "3 windows", "4": "4 or more"}
AUTO_SHORT = {"2": "2", "3": "3", "4": "4+"}


def _button(text: str, handler, name: str = "SecondaryButton") -> QPushButton:  # noqa: ANN001
    button = QPushButton(text)
    button.setObjectName(name)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.clicked.connect(handler)
    return button


def _dialog_buttons(dialog: QDialog, ok_text: str) -> tuple[QDialogButtonBox, QPushButton]:
    box = QDialogButtonBox()
    ok = box.addButton(ok_text, QDialogButtonBox.ButtonRole.AcceptRole)
    ok.setObjectName("PrimaryButton")
    cancel = box.addButton("Cancel", QDialogButtonBox.ButtonRole.RejectRole)
    cancel.setObjectName("SecondaryButton")
    box.accepted.connect(dialog.accept)
    box.rejected.connect(dialog.reject)
    return box, ok


class AutoLayoutPicker(QWidget):
    """Automatic layout: which layout for 2, 3 and 4+ windows."""

    changed = Signal(dict)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)  # never wider than needed
        self.combos: dict[str, QComboBox] = {}
        self._syncing = False
        for step in AUTO_STEPS:
            combo = QComboBox()
            combo.setToolTip(f"Layout used with {AUTO_LABELS[step]}")
            for kind in AUTO_KINDS:
                combo.addItem(f"{AUTO_SHORT[step]}: {AUTO_NAMES[kind]}", kind)
            combo.currentIndexChanged.connect(self._edited)
            self.combos[step] = combo
            row.addWidget(combo)

    def set_auto(self, auto: dict[str, str]) -> None:
        self._syncing = True
        try:
            for step, combo in self.combos.items():
                combo.setCurrentIndex(max(0, combo.findData(auto.get(step, DEFAULT_AUTO[step]))))
        finally:
            self._syncing = False

    def auto(self) -> dict[str, str]:
        return {step: combo.currentData() for step, combo in self.combos.items()}

    def _edited(self, _index: int) -> None:
        if not self._syncing:
            self.changed.emit(self.auto())


class ShortcutsDialog(QDialog):
    """Every command with its global shortcut; choose modifiers and a key for the selected one."""

    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.setWindowTitle("Keyboard shortcuts")
        self.resize(720, 620)
        self.bindings = ctx.hotkeys.bindings()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)
        layout.addWidget(label("Keyboard shortcuts", "SectionTitle"))
        layout.addWidget(label("They work in every application while CIRANA runs. Shortcuts Windows or most "
                               "applications already use are refused; one another program has taken is reported.",
                               "Muted", wrap=True))
        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._selected)
        layout.addWidget(self.list, 1)
        editor = QHBoxLayout()
        editor.setSpacing(8)
        self.mods = {name: QCheckBox(name) for name, _flag in MODIFIERS}
        for box in self.mods.values():
            box.toggled.connect(self._preview)
            editor.addWidget(box)
        self.key = QComboBox()
        self.key.addItem("(no key)", "")
        for name in KEY_NAMES:
            self.key.addItem(name, name)
        self.key.currentIndexChanged.connect(self._preview)
        editor.addWidget(self.key)
        editor.addWidget(_button("Set", self._set, "PrimaryButton"))
        editor.addWidget(_button("Clear", self._clear))
        editor.addWidget(_button("Default", self._default))
        editor.addStretch(1)
        layout.addLayout(editor)
        self.note = label("", "RowWarning", wrap=True)
        layout.addWidget(self.note)
        bottom = QHBoxLayout()
        bottom.addWidget(_button("Reset all to defaults", self._reset_all))
        bottom.addStretch(1)
        box, _ok = _dialog_buttons(self, "Save")
        bottom.addWidget(box)
        layout.addLayout(bottom)
        self._fill()
        self.list.setCurrentRow(0)

    def _fill(self) -> None:
        row = max(0, self.list.currentRow())
        self.list.clear()
        problems = self.ctx.hotkeys.check(self.bindings)
        registered = self.ctx.hotkeys.problems
        for action, text in self.bindings.items():
            problem = problems.get(action) or (registered.get(action) if text == self.ctx.hotkeys.bindings().get(
                action) else "")
            shown = text or "—"
            item = QListWidgetItem(f"{LABELS[action]}\t{shown}" + (f"   ⚠ {problem}" if problem else ""))
            item.setData(Qt.ItemDataRole.UserRole, action)
            item.setToolTip(problem or shown)
            self.list.addItem(item)
        self.list.setCurrentRow(min(row, self.list.count() - 1))

    def _action(self) -> str:
        item = self.list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else ""

    def _selected(self, _row: int) -> None:
        hotkey = parse(self.bindings.get(self._action(), ""))
        for name, flag in MODIFIERS:
            self.mods[name].blockSignals(True)
            self.mods[name].setChecked(bool(hotkey and hotkey.modifiers & flag))
            self.mods[name].blockSignals(False)
        self.key.blockSignals(True)
        self.key.setCurrentIndex(max(0, self.key.findData(hotkey.key if hotkey else "")))
        self.key.blockSignals(False)
        self._preview()

    def _composed(self) -> str:
        key = self.key.currentData()
        if not key:
            return ""
        return "+".join([name for name, _f in MODIFIERS if self.mods[name].isChecked()] + [key])

    def _preview(self, _value: object = None) -> None:
        text = self._composed()
        if not text:
            self.note.setText("")
            return
        self.note.setText(conflict(text) or warning(text) or f"{normalize(text)} is free.")

    def _set(self) -> None:
        text = self._composed()
        if text and conflict(text):
            self.note.setText(conflict(text))
            return
        self.bindings[self._action()] = normalize(text)
        self._fill()

    def _clear(self) -> None:
        self.bindings[self._action()] = ""
        self._fill()

    def _default(self) -> None:
        self.bindings[self._action()] = normalize(DEFAULTS[self._action()])
        self._fill()
        self._selected(self.list.currentRow())

    def _reset_all(self) -> None:
        self.bindings = {action: normalize(text) for action, text in DEFAULTS.items()}
        self._fill()

    def accept(self) -> None:
        problems = self.ctx.hotkeys.set_bindings(self.bindings)
        if problems:
            self.ctx.actions.message.emit("Not saved (conflicts): " + " ".join(problems.values()))
        super().accept()


class RuleDialog(QDialog):
    """Create or edit one window rule; the window lists show how many open windows it catches right now."""

    def __init__(self, ctx: AppContext, rule: WindowRule | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.rule = WindowRule(**{**(rule.to_dict() if rule else {"id": new_rule_id()})})
        self.setWindowTitle("Window rule")
        self.resize(560, 520)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)
        layout.addWidget(label("Window rule", "SectionTitle"))
        layout.addWidget(label("Name the program precisely (executable, window class or AppUserModelID). Every "
                               "field you fill in must match; a title on its own is not enough.", "Muted", wrap=True))
        self.pick = QComboBox()
        self.pick.addItem("Fill in from an open window…", None)
        for window in ctx.windows.windows():
            self.pick.addItem(f"{window.display_title}  ({window.exe_name or window.class_name})", window.key)
        self.pick.currentIndexChanged.connect(self._picked)
        layout.addWidget(self.pick)
        form = QFormLayout()
        form.setSpacing(8)
        self.name = QLineEdit(self.rule.name)
        self.exe = QLineEdit(self.rule.exe)
        self.exe.setPlaceholderText("code.exe, or a full path")
        self.class_name = QLineEdit(self.rule.class_name)
        self.class_name.setPlaceholderText("exact window class (optional)")
        self.aumid = QLineEdit(self.rule.aumid)
        self.aumid.setPlaceholderText("packaged app: Family!App (optional)")
        self.title = QLineEdit(self.rule.title)
        self.title.setPlaceholderText("the title contains… (optional)")
        self.action = QComboBox()
        for action in ACTIONS:
            self.action.addItem(ACTION_NAMES[action], action)
        self.action.setCurrentIndex(max(0, self.action.findData(self.rule.action)))
        self.workspace = QComboBox()
        for workspace in ctx.workspaces.all():
            self.workspace.addItem(workspace.name, workspace.id)
        self.workspace.setCurrentIndex(max(0, self.workspace.findData(self.rule.workspace_id)))
        self.enabled = QCheckBox("Enabled")
        self.enabled.setChecked(self.rule.enabled)
        for title, widget in (("Name", self.name), ("Executable", self.exe), ("Window class", self.class_name),
                              ("AppUserModelID", self.aumid), ("Title", self.title), ("Action", self.action),
                              ("Workspace", self.workspace), ("", self.enabled)):
            form.addRow(title, widget)
        layout.addLayout(form)
        self.matches = label("", "Muted", wrap=True)
        layout.addWidget(self.matches)
        self.problem = label("", "RowWarning", wrap=True)
        layout.addWidget(self.problem)
        layout.addStretch(1)
        box, self.ok = _dialog_buttons(self, "Save rule")
        layout.addWidget(box)
        for widget in (self.name, self.exe, self.class_name, self.aumid, self.title):
            widget.textChanged.connect(self._update)
        self.action.currentIndexChanged.connect(self._update)
        self.workspace.currentIndexChanged.connect(self._update)
        self.enabled.toggled.connect(self._update)
        self._update()

    def _picked(self, _index: int) -> None:
        window = self.ctx.windows.get(self.pick.currentData() or "")
        if window is None:
            return
        suggestion = WindowRule.for_window(window)
        self.exe.setText(suggestion.exe)
        self.class_name.setText(suggestion.class_name)
        self.aumid.setText(suggestion.aumid)
        if not self.name.text().strip():
            self.name.setText(suggestion.name)

    def current(self) -> WindowRule:
        return WindowRule(self.rule.id, self.name.text().strip(), self.exe.text().strip(),
                          self.class_name.text().strip(), self.aumid.text().strip(), self.title.text().strip(),
                          self.action.currentData(), self.workspace.currentData() or "", self.enabled.isChecked())

    def _update(self, _value: object = None) -> None:
        rule = self.current()
        self.workspace.setEnabled(rule.action == WORKSPACE)
        problem = rule.problem()
        self.problem.setText(problem)
        self.ok.setEnabled(not problem)
        if problem:
            self.matches.setText("")
            return
        caught = [w for w in self.ctx.windows.windows() if WindowRule(**{**rule.to_dict(), "enabled": True})
                  .matches(w)]
        names = ", ".join(w.display_title for w in caught[:4]) + (" …" if len(caught) > 4 else "")
        self.matches.setText(f"Matches {len(caught)} open window{'s' if len(caught) != 1 else ''} now"
                             + (f": {names}" if caught else "."))
