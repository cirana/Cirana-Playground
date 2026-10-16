"""Phase 1 and 2 on the simulated desktop: keyboard focus, swaps, resizing, monocle, BSP and automatic layouts,
window rules, and that nothing ever ends up off-screen or in a focus loop."""
import unittest

from cirana.core.dynamic_tiling import GLOBAL, RULE_SLOT
from cirana.core.models import stable_app_id
from cirana.native import win32_api as w
from cirana.wm import bsp
from cirana.wm.layout import AUTO, BSP, GRID, MAIN_SIDE, SIDE_BY_SIDE, LayoutSpec, tile
from cirana.wm.monitors import Monitor, intersection_area
from cirana.wm.navigation import DOWN, LEFT, RIGHT, UP
from cirana.wm.rules import FLOAT, IGNORE, WORKSPACE, WindowRule
from tests.test_dynamic_tiling import TilingEnv
from tests.test_workspace_controller import LEFT as LEFT_MONITOR
from tests.test_workspace_controller import RIGHT as RIGHT_MONITOR


class CommandEnv(TilingEnv):
    def turn_on(self, kind=SIDE_BY_SIDE, **spec):
        self.tiler.set_global_layout(LayoutSpec(kind, gap=0, **spec))
        self.tiler.set_global(True)

    def drag(self, window, started):
        self.tracker._on_event(w.EVENT_SYSTEM_MOVESIZESTART if started else w.EVENT_SYSTEM_MOVESIZEEND, window.hwnd)
        self.pump(timeout=0.05)

    def focus_on(self, window):
        """The user clicked this window: Windows reports it as the foreground window."""
        self.desktop.foreground = window.hwnd
        self.tracker._on_batch(("partial", None, {}, window.hwnd))
        self.pump(timeout=0.03)

    def run_and_settle(self, command, *args):
        result = command(*args)
        self.settle()
        self.pump(timeout=0.05)  # focus jobs
        return result

    def assert_on_screen(self):
        """No managed window may end up outside every work area."""
        monitors = self.desktop.monitors()
        for window in self.desktop.windows.values():
            if window.minimized:
                continue
            self.assertTrue(any(intersection_area(window.rect, m.work) > 0 for m in monitors),
                            f"{window.title} is off-screen at {window.rect}")


class FocusTests(CommandEnv):
    def test_focus_moves_to_the_neighbour_and_uses_the_foreground_window(self):
        self.turn_on(MAIN_SIDE, ratio=0.5)
        code, notes, mail = self.open("Code"), self.open("Notes"), self.open("Mail")
        self.settle()
        self.focus_on(code)
        self.assertTrue(self.run_and_settle(self.tiler.focus_direction, RIGHT))
        self.assertEqual(self.desktop.foreground, notes.hwnd)  # the upper of the two side tiles
        self.run_and_settle(self.tiler.focus_direction, DOWN)
        self.assertEqual(self.desktop.foreground, mail.hwnd)
        self.run_and_settle(self.tiler.focus_direction, LEFT)
        self.assertEqual(self.desktop.foreground, code.hwnd)
        self.assertFalse(self.tiler.focus_direction(LEFT))  # nothing further left: nothing happens
        self.assertFalse(self.tiler.focus_direction(UP))

    def test_focus_crosses_monitors(self):
        self.turn_on()
        code = self.open("Code")
        game = self.open("Game", rect=(2100, 100, 2600, 600))
        self.settle()
        self.focus_on(code)
        self.run_and_settle(self.tiler.focus_direction, RIGHT)
        self.assertEqual(self.desktop.foreground, game.hwnd)

    def test_focus_without_tiling_uses_the_visible_windows(self):
        left = self.open("Code", rect=(0, 0, 900, 900))
        right = self.open("Notes", rect=(1000, 0, 1900, 900))
        hidden = self.open("Mail", rect=(950, 0, 1000, 900), minimized=True)
        self.focus_on(left)
        self.run_and_settle(self.tiler.focus_direction, RIGHT)
        self.assertEqual(self.desktop.foreground, right.hwnd)
        self.assertNotEqual(self.desktop.foreground, hidden.hwnd)

    def test_refused_focus_is_reported_once_and_never_retried(self):
        self.turn_on()
        code, notes = self.open("Code"), self.open("Notes")
        self.settle()
        self.focus_on(code)
        self.desktop.foreground_locked = True  # Windows refuses (CIRANA did not get the last input)
        self.desktop.calls.clear()
        count = self.tiler.count
        self.run_and_settle(self.tiler.focus_direction, RIGHT)
        self.assertEqual(self.desktop.foreground, code.hwnd)
        self.assertEqual(sum(1 for c in self.desktop.calls if c[0] == "foreground"), 1)
        self.assertTrue(any("did not let CIRANA give" in m for m in self.messages))
        self.assertEqual(self.tiler.count, count)  # no re-tile was started for a focus change

    def test_no_focus_means_nothing_happens(self):
        self.turn_on()
        self.open("Code")
        self.settle()
        self.assertFalse(self.tiler.focus_direction(RIGHT))
        self.assertIn("No application window has the focus.", self.messages)


