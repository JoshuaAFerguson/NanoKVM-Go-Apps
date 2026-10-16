#!/usr/bin/env python3
"""Screen Watcher panel: status of the background watcher service, recent alerts,
and Test / Pause controls. The watching itself runs in a systemd service, so it
keeps working when this App is closed.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
import uuid
from pathlib import Path

from appbase import AppContext, app

import appdata
import ui
from applog import AppLog

APP_DIR = Path(__file__).resolve().parent
APP_NAME = "screen-watcher"
UNIT_PATH = Path("/etc/systemd/system/nanokvm-screen-watcher.service")
LOG = AppLog(APP_DIR)
HEADER_H, TILE_H, BUTTON_H, GAP = 28, 72, 44, 4
PAUSE_S = 3600
FPS = 4
LEVEL_COLORS = {"alert": ui.ERR, "recovered": ui.OK, "info": ui.INFO}
ACTIVE_NAMES = {"signal": "No signal", "bsod": "Blue screen", "black": "Black screen", "frozen": "Frozen"}


def ago(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    if seconds < 172800:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


class Panel:
    def __init__(self, ctx: AppContext, data: Path) -> None:
        self.ctx = ctx
        self.data = data
        screen = ui.screen(ctx)
        self.header, rest = screen.split_top(HEADER_H)
        body, buttons = rest.split_bottom(BUTTON_H + GAP)
        tiles, self.events_box = body.inset(GAP, GAP).split_top(TILE_H)
        self.target_box, self.alerts_box = ui.grid(tiles, 2, 1, GAP)
        self.test_btn, self.pause_btn = ui.grid(buttons.inset(GAP, 0).split_top(BUTTON_H)[0], 2, 1, GAP)
        self.message, self.message_until = "", 0.0
        self.installing = False
        self.last_draw_key = None

    # -- control ---------------------------------------------------------------

    def control(self) -> dict:
        return appdata.read_json(self.data / "control.json", {}) or {}

    def write_control(self, **changes) -> None:
        appdata.write_json(self.data / "control.json", {**self.control(), **changes})

    def flash(self, text: str) -> None:
        self.message, self.message_until = text, time.monotonic() + 4

    def on_tap(self, x: int, y: int) -> None:
        if self.test_btn.contains(x, y):
            self.write_control(test_id=uuid.uuid4().hex)
            self.flash("Test alert requested...")
            LOG.info("test alert requested")
        elif self.pause_btn.contains(x, y):
            paused = float(self.control().get("paused_until", 0) or 0) > time.time()
            self.write_control(paused_until=0 if paused else time.time() + PAUSE_S)
            self.flash("Alerts resumed" if paused else "Alerts paused for 1 hour")
            LOG.info("resumed" if paused else "paused 1h")

    def ensure_service(self) -> None:
        if UNIT_PATH.exists() or self.installing:
            return
        self.installing = True

        def install() -> None:
            env = {**os.environ, "NANOKVM_APP_DIR": str(APP_DIR), "NANOKVM_APP_ID": "screen-watcher"}
            try:
                out = subprocess.run(["/bin/bash", str(APP_DIR / "install-service.sh")], env=env,
                                     capture_output=True, text=True, timeout=60)
                LOG.info(f"install-service: rc={out.returncode} {out.stdout.strip()} {out.stderr.strip()}"[:300])
                self.flash("Service installed" if out.returncode == 0 else "Service install failed (see log)")
            except (OSError, subprocess.SubprocessError) as exc:
                LOG.error(f"install-service failed: {exc}")
                self.flash("Service install failed")
            finally:
                self.installing = False
        threading.Thread(target=install, daemon=True).start()

    # -- drawing ----------------------------------------------------------------

    def tick(self, dt: float) -> None:
        status = appdata.read_json(self.data / "status.json", {}) or {}
        now = time.time()
        if self.message and time.monotonic() > self.message_until:
            self.message = ""
        key = (status.get("heartbeat"), self.message, self.installing, int(now) // 5,
               float(self.control().get("paused_until", 0) or 0) > now)
        if key != self.last_draw_key:
            self.last_draw_key = key
            self.draw(status, now)

    def service_state(self, status: dict, now: float) -> tuple[str, int]:
        if self.installing:
            return "installing", ui.WARN
        beat = status.get("heartbeat")
        if not beat or now - beat > 3 * float(status.get("check_s", 30)) + 15:
            return "service down", ui.ERR
        if float(self.control().get("paused_until", 0) or 0) > now:
            return "paused", ui.WARN
        return "watching", ui.OK

    def draw(self, status: dict, now: float) -> None:
        fb = self.ctx.fb
        fb.clear(ui.BG)
        state, state_color = self.service_state(status, now)
        ui.draw_header(fb, self.header, "Screen Watch", right=state, color=state_color)

        # Target tile: colour swatch of the last frame
        if status.get("signal"):
            w, h = status.get("resolution", [0, 0])
            ui.draw_stat_tile(fb, self.target_box, "Target", f"{h}p" if h else "video", f"{w}x{h}", ui.OK)
            if status.get("mean_rgb"):
                r, g, b = status["mean_rgb"]
                box = self.target_box
                fb.fill_rect(box.x + box.w - 30, box.y + 8, 22, 22, ui.rgb565(r, g, b))
        else:
            ui.draw_stat_tile(fb, self.target_box, "Target", "None", "no video", ui.WARN, ui.WARN)

        active = [ACTIVE_NAMES.get(k, k) for k in status.get("active", [])]
        channels = status.get("channels", [])
        if status.get("config_error"):
            ui.draw_stat_tile(fb, self.alerts_box, "Alerts", "Setup", "fix notify URL", ui.ERR, ui.ERR)
        elif active:
            ui.draw_stat_tile(fb, self.alerts_box, "Alerts", active[0], f"{len(active)} active", ui.ERR, ui.ERR)
        else:
            detail = f"{len(channels)} channel{'s' if len(channels) != 1 else ''}" if channels else "no channels!"
            ui.draw_stat_tile(fb, self.alerts_box, "Alerts", "Clear", detail,
                              ui.OK if channels else ui.WARN)

        lines = []
        if self.message:
            lines.append((self.message, ui.ACCENT))
        if status.get("config_error"):
            lines.append((status["config_error"], ui.ERR))
        for event in reversed(status.get("events", [])[-4:]):
            text = f"{ago(now - event.get('t', now)):>4} {event.get('title', '')}"
            lines.append((text, LEVEL_COLORS.get(event.get("level"), ui.TEXT_DIM)))
        if not lines:
            lines.append(("No events yet", ui.TEXT_DIM))
        y = self.events_box.y + 6
        for text, color in lines[:5]:
            ui.text(fb, self.events_box.x + 4, y, ui.truncate(text, self.events_box.w - 8), color)
            y += 13

        paused = float(self.control().get("paused_until", 0) or 0) > now
        ui.draw_button(fb, self.test_btn, "Test", ui.SURFACE, accent=ui.INFO, sublabel="send alert")
        ui.draw_button(fb, self.pause_btn, "Resume" if paused else "Pause", ui.SURFACE,
                       accent=ui.WARN, sublabel="alerts" if paused else "1 hour")


@app()
def main(ctx: AppContext) -> None:
    data = appdata.data_dir(APP_NAME)
    # Hand the launcher-provided settings to the service (it re-reads on change).
    appdata.write_json(data / "env.json", dict(ctx.env))
    panel = Panel(ctx, data)
    panel.ensure_service()
    LOG.info("panel opened")
    ctx.run(LOG.guard(panel.tick), fps=FPS, on_tap=LOG.guard(panel.on_tap))


if __name__ == "__main__":
    main()
