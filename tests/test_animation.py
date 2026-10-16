"""Animations: a visual layer only. Windows glide to the places the layout chose, borders fade, the drop preview
glides - and the final coordinates are exactly those of the immediate placement."""
import os
import shutil
import tempfile
import threading
import time
import unittest

from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QApplication

from cirana.native import win32_api as w
from cirana.wm import animation
from cirana.wm.animation import Move, ease_out_cubic, lerp_rect, run_animation
from cirana.wm.identity import TrackedWindow
from cirana.wm.layout import ADAPTIVE, BSP, GRID, MAIN_SIDE, SIDE_BY_SIDE, LayoutSpec
from cirana.wm.monitors import Monitor
from tests.test_minimum_sizes import inside, overlap
from tests.test_mouse_resize import ResizeEnv
from tests.test_workspace_controller import LEFT, RIGHT, app

MARGINS = (7, 0, 7, 7)  # the fake desktop's invisible borders


def frame_of(outer):
    return outer[0] + MARGINS[0], outer[1] + MARGINS[1], outer[2] - MARGINS[2], outer[3] - MARGINS[3]


def tracked(hwnd):
    return TrackedWindow(hwnd, hwnd, 1.0)


class SettingsStub:
    def __init__(self, **values):
        self.values = values

    def get(self, key):
        return self.values.get(key, {"animations": True, "animation_ms": 180, "animation_border": True,
                                     "animation_preview": True}.get(key))


class EasingAndSettingsTests(unittest.TestCase):
    def test_ease_out_cubic(self):
        self.assertEqual((ease_out_cubic(0), ease_out_cubic(1)), (0.0, 1.0))
        self.assertEqual((ease_out_cubic(-5), ease_out_cubic(5)), (0.0, 1.0))
        values = [ease_out_cubic(i / 20) for i in range(21)]
        self.assertEqual(values, sorted(values))
        self.assertGreater(ease_out_cubic(0.5), 0.8)  # fast start, slow end

    def test_lerp_rect(self):
        self.assertEqual(lerp_rect((0, 0, 100, 100), (100, 50, 300, 90), 0.0), (0, 0, 100, 100))
        self.assertEqual(lerp_rect((0, 0, 100, 100), (100, 50, 300, 90), 1.0), (100, 50, 300, 90))
        self.assertEqual(lerp_rect((0, 0, 100, 100), (100, 50, 300, 90), 0.5), (50, 25, 200, 95))

    def test_settings(self):
        self.assertEqual(animation.duration_ms(SettingsStub()), 180)
        self.assertEqual(animation.duration_ms(SettingsStub(animation_ms=0)), 0)
        self.assertEqual(animation.duration_ms(SettingsStub(animation_ms=9999)), 350)
        self.assertEqual(animation.duration_ms(SettingsStub(animation_ms=-4)), 0)
        self.assertEqual(animation.duration_ms(SettingsStub(animation_ms="fast")), 180)
        self.assertEqual(animation.duration_ms(SettingsStub(animations=False)), 0)
        self.assertEqual(animation.duration_ms(None), 0)
        self.assertEqual(animation.flag(SettingsStub(), "animation_border", 120), 120)
        self.assertEqual(animation.flag(SettingsStub(animation_border=False), "animation_border", 120), 0)
        self.assertEqual(animation.flag(SettingsStub(animations=False), "animation_preview", 100), 0)
        self.assertEqual(animation.flag(SettingsStub(animation_ms=0), "animation_preview", 100), 0)  # 0 ms: all off