class SwapAndMoveTests(CommandEnv):
    def test_swap_with_the_neighbour(self):
        self.turn_on()
        code, notes = self.open("Code"), self.open("Notes")
        self.settle()
        left, right = self.slots(2)
        self.focus_on(code)
        self.run_and_settle(self.tiler.swap_direction, RIGHT)
        self.assertEqual((code.rect, notes.rect), (right, left))
        self.assertEqual(self.desktop.foreground, code.hwnd)  # the moved window keeps the focus
        self.assert_on_screen()

    def test_swap_at_the_edge_moves_to_the_next_monitor(self):
        self.turn_on()
        code, notes = self.open("Code"), self.open("Notes")
        self.settle()
        self.focus_on(notes)
        self.run_and_settle(self.tiler.swap_direction, RIGHT)
        self.assertEqual((code.rect, notes.rect), (LEFT_MONITOR.work, RIGHT_MONITOR.work))
        self.run_and_settle(self.tiler.swap_direction, LEFT)  # and back
        self.assertEqual([code.rect, notes.rect], self.slots(2))
        self.focus_on(code)
        self.assertFalse(self.tiler.swap_direction(LEFT))  # the left edge of the left monitor: nowhere to go
        self.assert_on_screen()

    def test_workspace_window_moved_to_another_monitor_floats_there(self):
        self.tiled_workspace("Code", "Notes")
        code, notes = self.open("Code"), self.open("Notes")
        self.settle()
        self.focus_on(notes)
        self.run_and_settle(self.tiler.swap_direction, RIGHT)
        self.assertGreater(intersection_area(notes.rect, RIGHT_MONITOR.work), 0)
        self.assertEqual(code.rect, LEFT_MONITOR.work)  # the workspace layout fills its gap
        self.assertIn("you chose so for this window", self.tiler._rule_reason(self.desktop.tracked(notes.hwnd)))
        self.assert_on_screen()

    def test_move_to_another_monitor_with_another_dpi_keeps_the_relative_place(self):
        right = Monitor(RIGHT_MONITOR.device, RIGHT_MONITOR.rect, RIGHT_MONITOR.work, False, 144)
        self.desktop.monitors_ = [LEFT_MONITOR, right]
        window = self.open("Code", rect=(500, 240, 1500, 720), dpi_aware=False)
        self.focus_on(window)
        self.run_and_settle(self.tiler.swap_direction, RIGHT)
        self.assertEqual(window.rect, (2500, 250, 3500, 750))  # same fractions of the other work area
        self.sync()
        self.assertFalse(self.tiler.swap_direction(RIGHT))  # no monitor further right

    def test_move_window_to_another_workspace(self):
        first = self.tiled_workspace("Code", "Notes")
        second = self.manager.create("Second", [])
        code, notes = self.open("Code"), self.open("Notes")
        self.settle()
        self.assertTrue(self.wm.move_window(self.desktop.tracked(notes.hwnd), second.id))
        self.settle()
        self.assertEqual([a.app_id for a in self.manager.get(first.id).apps], [stable_app_id("Code")])
        self.assertEqual(self.wm.bound_window(second.id, self.manager.get(second.id).apps[0].id).hwnd, notes.hwnd)
        self.assertEqual(code.rect, LEFT_MONITOR.work)  # the first workspace filled the gap
        self.assertFalse(self.wm.move_window(self.desktop.tracked(code.hwnd), first.id))  # already there


