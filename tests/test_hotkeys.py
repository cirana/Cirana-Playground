"""Global shortcuts (parsing, conflicts with Windows and applications, registration failures) and focus follows the
mouse (Windows' own setting, always put back). Nothing here registers a real shortcut or changes a real setting."""
import unittest

from cirana.core.focus_follows_mouse import FocusFollowsMouse
from cirana.core.hotkeys import (ACTIONS, DEFAULTS, MOD_ALT, MOD_CONTROL, MOD_NOREPEAT, MOD_SHIFT, HotkeyManager,
                                 conflict, normalize, parse, warning)
from cirana.core.window_commands import WindowCommands
from cirana.storage.settings_manager import SettingsManager
from cirana.wm.layout import SIDE_BY_SIDE, LayoutSpec
from tests.support import TempHomeTestCase
from tests.test_keyboard_tiling import CommandEnv


class FakeRegistrar:
    def __init__(self, taken=()):
        self.taken = set(taken)  # (modifiers, vk) another program has
        self.active = {}

    def register(self, ident, modifiers, vk):
        if (modifiers & ~MOD_NOREPEAT, vk) in self.taken:
            return 1409
        self.active[ident] = (modifiers, vk)
        return 0

    def unregister(self, ident):
        self.active.pop(ident, None)


class ParsingTests(unittest.TestCase):
    def test_parse_and_normalize(self):
        hotkey = parse("alt+ctrl+left")
        self.assertEqual((hotkey.modifiers, hotkey.vk, hotkey.text), (MOD_CONTROL | MOD_ALT, 0x25, "Ctrl+Alt+Left"))
        self.assertEqual(normalize("Shift+Alt+Ctrl+PageDown"), "Ctrl+Alt+Shift+PageDown")
        self.assertEqual(parse("Ctrl+Alt+=").vk, 0xBB)
        for bad in ("", "Left", "Shift+A", "Ctrl+Nope", "Hyper+A", "Ctrl+", "Ctrl+Alt++"):
            self.assertIsNone(parse(bad), bad)

    def test_every_default_is_valid_and_free_of_known_conflicts(self):
        for action, _label, default in ACTIONS:
            if default:
                self.assertIsNotNone(parse(default), action)
                self.assertEqual(conflict(default), "", action)
                self.assertEqual(warning(default), "", action)  # works with AltGr keyboards (Turkish Q)
        texts = [d for d in DEFAULTS.values() if d]
        self.assertEqual(len(texts), len(set(texts)))  # no default is used twice

    def test_conflicts_with_windows_applications_and_cirana(self):
        for text in ("Alt+Tab", "Alt+F4", "Win+Left", "Win+Shift+S", "Ctrl+Win+Right", "Ctrl+C", "Ctrl+K",
                     "Ctrl+1", "Alt+F", "Alt+Left", "Ctrl+Shift+Esc", "Alt+Win+R", "Ctrl+Tab"):
            self.assertTrue(conflict(text), text)
        for text in ("Ctrl+Alt+Left", "Ctrl+Alt+Win+H", "Alt+Win+H", "Ctrl+Alt+F5", "Ctrl+Shift+F12"):
            self.assertEqual(conflict(text), "", text)
        self.assertIn("AltGr", warning("Ctrl+Alt+Q"))  # @ on a Turkish Q keyboard
        self.assertEqual(warning("Ctrl+Alt+Win+Q"), "")


class ManagerTests(TempHomeTestCase):
    def setUp(self):
        super().setUp()
        self.settings = SettingsManager(self.home)

    def test_registration_failures_are_reported_per_shortcut(self):
        registrar = FakeRegistrar(taken={(MOD_CONTROL | MOD_ALT, 0x25)})  # someone has Ctrl+Alt+Left
        manager = HotkeyManager(self.settings, registrar)
        changes, toasts = [], []
        manager.status_changed.connect(lambda: changes.append(True))
        manager.failed.connect(toasts.append)
        manager.start()
        self.assertEqual(len(toasts), 1)
        self.assertIn("Ctrl+Alt+Left", toasts[0])  # the user is told which shortcut does not work
        self.assertIn("focus_left", manager.problems)
        self.assertIn("already uses it", manager.problems["focus_left"])
        self.assertEqual(len(registrar.active), sum(1 for d in DEFAULTS.values() if d) - 1)  # the others work
        self.assertTrue(changes)
        pressed = []
        manager.triggered.connect(pressed.append)
        manager.press("focus_right")
        self.assertEqual(pressed, ["focus_right"])
        manager.stop()
        self.assertEqual(registrar.active, {})

    def test_editing_rejects_conflicts_and_duplicates_and_is_saved(self):
        registrar = FakeRegistrar()
        manager = HotkeyManager(self.settings, registrar)
        manager.start()
        problems = manager.set_bindings({"monocle": "Alt+F4", "focus_left": "Ctrl+Alt+F1",
                                         "focus_right": "ctrl+alt+f1", "stack_left": "Ctrl+Alt+Win+H"})
        self.assertEqual(set(problems), {"monocle", "focus_right"})
        bindings = HotkeyManager(SettingsManager(self.home)).bindings()  # restart
        self.assertEqual(bindings["focus_left"], "Ctrl+Alt+F1")
        self.assertEqual(bindings["stack_left"], "Ctrl+Alt+Win+H")
        self.assertEqual(bindings["monocle"], "")  # refused: not used
        self.assertEqual(bindings["swap_left"], DEFAULTS["swap_left"])
        manager.set_enabled(False)
        self.assertEqual(registrar.active, {})
        manager.set_enabled(True)
        manager.reset()
        self.assertEqual(manager.bindings(), {a: (d and normalize(d)) for a, d in DEFAULTS.items()})

    def test_nothing_is_registered_before_start(self):
        registrar = FakeRegistrar()
        HotkeyManager(self.settings, registrar).apply()
        self.assertEqual(registrar.active, {})


