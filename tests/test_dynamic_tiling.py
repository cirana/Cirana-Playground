"""Dynamic tiling on the simulated desktop: windows opening, closing, minimizing, being re-created or dragged re-tile
the active workspace by themselves - once per change, never in a loop, and never touching unrelated windows."""
import time
import unittest

from cirana.core.dynamic_tiling import DynamicTiler, RATE_LIMIT
from cirana.core.models import stable_app_id
from cirana.core.workspace_controller import EntryState
from cirana.native import win32_api as w
from cirana.wm.layout import FREE, GRID, MAIN_SIDE, SIDE_BY_SIDE, LayoutSpec, tile
from cirana.wm.monitors import Monitor
from tests.test_workspace_controller import LEFT, RIGHT, Env, app


class TilingEnv(Env):
    def build(self):
        super().build()
        self.tiler = DynamicTiler(self.wm, self.settings, debounce_ms=10, display_debounce_ms=10)
        self.addCleanup(self.tiler.stop)

    def settle(self, timeout=3.0):
        """Wait until no re-tile is pending or running, and stays so for a moment."""
        end = time.monotonic() + timeout
        quiet_since = None
        while time.monotonic() < end:
            app.processEvents()
            busy = self.tiler._timer.isActive() or self.tiler._job is not None or self.wm._notify.isActive()
            if busy:
                quiet_since = None
            elif quiet_since is None:
                quiet_since = time.monotonic()
            elif time.monotonic() - quiet_since > 0.08:
                return True
            time.sleep(0.005)
        return False

    def tiled_workspace(self, *names, kind=SIDE_BY_SIDE, dynamic=True, activate=True, **spec):
        w = self.manager.create("Tiled", [stable_app_id(n) for n in names])
        self.manager.set_layout(w.id, LayoutSpec(kind, gap=0, dynamic=dynamic, **spec))
        for entry in self.manager.get(w.id).apps:
            self.manager.set_launch(w.id, entry.id, False)  # these tests open the windows themselves
        if activate:
            self.manager.set_active(w.id)
        return w

    def open(self, name, title=None, **kwargs):
        window = self.desktop.add(f"c:\\apps\\{name.lower()}.exe", title or name, **kwargs)
        self.sync()
        return window

    def slots(self, count, kind=SIDE_BY_SIDE, work=LEFT.work, **spec):
        return tile(LayoutSpec(kind, gap=0, **spec), work, count)


