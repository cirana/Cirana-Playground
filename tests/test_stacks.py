"""Phase 3 on the simulated desktop: window stacks (several windows in one tile), how their hidden windows are kept
out of sight and always shown again, and stacks, focus and order surviving workspace switches."""
import unittest

from cirana.core.dynamic_tiling import BEHIND, MINIMIZE, DynamicTiler
from cirana.native import win32_api as w
from cirana.wm.layout import BSP, FREE, SIDE_BY_SIDE
from cirana.wm.navigation import LEFT, RIGHT
from tests.test_keyboard_tiling import CommandEnv
from tests.test_workspace_controller import LEFT as LEFT_MONITOR


class StackEnv(CommandEnv):
    def stack(self, window, direction):
        self.focus_on(window)
        result = self.tiler.stack_direction(direction)
        self.settle()
        self.pump(timeout=0.05)
        self.sync()  # the tracker reports what the re-tile did (positions, minimized windows)
        self.settle()
        return result

    def hides(self):
        return [c for c in self.desktop.calls if c[0] == "show" and c[2] == w.SW_SHOWMINNOACTIVE]


class BehindStackTests(StackEnv):
    def test_stacked_windows_share_one_tile_and_the_shown_one_is_on_top(self):
        self.turn_on()
        code, notes, mail = self.open("Code"), self.open("Notes"), self.open("Mail")
        self.settle()
        self.assertTrue(self.stack(mail, LEFT))  # Mail joins Notes' tile
        self.assertEqual(self.tiler.stack_count(), 1)
        left, right = self.slots(2)
        self.assertEqual(code.rect, left)
        self.assertEqual((notes.rect, mail.rect), (right, right))
        self.assertFalse(mail.minimized or notes.minimized)  # nothing hidden by default: one lies under the other
        self.assertEqual(self.desktop.foreground, mail.hwnd)
        self.assertEqual(self.tiler.describe(self.desktop.tracked(mail.hwnd)), "tiled, stack 2/2 (shown)")
        self.assertEqual(self.tiler.describe(self.desktop.tracked(notes.hwnd)), "tiled, stack 1/2 (hidden)")
        self.assertEqual(self.hides(), [])

    def test_cycling_shows_and_focuses_the_next_window(self):
        self.turn_on()
        code, notes, mail = self.open("Code"), self.open("Notes"), self.open("Mail")
        self.settle()
        self.stack(mail, LEFT)
        self.desktop.calls.clear()
        self.tiler.stack_cycle(1)
        self.settle()
        self.pump(timeout=0.05)
        self.assertEqual(self.desktop.foreground, notes.hwnd)
        self.assertIn(("raise", notes.hwnd), self.desktop.calls)
        del code

    def test_alt_tab_to_a_hidden_window_makes_it_the_shown_one(self):
        self.turn_on()
        self.open("Code")
        notes, mail = self.open("Notes"), self.open("Mail")
        self.settle()
        self.stack(mail, LEFT)
        self.focus_on(notes)  # the user switched to it with Alt+Tab
        self.settle()
        self.assertEqual(self.tiler.describe(self.desktop.tracked(notes.hwnd)), "tiled, stack 1/2 (shown)")

    def test_unstack_gives_the_window_its_own_tile_again(self):
        self.turn_on()
        code, notes, mail = self.open("Code"), self.open("Notes"), self.open("Mail")
        self.settle()
        self.stack(mail, LEFT)
        self.focus_on(mail)
        self.assertTrue(self.tiler.unstack())
        self.settle()
        self.assertEqual(self.tiler.stack_count(), 0)
        self.assertEqual(len({code.rect, notes.rect, mail.rect}), 3)
        self.assertFalse(self.tiler.unstack())  # not in a stack any more

    def test_closing_the_shown_window_shows_the_next_one(self):
        self.turn_on()
        code, notes, mail = self.open("Code"), self.open("Notes"), self.open("Mail")
        self.settle()
        self.stack(mail, LEFT)
        self.desktop.close(mail.hwnd)
        self.sync()
        self.settle()
        self.assertEqual(self.tiler.stack_count(), 0)  # one window left: the stack ended
        self.assertEqual([code.rect, notes.rect], self.slots(2))

    def test_stacks_in_bsp_keep_the_tile(self):
        self.turn_on(BSP)
        code, notes, mail = self.open("Code"), self.open("Notes"), self.open("Mail")
        self.settle()
        code_tile = code.rect
        self.stack(mail, LEFT)  # Mail's tile closes (its sibling Notes takes it) and Mail joins Notes there
        self.assertEqual(mail.rect, notes.rect)
        self.assertEqual(notes.rect, (1000, 0, 2000, 960))
        self.assertEqual(code.rect, code_tile)  # untouched
        self.assertTrue(self.stack(code, RIGHT))  # Code joins the same stack: three windows in one tile
        self.assertEqual(len(next(u for g in self.tiler._last_groups for u in g.units).members), 3)
        self.assertEqual({code.rect, notes.rect, mail.rect}, {LEFT_MONITOR.work})

    def test_only_tiled_windows_can_be_stacked(self):
        self.turn_on()
        code = self.open("Code")
        dialog = self.open("Calc", fixed_size=True, rect=(5, 5, 300, 300))
        self.settle()
        self.assertFalse(self.tiler.stack_with(self.desktop.tracked(dialog.hwnd), self.desktop.tracked(code.hwnd)))
        self.focus_on(code)
        self.assertFalse(self.tiler.stack_direction(RIGHT))  # nothing there