class CommandWiringTests(CommandEnv):
    def test_every_action_has_a_command_and_shortcuts_run_them(self):
        commands = WindowCommands(self.tiler)
        self.assertEqual(set(commands.handlers), set(DEFAULTS))
        manager = HotkeyManager(self.settings, FakeRegistrar())
        manager.triggered.connect(commands.run)
        manager.start()
        self.tiler.set_global_layout(LayoutSpec(SIDE_BY_SIDE, gap=0))
        self.tiler.set_global(True)
        code, notes = self.open("Code"), self.open("Notes")
        self.settle()
        self.focus_on(code)
        manager.press("focus_right")
        self.pump(timeout=0.1)
        self.assertEqual(self.desktop.foreground, notes.hwnd)
        self.assertFalse(commands.run("no_such_action"))

    def test_workspace_commands(self):
        commands = WindowCommands(self.tiler)
        self.assertFalse(commands.run("workspace_next"))  # no workspace at all
        first = self.manager.create("First", [])
        second = self.manager.create("Second", [])
        self.manager.add_app(first.id, self.registry.all_apps()[0].id)
        self.manager.add_app(second.id, self.registry.all_apps()[1].id)
        self.manager.set_active(first.id)
        self.assertTrue(commands.run("workspace_next"))
        self.assertEqual(self.manager.active_id, second.id)
        self.assertTrue(commands.run("workspace_previous"))
        self.assertEqual(self.manager.active_id, first.id)
        window = self.open("Game")
        self.focus_on(window)
        self.assertTrue(commands.run("move_workspace_next"))
        self.assertEqual(self.manager.get(second.id).apps[-1].hint_title, "Game")


class FakeSystem:
    def __init__(self):
        self.values = {"tracking": 0, "raise": 1, "delay": 0}
        self.writes = 0

    def read(self):
        return dict(self.values)

    def write(self, values):
        self.values = dict(values)
        self.writes += 1
        return True


class FocusFollowsMouseTests(TempHomeTestCase):
    def setUp(self):
        super().setUp()
        self.settings = SettingsManager(self.home)
        self.system = FakeSystem()

    def test_off_by_default_and_nothing_changes(self):
        ffm = FocusFollowsMouse(self.settings, self.system)
        ffm.start()
        self.assertFalse(ffm.enabled)
        self.assertEqual(self.system.writes, 0)

    def test_on_uses_windows_tracking_and_off_puts_the_users_values_back(self):
        ffm = FocusFollowsMouse(self.settings, self.system)
        ffm.set_enabled(True, delay=300)
        self.assertEqual(self.system.values, {"tracking": 1, "raise": 0, "delay": 300})
        ffm.set_enabled(False)
        self.assertEqual(self.system.values, {"tracking": 0, "raise": 1, "delay": 0})
        self.assertEqual(self.settings.get("ffm_restore"), {})

    def test_quit_restores_and_the_next_start_applies_again(self):
        ffm = FocusFollowsMouse(self.settings, self.system)
        ffm.set_enabled(True)
        ffm.stop()
        self.assertEqual(self.system.values["tracking"], 0)
        FocusFollowsMouse(SettingsManager(self.home), self.system).start()
        self.assertEqual(self.system.values["tracking"], 1)

    def test_a_crash_is_repaired_at_the_next_start(self):
        FocusFollowsMouse(self.settings, self.system).set_enabled(True)  # ... and CIRANA crashes
        self.settings.set("focus_follows_mouse", False)  # the user switched it off since (or it was never saved)
        FocusFollowsMouse(SettingsManager(self.home), self.system).start()
        self.assertEqual(self.system.values, {"tracking": 0, "raise": 1, "delay": 0})

    def test_delay_is_bounded(self):
        ffm = FocusFollowsMouse(self.settings, self.system)
        ffm.set_enabled(True, delay=99999, raises=True)
        self.assertEqual(self.system.values, {"tracking": 1, "raise": 1, "delay": 2000})


if __name__ == "__main__":
    unittest.main()
