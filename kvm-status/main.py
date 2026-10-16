#!/usr/bin/env python3
"""KVM Status: at-a-glance health of the KVM and its target.

Tiles: target video, target USB, Wi-Fi, network/Tailscale, CPU/temperature,
memory. If the video service (kvm_vin) gets stuck burning CPU with no video
(a known firmware bug), the footer turns red. Tap it twice to restart it.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from appbase import AppContext, app

import ui
from applog import AppLog
from collector import Collector
from tiles import Level, Tile, build_tiles, footer

APP_DIR = Path(__file__).resolve().parent
LOG = AppLog(APP_DIR)
HEADER_H, FOOTER_H, GAP = 28, 20, 4
ARM_S = 3.0  # second tap must come within this window
MESSAGE_S = 5.0
FPS = 5
LEVEL_COLORS = {Level.OK: ui.OK, Level.WARN: ui.WARN, Level.ERR: ui.ERR}


def draw_tile(fb, box: ui.Box, tile: Tile) -> None:
    fb.fill_rect(box.x, box.y, box.w, box.h, ui.SURFACE)
    fb.fill_rect(box.x, box.y, 3, box.h, LEVEL_COLORS[tile.level])
    inner = box.inset(9, 6)
    ui.text(fb, inner.x, inner.y, tile.label.upper(), ui.TEXT_DIM, 1)
    value_scale = 2 if len(tile.value) * 16 <= inner.w else 1
    value_y = inner.cy - 4 * value_scale + 1
    color = ui.TEXT if tile.level == Level.OK else LEVEL_COLORS[tile.level]
    ui.text(fb, inner.x, value_y, tile.value, color, value_scale)
    ui.text(fb, inner.x, inner.y + inner.h - 8, ui.truncate(tile.detail, inner.w), ui.TEXT_DIM, 1)


class StatusScreen:
    def __init__(self, ctx: AppContext, collector: Collector) -> None:
        self.ctx = ctx
        self.collector = collector
        header, rest = ui.screen(ctx).split_top(HEADER_H)
        body, self.footer = rest.split_bottom(FOOTER_H)
        self.header = header
        self.cells = ui.grid(body.inset(GAP), 2, 3, GAP)
        self.seen = -1
        self.minute = ""
        self.armed_until = 0.0
        self.message = ""
        self.message_until = 0.0
        self.stale = False
        self.dirty = True

    def on_tap(self, x: int, y: int) -> None:
        snap, _ = self.collector.snapshot()
        if not (snap and snap.vin_stuck and self.footer.contains(x, y)):
            return
        now = time.monotonic()
        if now < self.armed_until:
            self.armed_until = 0.0
            threading.Thread(target=self._restart, daemon=True).start()
        else:
            self.armed_until = now + ARM_S
        self.dirty = True

    def _restart(self) -> None:
        try:
            message = self.collector.restart_video_service()
        except OSError as exc:
            message = f"Restart failed: {exc}"
            LOG.error(message)
        self.message, self.message_until = message, time.monotonic() + MESSAGE_S
        self.dirty = True

    def tick(self, dt: float) -> None:
        snap, version = self.collector.snapshot()
        now = time.monotonic()
        minute = time.strftime("%H:%M")
        armed = now < self.armed_until
        stale = self.collector.is_stale()
        expired = bool((self.armed_until and not armed) or (self.message_until and now >= self.message_until))
        if expired:
            self.armed_until = 0.0 if not armed else self.armed_until
            if self.message_until and now >= self.message_until:
                self.message, self.message_until = "", 0.0
        if version != self.seen or minute != self.minute or stale != self.stale or self.dirty or expired:
            self.seen, self.minute, self.stale, self.dirty = version, minute, stale, False
            self.draw(snap, armed)

    def draw(self, snap, armed: bool) -> None:
        fb = self.ctx.fb
        fb.clear(ui.BG)
        title = snap.model if snap else "KVM Status"
        right = f"stale {self.minute}" if self.stale else self.minute
        ui.draw_header(fb, self.header, title, right=right, color=ui.WARN if self.stale else ui.INFO)
        if snap is None:
            ui.draw_text_center(fb, self.cells[0], "Reading...", ui.TEXT_DIM, 1)
            return
        for cell, tile in zip(self.cells, build_tiles(snap)):
            draw_tile(fb, cell, tile)
        text = self.message or footer(snap, armed)
        if snap.vin_stuck and not self.message:
            fb.fill_rect(self.footer.x, self.footer.y, self.footer.w, self.footer.h, ui.ERR if armed else ui.SURFACE)
            color = ui.TEXT if armed else ui.ERR
        else:
            color = ui.TEXT_DIM
        ui.text(fb, self.footer.x + 8, self.footer.cy - 4, ui.truncate(text, self.footer.w - 16), color)


@app()
def main(ctx: AppContext) -> None:
    collector = Collector(log=LOG.warn)
    collector.start()
    screen = StatusScreen(ctx, collector)
    LOG.info("started")
    try:
        ctx.run(LOG.guard(screen.tick), fps=FPS, on_tap=LOG.guard(screen.on_tap))
    finally:
        collector.stop()
        LOG.info("stopped")


if __name__ == "__main__":
    main()