class DynamicTilingTests(TilingEnv):
    def test_off_means_nothing_moves_by_itself(self):
        w = self.tiled_workspace("Code", "Notes", dynamic=False)
        code = self.open("Code")
        self.settle()
        self.assertEqual(code.rect, (100, 100, 900, 700))
        self.assertEqual(self.tiler.count, 0)
        self.assertFalse(self.tiler.manages(w.id))
        self.wm.apply_layout(w.id)  # the manual button still works exactly as before
        self.idle(w.id)
        self.assertEqual(code.rect, LEFT.work)  # (Notes is not started automatically: no place is kept for it)

    def test_opening_and_closing_windows_retiles(self):
        self.tiled_workspace("Code", "Notes")
        code = self.open("Code")
        self.settle()
        self.assertEqual(code.rect, LEFT.work)  # alone: the whole work area
        notes = self.open("Notes")
        self.settle()
        self.assertEqual([code.rect, notes.rect], self.slots(2))
        self.desktop.close(code.hwnd)
        self.sync()
        self.settle()
        self.assertEqual(notes.rect, LEFT.work)  # the gap is filled

    def test_minimize_leaves_the_layout_and_restore_comes_back(self):
        self.tiled_workspace("Code", "Notes", "Brave", kind=MAIN_SIDE, ratio=0.6)
        code, notes, brave = self.open("Code"), self.open("Notes"), self.open("Brave")
        self.settle()
        self.assertEqual([code.rect, notes.rect, brave.rect], self.slots(3, MAIN_SIDE, ratio=0.6))
        notes.minimized = True
        self.sync()
        self.settle()
        self.assertEqual([code.rect, brave.rect], self.slots(2, MAIN_SIDE, ratio=0.6))
        self.assertTrue(notes.minimized)  # never restored by CIRANA
        notes.minimized = False
        self.sync()
        self.settle()
        self.assertEqual([code.rect, notes.rect, brave.rect], self.slots(3, MAIN_SIDE, ratio=0.6))

    def test_maximized_window_is_left_alone(self):
        self.tiled_workspace("Code", "Notes")
        code = self.open("Code")
        notes = self.open("Notes", maximized=True, rect=LEFT.work)
        self.settle()
        self.assertTrue(notes.maximized)
        self.assertEqual(code.rect, LEFT.work)

    def test_recreated_window_takes_the_old_ones_place(self):
        self.tiled_workspace("Code", "Notes")
        code, notes = self.open("Code"), self.open("Notes")
        self.settle()
        self.desktop.close(notes.hwnd)  # the application re-creates its window (new handle, same process)
        again = self.desktop.add("c:\\apps\\notes.exe", "Notes", pid=notes.pid, created=notes.created)
        self.sync()
        self.settle()
        self.assertEqual([code.rect, again.rect], self.slots(2))

    def test_unrelated_and_ambiguous_windows_are_never_touched(self):
        self.tiled_workspace("Code", "Brave")
        code = self.open("Code")
        game = self.open("Game", rect=(5, 5, 300, 300))
        first = self.open("Brave", "Mail - Brave", rect=(10, 10, 400, 400))
        second = self.open("Brave", "Docs - Brave", rect=(20, 20, 420, 420))
        self.settle()
        self.assertEqual(game.rect, (5, 5, 300, 300))
        self.assertEqual((first.rect, second.rect), ((10, 10, 400, 400), (20, 20, 420, 420)))  # which one? not sure
        self.assertEqual(code.rect, LEFT.work)
        self.assertIs(self.wm.statuses(self.manager.active_id)[1].state, EntryState.AMBIGUOUS)
        self.wm.choose(self.manager.active_id, self.manager.active().apps[1].id,
                       self.desktop.tracked(second.hwnd).key, place=False)  # the user decides
        self.settle()
        self.assertEqual([code.rect, second.rect], self.slots(2))
        self.assertEqual(first.rect, (10, 10, 400, 400))

    def test_own_moves_never_trigger_another_retile(self):
        self.tiled_workspace("Code", "Notes")
        self.open("Code")
        self.open("Notes")
        self.settle()
        count = self.tiler.count
        for _ in range(5):  # the tracker reports the new positions (location events of CIRANA's own moves)
            self.sync()
            self.settle(timeout=0.3)
        self.assertEqual(self.tiler.count, count)

    def test_an_application_that_fights_back_is_rate_limited(self):
        self.tiled_workspace("Code", "Notes")
        self.open("Code")
        notes = self.open("Notes")
        self.settle()
        start = self.tiler.count
        for i in range(RATE_LIMIT * 3):  # minimizes / restores itself whenever it is moved
            notes.minimized = not notes.minimized
            self.sync()
            self.settle(timeout=0.15)
        self.assertLessEqual(self.tiler.count - start, RATE_LIMIT + 1)

    def test_a_burst_of_windows_is_one_retile(self):
        self.tiled_workspace("Code", "Notes", "Brave", kind=GRID)
        before = self.tiler.count
        for name in ("Code", "Notes", "Brave"):
            self.desktop.add(f"c:\\apps\\{name.lower()}.exe", name)
        self.sync()  # all three reported together
        self.settle()
        self.assertEqual(self.tiler.count - before, 1)
        rects = sorted(win.rect for win in self.desktop.windows.values())
        self.assertEqual(rects, sorted(self.slots(3, GRID)))


