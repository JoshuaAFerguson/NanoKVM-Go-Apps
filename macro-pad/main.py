#!/usr/bin/env python3
"""Macro Pad: touch buttons that send key combos, boot keys, and text to the KVM target.

Tap the header to switch profile (Windows, macOS, Linux, Mobile, Nav, and any
custom ones). Swipe up/down in the middle of the screen to change page. Tap a
running boot-key button again to cancel it.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from appbase import AppContext, app

import ui
from applog import AppLog
from kvm_mcp import KvmMcp
from macros import MacroError, Profile, describe, load_profiles, paginate, start_index
from runner import Runner

APP_DIR = Path(__file__).resolve().parent
STATE_FILE = APP_DIR / ".state.json"
LOG = AppLog(APP_DIR)
COLS, ROWS = 2, 3
HEADER_H, FOOTER_H, GAP = 28, 18, 4
FLASH_S = 0.6
FPS = 15


def load_saved_profile() -> str | None:
    try:
        state = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return None
    return state.get("profile") if isinstance(state, dict) else None


def save_profile(name: str) -> None:
    try:
        STATE_FILE.write_text(json.dumps({"profile": name}))
    except OSError as exc:
        LOG.warn(f"could not save state: {exc}")


class MacroPad:
    def __init__(self, ctx: AppContext, profiles: tuple[Profile, ...], runner: Runner,
                 profile_index: int, notice: str = "") -> None:
        self.ctx = ctx
        self.profiles = profiles
        self.runner = runner
        self.profile_index = profile_index
        self.page = 0
        self.notice = notice
        header, rest = ui.screen(ctx).split_top(HEADER_H)
        body, self.footer = rest.split_bottom(FOOTER_H)
        self.header = header
        self.cells = ui.grid(body.inset(GAP), COLS, ROWS, GAP)
        self.flash: tuple[int, int, float] | None = None  # (cell, color, until)
        self.seen_version = -1
        self.dirty = True

    @property
    def profile(self) -> Profile:
        return self.profiles[self.profile_index]

    def pages(self):
        return paginate(self.profile.actions, COLS * ROWS)

    # -- input --------------------------------------------------------

    def on_tap(self, x: int, y: int) -> None:
        if self.header.contains(x, y):
            self.switch_profile(1)
            return
        cell = ui.hit(self.cells, x, y)
        page = self.pages()[self.page]
        if cell is None or cell >= len(page):
            return
        status = self.runner.status()
        if status.busy:
            if status.index == cell:
                self.runner.cancel()
            else:
                self.notice = "Busy - tap the running button to cancel"
                self.dirty = True
            return
        if self.notice.startswith("Busy"):
            self.notice = ""
        self.runner.start(page[cell], cell)

    def on_swipe(self, kind: str, x: int, y: int) -> None:
        if x < ui.EXIT_ZONE_PX or x >= self.ctx.width - ui.EXIT_ZONE_PX:
            return  # host exit gesture territory
        if self.runner.status().busy:
            return  # the running button must stay on screen so it can be cancelled
        count = len(self.pages())
        if kind == "up":
            self.page = (self.page + 1) % count
        elif kind == "down":
            self.page = (self.page - 1) % count
        elif kind in ("left", "right"):
            self.switch_profile(1 if kind == "left" else -1)
            return
        LOG.info(f"page {self.page + 1}/{count}")
        self.dirty = True

    def switch_profile(self, step: int) -> None:
        if self.runner.status().busy:
            return
        self.profile_index = (self.profile_index + step) % len(self.profiles)
        self.page = 0
        self.notice = ""
        save_profile(self.profile.name)
        LOG.info(f"profile {self.profile.name}")
        self.dirty = True

    # -- drawing ------------------------------------------------------

    def tick(self, dt: float) -> None:
        status = self.runner.status()
        if status.version != self.seen_version:
            if self.seen_version >= 0 and not status.busy and status.index is not None:
                self.flash = (status.index, ui.OK if status.ok else ui.ERR, time.monotonic() + FLASH_S)
            self.seen_version = status.version
            self.dirty = True
        if self.flash and time.monotonic() > self.flash[2]:
            self.flash = None
            self.dirty = True
        if self.dirty:
            self.draw(status)
            self.dirty = False

    def draw(self, status) -> None:
        fb = self.ctx.fb
        fb.clear(ui.BG)
        pages = self.pages()
        right = f"{self.page + 1}/{len(pages)}" if len(pages) > 1 else ""
        ui.draw_header(fb, self.header, self.profile.name, right=(right + "  tap: next >").strip())
        page = pages[self.page]
        label_box = ui.button_label_box(self.cells[0], True)
        scale = ui.common_scale([a.label for a in page], label_box.w, label_box.h)
        for i, action in enumerate(page):
            bg, sub = ui.SURFACE, describe(action, self.profile.meta)
            if status.busy and status.index == i:
                bg, sub = ui.SURFACE_HI, (status.progress or "sending") + "  tap=stop"
            elif self.flash and self.flash[0] == i:
                bg = self.flash[1]
            accent = ui.VIOLET if action.repeat > 1 else (ui.INFO if action.text else ui.ACCENT)
            ui.draw_button(fb, self.cells[i], action.label, bg, accent=accent, sublabel=sub, scale=scale)
        message = self.notice or status.message
        color = ui.ERR if (not status.ok and not self.notice) else (ui.WARN if self.notice else ui.TEXT_DIM)
        ui.text(fb, self.footer.x + 6, self.footer.cy - 4, ui.truncate(message, self.footer.w - 12), color)


def draw_problem(ctx: AppContext, title: str, detail: str) -> None:
    fb = ctx.fb
    fb.clear(ui.BG)
    header, body = ui.screen(ctx).split_top(HEADER_H)
    ui.draw_header(fb, header, title, color=ui.ERR)
    for n, line in enumerate(ui.wrap(detail, body.w - 16)):
        ui.text(fb, body.x + 8, body.y + 12 + n * 12, line, ui.TEXT)


@app()
def main(ctx: AppContext) -> None:
    api_key = ctx.env.get("NANOKVM_MCP_KEY", "").strip()
    if not api_key:
        draw_problem(ctx, "Setup needed", "Set the NanoKVM MCP API key for Macro Pad in the web UI: "
                     "Settings > Apps > Installed > Macro Pad. Enable MCP first in Settings > MCP.")
        ctx.run(lambda dt: None, fps=2)
        return

    notice = ""
    try:
        profiles = load_profiles(ctx.env.get("CUSTOM_MACROS"))
    except MacroError as exc:
        profiles, notice = load_profiles(None), f"Custom macros ignored: {exc}"
    if notice:
        LOG.warn(notice)
    saved = load_saved_profile()
    names = {p.name.lower() for p in profiles}
    preferred = saved if saved and saved.lower() in names else ctx.env.get("DEFAULT_PROFILE")
    runner = Runner(KvmMcp(api_key, client_name="macro-pad"), on_result=LOG.info)
    pad = MacroPad(ctx, profiles, runner, start_index(profiles, preferred), notice)
    LOG.info(f"started {ctx.width}x{ctx.height} rotate={ctx.fb.rotate} profile={pad.profile.name}")
    try:
        ctx.run(LOG.guard(pad.tick), fps=FPS, on_tap=LOG.guard(pad.on_tap), on_swipe=LOG.guard(pad.on_swipe))
    finally:
        runner.cancel()  # don't keep sending boot keys while the process exits
        runner.join(1)
        LOG.info("stopped")


if __name__ == "__main__":
    main()
