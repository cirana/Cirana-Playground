"""Two, three, four and five windows get their own tiles in every layout and both scopes - also when applications have
minimum sizes - and nothing is stacked unless the user stacks it.

The third window used to end up floating on top of the others when the applications' minimum sizes did not fit the
chosen layout (typically side by side), because the tiler only ever floated the newest window instead of trying another
layout. Now another layout is used; a window floats only when no layout at all can hold it, and the reason is shown.
"""
import unittest

from cirana.core.models import stable_app_id
from cirana.wm import bsp
from cirana.wm.layout import AUTO, BSP, GRID, MAIN_SIDE, SIDE_BY_SIDE, LayoutSpec
from cirana.wm.rules import FLOAT, IGNORE, WindowRule, new_rule_id
from tests.test_minimum_sizes import inside, overlap
from tests.test_tiling_overlay import OverlayEnv
from tests.test_workspace_controller import LEFT, exe_app

KINDS = (SIDE_BY_SIDE, MAIN_SIDE, GRID, BSP, AUTO)
NAMES = ("Alpha", "Bravo", "Charlie", "Delta", "Echo")


class ThirdWindowEnv(OverlayEnv):
    def build(self):
        super().build()
        self.registry.apply_scan([exe_app(name) for name in NAMES])

    def fresh(self):
        self.desktop.windows.clear()
        self.sync()
        self.settle()
        self.tiler.set_global(False)
        for state in (self.tiler._adjust, self.tiler._trees, self.tiler._order, self.tiler._home, self.tiler._minimums,
                      self.tiler._history):
            state.clear()
        self.settings.set("global_adjust", {})

    def global_tiling(self, kind):
        self.tiler.set_global_layout(LayoutSpec(kind, gap=0))
        self.tiler.set_global(True)

    def open_scope(self, scope, kind, count, **kwargs):
        """``count`` windows in the scope's tile set (all windows, or the bound windows of a workspace)."""
        self.fresh()
        if scope == "global":
            self.global_tiling(kind)
        else:
            self.tiled_workspace(*NAMES[:count], kind=kind)
        windows = []
        for name in NAMES[:count]:
            self.tiler._history.clear()  # applications are opened one after the other, not within milliseconds
            windows.append(self.open(name, **kwargs))
            self.flush()
        return windows

    def assert_distinct(self, windows, work=LEFT.work):
        for i, a in enumerate(windows):
            self.assertTrue(inside(a.rect, work), (a.title, a.rect))
            for b in windows[i + 1:]:
                self.assertFalse(overlap(a.rect, b.rect), (a.title, a.rect, b.title, b.rect))


