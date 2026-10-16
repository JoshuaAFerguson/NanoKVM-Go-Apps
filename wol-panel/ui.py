"""Layout and drawing helpers for the NanoKVM Go panel.

Shared module: edit _shared/ui.py, then run `python3 _tools/sync_shared.py`.

Verified geometry (current firmware, both Go and Go+): the host launches Apps at
rotate=90, giving a 284x240 landscape canvas, and the whole canvas is visible.
The upstream guide's 240x284 portrait + 14px hidden strip does not apply.
The host reserves a 40px zone at the left and right edges for its exit swipe,
so keep swipe-driven controls away from the sides.
"""

from __future__ import annotations

from dataclasses import dataclass

import font
from font import GLYPH_PX

EXIT_ZONE_PX = 40
LINE_GAP = 3


def rgb565(r: int, g: int, b: int) -> int:
    """Same packing as appbase.rgb565, so palettes work without importing appbase."""
    return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)


# A deliberate dark palette: warm neutral surfaces plus semantic accents.
BG = rgb565(14, 14, 18)
SURFACE = rgb565(34, 34, 44)
SURFACE_HI = rgb565(58, 58, 74)
TEXT = rgb565(236, 236, 240)
TEXT_DIM = rgb565(140, 140, 156)
ACCENT = rgb565(255, 176, 32)
OK = rgb565(48, 208, 120)
WARN = rgb565(255, 196, 40)
ERR = rgb565(240, 64, 64)
INFO = rgb565(64, 156, 255)
VIOLET = rgb565(176, 120, 255)


@dataclass(frozen=True)
class Box:
    x: int
    y: int
    w: int
    h: int

    @property
    def cx(self) -> int:
        return self.x + self.w // 2

    @property
    def cy(self) -> int:
        return self.y + self.h // 2

    def contains(self, px: int, py: int) -> bool:
        return self.x <= px < self.x + self.w and self.y <= py < self.y + self.h

    def inset(self, dx: int, dy: int | None = None) -> "Box":
        dy = dx if dy is None else dy
        return Box(self.x + dx, self.y + dy, max(0, self.w - 2 * dx), max(0, self.h - 2 * dy))

    def split_top(self, height: int) -> "tuple[Box, Box]":
        return Box(self.x, self.y, self.w, height), Box(self.x, self.y + height, self.w, self.h - height)

    def split_bottom(self, height: int) -> "tuple[Box, Box]":
        return Box(self.x, self.y, self.w, self.h - height), Box(self.x, self.y + self.h - height, self.w, height)


def screen(ctx) -> Box:
    """The full visible canvas."""
    return Box(0, 0, ctx.width, ctx.height)


def grid(box: Box, cols: int, rows: int, gap: int = 4) -> list[Box]:
    """Row-major cells that exactly fill `box`; leftover pixels go to the last row/column."""
    cell_w = (box.w - gap * (cols - 1)) // cols
    cell_h = (box.h - gap * (rows - 1)) // rows
    cells = []
    for r in range(rows):
        for c in range(cols):
            x = box.x + c * (cell_w + gap)
            y = box.y + r * (cell_h + gap)
            w = box.x + box.w - x if c == cols - 1 else cell_w
            h = box.y + box.h - y if r == rows - 1 else cell_h
            cells.append(Box(x, y, w, h))
    return cells


def hit(boxes: list[Box], x: int, y: int) -> int | None:
    return next((i for i, b in enumerate(boxes) if b.contains(x, y)), None)


def fit_scale(text: str, width: int, max_scale: int = 3) -> int:
    for scale in range(max_scale, 1, -1):
        if len(text) * GLYPH_PX * scale <= width:
            return scale
    return 1