class RunnerTests(unittest.TestCase):
    def run_it(self, moves, post, duration=0.15, cancel=None, check=None):
        started = time.monotonic()
        stats = run_animation(moves, post, cancel or threading.Event(), duration, check)
        return stats, time.monotonic() - started

    def test_frames_approach_the_target_without_reaching_or_passing_it(self):
        seen = []
        move = Move(tracked(1), (0, 0, 100, 100), (300, 0, 500, 100))
        stats, took = self.run_it([move], lambda m, r: seen.append((time.monotonic(), r)) or True)
        rects = [r for _t, r in seen]
        self.assertGreaterEqual(len(rects), 3)
        self.assertEqual([r[0] for r in rects], sorted(r[0] for r in rects))  # steadily towards it
        self.assertTrue(all(0 <= r[0] <= 300 and r[2] <= 500 for r in rects))
        self.assertLess(rects[-1][0], 300 + 1)
        self.assertEqual(len(rects), len(set(rects)))  # a repeated frame is never posted again
        self.assertGreaterEqual(took, 0.14)  # the whole duration
        self.assertLessEqual(took, 0.4)
        self.assertEqual(stats["posts"], len(rects))

    def test_about_thirty_frames_a_second_not_more(self):
        count = []
        move = Move(tracked(1), (0, 0, 100, 100), (3000, 0, 3100, 100))
        stats, took = self.run_it([move], lambda m, r: count.append(r) or True, duration=0.3)
        self.assertLessEqual(stats["frames"], round(0.3 * animation.FPS) + 3)
        self.assertGreaterEqual(stats["frames"], 4)

    def test_late_frames_are_skipped_not_queued(self):
        def slow(_move, _rect):
            time.sleep(0.08)  # every frame takes much longer than the frame interval
            return True

        stats, took = self.run_it([Move(tracked(1), (0, 0, 100, 100), (900, 0, 1000, 100))], slow, duration=0.2)
        self.assertLessEqual(stats["frames"], 4)  # time based: it does not play the missed frames
        self.assertLessEqual(took, 0.2 + 0.08 * 2)

    def test_cancel_stops_at_once_and_nothing_is_posted_afterwards(self):
        cancel = threading.Event()
        posts = []

        def post(move, rect):
            posts.append(time.monotonic())
            return True

        thread = threading.Thread(target=lambda: self.run_it([Move(tracked(1), (0, 0, 100, 100),
                                                                   (900, 0, 1000, 100))], post, 1.0, cancel))
        thread.start()
        time.sleep(0.1)
        cancel.set()
        stopped = time.monotonic()
        thread.join(1.0)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - stopped, 0.15)
        self.assertTrue(all(t <= stopped + 0.01 for t in posts))

    def test_a_window_that_refuses_or_fails_the_check_leaves_the_others_going(self):
        good, refusing, vanishing = (Move(tracked(i), (0, 0, 100, 100), (500, 0, 600, 100)) for i in (1, 2, 3))
        posted = {1: 0, 2: 0, 3: 0}

        def post(move, rect):
            posted[move.window.hwnd] += 1
            return move.window.hwnd != 2

        stats, _ = self.run_it([good, refusing, vanishing], post, 0.3, check=lambda m: m.window.hwnd != 3)
        self.assertEqual(posted[2], 1)  # refused once, never asked again
        self.assertGreater(posted[1], 3)
        self.assertGreaterEqual(stats["dropped"], 2)

    def test_nothing_to_do_returns_at_once(self):
        stats, took = self.run_it([], lambda m, r: True)
        self.assertLess(took, 0.05)
        self.assertEqual(stats["posts"], 0)
        stats, took = self.run_it([Move(tracked(1), (0, 0, 1, 1), (9, 9, 10, 10))], lambda m, r: True, duration=0)
        self.assertEqual(stats["posts"], 0)


class AnimatedTilerEnv(ResizeEnv):
    MS = 160

    def build(self):
        super().build()
        self.settings.set("animation_ms", self.MS)

    def finish(self):
        self.settle(timeout=4.0)
        self.sync()
        self.pump(timeout=0.1)

    def history(self, window):
        return [frame_of(call[2]) for call in self.desktop.calls if call[0] == "pos" and call[1] == window.hwnd]