class DragTests(TilingEnv):
    def drag(self, window, started):
        self.tracker._on_event(w.EVENT_SYSTEM_MOVESIZESTART if started else w.EVENT_SYSTEM_MOVESIZEEND, window.hwnd)
        self.pump(timeout=0.05)

    def test_nothing_moves_while_the_user_drags(self):
        self.tiled_workspace("Code", "Notes", "Brave")
        code, notes = self.open("Code"), self.open("Notes")
        self.settle()
        self.drag(code, True)
        self.assertTrue(self.tiler.paused(self.manager.active_id))
        code.rect = (300, 300, 700, 700)  # being dragged
        brave = self.open("Brave")  # something opens meanwhile
        self.settle(timeout=0.3)
        self.assertEqual(code.rect, (300, 300, 700, 700))
        self.assertEqual(brave.rect, (100, 100, 900, 700))
        self.drag(code, False)  # released where no other tile is
        self.settle()
        self.assertFalse(self.tiler.paused(self.manager.active_id))
        self.assertEqual([code.rect, notes.rect, brave.rect], self.slots(3))

    def test_dropping_a_window_on_another_tile_swaps_them(self):
        w_ = self.tiled_workspace("Code", "Notes")
        code, notes = self.open("Code"), self.open("Notes")
        self.settle()
        left, right = self.slots(2)
        self.drag(code, True)
        code.rect = (right[0] + 50, 100, right[0] + 450, 500)  # dropped over the Notes tile
        self.drag(code, False)
        self.settle()
        self.assertEqual((code.rect, notes.rect), (right, left))
        self.assertEqual([a.app_id for a in self.manager.get(w_.id).apps],
                         [stable_app_id("Notes"), stable_app_id("Code")])  # the order is kept

    def test_resizing_in_place_snaps_back_to_the_layout(self):
        self.tiled_workspace("Code", "Notes")
        code, notes = self.open("Code"), self.open("Notes")
        self.settle()
        self.drag(notes, True)
        notes.rect = (notes.rect[0] + 40, notes.rect[1], notes.rect[2], notes.rect[3])
        self.drag(notes, False)
        self.settle()
        self.assertEqual([code.rect, notes.rect], self.slots(2))

    def test_drags_of_other_windows_are_ignored(self):
        self.tiled_workspace("Code")
        self.open("Code")
        game = self.open("Game")
        self.settle()
        self.drag(game, True)
        self.assertFalse(self.tiler.paused(self.manager.active_id))


