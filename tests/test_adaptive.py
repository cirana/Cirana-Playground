"""Adaptive layout engine and smart BSP: the layout is chosen from real placement quality (minimum sizes, monitor
shape, balance, how much the windows move), stays calm, and never overrides what the user chose or shaped."""
import unittest

from cirana.core.dynamic_tiling import GLOBAL
from cirana.wm import adaptive, bsp
from cirana.wm.layout import ADAPTIVE, AUTO, BSP, GRID, MAIN_SIDE, SIDE_BY_SIDE, LayoutSpec, tile
from cirana.wm.monitors import Monitor
from tests.test_minimum_sizes import inside, overlap
from tests.test_mouse_resize import ResizeEnv
from tests.test_workspace_controller import LEFT, RIGHT

MONITORS = {  # name: (work area, scale)
    "fhd": ((0, 0, 1920, 1040), 1.0), "ultrawide": ((0, 0, 3440, 1400), 1.0), "portrait": ((0, 0, 1080, 1880), 1.0),
    "qhd at 150 %": ((0, 0, 2560, 1400), 1.5), "square": ((0, 0, 1600, 1200), 1.0),
}


def pick(work, scale, count, sizes=None, previous="", last=None, hold=False, gap=8):
    ids = [f"w{i}" for i in range(count)]
    return adaptive.choose(LayoutSpec(ADAPTIVE, gap=gap), work, scale, sizes or [(0, 0)] * count, ids, previous,
                           last, hold), ids


def rects_of(choice, work, scale, count, sizes=None):
    ids = [f"w{i}" for i in range(count)]
    return tile(choice.spec, work, count, scale, sizes or [(0, 0)] * count, ids)


class EngineChoiceTests(unittest.TestCase):
    def test_one_to_eight_windows_on_every_monitor_shape_all_fit_cleanly(self):
        for name, (work, scale) in MONITORS.items():
            for count in range(1, 9):
                with self.subTest(monitor=name, count=count):
                    choice, _ids = pick(work, scale, count)
                    self.assertEqual(choice.floating, [])
                    self.assertNotEqual(choice.spec.kind, ADAPTIVE)  # always a concrete layout
                    rects = rects_of(choice, work, scale, count)
                    self.assertEqual(len(rects), count)
                    for i, a in enumerate(rects):
                        self.assertTrue(inside(a, work), a)
                        for b in rects[i + 1:]:
                            self.assertFalse(overlap(a, b), (a, b))
                    if count <= 4:  # no sliver while there is room
                        width, height = work[2] - work[0], work[3] - work[1]
                        for r in rects:
                            self.assertGreater(min((r[2] - r[0]) / width, (r[3] - r[1]) / height), 0.2, (name, a))

    def test_the_monitor_shape_decides(self):
        wide, _ = pick(*MONITORS["ultrawide"], 3)
        tall, _ = pick(*MONITORS["portrait"], 3)
        self.assertEqual(wide.spec.kind, SIDE_BY_SIDE)  # three landscape tiles on an ultrawide
        self.assertEqual((tall.spec.kind, tall.spec.columns), (GRID, 1))  # a stack of rows on a portrait monitor
        self.assertEqual(pick(*MONITORS["fhd"], 4)[0].spec.kind, GRID)  # four equal tiles beat a spiral of halves

    def test_decisions_are_deterministic_and_cheap(self):
        for _ in range(3):
            a, b = pick(*MONITORS["fhd"], 6)[0], pick(*MONITORS["fhd"], 6)[0]
            self.assertEqual((a.key, a.score, a.spec.to_dict()), (b.key, b.score, b.spec.to_dict()))
        self.assertLessEqual(len(adaptive.CANDIDATES), 5)  # at most five layouts are laid out per decision

    def test_minimum_sizes_decide_before_anything_else(self):
        work, scale = MONITORS["fhd"]
        sizes = [(800, 300)] * 3  # side by side cannot hold three of them (3 x 800 > 1920)
        choice, _ = pick(work, scale, 3, sizes)
        self.assertEqual(choice.floating, [])
        self.assertNotEqual(choice.spec.kind, SIDE_BY_SIDE)
        for rect, (w, h) in zip(rects_of(choice, work, scale, 3, sizes), sizes):
            self.assertGreaterEqual(rect[2] - rect[0], w)
            self.assertGreaterEqual(rect[3] - rect[1], h)
        side = adaptive.evaluate(adaptive.candidate(LayoutSpec(ADAPTIVE, gap=8), SIDE_BY_SIDE, 0), work, scale, sizes,
                                 ["w0", "w1", "w2"], {})
        self.assertGreaterEqual(side.score, adaptive.FLOAT_COST)  # the layout that floats a window is far behind

    def test_a_window_nothing_can_hold_floats_but_the_rest_is_still_tiled(self):
        work, scale = MONITORS["fhd"]
        sizes = [(0, 0), (2500, 0), (0, 0)]
        choice, _ = pick(work, scale, 3, sizes)
        self.assertEqual(choice.floating, [1])

    def test_dpi_scales_the_gap_not_the_choice(self):
        plain, _ = pick((0, 0, 2560, 1400), 1.0, 4)
        scaled, _ = pick((0, 0, 2560, 1400), 1.5, 4)
        self.assertEqual(plain.key, scaled.key)

    def test_the_user_shaped_proportions_count(self):
        work, scale = MONITORS["fhd"]
        shaped = LayoutSpec(ADAPTIVE, gap=8, weights=[0.8, 0.1, 0.1])  # an extreme side-by-side
        ids = ["a", "b", "c"]
        choice = adaptive.choose(shaped, work, scale, [(0, 0)] * 3, ids)
        self.assertNotEqual(choice.key, f"{SIDE_BY_SIDE}:0")  # its slivers are judged as they are


