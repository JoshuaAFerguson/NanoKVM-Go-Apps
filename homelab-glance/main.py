#!/usr/bin/env python3
"""Homelab Glance: Home Assistant, Uptime Kuma, Proxmox, Prometheus, Kubernetes and
Docker at a glance. Tap a tile for details; tap the header to go back; swipe
up/down to page through details; tap the footer to refresh now.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from appbase import AppContext, app

import config
import services
import ui
from applog import AppLog
from services import Level, Result

APP_DIR = Path(__file__).resolve().parent
LOG = AppLog(APP_DIR)
HEADER_H, FOOTER_H, GAP = 28, 18, 4
ROW_H = 22
ROW_SCALE = 2
FPS = 5
LEVEL_COLORS = {Level.OK: ui.OK, Level.WARN: ui.WARN, Level.ERR: ui.ERR}


class Poller:
    """Fetches every configured service on a background thread."""

    def __init__(self, svcs: list, refresh_s: int) -> None:
        self.services = svcs
        self.refresh_s = refresh_s
        self._lock = threading.Lock()
        self._results: dict[str, Result] = {}
        self._now = threading.Event()
        self._stop = threading.Event()
        self.version = 0
        self.busy = False
        self.cycle_at = 0.0

    def results(self) -> dict:
        with self._lock:
            return dict(self._results)

    def refresh(self) -> None:
        self._now.set()

    def stop(self) -> None:
        self._stop.set()
        self._now.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._now.clear()
            self.busy = True
            for svc in self.services:
                try:
                    result = services.fetch(svc)  # designed never to raise
                    if result.error:
                        LOG.warn(result.error)
                except Exception:  # keep polling the other services no matter what
                    LOG.exception(f"poll {svc.kind}")
                    continue
                with self._lock:
                    self._results[svc.kind] = result
                    self.version += 1
            self.busy, self.cycle_at = False, time.monotonic()
            with self._lock:
                self.version += 1
            self._now.wait(self.refresh_s)

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()


class GlanceScreen:
    def __init__(self, ctx: AppContext, poller: Poller) -> None:
        self.ctx = ctx
        self.poller = poller
        self.header, rest = ui.screen(ctx).split_top(HEADER_H)
        self.body, self.footer = rest.split_bottom(FOOTER_H)
        self.cells = ui.grid(self.body.inset(GAP), 2, 3, GAP)
        self.rows_per_page = (self.body.h - 2 * GAP) // ROW_H
        self.detail: str | None = None  # service kind being viewed
        self.page = 0
        self.seen = -1
        self.second = -1
        self.dirty = True

    # -- input ----------------------------------------------------------------

    def on_tap(self, x: int, y: int) -> None:
        if self.footer.contains(x, y):
            self.poller.refresh()
        elif self.detail and self.header.contains(x, y):
            self.detail, self.page = None, 0
        elif not self.detail:
            cell = ui.hit(self.cells, x, y)
            if cell is not None and cell < len(self.poller.services):
                self.detail, self.page = self.poller.services[cell].kind, 0
        self.dirty = True

    def on_swipe(self, kind: str, x: int, y: int) -> None:
        if not self.detail or x < ui.EXIT_ZONE_PX or x >= self.ctx.width - ui.EXIT_ZONE_PX:
            return
        result = self.poller.results().get(self.detail)
        pages = max(1, -(-len(result.rows) // self.rows_per_page)) if result else 1
        step = {"up": 1, "down": -1}.get(kind, 0)
        self.page = (self.page + step) % pages
        self.dirty = True

    # -- drawing ---------------------------------------------------------------

    def tick(self, dt: float) -> None:
        second = int(time.monotonic())
        if self.poller.version != self.seen or self.dirty or second != self.second and second % 5 == 0:
            self.seen, self.second, self.dirty = self.poller.version, second, False
            self.draw()

    def footer_text(self) -> str:
        if self.poller.busy:
            return "Refreshing..."
        if not self.poller.cycle_at:
            return "Loading..."
        age = int(time.monotonic() - self.poller.cycle_at)
        return f"Updated {age}s ago - tap to refresh"

    def draw(self) -> None:
        fb = self.ctx.fb
        fb.clear(ui.BG)
        results = self.poller.results()
        if self.detail:
            self.draw_detail(results.get(self.detail))
        else:
            ui.draw_header(fb, self.header, "Homelab", right=time.strftime("%H:%M"), color=ui.VIOLET)
            for box, svc in zip(self.cells, self.poller.services):
                r = results.get(svc.kind)
                if r is None:
                    ui.draw_stat_tile(fb, box, svc.title, "...", "loading", ui.SURFACE_HI)
                    continue
                color = LEVEL_COLORS[r.level]
                ui.draw_stat_tile(fb, box, r.title, r.summary, r.detail, color,
                                  ui.TEXT if r.level == Level.OK else color)
        ui.text(fb, self.footer.x + 6, self.footer.cy - 4, ui.truncate(self.footer_text(), self.footer.w - 12),
                ui.TEXT_DIM)

    def draw_detail(self, r: Result | None) -> None:
        fb = self.ctx.fb
        svc = next(s for s in self.poller.services if s.kind == self.detail)
        rows = r.rows if r else ()
        pages = max(1, -(-len(rows) // self.rows_per_page))
        self.page = min(self.page, pages - 1)  # rows can shrink after a refresh
        right = f"{self.page + 1}/{pages}" if pages > 1 else ""
        color = LEVEL_COLORS[r.level] if r else ui.SURFACE_HI
        ui.draw_header(fb, self.header, f"< {svc.title}", right=right, color=color)
        area = self.body.inset(8, GAP)
        if r is None:
            ui.text(fb, area.x, area.y, "Loading...", ui.TEXT_DIM)
            return
        if r.error:
            for n, line in enumerate(ui.wrap(r.error, area.w)[:12]):
                ui.text(fb, area.x, area.y + n * ROW_H, line, ui.ERR)
            return
        start = self.page * self.rows_per_page
        glyph = 8 * ROW_SCALE
        for n, row in enumerate(rows[start:start + self.rows_per_page]):
            y = area.y + n * ROW_H + 2
            value = ui.truncate(row.value, area.w // 2, ROW_SCALE)
            value_x = area.x + area.w - len(value) * glyph
            label = ui.truncate(row.label, value_x - area.x - glyph // 2, ROW_SCALE)
            ui.text(fb, area.x, y, label, ui.TEXT, ROW_SCALE)
            ui.text(fb, value_x, y, value, ui.TEXT_DIM if row.level == Level.OK else LEVEL_COLORS[row.level], ROW_SCALE)
        if not rows:
            ui.text(fb, area.x, area.y, "Nothing to show", ui.TEXT_DIM)


SETUP_TEXT = ("Configure services in Settings > Apps > Installed > Homelab Glance. "
              "Set a URL to enable each one: HA_URL, KUMA_URL, PVE_URL, PROM_URL, K8S_URL, DOCKER_URL. "
              "See the App README for tokens.")


def draw_message(ctx: AppContext, title: str, message: str, color: int) -> None:
    fb = ctx.fb
    fb.clear(ui.BG)
    header, body = ui.screen(ctx).split_top(HEADER_H)
    ui.draw_header(fb, header, title, color=color)
    for n, line in enumerate(ui.wrap(message, body.w - 16)[:15]):
        ui.text(fb, body.x + 8, body.y + 10 + n * ROW_H, line, ui.TEXT)


@app()
def main(ctx: AppContext) -> None:
    env = dict(ctx.env)
    try:
        svcs = config.load(env)
    except config.ConfigError as exc:
        LOG.warn(f"config: {exc}")
        draw_message(ctx, "Fix settings", str(exc), ui.ERR)
        ctx.run(lambda dt: None, fps=1)
        return
    if not svcs:
        draw_message(ctx, "Homelab Glance", SETUP_TEXT, ui.VIOLET)
        ctx.run(lambda dt: None, fps=1)
        return
    poller = Poller(svcs[:6], config.refresh_seconds(env))
    poller.start()
    LOG.info(f"started: {[s.kind for s in svcs]}")
    screen = GlanceScreen(ctx, poller)
    try:
        ctx.run(LOG.guard(screen.tick), fps=FPS, on_tap=LOG.guard(screen.on_tap),
                on_swipe=LOG.guard(screen.on_swipe))
    finally:
        poller.stop()
        LOG.info("stopped")


if __name__ == "__main__":
    main()
