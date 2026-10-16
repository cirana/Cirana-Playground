"""Focus borders and the drop preview with the new modes: stacks show tabs on the shown window's border (hidden
windows get none), monocle doubles the border, BSP drops preview exactly where the window lands."""
import unittest

from cirana.wm.layout import BSP, LayoutSpec
from cirana.wm.navigation import LEFT
from tests.test_tiling_overlay import OverlayEnv


class OverlayModeTests(OverlayEnv):
    def focus_on(self, window):
        self.desktop.foreground = window.hwnd
        self.focus(window)

    def test_stack_tabs_and_no_border_for_hidden_windows(self):
        self.global_on()
        code, notes, mail = self.open("Code"), self.open("Notes"), self.open("Mail")
        self.flush()
        self.focus_on(mail)
        self.tiler.stack_direction(LEFT)
        self.flush()
        self.flush()
        self.assertIsNone(self.border(notes))  # under the shown window: no border of its own
        border = self.border(mail)
        self.assertEqual(border.stack, (1, 2))
        self.assertTrue(border.active)
        self.assertIsNone(self.border(code).stack)
        border.grab()  # paints without error (tabs on the top edge)

    def test_monocle_border_is_thicker_and_goes_back(self):
        self.global_on()
        code, _notes = self.open("Code"), self.open("Notes")
        self.flush()
        self.focus_on(code)
        self.tiler.toggle_monocle()
        self.flush()
        self.flush()
        border = self.border(code)
        self.assertTrue(border.monocle)
        self.assertEqual(border.thickness, 4)
        self.tiler.toggle_monocle()
        self.flush()
        self.flush()
        self.assertFalse(self.border(code).monocle)
        self.assertEqual(self.border(code).thickness, 2)

    def test_bsp_drop_preview_matches_the_landing(self):
        self.tiler.set_global_layout(LayoutSpec(BSP, gap=0))
        self.tiler.set_global(True)
        code, notes, mail = self.open("Code"), self.open("Notes"), self.open("Mail")
        self.flush()
        target = mail.rect
        self.drag(code, True)
        self.move(code, (target[0] + 30, target[1] + 30, target[0] + 330, target[1] + 230))
        shown = self.preview()
        self.drag(code, False)
        self.flush()
        self.assertEqual(shown, code.rect)
        self.assertIsNone(self.preview())
        del notes


if __name__ == "__main__":
    unittest.main()