class SafetyTests(TilingEnv):
    def test_refused_and_hung_windows_do_not_break_the_others(self):
        self.tiled_workspace("Code", "Notes", "Brave", "Shell")
        code = self.open("Code")
        admin = self.open("Notes", "Notes (admin)", elevated=True, rect=(10, 10, 200, 200))
        hung = self.open("Brave", hung=True, rect=(20, 20, 220, 220))
        fixed = self.desktop.add("c:\\windows\\system32\\powershell.exe", "Shell", fixed_size=True,
                                 rect=(0, 0, 300, 200))
        self.sync()
        self.settle()
        # the elevated window is left out; the hung one is skipped for now; the others share the space
        self.assertEqual((admin.rect, hung.rect), ((10, 10, 200, 200), (20, 20, 220, 220)))
        slots = self.slots(2)
        self.assertEqual(code.rect, slots[0])
        self.assertEqual(fixed.rect[:2], slots[1][:2])  # moved, size kept
        self.assertEqual(fixed.rect[2] - fixed.rect[0], 300)
        statuses = self.wm.statuses(self.manager.active_id)
        self.assertIn("higher rights", statuses[1].message)
        self.assertEqual(sum("higher rights" in m for m in self.messages), 1)  # reported once, not every re-tile

    def test_recycled_handle_is_not_moved(self):
        self.tiled_workspace("Code", "Notes")
        code, notes = self.open("Code"), self.open("Notes")
        self.settle()
        self.desktop.close(notes.hwnd)
        stranger = self.desktop.add("c:\\apps\\other.exe", "Other", hwnd=notes.hwnd, pid=4444, rect=(1, 1, 99, 99))
        self.sync()
        self.settle()
        self.assertEqual(stranger.rect, (1, 1, 99, 99))
        self.assertEqual(code.rect, LEFT.work)

    def test_only_the_active_workspace_is_managed(self):
        first = self.tiled_workspace("Code", activate=False)
        second = self.manager.create("Second", [stable_app_id("Notes")])
        self.manager.set_layout(second.id, LayoutSpec(SIDE_BY_SIDE, gap=0, dynamic=True))
        self.manager.set_active(second.id)
        code = self.open("Code", rect=(5, 5, 500, 500))
        notes = self.open("Notes")
        self.settle()
        self.assertEqual(code.rect, (5, 5, 500, 500))  # its workspace is not the active one
        self.assertEqual(notes.rect, LEFT.work)
        self.manager.set_active(first.id)  # switching: now Code is managed, Notes is left where it is
        self.settle()
        self.assertEqual(code.rect, LEFT.work)
        notes_place = notes.rect
        self.desktop.close(code.hwnd)
        self.sync()
        self.open("Notes", "Another Notes")
        self.settle()
        self.assertEqual(notes.rect, notes_place)

    def test_switching_workspace_drops_a_running_retile(self):
        self.tiled_workspace("Code")
        code = self.open("Code", rect=(5, 5, 500, 500))
        gate = []
        original = self.wm.windows.place_all

        def slow(placements, cancel=None):
            while not gate:
                time.sleep(0.01)
            return original(placements, cancel)

        self.wm.windows.place_all = slow
        self.addCleanup(gate.append, True)
        self.pump(lambda: self.tiler._job is not None)
        self.manager.set_active("")  # switched away while the re-tile runs
        reflowed = []
        self.tiler.reflowed.connect(reflowed.append)
        gate.append(True)
        self.pump(timeout=0.3)
        self.assertEqual(reflowed, [])  # its late result is ignored
        self.assertIsNone(self.tiler._job)
        del code

    def test_stopping_leaves_every_window_where_it_is(self):
        self.tiled_workspace("Code", "Notes")
        code = self.open("Code")
        self.settle()
        self.tiler.stop()
        self.desktop.calls.clear()
        self.open("Notes")
        self.settle()
        self.assertEqual(self.desktop.calls, [])
        self.assertEqual(code.rect, LEFT.work)


class MonitorTests(TilingEnv):
    def test_other_monitor_with_another_dpi(self):
        right = Monitor(RIGHT.device, RIGHT.rect, RIGHT.work, False, 144)
        self.desktop.monitors_ = [LEFT, right]
        self.tiled_workspace("Code", "Notes", monitor=right.ref())
        code, notes = self.open("Code"), self.open("Notes")
        self.settle()
        self.assertEqual([code.rect, notes.rect], self.slots(2, work=right.work))

    def test_monitor_unplugged_moves_the_layout_to_the_primary(self):
        self.tiled_workspace("Code", monitor=RIGHT.ref())
        code = self.open("Code")
        self.settle()
        self.assertEqual(code.rect, RIGHT.work)
        self.desktop.monitors_ = [LEFT]
        self.tiler.display_changed()
        self.settle()
        self.assertEqual(code.rect, LEFT.work)

    def test_minimum_size_is_kept(self):
        self.tiled_workspace("Code", "Notes")
        code = self.open("Code", min_size=(1500, 300))
        notes = self.open("Notes")
        self.settle()
        self.assertEqual(code.rect[2] - code.rect[0], 1500)
        self.assertEqual(notes.rect, (1500, 0, 2000, 960))  # the layout made room for it (tests.test_minimum_sizes)
        self.assertEqual(self.wm.statuses(self.manager.active_id)[0].message, "")