class MinimizedStackTests(StackEnv):
    def setUp(self):
        super().setUp()
        self.settings.set("stack_hide", MINIMIZE)

    def test_hidden_windows_are_minimized_without_activation_and_written_down(self):
        self.turn_on()
        code, notes, mail = self.open("Code"), self.open("Notes"), self.open("Mail")
        self.settle()
        self.stack(mail, LEFT)
        self.assertTrue(notes.minimized)
        self.assertFalse(mail.minimized)
        self.assertEqual([c[1] for c in self.hides()], [notes.hwnd])
        self.assertEqual(self.tiler._hidden_store.load()["windows"][0]["hwnd"], notes.hwnd)
        self.assertEqual([code.rect, mail.rect], self.slots(2))
        count = self.tiler.count
        self.sync()  # CIRANA's own minimize is reported back: it starts nothing
        self.settle()
        self.assertEqual(self.tiler.count, count)

    def test_cycling_swaps_which_one_is_minimized(self):
        self.turn_on()
        self.open("Code")
        notes, mail = self.open("Notes"), self.open("Mail")
        self.settle()
        self.stack(mail, LEFT)
        self.tiler.stack_cycle(1)
        self.settle()
        self.pump(timeout=0.05)
        self.sync()
        self.settle()
        self.assertFalse(notes.minimized)
        self.assertTrue(mail.minimized)
        self.assertEqual(notes.rect, self.slots(2)[1])
        self.assertEqual(self.desktop.foreground, notes.hwnd)

    def test_restoring_a_hidden_window_from_the_taskbar_shows_it_in_the_stack(self):
        self.turn_on()
        self.open("Code")
        notes, mail = self.open("Notes"), self.open("Mail")
        self.settle()
        self.stack(mail, LEFT)
        notes.minimized = False  # the user clicked its taskbar button
        self.sync()
        self.settle()
        self.sync()
        self.settle()
        self.assertFalse(notes.minimized)
        self.assertTrue(mail.minimized)  # now the other one is out of sight
        self.assertEqual(notes.rect, self.slots(2)[1])

    def test_unstack_and_dissolving_show_the_window_again(self):
        self.turn_on()
        self.open("Code")
        notes, mail = self.open("Notes"), self.open("Mail")
        self.settle()
        self.stack(mail, LEFT)
        self.focus_on(mail)
        self.tiler.unstack()
        self.settle()
        self.sync()
        self.settle()
        self.assertFalse(notes.minimized or mail.minimized)
        self.assertEqual(self.tiler._stack_hidden, set())
        self.assertEqual(self.tiler._hidden_store.load()["windows"], [])

    def test_quitting_and_switching_tiling_off_show_every_hidden_window(self):
        self.turn_on()
        self.open("Code")
        notes, mail = self.open("Notes"), self.open("Mail")
        self.settle()
        self.stack(mail, LEFT)
        self.tiler.set_global(False)
        self.assertFalse(notes.minimized)
        self.sync()  # the tracker sees it shown again
        self.tiler.set_global(True)
        self.settle()
        self.sync()
        self.settle()
        self.assertTrue(notes.minimized)  # the stack is kept: hidden again
        self.tiler.stop()  # CIRANA closes
        self.assertFalse(notes.minimized)
        self.assertEqual(self.tiler._hidden_store.load()["windows"], [])
        self.assertFalse(any(c[0] == "show" and c[2] == 0 for c in self.desktop.calls))  # never SW_HIDE

    def test_after_a_crash_the_next_start_shows_them_again(self):
        self.turn_on()
        self.open("Code")
        notes, mail = self.open("Notes"), self.open("Mail")
        self.settle()
        self.stack(mail, LEFT)
        self.assertTrue(notes.minimized)
        user_minimized = self.open("Game", minimized=True)
        self.tiler._stopped = True  # a crash: nothing is cleaned up
        self.tiler._timer.stop()
        self.desktop.calls.clear()
        DynamicTiler(self.wm, self.settings, debounce_ms=10)  # the next start
        self.assertFalse(notes.minimized)
        self.assertTrue(user_minimized.minimized)  # windows the user minimized are not CIRANA's business
        self.assertEqual(self.tiler._hidden_store.load()["windows"], [])

    def test_a_recycled_handle_is_never_shown(self):
        self.turn_on()
        self.open("Code")
        notes, mail = self.open("Notes"), self.open("Mail")
        self.settle()
        self.stack(mail, LEFT)
        self.tiler._stopped = True
        self.tiler._timer.stop()
        self.desktop.close(notes.hwnd)
        other = self.desktop.add("c:\\apps\\other.exe", "Other", hwnd=notes.hwnd, pid=7777, minimized=True)
        DynamicTiler(self.wm, self.settings, debounce_ms=10)
        self.assertTrue(other.minimized)

    def test_switching_the_mode_shows_them(self):
        self.turn_on()
        self.open("Code")
        notes, mail = self.open("Notes"), self.open("Mail")
        self.settle()
        self.stack(mail, LEFT)
        self.tiler.set_stack_mode(BEHIND)
        self.settle()
        self.assertFalse(notes.minimized)
        self.assertEqual(notes.rect, mail.rect)