class WindowGlideTests(AnimatedTilerEnv):
    def test_a_window_glides_to_its_tile_in_steps_and_ends_exactly_there(self):
        self.turn_on(SIDE_BY_SIDE)
        a = self.open("A")
        self.finish()
        self.desktop.calls.clear()
        b = self.open("B")
        self.finish()
        self.assertEqual((a.rect, b.rect), ((0, 0, 1000, 960), (1000, 0, 2000, 960)))
        steps = self.history(a)
        self.assertGreaterEqual(len(steps), 3, steps)  # glided: several frames, then the exact placement
        self.assertEqual(steps[-1], a.rect)
        widths = [s[2] - s[0] for s in steps]
        self.assertEqual(widths, sorted(widths, reverse=True))  # A shrinks from the whole screen to half of it
        self.assertEqual(len(steps), len(set(steps)) + steps.count(a.rect) - 1)  # no frame is repeated

    def test_final_coordinates_are_the_same_with_and_without_animation(self):
        for kind in (SIDE_BY_SIDE, MAIN_SIDE, GRID, BSP, ADAPTIVE):
            results = []
            for ms in (0, self.MS):
                with self.subTest(kind=kind, ms=ms):
                    self.settings.set("animation_ms", ms)
                    self.fresh()
                    self.turn_on(kind)
                    windows = []
                    for i in range(4):
                        self.tiler._history.clear()
                        windows.append(self.open(f"W{i}", min_size=(300, 200)))
                        self.finish()
                    results.append([w.rect for w in windows])
                    self.assert_clean(windows)
            self.assertEqual(results[0], results[1], kind)

    def test_a_new_target_during_a_glide_starts_from_where_the_windows_are(self):
        self.turn_on(SIDE_BY_SIDE)
        windows = [self.open("A")]
        self.finish()
        self.desktop.calls.clear()
        windows.append(self.open("B"))
        self.pump(timeout=0.06)  # the first glide is under way ...
        windows.append(self.open("C"))  # ... and the layout changes again
        self.finish()
        self.assertEqual([w.rect for w in windows], self.slots(3))
        steps = self.history(windows[0])
        self.assertEqual(steps[-1], windows[0].rect)
        self.assertTrue(all(s[2] - s[0] >= 600 for s in steps))  # never beyond the old or the new tile
        self.assertEqual(self.tiler._job, None)

    def test_rapid_open_and_close_ends_in_the_right_layout(self):
        self.turn_on(SIDE_BY_SIDE)
        keep = [self.open("A")]
        for i in range(5):
            self.tiler._history.clear()  # (the rate limit of runaway applications is not what is tested here)
            extra = self.open(f"X{i}")
            self.pump(timeout=0.03)
            self.desktop.close(extra.hwnd)
            self.sync()
            self.pump(timeout=0.03)
        keep.append(self.open("B"))
        self.finish()
        self.assertEqual([w.rect for w in keep], self.slots(2))

    def test_consecutive_swaps(self):
        self.turn_on(SIDE_BY_SIDE)
        a, b, c = self.open_many(3)
        self.desktop.foreground = a.hwnd
        for _ in range(2):
            self.assertTrue(self.tiler.swap_direction("right"))
            self.pump(timeout=0.05)
        self.finish()
        self.assertEqual({a.rect, b.rect, c.rect}, set(self.slots(3)))
        self.assertEqual(a.rect, self.slots(3)[2])  # a travelled two tiles to the right

    def test_a_window_that_closes_mid_glide_costs_nobody_else(self):
        self.turn_on(SIDE_BY_SIDE)
        a, b = self.open_many(2)
        c = self.open("C")
        self.pump(timeout=0.05)
        self.desktop.close(b.hwnd)  # gone while the others are moving
        self.sync()
        self.finish()
        self.assertEqual([a.rect, c.rect], self.slots(2))

    def test_a_reused_handle_is_never_moved(self):
        self.tiled_workspace("Code", "Notes", "Brave")
        code, notes = self.open("Code"), self.open("Notes")
        self.finish()
        brave = self.open("Brave")
        self.pump(timeout=0.05)  # the others are gliding
        self.desktop.close(notes.hwnd)
        stranger = self.desktop.add("c:\\apps\\other.exe", "Other", hwnd=notes.hwnd, pid=4444, rect=(1, 1, 99, 99))
        self.desktop.calls.clear()
        self.sync()
        self.finish()
        self.assertEqual(stranger.rect, (1, 1, 99, 99))
        self.assertEqual(self.history(stranger), [])
        self.assertEqual([code.rect, brave.rect], self.slots(2))

    def test_nothing_animates_that_should_not(self):
        self.turn_on(SIDE_BY_SIDE)
        a = self.open("A")
        mini = self.open("Mini")
        self.finish()
        self.desktop.calls.clear()
        mini.minimized = True
        self.sync()
        self.finish()
        self.assertEqual(self.history(mini), [])  # a minimized window is not touched
        self.desktop.calls.clear()
        mini.minimized = False  # restored: it just appears in its tile
        self.sync()
        self.finish()
        self.assertEqual([a.rect, mini.rect], self.slots(2))
        fixed = self.open("Fixed", fixed_size=True, rect=(20, 20, 220, 220))
        self.finish()
        self.assertEqual(fixed.rect, (20, 20, 220, 220))  # not tiled: not touched at all
        self.assertEqual(self.history(fixed), [])

    def test_a_scaled_monitor_is_placed_not_glided(self):
        right = Monitor(RIGHT.device, RIGHT.rect, RIGHT.work, False, 144)
        self.desktop.monitors_ = [LEFT, right]
        self.turn_on(SIDE_BY_SIDE)
        a = self.open("A")
        self.finish()
        self.desktop.calls.clear()
        far = self.open("Far", rect=(2100, 100, 2600, 600))  # lives on the scaled monitor
        self.finish()
        self.assertEqual(far.rect, right.work)
        mover = self.open("Mover", rect=(2200, 200, 2700, 700))
        self.finish()
        self.assert_clean([far, mover], work=right.work)

    def test_minimum_sizes_and_floating_are_unchanged(self):
        self.turn_on(SIDE_BY_SIDE)
        windows = [self.open(f"M{i}", min_size=(800, 300)) for i in range(3)]
        self.finish()
        self.assert_clean(windows)
        for window in windows:
            self.assertGreaterEqual(window.rect[2] - window.rect[0], 800)
        huge = self.open("Huge", min_size=(2600, 100))
        self.finish()
        self.assertIn("cannot be shown completely", self.tiler.float_reason(self.desktop.tracked(huge.hwnd)))

    def test_monocle_stacks_and_workspace_switches(self):
        self.turn_on(SIDE_BY_SIDE)
        a, b, c = self.open_many(3)
        self.desktop.foreground = a.hwnd
        self.assertTrue(self.tiler.toggle_monocle(self.desktop.tracked(a.hwnd)))
        self.finish()
        self.assertEqual(a.rect, LEFT.work)
        self.assertTrue(self.tiler.toggle_monocle(self.desktop.tracked(a.hwnd)))
        self.finish()
        self.assertEqual([a.rect, b.rect, c.rect], self.slots(3))
        self.assertTrue(self.tiler.stack_with(self.desktop.tracked(c.hwnd), self.desktop.tracked(b.hwnd)))
        self.finish()
        self.assertEqual(b.rect, c.rect)
        self.tiler.set_global(False)
        ws = self.tiled_workspace("Code", "Notes", kind=GRID)
        code, notes = self.open("Code"), self.open("Notes")
        self.finish()
        self.assertEqual(len({code.rect, notes.rect}), 2)
        self.assert_clean([code, notes])

    def test_zero_milliseconds_is_the_old_immediate_placement(self):
        self.settings.set("animation_ms", 0)
        self.turn_on(SIDE_BY_SIDE)
        a = self.open("A")
        self.finish()
        self.desktop.calls.clear()
        self.open("B")
        self.finish()
        self.assertEqual(len(self.history(a)), 1)  # exactly one placement (the verification may post it once more)

    def test_the_switch_turns_everything_off(self):
        self.settings.set("animations", False)
        self.turn_on(SIDE_BY_SIDE)
        a = self.open("A")
        self.finish()
        self.desktop.calls.clear()
        self.open("B")
        self.finish()
        self.assertLessEqual(len(self.history(a)), 2)

    def test_no_animation_while_the_user_drags_and_the_release_still_lays_out(self):
        self.turn_on(SIDE_BY_SIDE)
        a, b = self.open_many(2)
        self.drag(a, True)
        self.assertTrue(self.tiler.paused())
        self.desktop.calls.clear()
        self.pump(timeout=0.3)
        self.assertEqual(self.desktop.calls, [])  # nothing is moved, glided or not, while the mouse is held
        self.move(a, (50, 50, 450, 450))
        self.drag(a, False)
        self.finish()
        self.assertEqual([a.rect, b.rect], self.slots(2))

    def test_a_mouse_resize_keeps_its_size_and_the_neighbour_glides_after_the_release(self):
        self.turn_on(SIDE_BY_SIDE)
        a, b = self.open_many(2)
        self.desktop.calls.clear()
        self.resize(a, right=-200)
        self.finish()
        self.assertEqual(a.rect[2], 800)
        self.assertEqual(b.rect, (800, 0, 2000, 960))
        self.assertGreaterEqual(len(self.history(b)), 3)
        self.assertLessEqual(len(set(self.history(a))), 1)  # the dragged window is already there: no glide

    def test_no_threads_or_jobs_are_left_behind(self):
        self.turn_on(SIDE_BY_SIDE)
        self.open_many(3)
        self.finish()
        self.assertIsNone(self.tiler._job)
        self.assertEqual(QThreadPool.globalInstance().activeThreadCount(), 0)
        self.tiler.stop()
        self.assertIsNone(self.tiler._job)

    def test_stopping_during_a_glide_cancels_it(self):
        self.turn_on(SIDE_BY_SIDE)
        self.open_many(1)
        self.open("B")
        self.pump(timeout=0.04)
        self.tiler.stop()
        self.desktop.calls.clear()
        self.pump(timeout=0.4)
        self.assertEqual(self.desktop.calls, [])  # no frame after stop()
        self.assertIsNone(self.tiler._job)