class SettingTests(TilingEnv):
    def test_setting_is_saved_and_old_records_default_to_off(self):
        w_ = self.tiled_workspace("Code", dynamic=False, activate=False)
        self.manager.set_dynamic(w_.id, True)
        self.build()
        self.assertTrue(self.manager.get(w_.id).layout.dynamic)
        raw = self.manager.get(w_.id).layout.to_dict()
        raw.pop("dynamic")
        self.assertFalse(LayoutSpec.from_dict(raw).dynamic)  # written before the setting existed
        self.assertFalse(LayoutSpec.from_dict({**raw, "dynamic": "yes"}).dynamic)

    def test_saved_positions_layout_is_not_tiled(self):
        w_ = self.tiled_workspace("Code", kind=FREE)
        code = self.open("Code")
        self.settle()
        self.assertFalse(self.tiler.manages(w_.id))
        self.assertEqual(code.rect, (100, 100, 900, 700))

    def test_activation_and_launch_with_tiling(self):
        w_ = self.tiled_workspace("Code", "Notes", activate=False)
        self.manager.set_launch(w_.id, self.manager.get(w_.id).apps[1].id, True)
        code = self.open("Code")
        self.on_launch["Notes"] = lambda pid: None
        self.wm.activate(w_.id)
        self.assertTrue(self.pump(lambda: all(p.launched for p in self.launcher._pending) and self.launcher._pending))
        self.settle()
        self.assertEqual(code.rect, LEFT.work)  # no place is kept for an application that is still starting
        self.assertEqual(self.desktop.foreground, code.hwnd)  # the group was raised
        notes = self.desktop.add("c:\\apps\\notes.exe", "Notes")
        self.sync()
        self.settle()
        self.assertEqual([code.rect, notes.rect], self.slots(2))


