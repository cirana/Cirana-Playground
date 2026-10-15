"""Drop preview and focus borders of dynamic tiling, on the simulated desktop (offscreen overlays).

The preview must show exactly where a dragged window lands; borders must follow focus, moves and the tile set, and
vanish for minimized, maximized, full-screen, closed or no longer managed windows.
"""
import unittest

from PySide6.QtCore import Qt

from cirana.native import win32_api as w
from cirana.ui.tiling_overlay import ACTIVE_COLOR, INACTIVE_COLOR, TilingOverlays
from cirana.wm.layout import SIDE_BY_SIDE, LayoutSpec
from cirana.wm.monitors import Monitor
from tests.test_dynamic_tiling import TilingEnv
from tests.test_workspace_controller import LEFT, RIGHT


class OverlayEnv(TilingEnv):
    def build(self):
        super().build()
        self.overlays = TilingOverlays(self.tiler, self.settings)
        self.addCleanup(self.overlays.stop)

    def flush(self):
        self.settle()
        self.sync()  # the tracker reports the positions the re-tile set (location events on a real desktop)
        self.pump(timeout=0.08)  # the border sync is coalesced too

    def drag(self, window, started):
        self.tracker._on_event(w.EVENT_SYSTEM_MOVESIZESTART if started else w.EVENT_SYSTEM_MOVESIZEEND, window.hwnd)
        self.pump(timeout=0.05)

    def move(self, window, rect):
        """The user moves the window during the drag: the tracker reports its new position."""
        window.rect = rect
        self.sync()
        self.pump(timeout=0.05)

    def focus(self, window):
        self.tracker._on_batch(("partial", None, {}, window.hwnd))  # what a foreground event produces
        self.pump(timeout=0.05)

    def preview(self):
        return self.overlays.preview.rect_px if self.overlays.preview.isVisible() else None

    def border(self, window):
        return self.overlays.borders.get(self.desktop.tracked(window.hwnd).key)

    def global_on(self, **spec):
        self.tiler.set_global_layout(LayoutSpec(SIDE_BY_SIDE, gap=0, **spec))
        self.tiler.set_global(True)


class DropPreviewTests(OverlayEnv):
    def test_preview_follows_the_target_and_matches_where_the_window_lands(self):
        self.tiled_workspace("Code", "Notes")
        code, notes = self.open("Code"), self.open("Notes")
        self.flush()
        left, right = self.slots(2)
        self.drag(code, True)
        self.assertEqual(self.preview(), left)  # not moved yet: back to its own tile
        self.move(code, (right[0] + 50, 100, right[0] + 450, 500))  # over Notes' tile
        self.assertEqual(self.preview(), right)
        self.move(code, (50, 100, 450, 500))  # back over its own tile
        self.assertEqual(self.preview(), left)
        self.move(code, (right[0] + 80, 200, right[0] + 480, 600))
        predicted = self.preview()
        self.drag(code, False)
        self.assertIsNone(self.preview())  # removed when the drag ends
        self.flush()
        self.assertEqual(code.rect, predicted)  # exactly where the preview said
        self.assertEqual(notes.rect, left)

    def test_moving_to_another_monitor_with_another_dpi(self):
        right = Monitor(RIGHT.device, RIGHT.rect, RIGHT.work, False, 144)
        self.desktop.monitors_ = [LEFT, right]
        self.global_on()
        code, mail = self.open("Code"), self.open("Mail")
        self.flush()
        self.drag(mail, True)
        self.move(mail, (3200, 300, 3700, 800))  # onto the empty right monitor: it joins that monitor's layout
        predicted = self.preview()
        self.assertEqual(predicted, right.work)
        self.assertEqual(self.overlays.preview._scale, 1.5)
        self.drag(mail, False)
        self.flush()
        self.assertEqual((mail.rect, code.rect), (predicted, LEFT.work))
        # dropped over the tile of a window on the other monitor: the two swap places (and monitors)
        self.drag(code, True)
        self.move(code, (2500, 300, 3000, 800))
        predicted = self.preview()
        self.assertEqual(predicted, right.work)
        self.drag(code, False)
        self.flush()
        self.assertEqual((code.rect, mail.rect), (right.work, LEFT.work))

    def test_no_target_hides_the_preview(self):
        self.global_on()
        code = self.open("Code")
        self.flush()
        self.drag(code, True)
        self.assertEqual(self.preview(), LEFT.work)
        self.move(code, (-9000, -9000, -8500, -8500))  # off every monitor
        self.assertIsNone(self.preview())
        self.move(code, (100, 100, 500, 500))
        self.assertEqual(self.preview(), LEFT.work)

    def test_cancelled_drag_and_closed_window_remove_the_preview(self):
        self.tiled_workspace("Code", "Notes")
        code, notes = self.open("Code"), self.open("Notes")
        self.flush()
        self.drag(code, True)
        self.assertIsNotNone(self.preview())
        self.drag(code, False)  # Esc: the move loop ends without a move
        self.assertIsNone(self.preview())
        self.drag(notes, True)
        self.assertIsNotNone(self.preview())
        self.desktop.close(notes.hwnd)
        self.sync()
        self.pump(timeout=0.05)
        self.assertIsNone(self.preview())

    def test_untiled_windows_get_no_preview(self):
        self.tiled_workspace("Code")
        self.open("Code")
        game = self.open("Game")
        self.flush()
        self.drag(game, True)
        self.move(game, (10, 10, 400, 400))
        self.assertIsNone(self.preview())