class OverlayAnimationTests(AnimatedTilerEnv):
    def test_the_focus_border_fades_and_ends_in_the_right_colour(self):
        self.turn_on(SIDE_BY_SIDE)
        a, b = self.open_many(2)
        self.focus(a)
        self.pump(timeout=0.2)
        border_a, border_b = self.border(a), self.border(b)
        self.assertEqual((border_a.mix, border_b.mix), (1.0, 0.0))
        self.tracker._on_batch(("partial", None, {}, b.hwnd))  # focus moves to b
        seen = []
        end = time.monotonic() + 0.3
        while time.monotonic() < end:
            app.processEvents()
            seen.append(border_a.mix)
            time.sleep(0.005)
        self.assertTrue(any(0.0 < m < 1.0 for m in seen), "the colour changed in one step")
        self.assertEqual(seen, sorted(seen, reverse=True))  # a fades out steadily, never flickers back
        self.assertEqual((border_a.mix, border_b.mix), (0.0, 1.0))
        self.assertEqual((border_a.active, border_b.active), (False, True))
        self.assertIsNone(border_a._fade)

    def test_the_fade_can_be_turned_off(self):
        self.settings.set("animation_border", False)
        self.turn_on(SIDE_BY_SIDE)
        a, b = self.open_many(2)
        self.focus(a)
        self.pump(timeout=0.1)
        self.tracker._on_batch(("partial", None, {}, b.hwnd))
        self.pump(timeout=0.02)
        self.assertEqual((self.border(a).mix, self.border(b).mix), (0.0, 1.0))  # at once
        self.assertIsNone(self.border(a)._fade)

    def test_borders_follow_windows_that_are_gliding(self):
        self.turn_on(SIDE_BY_SIDE)
        a = self.open("A")
        self.finish()
        self.open("B")
        self.finish()
        t = self.border(a).thickness
        self.assertEqual(self.border(a).rect_px, (a.rect[0] - t, a.rect[1] - t, a.rect[2] + t, a.rect[3] + t))

    def test_the_drop_preview_glides_between_targets_and_is_gone_when_the_drag_ends(self):
        self.turn_on(SIDE_BY_SIDE)
        a, b = self.open_many(2)
        left, right = self.slots(2)
        self.drag(a, True)
        self.assertEqual(self.overlays.preview.rect_px, left)
        self.move(a, (1100, 100, 1500, 500))  # over b: the preview moves to b's tile
        self.assertEqual(self.overlays.preview.target_px, right)
        mid = []
        end = time.monotonic() + 0.25
        while time.monotonic() < end:
            app.processEvents()
            mid.append(self.overlays.preview.rect_px)
            time.sleep(0.004)
        self.assertTrue(any(left[0] < r[0] < right[0] for r in mid), "the preview jumped")
        self.assertEqual(self.overlays.preview.rect_px, right)
        self.move(a, (-9000, -9000, -8500, -8500))  # no valid target: gone at once
        self.assertIsNone(self.preview())
        self.move(a, (1100, 100, 1500, 500))
        self.drag(a, False)  # released: gone at once, nothing keeps gliding
        self.assertIsNone(self.preview())
        self.assertIsNone(self.overlays.preview._glide)
        self.assertIsNone(self.overlays.preview.target_px)
        self.flush()
        self.assertEqual([a.rect, b.rect], [right, left])

    def test_the_preview_glide_can_be_turned_off(self):
        self.settings.set("animation_preview", False)
        self.turn_on(SIDE_BY_SIDE)
        a, b = self.open_many(2)
        left, right = self.slots(2)
        self.drag(a, True)
        self.move(a, (1100, 100, 1500, 500))
        self.assertEqual(self.overlays.preview.rect_px, right)  # at once
        self.drag(a, False)

    def test_overlays_do_not_take_the_focus(self):
        self.turn_on(SIDE_BY_SIDE)
        a, b = self.open_many(2)
        self.desktop.foreground = a.hwnd
        self.desktop.calls.clear()
        self.tracker._on_batch(("partial", None, {}, b.hwnd))
        self.pump(timeout=0.3)
        self.assertEqual([c for c in self.desktop.calls if c[0] == "foreground"], [])  # nothing asked for the focus

    def test_stopping_the_overlays_ends_the_animations(self):
        self.turn_on(SIDE_BY_SIDE)
        a, b = self.open_many(2)
        self.focus(a)
        self.pump(timeout=0.1)
        self.tracker._on_batch(("partial", None, {}, b.hwnd))
        self.overlays.stop()
        self.assertEqual(self.overlays.borders, {})
        self.assertIsNone(self.overlays.preview._glide)


class SettingsPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.mkdtemp(prefix="cirana-ui-anim-")
        os.environ["CIRANA_HOME"] = cls.home
        from cirana.main import build_app
        from tests.fake_desktop import FakeDesktop

        cls.desktop = FakeDesktop()
        cls.app, cls.window, cls.ctx = build_app([], window_hooks=False, window_backend=cls.desktop)

    @classmethod
    def tearDownClass(cls):
        cls.ctx.windows.stop()
        cls.ctx.tracker.shutdown()
        cls.window.close()
        shutil.rmtree(cls.home, ignore_errors=True)

    def test_defaults_and_persistence(self):
        settings = self.ctx.settings
        page = self.window.pages["settings"]
        self.assertEqual((settings.get("animations"), settings.get("animation_ms"), settings.get("animation_border"),
                          settings.get("animation_preview")), (True, 180, True, True))
        self.assertEqual((page.anim_on.isChecked(), page.anim_ms.value()), (True, 180))
        self.assertEqual((page.anim_ms.minimum(), page.anim_ms.maximum()), (0, 350))
        page.anim_ms.setValue(90)
        page.anim_border.setChecked(False)
        page.anim_preview.setChecked(False)
        self.assertEqual((settings.get("animation_ms"), settings.get("animation_border"),
                          settings.get("animation_preview")), (90, False, False))
        page.anim_on.setChecked(False)
        self.assertFalse(settings.get("animations"))
        self.assertFalse(page.anim_ms.isEnabled())
        self.assertEqual(animation.duration_ms(settings), 0)
        from cirana.storage.settings_manager import SettingsManager

        again = SettingsManager(settings.root)  # persisted
        self.assertEqual((again.get("animations"), again.get("animation_ms"), again.get("animation_border")),
                         (False, 90, False))
        page.anim_on.setChecked(True)
        page.anim_ms.setValue(0)
        self.assertEqual(animation.duration_ms(settings), 0)  # 0 ms = the old immediate placement
        page.anim_ms.setValue(180)
        page.anim_border.setChecked(True)
        page.anim_preview.setChecked(True)


if __name__ == "__main__":
    unittest.main()
