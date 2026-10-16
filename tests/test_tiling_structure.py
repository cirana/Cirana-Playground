"""The structure of dynamic tiling after it was split into parts: no method is shadowed or lost, the worker function
of a re-tile runs on its own, and the connection between animation, controller and tiler holds in the corner cases
(a scope change or a cancel in the middle of a glide, quitting)."""
import threading
import unittest

from cirana.core import tiling_interactions, tiling_plan, tiling_reflow, tiling_stacks
from cirana.core.dynamic_tiling import DynamicTiler
from cirana.core.tiling_model import Group, Unit
from cirana.core.tiling_reflow import ReflowJob, run_reflow
from cirana.ui.layout_choices import LAYOUT_CHOICES, TILING_CHOICES, choice_index
from cirana.wm.controller import Placement
from cirana.wm.layout import ADAPTIVE, FREE, GRID, MAIN_SIDE, SIDE_BY_SIDE, LayoutSpec
from tests.test_animation import AnimatedTilerEnv, frame_of
from tests.test_tiling_overlay import OverlayEnv
from tests.test_workspace_controller import LEFT

PARTS = (tiling_plan.PlanMixin, tiling_interactions.InteractionMixin, tiling_stacks.StackMixin,
         tiling_reflow.ReflowMixin)

# what the rest of CIRANA (pages, shortcuts, workspace controller, overlays) calls
PUBLIC_API = """global_on set_global global_layout set_global_layout exclusions set_floating stack_mode set_stack_mode
rules set_rules rule_for ignores rule_windows float_reason manages in_charge paused tile_set tiled_windows shown_groups
tile_of minimum plan managed_windows target_of out_of_sight describe status monocle_on stack_count mode_notes
monitor_count adaptive_layouts schedule reflow display_changed predict_drop focused_window focus focus_direction
swap_direction move_to_monitor toggle_float toggle_monocle resize stack_direction stack_with unstack stack_cycle
show_stacked stop""".split()


class StructureTests(unittest.TestCase):
    def test_every_method_lives_in_exactly_one_part(self):
        seen = {}
        for part in PARTS + (DynamicTiler,):
            for name, value in vars(part).items():
                if callable(value) or isinstance(value, (staticmethod, classmethod)):
                    if name.startswith("__") and name != "__init__":
                        continue
                    self.assertNotIn(name, seen, f"{name} is defined in {seen.get(name)} and in {part.__name__}")
                    seen[name] = part.__name__

    def test_the_public_api_is_still_there(self):
        for name in PUBLIC_API:
            self.assertTrue(hasattr(DynamicTiler, name), name)  # (methods and properties)
        for signal in ("reflowed", "state_changed", "drop_preview", "resize_preview"):
            self.assertTrue(hasattr(DynamicTiler, signal), signal)

    def test_the_tiler_stays_a_qobject_with_the_parts_before_it(self):
        from PySide6.QtCore import QObject

        mro = DynamicTiler.__mro__
        self.assertEqual(mro[-3], QObject)
        for part in PARTS:
            self.assertLess(mro.index(part), mro.index(QObject))

    def test_the_layout_choices_are_shared(self):
        self.assertEqual([c[0] for c in TILING_CHOICES], [c[0] for c in LAYOUT_CHOICES if c[1] != FREE])
        self.assertEqual(LAYOUT_CHOICES[4][1], FREE)  # (saved positions: the place tests and users know)
        self.assertEqual(choice_index(TILING_CHOICES, LayoutSpec(ADAPTIVE), -1), len(TILING_CHOICES) - 1)
        self.assertEqual(choice_index(LAYOUT_CHOICES, LayoutSpec(GRID, columns=2), -1), 2)
        self.assertEqual(choice_index(LAYOUT_CHOICES, LayoutSpec(GRID, columns=0), -1), 3)
        self.assertEqual(choice_index(LAYOUT_CHOICES, LayoutSpec(GRID, columns=3), -1), 3)  # "dynamic" = not 2
        self.assertEqual(choice_index(TILING_CHOICES, LayoutSpec(FREE), 7), 7)