class EveryWindowGetsATileTests(ThirdWindowEnv):
    def test_two_to_five_windows_in_every_layout_and_scope(self):
        for scope in ("global", "workspace"):
            for kind in KINDS:
                for count in (2, 3, 4, 5):
                    if scope == "workspace" and count > len(NAMES):
                        continue
                    with self.subTest(scope=scope, kind=kind, count=count):
                        windows = self.open_scope(scope, kind, count)
                        self.assert_distinct(windows)
                        self.assertEqual(self.tiler.stack_count(), 0)  # never stacked by itself
                        self.assertEqual(len({w.rect for w in windows}), count)
                        for w in windows:
                            self.assertEqual(self.tiler.status(self.desktop.tracked(w.hwnd))[0], "tiled")

    def test_the_third_window_with_minimum_sizes_gets_its_own_tile(self):
        """The reported bug: three applications that each need 40 % of the width."""
        for scope in ("global", "workspace"):
            for kind in KINDS:
                with self.subTest(scope=scope, kind=kind):
                    windows = self.open_scope(scope, kind, 3, min_size=(800, 300))
                    self.assert_distinct(windows)
                    for w in windows:
                        self.assertGreaterEqual(w.rect[2] - w.rect[0], 800)
                        self.assertGreaterEqual(w.rect[3] - w.rect[1], 300)
                        self.assertEqual(self.tiler.status(self.desktop.tracked(w.hwnd))[0], "tiled")
                    self.assertEqual(self.tiler.stack_count(), 0)

    def test_four_and_five_windows_with_minimum_sizes(self):
        for kind in KINDS:
            for count in (4, 5):
                with self.subTest(kind=kind, count=count):
                    windows = self.open_scope("global", kind, count, min_size=(600, 250))
                    self.assert_distinct(windows)
                    for w in windows:
                        self.assertGreaterEqual(w.rect[2] - w.rect[0], 600)
                        self.assertGreaterEqual(w.rect[3] - w.rect[1], 250)

    def test_the_layout_that_was_chosen_is_not_changed_by_the_fallback(self):
        ws = self.tiled_workspace("Alpha", "Bravo", "Charlie", kind=SIDE_BY_SIDE)
        windows = [self.open(n, min_size=(800, 300)) for n in NAMES[:3]]
        self.flush()
        self.assert_distinct(windows)
        layout = self.manager.get(ws.id).layout
        self.assertEqual((layout.kind, layout.bsp, layout.weights), (SIDE_BY_SIDE, {}, []))  # still what the user chose
        group = self.tiler.shown_groups()[0]
        self.assertIn("Side by side →", group.fallback)
        state, why = self.tiler.status(self.desktop.tracked(windows[2].hwnd))
        self.assertEqual(state, "tiled")
        self.assertIn("layout adjusted", why)
        self.assertEqual(self.messages, [])  # nothing to complain about: every window has its tile
        # a window leaves: the chosen layout fits again and is used again
        self.desktop.close(windows[2].hwnd)
        self.sync()
        self.flush()
        self.assertEqual(self.tiler.shown_groups()[0].fallback, "")
        self.assertEqual(windows[0].rect[2], 1000)

    def test_a_chosen_bsp_tree_is_never_overwritten_by_the_fallback(self):
        ws = self.tiled_workspace("Alpha", "Bravo", "Charlie", kind=BSP)
        for n in NAMES[:3]:
            self.open(n, min_size=(1300, 200))
            self.flush()
        group = self.tiler.shown_groups()[0]
        self.assertTrue(group.fallback)
        self.assertEqual(self.manager.get(ws.id).layout.kind, BSP)
        stored = bsp.leaves(bsp.from_dict(self.manager.get(ws.id).layout.bsp))
        self.assertLess(len(stored), 3)  # the third window never entered the stored tree: it is not the layout in use

    def test_only_windows_no_layout_can_hold_float_and_say_why(self):
        self.fresh()
        self.global_tiling(SIDE_BY_SIDE)
        a = self.open("Alpha", min_size=(1200, 700))
        b = self.open("Bravo", min_size=(1200, 700))
        c = self.open("Charlie")
        self.flush()
        tracked = [self.desktop.tracked(w.hwnd) for w in (a, b, c)]
        states = [self.tiler.status(w) for w in tracked]
        floating = [s for s in states if s[0] == "floating"]
        self.assertEqual(len(floating), 1)  # one of the two big ones cannot be held next to the other
        self.assertIn("does not fit beside the other tiled windows in any layout", floating[0][1])
        self.assertTrue(any("in any layout" in m for m in self.messages))
        tiled = [w for w, s in zip((a, b, c), states) if s[0] == "tiled"]
        self.assert_distinct(tiled)
        for w in (a, b, c):
            self.assertTrue(inside(w.rect, LEFT.work), w.rect)  # the floating one is still completely visible

    def test_the_fallback_is_stable(self):
        self.fresh()
        self.global_tiling(SIDE_BY_SIDE)
        windows = [self.open(n, min_size=(800, 300)) for n in NAMES[:3]]
        self.flush()
        count = self.tiler.count
        rects = [w.rect for w in windows]
        for _ in range(3):
            self.tiler.schedule(force=True)
            self.flush()
        self.assertEqual([w.rect for w in windows], rects)
        self.assertLessEqual(self.tiler.count - count, 3)