class ResizeTests(CommandEnv):
    def test_side_by_side_width(self):
        self.turn_on()
        code, notes = self.open("Code"), self.open("Notes")
        self.settle()
        self.focus_on(code)
        self.run_and_settle(self.tiler.resize, True, True)
        self.assertEqual(code.rect[2] - code.rect[0], 1100)  # 55 % of 2000
        self.assertEqual(notes.rect[0], 1100)
        self.assertFalse(self.tiler.resize(False, True))  # side by side has no height to change
        self.build()  # restart: the proportion is kept for this monitor
        self.assertEqual(self.tiler._adjust[LEFT_MONITOR.device]["weights"], [0.55, 0.45])

    def test_main_side_width_and_heights(self):
        self.turn_on(MAIN_SIDE, ratio=0.5)
        code, notes, mail = self.open("Code"), self.open("Notes"), self.open("Mail")
        self.settle()
        self.focus_on(notes)
        self.run_and_settle(self.tiler.resize, True, True)  # a side window wider = the main window narrower
        self.assertEqual(code.rect[2], 900)
        self.run_and_settle(self.tiler.resize, False, True)
        self.assertEqual(notes.rect[3] - notes.rect[1], 528)  # 55 % of 960
        self.assertEqual(mail.rect[1], 528)

    def test_bsp_resize_and_many_presses_are_not_rate_limited(self):
        self.turn_on(BSP)
        code, notes = self.open("Code"), self.open("Notes")
        self.settle()
        self.focus_on(code)
        for _ in range(10):
            self.tiler.resize(True, True)
            self.settle()
        self.assertEqual(code.rect[2], 1800)  # 90 %: as far as it goes, every press applied (no back-off)
        self.assertEqual(notes.rect[0], 1800)
        self.assertFalse(self.tiler.resize(True, True))

    def test_grid_tiles_cannot_be_resized_alone(self):
        self.turn_on(GRID)
        code = self.open("Code")
        self.open("Notes")
        self.settle()
        self.focus_on(code)
        self.assertFalse(self.tiler.resize(True, True))
        self.assertTrue(any("BSP can resize" in m for m in self.messages))

    def test_workspace_resize_is_stored_with_the_workspace(self):
        ws = self.tiled_workspace("Code", "Notes")
        code, _notes = self.open("Code"), self.open("Notes")
        self.settle()
        self.focus_on(code)
        self.run_and_settle(self.tiler.resize, True, False)
        self.assertEqual(self.manager.get(ws.id).layout.weights, [0.45, 0.55])
        self.assertEqual(code.rect[2], 900)