class GlobalTilingTests(TilingEnv):
    """All-window tiling: every application window, per monitor, workspace or not."""

    def turn_on(self, kind=SIDE_BY_SIDE, **spec):
        self.tiler.set_global_layout(LayoutSpec(kind, gap=0, **spec))
        self.tiler.set_global(True)

    def drag(self, window, started):
        self.tracker._on_event(w.EVENT_SYSTEM_MOVESIZESTART if started else w.EVENT_SYSTEM_MOVESIZEEND, window.hwnd)
        self.pump(timeout=0.05)

    def test_every_window_is_tiled_without_any_workspace(self):
        game, code = self.open("Game"), self.open("Code")
        self.settle()
        self.assertEqual((game.rect, code.rect), ((100, 100, 900, 700), (100, 100, 900, 700)))  # off by default
        self.turn_on()
        self.settle()
        self.assertEqual([game.rect, code.rect], self.slots(2))
        mail = self.open("Mail")  # a new window joins at the end
        self.settle()
        self.assertEqual([game.rect, code.rect, mail.rect], self.slots(3))
        self.desktop.close(game.hwnd)  # the others fill the gap
        self.sync()
        self.settle()
        self.assertEqual([code.rect, mail.rect], self.slots(2))

    def test_each_monitor_has_its_own_layout(self):
        self.turn_on()
        left = self.open("Code")
        right1 = self.open("Game", rect=(2100, 100, 2600, 600))
        right2 = self.open("Mail", rect=(2200, 200, 2700, 700))
        self.settle()
        self.assertEqual(left.rect, LEFT.work)
        self.assertEqual([right1.rect, right2.rect], self.slots(2, work=RIGHT.work))
        self.desktop.monitors_ = [LEFT]  # the right monitor is unplugged: everything moves to the primary
        self.tiler.display_changed()
        self.settle()
        self.assertEqual(sorted([left.rect, right1.rect, right2.rect]), sorted(self.slots(3)))

    def test_fixed_size_full_screen_and_chosen_programs_float(self):
        self.turn_on()
        code = self.open("Code")
        dialog = self.open("Calc", fixed_size=True, rect=(5, 5, 305, 405))  # no sizing border: fixed size / full screen
        player = self.open("Player", rect=(50, 50, 650, 450))
        self.tiler.set_floating("player.exe", True)
        self.settle()
        self.assertEqual((dialog.rect, player.rect), ((5, 5, 305, 405), (50, 50, 650, 450)))
        self.assertEqual(code.rect, LEFT.work)
        self.assertIn("player.exe", self.settings.get("tiling_exclusions"))
        self.assertIn("fixed size", self.tiler.float_reason(self.desktop.tracked(dialog.hwnd)))
        self.tiler.set_floating("player.exe", False)  # tiled again
        self.settle()
        self.assertEqual([code.rect, player.rect], self.slots(2))

    def test_minimize_maximize_and_restore(self):
        self.turn_on()
        code, mail = self.open("Code"), self.open("Mail")
        self.settle()
        mail.minimized = True
        self.sync()
        self.settle()
        self.assertEqual(code.rect, LEFT.work)
        mail.minimized = False
        self.sync()
        self.settle()
        self.assertEqual([code.rect, mail.rect], self.slots(2))
        code.maximized, code.rect = True, LEFT.work
        self.sync()
        self.settle()
        self.assertTrue(code.maximized)  # left alone while maximized
        self.assertEqual(mail.rect, LEFT.work)

    def test_drag_swaps_tiles_and_moves_between_monitors(self):
        self.turn_on()
        code, mail = self.open("Code"), self.open("Mail")
        self.settle()
        left_tile, right_tile = self.slots(2)
        self.drag(code, True)
        self.assertTrue(self.tiler.paused())
        code.rect = (right_tile[0] + 40, 100, right_tile[0] + 400, 500)
        self.drag(code, False)
        self.settle()
        self.assertEqual((code.rect, mail.rect), (right_tile, left_tile))
        self.drag(mail, True)
        mail.rect = (2300, 200, 2800, 700)  # dropped on the other monitor
        self.drag(mail, False)
        self.settle()
        self.assertEqual((code.rect, mail.rect), (LEFT.work, RIGHT.work))

    def test_no_feedback_loop_and_bursts_coalesce(self):
        self.turn_on()
        for name in ("A", "B", "C", "D"):
            self.desktop.add(f"c:\\apps\\{name.lower()}.exe", name)
        self.sync()
        self.settle()
        count = self.tiler.count
        for _ in range(5):
            self.sync()  # the tracker reports the positions CIRANA set
            self.settle(timeout=0.3)
        self.assertEqual(self.tiler.count, count)
        self.assertLessEqual(count, 2)

    def test_refused_windows_float_and_unrelated_windows_are_safe(self):
        self.turn_on()
        admin = self.open("Admin", elevated=True, rect=(10, 10, 300, 300))
        code = self.open("Code")
        self.settle()
        self.assertEqual(admin.rect, (10, 10, 300, 300))
        self.assertEqual(code.rect, LEFT.work)
        self.assertIn("does not let", self.tiler.float_reason(self.desktop.tracked(admin.hwnd)))

    def test_workspace_tiling_steps_aside_and_activation_still_raises(self):
        ws = self.tiled_workspace("Code", kind=GRID)
        self.turn_on()
        self.assertFalse(self.tiler.manages(ws.id))
        code, game = self.open("Code"), self.open("Game")
        self.settle()
        self.assertEqual([code.rect, game.rect], self.slots(2))  # the global layout, not the workspace's
        self.wm.activate(ws.id)
        self.settle()
        self.assertEqual(self.desktop.foreground, code.hwnd)
        self.assertEqual([code.rect, game.rect], self.slots(2))

    def test_switching_off_leaves_windows_and_is_remembered(self):
        self.turn_on()
        code = self.open("Code")
        self.settle()
        self.tiler.set_global(False)
        self.desktop.calls.clear()
        self.open("Mail")
        self.settle()
        self.assertEqual(self.desktop.calls, [])
        self.assertEqual(code.rect, LEFT.work)
        self.tiler.set_global(True)
        self.build()  # restart: the setting and the layout are kept
        self.assertTrue(self.tiler.global_on)
        self.assertEqual(self.tiler.global_layout().kind, SIDE_BY_SIDE)


if __name__ == "__main__":
    unittest.main()