class StabilityTests(unittest.TestCase):
    def test_the_layout_in_use_is_kept_when_another_is_only_slightly_better(self):
        work, scale = MONITORS["fhd"]
        best, _ = pick(work, scale, 3)
        for other in adaptive.CANDIDATES:
            key = f"{other[0]}:{other[1]}"
            kept, _ = pick(work, scale, 3, previous=key)
            alone = adaptive.evaluate(adaptive.candidate(LayoutSpec(ADAPTIVE, gap=8), *other), work, scale,
                                      [(0, 0)] * 3, ["w0", "w1", "w2"], {})
            if alone.score <= best.score + adaptive.HYSTERESIS:
                self.assertEqual(kept.key, key)
            else:
                self.assertEqual(kept.key, best.key)

    def test_a_layout_that_floats_a_window_is_left_at_once(self):
        work, scale = MONITORS["fhd"]
        sizes = [(800, 300)] * 3
        left, _ = pick(work, scale, 3, sizes, previous=f"{SIDE_BY_SIDE}:0")
        self.assertNotEqual(left.key, f"{SIDE_BY_SIDE}:0")
        self.assertEqual(left.floating, [])

    def test_hold_keeps_the_choice_whatever_the_score(self):
        work, scale = MONITORS["fhd"]
        held, _ = pick(work, scale, 5, previous=f"{SIDE_BY_SIDE}:0", hold=True)
        self.assertEqual(held.key, f"{SIDE_BY_SIDE}:0")
        free, _ = pick(work, scale, 5, previous=f"{SIDE_BY_SIDE}:0")
        self.assertNotEqual(free.key, f"{SIDE_BY_SIDE}:0")

    def test_growing_and_shrinking_one_window_at_a_time_changes_the_layout_rarely(self):
        for name, (work, scale) in MONITORS.items():
            with self.subTest(monitor=name):
                previous, changes = "", 0
                for count in list(range(1, 9)) + list(range(7, 0, -1)):
                    choice, _ = pick(work, scale, count, previous=previous)
                    changes += previous not in ("", choice.key)
                    previous = choice.key
                self.assertLessEqual(changes, 6)  # (a flip-flop per window would be 14)

    def test_movement_is_part_of_the_decision(self):
        work, scale = MONITORS["fhd"]
        ids = [f"w{i}" for i in range(4)]
        current = tile(adaptive.candidate(LayoutSpec(ADAPTIVE, gap=8), SIDE_BY_SIDE, 0), work, 4, scale, ids=ids)
        near = adaptive.evaluate(adaptive.candidate(LayoutSpec(ADAPTIVE, gap=8), SIDE_BY_SIDE, 0), work, scale,
                                 [(0, 0)] * 4, ids, dict(zip(ids, current)))
        far = adaptive.evaluate(adaptive.candidate(LayoutSpec(ADAPTIVE, gap=8), SIDE_BY_SIDE, 0), work, scale,
                                [(0, 0)] * 4, ids, {i: (0, 0, 100, 100) for i in ids})
        self.assertLess(near.score, far.score)