class MonocleTests(CommandEnv):
    def test_monocle_fills_the_screen_and_switching_off_restores_the_layout_exactly(self):
        self.turn_on(MAIN_SIDE, ratio=0.6)
        code, notes, mail = self.open("Code"), self.open("Notes"), self.open("Mail")
        self.settle()
        before = [code.rect, notes.rect, mail.rect]
        self.focus_on(notes)
        self.run_and_settle(self.tiler.toggle_monocle)
        self.assertEqual(notes.rect, LEFT_MONITOR.work)
        self.assertEqual((code.rect, mail.rect), (before[0], before[2]))  # the others are not touched
        self.assertTrue(self.tiler.monocle_on())
        self.assertIn(("raise", notes.hwnd), self.desktop.calls)
        self.assertEqual(self.tiler.describe(self.desktop.tracked(notes.hwnd)), "monocle")
        self.run_and_settle(self.tiler.toggle_monocle)
        self.assertEqual([code.rect, notes.rect, mail.rect], before)
        self.assertFalse(self.tiler.monocle_on())

    def test_monocle_ends_when_its_window_closes_or_a_new_one_opens(self):
        self.turn_on()
        code, notes = self.open("Code"), self.open("Notes")
        self.settle()
        self.focus_on(code)
        self.run_and_settle(self.tiler.toggle_monocle)
        mail = self.open("Mail")  # a new window: the layout comes back, with it
        self.settle()
        self.assertFalse(self.tiler.monocle_on())
        self.assertEqual([code.rect, notes.rect, mail.rect], self.slots(3))
        self.focus_on(mail)
        self.run_and_settle(self.tiler.toggle_monocle)
        self.desktop.close(mail.hwnd)
        self.sync()
        self.settle()
        self.assertFalse(self.tiler.monocle_on())
        self.assertEqual([code.rect, notes.rect], self.slots(2))

    def test_dragging_the_monocle_window_ends_monocle(self):
        self.turn_on()
        code, notes = self.open("Code"), self.open("Notes")
        self.settle()
        self.focus_on(code)
        self.run_and_settle(self.tiler.toggle_monocle)
        self.drag(code, True)
        self.drag(code, False)
        self.settle()
        self.assertFalse(self.tiler.monocle_on())
        self.assertEqual([code.rect, notes.rect], self.slots(2))

    def test_monocle_in_a_workspace_with_dpi_and_a_gap(self):
        right = Monitor(RIGHT_MONITOR.device, RIGHT_MONITOR.rect, RIGHT_MONITOR.work, False, 144)
        self.desktop.monitors_ = [LEFT_MONITOR, right]
        ws = self.tiled_workspace("Code", "Notes", monitor=right.ref())
        self.manager.update_tiling(ws.id)
        layout = self.manager.get(ws.id).layout
        layout.gap = 8
        code, _notes = self.open("Code"), self.open("Notes")
        self.settle()
        self.focus_on(code)
        self.run_and_settle(self.tiler.toggle_monocle)
        self.assertEqual(code.rect, (2012, 12, 3988, 988))  # 8 px gap at 150 %

    def test_monocle_needs_tiling(self):
        code = self.open("Code")
        self.focus_on(code)
        self.assertFalse(self.tiler.toggle_monocle())
        self.assertTrue(any("Dynamic tiling is off" in m for m in self.messages))


class BspTilingTests(CommandEnv):
    def test_new_windows_split_the_focused_tile_and_closing_moves_only_the_sibling(self):
        self.turn_on(BSP)
        code, notes = self.open("Code"), self.open("Notes")
        self.settle()
        self.focus_on(code)
        notes_before = notes.rect
        mail = self.open("Mail")  # it splits Code's tile (the focused one), Notes stays
        self.settle()
        self.assertEqual(notes.rect, notes_before)
        self.assertEqual(code.rect, (0, 0, 500, 960))
        self.assertEqual(mail.rect, (500, 0, 1000, 960))
        self.desktop.calls.clear()
        self.desktop.close(mail.hwnd)
        self.sync()
        self.settle()
        self.assertEqual(code.rect, (0, 0, 1000, 960))
        self.assertEqual({c[1] for c in self.desktop.calls if c[0] == "pos"}, {code.hwnd})  # Notes was not moved

    def test_workspace_tree_is_stored_by_entry_and_survives_a_restart(self):
        ws = self.tiled_workspace("Code", "Notes", "Brave", kind=BSP)
        code, notes, brave = self.open("Code"), self.open("Notes"), self.open("Brave")
        self.settle()
        self.focus_on(code)
        self.run_and_settle(self.tiler.resize, True, True)
        stored = self.manager.get(ws.id).layout.bsp
        entries = [a.id for a in self.manager.get(ws.id).apps]
        self.assertEqual(sorted(bsp.leaves(bsp.from_dict(stored))), sorted(entries))
        places = [code.rect, notes.rect, brave.rect]
        self.tiler.stop()
        self.build()  # CIRANA restarts: the windows are found again and the same tree is used
        self.manager.set_active(ws.id)
        self.settle()
        self.assertEqual([code.rect, notes.rect, brave.rect], places)

    def test_drag_swap_and_preview_in_bsp(self):
        self.turn_on(BSP)
        code, notes, mail = self.open("Code"), self.open("Notes"), self.open("Mail")
        self.settle()
        before = {x.hwnd: x.rect for x in (code, notes, mail)}
        self.drag(code, True)
        code.rect = (mail.rect[0] + 40, mail.rect[1] + 40, mail.rect[0] + 400, mail.rect[1] + 300)
        self.sync()
        self.pump(timeout=0.03)
        preview = self.tiler._preview
        self.drag(code, False)
        self.settle()
        self.assertEqual((code.rect, mail.rect), (before[mail.hwnd], before[code.hwnd]))
        self.assertEqual(preview, code.rect)  # the preview showed exactly where it landed
        self.assertEqual(notes.rect, before[notes.hwnd])

    def test_bsp_minimum_size_learned_and_room_made(self):
        self.turn_on(BSP)
        code = self.open("Code")
        notes = self.open("Notes", min_size=(1400, 0))
        self.settle()
        self.assertEqual(notes.rect[2] - notes.rect[0], 1400)
        self.assertEqual(code.rect, (0, 0, 600, 960))  # room was made, nothing overlaps
        self.assert_on_screen()

    def test_automatic_layout_changes_with_the_count(self):
        self.turn_on(AUTO)
        windows = [self.open("Code"), self.open("Notes")]
        self.settle()
        self.assertEqual([x.rect for x in windows], self.slots(2, SIDE_BY_SIDE))
        windows.append(self.open("Mail"))
        self.settle()
        self.assertEqual([x.rect for x in windows], self.slots(3, MAIN_SIDE, ratio=0.6))
        windows.append(self.open("Game"))
        self.settle()
        self.assertEqual([x.rect for x in windows], self.slots(4, GRID))
        count = self.tiler.count
        for _ in range(3):
            self.sync()
            self.settle(timeout=0.2)
        self.assertEqual(self.tiler.count, count)  # stable: no flip-flopping between layouts


