"""Tests for _shared/ui.py layout helpers and drawing against a fake framebuffer."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared"))

import ui  # noqa: E402
from ui import Box  # noqa: E402


class FakeFb:
    width, height = 284, 240

    def __init__(self):
        self.ops = []

    def fill_rect(self, x, y, w, h, color):
        self.ops.append(("rect", x, y, w, h, color))

    def draw_text(self, x, y, text, color, scale=1):
        self.ops.append(("text", x, y, text, color, scale))

    def text_width(self, text, scale=1):
        return len(text) * 8 * scale


class BoxTest(unittest.TestCase):
    def test_contains_is_half_open(self):
        box = Box(10, 10, 20, 20)
        self.assertTrue(box.contains(10, 10))
        self.assertTrue(box.contains(29, 29))
        self.assertFalse(box.contains(30, 10))

    def test_inset_and_center(self):
        box = Box(0, 0, 100, 50).inset(10, 5)
        self.assertEqual(box, Box(10, 5, 80, 40))
        self.assertEqual((box.cx, box.cy), (50, 25))

    def test_split_top(self):
        head, body = Box(0, 0, 284, 240).split_top(30)
        self.assertEqual(head, Box(0, 0, 284, 30))
        self.assertEqual(body, Box(0, 30, 284, 210))

    def test_split_bottom(self):
        body, foot = Box(0, 0, 284, 240).split_bottom(20)
        self.assertEqual(foot, Box(0, 220, 284, 20))
        self.assertEqual(body.h, 220)


class GridTest(unittest.TestCase):
    def test_grid_fills_box_without_overlap(self):
        cells = ui.grid(Box(0, 0, 284, 200), cols=3, rows=2, gap=6)
        self.assertEqual(len(cells), 6)
        for i, a in enumerate(cells):
            for b in cells[i + 1:]:
                overlap = a.x < b.x + b.w and b.x < a.x + a.w and a.y < b.y + b.h and b.y < a.y + a.h
                self.assertFalse(overlap)
        self.assertEqual(cells[0].x, 0)
        self.assertEqual(cells[-1].x + cells[-1].w, 284)
        self.assertEqual(cells[-1].y + cells[-1].h, 200)

    def test_hit_returns_index(self):
        cells = ui.grid(Box(0, 0, 90, 30), cols=3, rows=1, gap=0)
        self.assertEqual(ui.hit(cells, 45, 10), 1)
        self.assertIsNone(ui.hit(cells, 200, 10))


class TextTest(unittest.TestCase):
    def test_fit_scale_picks_largest_that_fits(self):
        self.assertEqual(ui.fit_scale("ABCD", 70, max_scale=3), 2)  # 64px at 2x
        self.assertEqual(ui.fit_scale("ABCDEFGHIJ", 70, max_scale=3), 1)

    def test_truncate_adds_marker(self):
        self.assertEqual(ui.truncate("ABCDEFGHIJ", 48, scale=1), "ABCDE~")
        self.assertEqual(ui.truncate("ABC", 48, scale=1), "ABC")

    def test_wrap_breaks_on_words(self):
        # 80px holds 10 glyphs at scale 1
        self.assertEqual(ui.wrap("Lock the screen now", 80, scale=1), ["Lock the", "screen now"])
        self.assertEqual(ui.wrap("Lock the screen now", 64, scale=1), ["Lock the", "screen", "now"])

    def test_rgb565_matches_sdk_packing(self):
        self.assertEqual(ui.rgb565(255, 255, 255), 0xFFFF)
        self.assertEqual(ui.rgb565(255, 0, 0), 0xF800)


class LabelLayoutTest(unittest.TestCase):
    def test_short_label_uses_big_scale_one_line(self):
        self.assertEqual(ui.label_layout("Lock", 120, 40), (2, ["Lock"]))

    def test_long_label_wraps_at_big_scale(self):
        self.assertEqual(ui.label_layout("Task Manager", 120, 40), (2, ["Task", "Manager"]))

    def test_unbreakable_label_drops_scale(self):
        scale, lines = ui.label_layout("Ctrl+Alt+Del", 120, 40)
        self.assertEqual((scale, lines), (1, ["Ctrl+Alt+Del"]))

    def test_common_scale_is_the_minimum(self):
        self.assertEqual(ui.common_scale(["Lock", "Ctrl+Alt+Del"], 120, 40), 1)


class DrawTest(unittest.TestCase):
    def test_button_draws_fill_and_label_pixels_inside_box(self):
        fb = FakeFb()
        box = Box(10, 10, 100, 40)
        ui.draw_button(fb, box, "OK", bg=1, fg=2)
        rects = [op for op in fb.ops if op[0] == "rect"]
        self.assertEqual(rects[0][1:], (10, 10, 100, 40, 1))
        glyph_rects = [r for r in rects if r[5] == 2]
        self.assertTrue(glyph_rects)
        for _, x, y, w, h, _ in glyph_rects:
            self.assertTrue(box.contains(x, y) and box.contains(x + w - 1, y + h - 1))

    def test_unknown_glyph_draws_fallback_not_nothing(self):
        fb = FakeFb()
        ui.text(fb, 0, 0, "\u2603", 1)
        self.assertTrue(fb.ops)


if __name__ == "__main__":
    unittest.main()