class RunReflowTests(AnimatedTilerEnv):
    """The worker function of a re-tile, run by itself on the simulated desktop."""

    def job_for(self, *windows, glide=0):
        tracked = [self.desktop.tracked(w.hwnd) for w in windows]
        spec = LayoutSpec(SIDE_BY_SIDE, gap=0)
        spec.monitor = LEFT.ref()
        units = [Unit(f"u{i}", [(f"s{i}", w)], f"s{i}") for i, w in enumerate(tracked)]
        group = Group(LEFT.device, spec, units)
        return ReflowJob([group], True, glide, False, None, [], [], [], set()), units

    def execute(self, job, cancel=None):
        return run_reflow(job, self.desktop, self.wm.windows, self.tiler.plan, {}, cancel or threading.Event())

    def test_it_places_and_reports_without_a_tiler_round(self):
        a, b = self.open("A"), self.open("B")
        job, units = self.job_for(a, b)
        count = self.tiler.count
        targets, tiled, floated = self.execute(job)
        self.assertEqual([a.rect, b.rect], self.slots(2))
        self.assertEqual([unit for unit, _ in targets], ["u0", "u1"])
        self.assertTrue(all(result.ok for _slot, result in tiled))
        self.assertEqual(floated, [])
        self.assertEqual(self.tiler.count, count)  # (nothing of the tiler's own state is touched)

    def test_a_cancelled_round_places_nothing(self):
        a, b = self.open("A"), self.open("B")
        before = (a.rect, b.rect)
        job, _units = self.job_for(a, b, glide=150)
        cancel = threading.Event()
        cancel.set()
        self.desktop.calls.clear()
        self.assertEqual(self.execute(job, cancel), ([], [], []))
        self.assertEqual(self.desktop.calls, [])  # not a single request: a stale round cannot undo a newer one
        self.assertEqual((a.rect, b.rect), before)

    def test_a_cancel_during_the_glide_returns_empty_and_leaves_the_registry_for_the_next_round(self):
        a, b = self.open("A"), self.open("B")
        job, _units = self.job_for(a, b, glide=300)
        cancel, registry = threading.Event(), {}
        timer = threading.Timer(0.08, cancel.set)
        timer.start()
        outcome = run_reflow(job, self.desktop, self.wm.windows, self.tiler.plan, registry, cancel)
        timer.join()
        self.assertEqual(outcome, ([], [], []))
        self.assertTrue(registry)  # the windows it moved are remembered: the next round places them exactly
        for window, outer in registry.values():
            self.assertIn(window.hwnd, (a.hwnd, b.hwnd))
        self.assertNotEqual([a.rect, b.rect], self.slots(2))  # they were left on the way, not placed

    def test_the_next_round_places_a_half_way_window_exactly_not_nearly(self):
        a, b = self.open("A"), self.open("B")
        job, _units = self.job_for(a, b)
        a.rect = (self.slots(2)[0][0] + 2, 0, self.slots(2)[0][2] + 2, 960)  # 2 px off: normally "close enough"
        registry = {a.hwnd: (self.desktop.tracked(a.hwnd), (0, 0, 1, 1))}
        run_reflow(job, self.desktop, self.wm.windows, self.tiler.plan, registry, threading.Event())
        self.assertEqual(a.rect, self.slots(2)[0])
        self.assertEqual(registry, {})
        # and without the note, 2 px is left alone
        a.rect = (self.slots(2)[0][0] + 2, 0, self.slots(2)[0][2] + 2, 960)
        self.desktop.calls.clear()
        run_reflow(job, self.desktop, self.wm.windows, self.tiler.plan, {}, threading.Event())
        self.assertEqual([c for c in self.desktop.calls if c[0] == "pos" and c[1] == a.hwnd], [])


class MidGlideTests(AnimatedTilerEnv):
    def test_switching_the_scope_in_the_middle_of_a_glide_leaves_nobody_half_way(self):
        self.turn_on(SIDE_BY_SIDE)
        a = self.open("A")
        self.finish()
        b = self.open("B")
        self.pump(timeout=0.06)  # the glide of a (and b) is under way
        self.tiler.set_global(False)  # all-window tiling is switched off
        self.settle(timeout=2.0)
        self.pump(timeout=0.2)
        self.assertEqual([a.rect, b.rect], self.slots(2))  # exactly their targets, not some frame on the way
        self.assertEqual(self.tiler._animated, {})
        self.desktop.calls.clear()
        self.pump(timeout=0.3)
        self.assertEqual(self.desktop.calls, [])  # and nothing keeps moving afterwards

    def test_a_workspace_change_in_the_middle_of_a_glide(self):
        first = self.tiled_workspace("Code", "Notes", kind=GRID)
        code = self.open("Code")
        self.finish()
        notes = self.open("Notes")
        self.pump(timeout=0.06)
        other = self.manager.create("Other", [])
        self.manager.set_layout(other.id, LayoutSpec(MAIN_SIDE, gap=0, dynamic=True))
        self.manager.set_active(other.id)
        self.finish()
        self.assertEqual(self.tiler._animated, {})
        self.assertEqual([code.rect, notes.rect], self.slots(2, GRID))
        self.manager.set_active(first.id)
        self.finish()
        self.assertEqual([code.rect, notes.rect], self.slots(2, GRID))

    def test_quitting_in_the_middle_of_a_glide_puts_the_windows_in_place_and_leaves_no_job(self):
        self.turn_on(SIDE_BY_SIDE)
        a = self.open("A")
        self.finish()
        b = self.open("B")
        self.pump(timeout=0.06)
        self.tiler.stop()
        self.pump(timeout=0.4)
        self.assertEqual([a.rect, b.rect], self.slots(2))
        self.assertIsNone(self.tiler._job)
        self.assertEqual(self.tiler._animated, {})
        self.assertFalse(self.tiler._timer.isActive())

    def test_the_numbers_on_the_pages_come_from_the_tiler(self):
        self.turn_on(SIDE_BY_SIDE)
        a, b, c = self.open_many(3)
        self.assertEqual(self.tiler.monitor_count(), 1)
        self.assertEqual(self.tiler.mode_notes(), "")
        self.tiler.stack_with(self.desktop.tracked(c.hwnd), self.desktop.tracked(b.hwnd))
        self.finish()
        self.desktop.foreground = a.hwnd
        self.tiler.toggle_monocle(self.desktop.tracked(a.hwnd))
        self.finish()
        self.assertEqual(self.tiler.mode_notes(), " · 1 stack · monocle")


if __name__ == "__main__":
    unittest.main()