def truncate(text: str, width: int, scale: int = 1) -> str:
    max_chars = max(1, width // (GLYPH_PX * scale))
    return text if len(text) <= max_chars else text[: max_chars - 1] + "~"


def wrap(text: str, width: int, scale: int = 1) -> list[str]:
    max_chars = max(1, width // (GLYPH_PX * scale))
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = truncate(word, width, scale)
    if current:
        lines.append(current)
    return lines


def text(fb, x: int, y: int, value: str, color: int, scale: int = 1) -> None:
    font.draw_text(fb, x, y, value, color, scale)


def draw_text_center(fb, box: Box, value: str, color: int, scale: int) -> None:
    value = truncate(value, box.w, scale)
    width = font.text_width(value, scale)
    text(fb, box.cx - width // 2, box.cy - GLYPH_PX * scale // 2, value, color, scale)


def label_layout(label: str, width: int, height: int, max_scale: int = 2,
                 max_lines: int = 2) -> "tuple[int, list[str]]":
    """Largest scale at which `label` wraps into <= max_lines lines that fit the box."""
    for scale in range(max_scale, 0, -1):
        lines = wrap(label, width, scale)
        fits_width = all(len(line) * GLYPH_PX * scale <= width for line in lines)
        tall = len(lines) * GLYPH_PX * scale + (len(lines) - 1) * LINE_GAP
        whole = " ".join(lines).replace("~", "") == " ".join(label.split())
        if len(lines) <= max_lines and fits_width and tall <= height and whole:
            return scale, lines
    return 1, [truncate(label, width, 1)]


def common_scale(labels: "list[str]", width: int, height: int, max_scale: int = 2) -> int:
    """One scale for a set of buttons so a page doesn't mix text sizes."""
    return min((label_layout(lb, width, height, max_scale)[0] for lb in labels), default=max_scale)


def draw_lines_center(fb, box: Box, lines: "list[str]", color: int, scale: int) -> None:
    line_h = GLYPH_PX * scale
    total = len(lines) * line_h + (len(lines) - 1) * LINE_GAP
    y = box.cy - total // 2
    for line in lines:
        width = font.text_width(line, scale)
        text(fb, box.cx - width // 2, y, line, color, scale)
        y += line_h + LINE_GAP


def button_label_box(box: Box, has_sublabel: bool) -> Box:
    inner = box.inset(8, 5)
    return inner.split_bottom(GLYPH_PX + 3)[0] if has_sublabel else inner


def draw_button(fb, box: Box, label: str, bg: int, fg: int = TEXT, accent: "int | None" = None,
                max_scale: int = 2, sublabel: str = "", scale: "int | None" = None) -> None:
    """Filled button with an optional accent stripe and a small hint line."""
    fb.fill_rect(box.x, box.y, box.w, box.h, bg)
    if accent is not None:
        fb.fill_rect(box.x, box.y, 3, box.h, accent)
    label_box = button_label_box(box, bool(sublabel))
    fitted_scale, lines = label_layout(label, label_box.w, label_box.h, scale or max_scale)
    if scale is not None and fitted_scale < scale:
        lines = wrap(label, label_box.w, fitted_scale)
    draw_lines_center(fb, label_box, lines, fg, fitted_scale)
    if sublabel:
        hint = box.inset(8, 5).split_bottom(GLYPH_PX)[1]
        draw_text_center(fb, hint, sublabel, TEXT_DIM, 1)


def draw_header(fb, box: Box, title: str, right: str = "", color: int = ACCENT) -> None:
    fb.fill_rect(box.x, box.y, box.w, box.h, SURFACE)
    fb.fill_rect(box.x, box.y + box.h - 2, box.w, 2, color)
    right_w = font.text_width(right) + 16 if right else 0
    scale = 2 if font.text_width(title, 2) <= box.w - right_w - 16 else 1
    text(fb, box.x + 8, box.cy - GLYPH_PX * scale // 2 - 1, title, TEXT, scale)
    if right:
        text(fb, box.x + box.w - right_w + 8, box.cy - GLYPH_PX // 2 - 1, right, TEXT_DIM, 1)