class FocusBorderTests(OverlayEnv):
    def test_borders_follow_focus(self):
        self.tiled_workspace("Code", "Notes")
        code, notes = self.open("Code"), self.open("Notes")
        self.flush()
        self.assertEqual(set(self.overlays.borders), {self.desktop.tracked(x.hwnd).key for x in (code, notes)})
        left = self.slots(2)[0]
        self.assertEqual(self.border(code).rect_px, (left[0] - 2, left[1] - 2, left[2] + 2, left[3] + 2))
        self.assertFalse(self.border(code).active or self.border(notes).active)  # nothing focused yet
        self.focus(code)
        self.assertEqual((self.border(code).active, self.border(notes).active), (True, False))
        self.focus(notes)  # Alt+Tab / a click on another window
        self.assertEqual((self.border(code).active, self.border(notes).active), (False, True))
        game = self.open("Game")  # focus on an untiled window: every border grey
        self.focus(game)
        self.assertFalse(any(b.active for b in self.overlays.borders.values()))
        self.assertEqual((ACTIVE_COLOR, INACTIVE_COLOR), ("#C3E85B", "#2A2D34"))

    def test_borders_follow_moves_and_scale_with_dpi(self):
        right = Monitor(RIGHT.device, RIGHT.rect, RIGHT.work, False, 144)
        self.desktop.monitors_ = [LEFT, right]
        self.global_on()
        game = self.open("Game", rect=(2100, 100, 2600, 600))
        self.flush()
        border = self.border(game)
        self.assertEqual(border.thickness, 3)  # 2 px at 150 %
        self.drag(game, True)
        self.move(game, (2200, 150, 2700, 650))  # the border follows the window being dragged
        self.assertEqual(border.rect_px, (2197, 147, 2703, 653))

    def test_minimized_maximized_fullscreen_and_closed_windows_lose_their_border(self):
        self.global_on()
        a, b, c, d = (self.open(name) for name in ("A", "B", "C", "D"))
        self.flush()
        self.assertEqual(len(self.overlays.borders), 4)
        a.minimized = True
        b.maximized, b.rect = True, LEFT.work
        self.sync()
        self.assertIsNone(self.border(a))  # removed at once, not after the re-tile
        self.assertIsNone(self.border(b))
        c.rect = LEFT.rect  # went full screen (still resizable, e.g. a browser video)
        self.sync()
        self.assertIsNone(self.border(c))
        d_key = self.desktop.tracked(d.hwnd).key
        self.desktop.close(d.hwnd)
        self.sync()
        self.assertNotIn(d_key, self.overlays.borders)  # closed: gone at once
        self.flush()
        for window in (a, b):
            self.assertIsNone(self.border(window))  # still minimized / maximized after the re-tile

    def test_workspace_switch_tiling_off_and_borders_off_clear_everything(self):
        first = self.tiled_workspace("Code")
        self.open("Code")
        self.flush()
        self.assertEqual(len(self.overlays.borders), 1)
        self.manager.set_active("")  # another workspace: this one is no longer managed
        self.flush()
        self.assertEqual(self.overlays.borders, {})
        self.manager.set_active(first.id)
        self.flush()
        self.assertEqual(len(self.overlays.borders), 1)
        self.manager.set_dynamic(first.id, False)  # tiling switched off
        self.flush()
        self.assertEqual(self.overlays.borders, {})
        self.manager.set_dynamic(first.id, True)
        self.flush()
        self.overlays.set_borders_enabled(False)
        self.assertEqual(self.overlays.borders, {})
        self.assertFalse(self.settings.get("tiling_borders"))
        self.overlays.set_borders_enabled(True)
        self.assertEqual(len(self.overlays.borders), 1)
        self.overlays.stop()  # CIRANA closes
        self.assertEqual(self.overlays.borders, {})

    def test_overlays_take_no_input_no_focus_and_no_taskbar_button(self):
        self.tiled_workspace("Code")
        self.open("Code")
        self.flush()
        for overlay in list(self.overlays.borders.values()) + [self.overlays.preview]:
            flags = overlay.windowFlags()
            for flag in (Qt.WindowType.WindowTransparentForInput, Qt.WindowType.WindowDoesNotAcceptFocus,
                         Qt.WindowType.Tool, Qt.WindowType.FramelessWindowHint):
                self.assertTrue(flags & flag, flag)
            self.assertTrue(overlay.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating))
            self.assertTrue(overlay.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents))
            self.assertEqual(overlay.focusPolicy(), Qt.FocusPolicy.NoFocus)

    def test_borders_never_trigger_tiling(self):
        self.tiled_workspace("Code", "Notes")
        code = self.open("Code")
        self.open("Notes")
        self.flush()
        count = self.tiler.count
        for _ in range(5):
            self.focus(code)
            self.overlays.sync()
            self.sync()
            self.flush()
        self.assertEqual(self.tiler.count, count)
        self.assertEqual(self.desktop.calls[-1][0], "pos")  # no extra request reached any application window


if __name__ == "__main__":
    unittest.main()