class WorkspaceStackTests(StackEnv):
    def test_switching_the_workspace_tiling_off_or_to_saved_positions_shows_hidden_windows(self):
        self.settings.set("stack_hide", MINIMIZE)
        ws = self.tiled_workspace("Code", "Notes", "Brave")
        self.open("Code")
        notes, brave = self.open("Notes"), self.open("Brave")
        self.settle()
        self.stack(brave, LEFT)
        self.assertTrue(notes.minimized)
        self.manager.set_dynamic(ws.id, False)  # the active workspace stays active: only its tiling is off
        self.assertFalse(notes.minimized)
        self.assertEqual(self.tiler._hidden_store.load()["windows"], [])
        self.sync()
        self.manager.set_dynamic(ws.id, True)  # back on: the stack is kept and hides it again
        self.settle()
        self.sync()
        self.settle()
        self.assertTrue(notes.minimized)
        layout = self.manager.get(ws.id).layout
        layout.kind = FREE
        self.manager.set_layout(ws.id, layout)  # saved positions cannot be tiled
        self.assertFalse(notes.minimized)

    def test_stacks_focus_and_order_survive_a_workspace_switch(self):
        self.settings.set("stack_hide", MINIMIZE)
        first = self.tiled_workspace("Code", "Notes", "Brave", kind=SIDE_BY_SIDE)
        second = self.manager.create("Second", [])
        code, notes, brave = self.open("Code"), self.open("Notes"), self.open("Brave")
        self.settle()
        self.stack(brave, LEFT)  # Brave joins Notes
        self.assertTrue(notes.minimized)
        stored = self.manager.get(first.id).layout.stacks
        self.assertEqual(len(stored), 1)
        self.assertEqual(len(stored[0]["members"]), 2)
        self.focus_on(code)
        self.manager.set_active(second.id)  # switched away: the hidden window is shown again
        self.pump(timeout=0.05)
        self.assertFalse(notes.minimized)
        self.sync()
        self.wm.activate(first.id)  # back: same stack, same shown window, focus on Code again
        self.settle()
        self.sync()
        self.settle()
        self.assertTrue(notes.minimized)
        self.assertFalse(brave.minimized)
        self.assertEqual([code.rect, brave.rect], self.slots(2))
        self.assertEqual(self.desktop.foreground, code.hwnd)

    def test_last_focused_window_gets_the_focus_on_activation(self):
        first = self.tiled_workspace("Code", "Notes")
        second = self.manager.create("Second", [])
        code, notes = self.open("Code"), self.open("Notes")
        self.settle()
        self.focus_on(notes)
        self.manager.set_active(second.id)
        self.wm.activate(first.id)
        self.settle()
        self.pump(timeout=0.05)
        self.assertEqual(self.desktop.foreground, notes.hwnd)
        self.assertEqual([code.rect, notes.rect], self.slots(2))  # the order did not change

    def test_removing_a_stacked_entry_ends_the_stack(self):
        ws = self.tiled_workspace("Code", "Notes")
        code, notes = self.open("Code"), self.open("Notes")
        self.settle()
        self.stack(notes, LEFT)
        self.manager.remove_entry(ws.id, self.manager.get(ws.id).apps[0].id)
        self.settle()
        self.assertEqual(self.manager.get(ws.id).layout.stacks, [])
        self.assertEqual(notes.rect, LEFT_MONITOR.work)
        del code


if __name__ == "__main__":
    unittest.main()