class SmartBspTests(unittest.TestCase):
    AREA = (0, 0, 2000, 960)

    def test_the_focused_tile_is_still_split_along_its_longer_side(self):
        tree = bsp.build(["a", "b"], self.AREA)
        after = bsp.rects(bsp.insert(tree, "c", self.AREA, target="a"), self.AREA)
        self.assertEqual((after["a"], after["c"]), ((0, 0, 500, 960), (500, 0, 1000, 960)))
        self.assertEqual(after["b"], (1000, 0, 2000, 960))

    def test_a_minimum_the_focused_tile_cannot_hold_moves_the_split_elsewhere(self):
        tree = bsp.build(["a", "b"], self.AREA)  # a | b, 1000 px each
        needs = {"c": (700, 300)}
        usual = bsp.rects(bsp._split_leaf(tree, "b", "c", False, 0.5), self.AREA, 0, needs)  # b over c: fits
        self.assertGreaterEqual(usual["c"][2] - usual["c"][0], 700)
        crowded = {"a": (900, 0), "b": (900, 0), "c": (700, 300)}  # no room beside a or b: only a row can take it
        tree = bsp.insert(tree, "c", self.AREA, target="a", minimums=crowded)
        boxes = bsp.rects(tree, self.AREA, 0, crowded)
        for leaf, (w, h) in crowded.items():
            self.assertGreaterEqual(boxes[leaf][2] - boxes[leaf][0], w, leaf)
            self.assertGreaterEqual(boxes[leaf][3] - boxes[leaf][1], h, leaf)

    def test_existing_ratios_survive_an_insert_and_a_removal(self):
        tree = bsp.from_dict({"split": "v", "ratio": 0.3, "a": {"leaf": "a"},
                              "b": {"split": "h", "ratio": 0.7, "a": {"leaf": "b"}, "b": {"leaf": "c"}}})
        grown = bsp.insert(tree, "d", self.AREA, target="c")
        self.assertEqual((grown.ratio, grown.b.ratio), (0.3, 0.7))  # nothing else was rebuilt
        shrunk = bsp.remove(grown, "d")
        self.assertEqual(bsp.to_dict(shrunk), bsp.to_dict(tree))

    def test_sync_is_deterministic_and_idempotent(self):
        needs = {"b": (600, 200), "c": (900, 400)}
        one = bsp.sync(None, ["a", "b", "c", "d"], self.AREA, None, 8, needs)
        two = bsp.sync(None, ["a", "b", "c", "d"], self.AREA, None, 8, needs)
        self.assertEqual(bsp.to_dict(one), bsp.to_dict(two))
        self.assertEqual(bsp.to_dict(bsp.sync(one, ["a", "b", "c", "d"], self.AREA, None, 8, needs)), bsp.to_dict(one))

    def test_eight_windows_in_a_bsp_keep_every_minimum(self):
        needs = {f"w{i}": (400, 250) for i in range(8)}
        ids = list(needs)
        tree = bsp.sync(None, ids, self.AREA, None, 8, needs)
        boxes = bsp.rects(tree, self.AREA, 8, needs)
        for leaf in ids:
            self.assertGreaterEqual(boxes[leaf][2] - boxes[leaf][0], 400, leaf)
            self.assertGreaterEqual(boxes[leaf][3] - boxes[leaf][1], 250, leaf)