class OpenCloseMinimizeSwitchTests(ThirdWindowEnv):
    def test_open_close_minimize_restore(self):
        self.fresh()
        self.global_tiling(MAIN_SIDE)
        a, b, c = [self.open(n) for n in NAMES[:3]]
        self.flush()
        self.assert_distinct([a, b, c])
        c.minimized = True
        self.sync()
        self.flush()
        self.assertEqual(self.tiler.status(self.desktop.tracked(c.hwnd))[0], "floating")
        self.assertIn("minimized", self.tiler.status(self.desktop.tracked(c.hwnd))[1])
        self.assertEqual((a.rect[2], b.rect[0]), (1200, 1200))
        self.assertEqual(b.rect[3], LEFT.work[3])  # b is alone in the side column
        c.minimized = False
        self.sync()
        self.flush()
        self.assert_distinct([a, b, c])
        self.assertEqual(self.tiler.status(self.desktop.tracked(c.hwnd))[0], "tiled")
        d = self.open("Delta")
        self.flush()
        self.assert_distinct([a, b, c, d])
        for w in (a, b):
            self.desktop.close(w.hwnd)
        self.sync()
        self.flush()
        self.assert_distinct([c, d])
        self.assertEqual({c.rect, d.rect}, set(self.slots(2, MAIN_SIDE, ratio=0.6)))

    def test_workspace_switch_places_every_window_of_the_new_workspace(self):
        first = self.tiled_workspace("Alpha", "Bravo", "Charlie", kind=GRID)
        windows = [self.open(n) for n in NAMES[:3]]
        self.flush()
        self.assert_distinct(windows)
        second = self.manager.create("Second", [stable_app_id("Delta"), stable_app_id("Echo")])
        self.manager.set_layout(second.id, LayoutSpec(SIDE_BY_SIDE, gap=0, dynamic=True))
        for entry in self.manager.get(second.id).apps:
            self.manager.set_launch(second.id, entry.id, False)
        d, e = self.open("Delta"), self.open("Echo")
        self.manager.set_active(second.id)
        self.flush()
        self.assertEqual([d.rect, e.rect], self.slots(2))
        self.manager.set_active(first.id)
        self.flush()
        self.assert_distinct(windows)
        self.assertEqual(self.tiler.status(self.desktop.tracked(windows[2].hwnd))[0], "tiled")

    def test_status_of_every_kind(self):
        self.fresh()
        self.global_tiling(SIDE_BY_SIDE)
        a, b, c, d = [self.open(n) for n in NAMES[:4]]
        self.tiler.set_rules([WindowRule(new_rule_id(), name="Echo off", action=IGNORE, exe="delta.exe")])
        self.tiler.toggle_float(self.desktop.tracked(c.hwnd))
        self.flush()
        self.assertTrue(self.tiler.stack_with(self.desktop.tracked(b.hwnd), self.desktop.tracked(a.hwnd)))
        self.flush()
        status = {w.title: self.tiler.status(self.desktop.tracked(w.hwnd)) for w in (a, b, c, d)}
        self.assertEqual(status["Alpha"][0], "stacked")
        self.assertEqual(status["Bravo"][0], "stacked")
        self.assertEqual(status["Charlie"][0], "floating")
        self.assertIn("you chose", status["Charlie"][1])
        self.assertEqual(status["Delta"][0], "ignored")
        self.assertIn("Echo off", status["Delta"][1])

    def test_nothing_is_reported_when_tiling_is_off(self):
        self.fresh()
        a = self.open("Alpha")
        self.assertEqual(self.tiler.status(self.desktop.tracked(a.hwnd)), ("", ""))

    def test_a_float_rule_is_floating(self):
        self.fresh()
        self.global_tiling(SIDE_BY_SIDE)
        self.tiler.set_rules([WindowRule(new_rule_id(), name="Floaty", action=FLOAT, exe="bravo.exe")])
        a, b = self.open("Alpha"), self.open("Bravo")
        self.flush()
        self.assertEqual(self.tiler.status(self.desktop.tracked(b.hwnd))[0], "floating")
        self.assertEqual(a.rect, LEFT.work)


if __name__ == "__main__":
    unittest.main()