class WindowRuleTests(CommandEnv):
    def rule(self, **fields):
        rule = WindowRule("r-test", **fields)
        self.tiler.set_rules(self.tiler.rules() + [rule])
        return rule

    def test_title_only_rules_are_refused(self):
        self.assertTrue(WindowRule("x", title="Mail").problem())
        self.tiler.set_rules([WindowRule("x", title="Mail", action=FLOAT)])
        self.assertEqual(self.tiler.rules(), [])

    def test_float_rule_by_exe_and_class_leaves_other_windows_of_the_program_tiled(self):
        self.turn_on()
        code = self.open("Code", class_name="Chrome_WidgetWin_1")
        palette = self.open("Code", "Palette", class_name="PaletteWindow", rect=(5, 5, 400, 300))
        self.rule(exe="code.exe", class_name="palettewindow", action=FLOAT)  # case does not matter
        self.settle()
        self.assertEqual(palette.rect, (5, 5, 400, 300))
        self.assertEqual(code.rect, LEFT_MONITOR.work)
        self.assertIn("window rule", self.tiler.float_reason(self.desktop.tracked(palette.hwnd)))

    def test_rules_do_not_catch_other_programs_or_titles(self):
        rule = WindowRule("r", exe="code.exe", title="Settings", action=IGNORE)
        same = self.desktop.tracked(self.open("Code", "Settings - Code").hwnd)
        other_title = self.desktop.tracked(self.open("Code", "main.py - Code").hwnd)
        other_exe = self.desktop.tracked(self.open("Notes", "Settings").hwnd)
        self.assertTrue(rule.matches(same))
        self.assertFalse(rule.matches(other_title))
        self.assertFalse(rule.matches(other_exe))
        by_path = WindowRule("p", exe="C:\\Apps\\CODE.exe", action=FLOAT)
        self.assertTrue(by_path.matches(same))
        self.assertFalse(WindowRule("q", exe="d:\\other\\code.exe", action=FLOAT).matches(same))
        packaged = self.desktop.tracked(self.open("Calc", aumid="Microsoft.Calc_8w!App").hwnd)
        self.assertTrue(WindowRule("a", aumid="microsoft.calc_8w!app", action=FLOAT).matches(packaged))
        self.assertFalse(WindowRule("a", aumid="Microsoft.Calc_8w!Other", action=FLOAT).matches(packaged))
        disabled = WindowRule("d", exe="code.exe", action=FLOAT, enabled=False)
        self.assertFalse(disabled.matches(same))

    def test_ignore_rule_keeps_cirana_away_from_the_window(self):
        ws = self.tiled_workspace("Code", "Notes")
        self.rule(exe="notes.exe", action=IGNORE)
        code, notes = self.open("Code"), self.open("Notes", rect=(7, 7, 300, 300))
        self.settle()
        self.assertEqual(notes.rect, (7, 7, 300, 300))
        self.assertEqual(code.rect, LEFT_MONITOR.work)
        self.assertIsNone(self.wm.bound_window(ws.id, self.manager.get(ws.id).apps[1].id))
        self.focus_on(code)
        self.assertFalse(self.tiler.focus_direction(RIGHT))  # not a target of keyboard focus either

    def test_workspace_rule_assigns_every_matching_window_without_stealing(self):
        ws = self.tiled_workspace("Code")
        self.rule(exe="brave.exe", action=WORKSPACE, workspace_id=ws.id)
        code = self.open("Code")
        first, second = self.open("Brave", "Mail - Brave"), self.open("Brave", "Docs - Brave")
        game = self.open("Game", rect=(9, 9, 300, 300))
        self.settle()
        self.assertEqual([code.rect, first.rect, second.rect], self.slots(3))  # both windows of the same exe
        self.assertEqual(game.rect, (9, 9, 300, 300))
        slots = [slot for slot, _w in self.tiler.tile_set(ws.id)]
        self.assertEqual(slots[1:], [RULE_SLOT + self.desktop.tracked(h).key for h in (first.hwnd, second.hwnd)])
        self.assertEqual(len(self.manager.get(ws.id).apps), 1)  # no entries were added
        self.wm.activate(ws.id)
        self.settle()
        self.assertTrue({first.hwnd, second.hwnd} <= {c[1] for c in self.desktop.calls if c[0] == "raise"})

    def test_rules_are_kept_and_damaged_ones_dropped(self):
        self.rule(exe="code.exe", action=FLOAT)
        self.settings.set("window_rules", self.settings.get("window_rules") + [{"title": "x"}, "junk"])
        self.build()
        self.assertEqual([r.exe for r in self.tiler.rules()], ["code.exe"])