class AdaptiveTilerTests(ResizeEnv):
    def turn_on_adaptive(self):
        self.tiler.set_global_layout(LayoutSpec(ADAPTIVE, gap=0))
        self.tiler.set_global(True)

    def kinds(self):
        return [g.spec.kind for g in self.tiler.shown_groups()]

    def test_nothing_stored_means_adaptive(self):
        self.assertEqual(self.settings.get("global_layout"), {})
        self.assertEqual(self.tiler.global_layout().kind, ADAPTIVE)

    def test_a_stored_choice_is_never_replaced(self):
        for kind in (SIDE_BY_SIDE, MAIN_SIDE, GRID, BSP, AUTO):
            self.settings.set("global_layout", LayoutSpec(kind, gap=4).to_dict())
            self.assertEqual(self.tiler.global_layout().kind, kind)
            self.assertEqual(self.tiler.global_layout().gap, 4)

    def test_one_to_eight_windows_are_all_tiled_on_a_real_run(self):
        self.turn_on_adaptive()
        windows = []
        for i in range(8):
            self.tiler._history.clear()
            windows.append(self.open(f"W{i}"))
            self.flush()
            self.assert_clean(windows)
            self.assertEqual(len({w.rect for w in windows}), len(windows))
            for w in windows:
                self.assertEqual(self.tiler.status(self.desktop.tracked(w.hwnd))[0], "tiled")
        self.assertTrue(self.tiler.adaptive_layouts())

    def test_minimum_sizes_make_the_engine_choose_a_layout_that_holds_everyone(self):
        self.turn_on_adaptive()
        windows = [self.open(f"M{i}", min_size=(800, 300)) for i in range(3)]
        self.flush()
        self.assert_clean(windows)
        for w in windows:
            self.assertGreaterEqual(w.rect[2] - w.rect[0], 800)
            self.assertEqual(self.tiler.status(self.desktop.tracked(w.hwnd))[0], "tiled")
        self.assertNotEqual(self.kinds(), [SIDE_BY_SIDE])

    def test_adding_and_closing_windows_moves_few_windows_and_is_stable(self):
        self.turn_on_adaptive()
        a, b = self.open_many(2)
        c = self.open("C")
        self.flush()
        before = [w.rect for w in (a, b, c)]
        count = self.tiler.count
        for _ in range(3):
            self.tiler.schedule(force=True)
            self.flush()
        self.assertEqual([w.rect for w in (a, b, c)], before)  # forcing a re-tile changes nothing
        d = self.open("D")
        self.flush()
        self.assertLessEqual(self.tiler.count - count, 5)
        self.desktop.close(d.hwnd)
        self.sync()
        self.flush()
        self.assert_clean([a, b, c])

    def test_a_manual_choice_stops_the_engine_but_keeps_dynamic_tiling(self):
        self.turn_on_adaptive()
        windows = self.open_many(3)
        self.assertTrue(self.tiler.adaptive_layouts())
        self.tiler.set_global_layout(LayoutSpec(SIDE_BY_SIDE, gap=0))
        self.flush()
        self.assertEqual(self.tiler.adaptive_layouts(), [])
        self.assertEqual(self.kinds(), [SIDE_BY_SIDE])
        for _ in range(5):
            windows.append(self.open(f"X{len(windows)}"))
            self.flush()
            self.assertEqual(self.kinds(), [SIDE_BY_SIDE])  # however many windows: the user's layout
            self.assert_clean(windows)
        self.tiler.set_global_layout(LayoutSpec(ADAPTIVE, gap=0))  # "Automatic" again
        self.flush()
        self.assertTrue(self.tiler.adaptive_layouts())
        self.assertNotEqual(self.kinds(), [SIDE_BY_SIDE])  # eight windows: the engine does better than a row
        self.assert_clean(windows)

    def test_a_resize_swap_and_stack_are_not_undone_by_the_engine(self):
        self.turn_on_adaptive()
        a, b, c, d = self.open_many(4)
        kind = self.kinds()
        self.resize(a, right=-60)
        shaped = [w.rect for w in (a, b, c, d)]
        self.assertEqual(a.rect[2], shaped[0][2])
        self.assertEqual(self.kinds(), kind)
        for _ in range(2):  # a forced re-tile, a dragged-and-dropped swap: the shape stays
            self.tiler.schedule(force=True)
            self.flush()
        self.assertEqual([w.rect for w in (a, b, c, d)], shaped)
        self.desktop.foreground = a.hwnd
        self.assertTrue(self.tiler.swap_direction("right"))  # a keyboard swap holds the choice too
        self.flush()
        self.assertEqual(self.kinds(), kind)
        self.assertTrue(self.tiler.stack_with(self.desktop.tracked(d.hwnd), self.desktop.tracked(c.hwnd)))
        self.flush()
        self.assertEqual(self.tiler.stack_count(), 1)
        self.assertEqual(self.kinds(), kind)  # stacking did not change the layout the engine uses
        self.assert_clean([a, b, c])

    def test_monocle_survives_the_engine(self):
        self.turn_on_adaptive()
        a, b, c = self.open_many(3)
        self.desktop.foreground = a.hwnd
        self.assertTrue(self.tiler.toggle_monocle(self.desktop.tracked(a.hwnd)))
        self.flush()
        self.assertEqual(a.rect, LEFT.work)
        self.tiler.schedule(force=True)
        self.flush()
        self.assertEqual(a.rect, LEFT.work)

    def test_each_monitor_is_decided_on_its_own(self):
        portrait = Monitor(RIGHT.device, (2000, 0, 3080, 1920), (2000, 0, 3080, 1880), False, 96)
        self.desktop.monitors_ = [LEFT, portrait]
        self.turn_on_adaptive()
        left = [self.open(f"L{i}", rect=(100 + i, 100, 700, 600)) for i in range(3)]
        right = [self.open(f"R{i}", rect=(2100 + i, 100, 2600, 600)) for i in range(3)]
        self.flush()
        self.assert_clean(left)
        self.assert_clean(right, work=portrait.work)
        names = self.tiler.adaptive_layouts()
        self.assertEqual(len(names), 2)
        self.assertIn("Column", names)  # the portrait monitor stacks its three windows

    def test_float_and_ignore_rules_are_respected(self):
        from cirana.wm.rules import FLOAT, IGNORE, WindowRule, new_rule_id

        self.turn_on_adaptive()
        self.tiler.set_rules([WindowRule(new_rule_id(), name="f", action=FLOAT, exe="f.exe"),
                              WindowRule(new_rule_id(), name="i", action=IGNORE, exe="i.exe")])
        a = self.open("A")
        floaty, ignored = self.open("F"), self.open("I")
        floaty_rect, ignored_rect = floaty.rect, ignored.rect
        self.flush()
        self.assertEqual(a.rect, LEFT.work)
        self.assertEqual((floaty.rect, ignored.rect), (floaty_rect, ignored_rect))

    def test_workspace_scope_uses_the_engine_and_a_manual_kind_does_not(self):
        ws = self.tiled_workspace("Code", "Notes", "Brave", kind=ADAPTIVE)
        windows = [self.open(n) for n in ("Code", "Notes", "Brave")]
        self.flush()
        self.assert_clean(windows)
        self.assertTrue(self.tiler.adaptive_layouts())
        self.assertEqual(self.manager.get(ws.id).layout.kind, ADAPTIVE)  # what is stored is the user's "Automatic"
        self.manager.set_layout(ws.id, LayoutSpec(GRID, gap=0, dynamic=True))
        self.flush()
        self.assertEqual(self.tiler.adaptive_layouts(), [])
        self.assertEqual(self.kinds(), [GRID])
        self.assert_clean(windows)

    def test_workspace_switch_keeps_the_engines_choice_and_shape(self):
        first = self.tiled_workspace("Code", "Notes", "Brave", kind=ADAPTIVE)
        windows = [self.open(n) for n in ("Code", "Notes", "Brave")]
        self.flush()
        self.resize(windows[0], right=-100)
        shaped = [w.rect for w in windows]
        other = self.manager.create("Other", [])
        self.manager.set_layout(other.id, LayoutSpec(SIDE_BY_SIDE, gap=0, dynamic=True))
        self.manager.set_active(other.id)
        self.settle()
        self.manager.set_active(first.id)
        self.flush()
        self.assertEqual([w.rect for w in windows], shaped)

    def test_the_gap_and_ratio_of_an_adaptive_layout_follow_the_settings(self):
        self.turn_on_adaptive()
        a, b = self.open_many(2)
        self.tiler.set_global_layout(LayoutSpec(ADAPTIVE, gap=10))
        self.flush()
        self.assertEqual(a.rect[0], 10)