class FloatToggleTests(CommandEnv):
    def test_toggle_float_and_back(self):
        self.turn_on()
        code, notes = self.open("Code"), self.open("Notes")
        self.settle()
        self.focus_on(notes)
        self.run_and_settle(self.tiler.toggle_float)
        self.assertEqual(code.rect, LEFT_MONITOR.work)
        self.run_and_settle(self.tiler.toggle_float)
        self.assertEqual([code.rect, notes.rect], self.slots(2))
        self.assertEqual(self.tiler.float_reason(self.desktop.tracked(notes.hwnd)), "")


class ConcurrencyTests(CommandEnv):
    def test_commands_during_a_running_retile_and_bursts(self):
        self.turn_on(BSP)
        windows = [self.desktop.add(f"c:\\apps\\a{i}.exe", f"A{i}") for i in range(5)]
        self.sync()
        self.focus_on(windows[0])
        for direction in (RIGHT, DOWN, LEFT, UP, RIGHT):  # commands while windows arrive and re-tiles run
            self.tiler.swap_direction(direction)
            self.tiler.resize(True, True)
            self.pump(timeout=0.01)
        self.desktop.close(windows[3].hwnd)
        self.sync()
        self.settle()
        rects = [x.rect for x in self.desktop.windows.values()]
        self.assertEqual(len(set(rects)), len(rects))  # every window has its own tile
        for monitor in self.desktop.monitors():  # a swap at the right edge may have moved one to the other monitor
            mine = [r for r in rects if intersection_area(r, monitor.work) > 0]
            if mine:
                work = monitor.work
                self.assertEqual(sum((r[2] - r[0]) * (r[3] - r[1]) for r in mine),
                                 (work[2] - work[0]) * (work[3] - work[1]))  # no overlap, nothing lost
        self.assert_on_screen()

    def test_tile_set_of_the_same_exe_several_windows(self):
        self.turn_on()
        windows = [self.open("Brave", f"Tab {i} - Brave") for i in range(3)]
        self.settle()
        self.assertEqual([x.rect for x in windows], self.slots(3))
        self.assertEqual(len({self.desktop.tracked(x.hwnd).key for x in windows}), 3)


if __name__ == "__main__":
    unittest.main()