class MigrationTests(unittest.TestCase):
    def test_old_records_keep_their_kind(self):
        old = {"kind": "grid", "gap": 8, "ratio": 0.6, "columns": 0, "weights": [], "monitor": None, "free": {},
               "dynamic": True}  # a record from before bsp, auto, stacks, grid proportions and the engine
        spec = LayoutSpec.from_dict(old)
        self.assertEqual((spec.kind, spec.dynamic, spec.gap), (GRID, True, 8))
        self.assertEqual((spec.grid_cols, spec.grid_rows, spec.bsp, spec.stacks), ([], [], {}, []))
        legacy = LayoutSpec.from_dict({"kind": "auto", "auto": {"2": "grid", "3": "bsp", "4": "grid"}})
        self.assertEqual(legacy.kind, AUTO)  # "by window count" stays what the user chose
        self.assertEqual(legacy.kind_for(3), BSP)

    def test_adaptive_round_trips_and_unknown_kinds_are_safe(self):
        spec = LayoutSpec(ADAPTIVE, gap=6, ratio=0.55)
        again = LayoutSpec.from_dict(spec.to_dict())
        self.assertEqual((again.kind, again.gap, again.ratio), (ADAPTIVE, 6, 0.55))
        self.assertEqual(LayoutSpec.from_dict({"kind": "from-the-future"}).kind, GRID)
        self.assertEqual(LayoutSpec(ADAPTIVE).kind_for(1), SIDE_BY_SIDE)  # static answers for code without the engine
        self.assertEqual(LayoutSpec(ADAPTIVE).kind_for(3), MAIN_SIDE)


if __name__ == "__main__":
    unittest.main()
