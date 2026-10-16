#!/usr/bin/env python3
"""Text Alert panel (Go+): watched phrases, recent matches, Test / Pause.
The matching itself runs in a background service.
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
APP_NAME = "text-alert"
UNIT_PATH = Path("/etc/systemd/system/nanokvm-text-alert.service")
LOG = AppLog(APP_DIR)
HEADER_H, BUTTON_H, GAP, ROW_H = 28, 44, 4, 13
PAUSE_S = 3600
FPS = 4


def safe_float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def ago(seconds: float) -> str:
    seconds = int(max(0, seconds))
    return f"{seconds}s" if seconds < 90 else f"{seconds // 60}m" if seconds < 5400 else f"{seconds // 3600}h"


class Panel:
    def __init__(self, ctx: AppContext, data: Path) -> None:
        self.ctx, self.data = ctx, data
        self.header, rest = ui.screen(ctx).split_top(HEADER_H)
        self.body, buttons = rest.split_bottom(BUTTON_H + GAP)
        self.test_btn, self.pause_btn = ui.grid(buttons.inset(GAP, 0).split_top(BUTTON_H)[0], 2, 1, GAP)
        self.installing = False
        self.message, self.message_until = "", 0.0
        self.key = None

    def control(self) -> dict:
        control = appdata.read_json(self.data / "control.json", {})
        return control if isinstance(control, dict) else {}

    def on_tap(self, x: int, y: int) -> None:
        control = self.control()
        if self.test_btn.contains(x, y):
            appdata.write_json(self.data / "control.json", {**control, "test_id": uuid.uuid4().hex})
            self.message = "Test alert requested..."
        elif self.pause_btn.contains(x, y):
            paused = safe_float(control.get("paused_until")) > time.time()
            appdata.write_json(self.data / "control.json",
                               {**control, "paused_until": 0 if paused else time.time() + PAUSE_S})
            self.message = "Alerts resumed" if paused else "Alerts paused for 1 hour"
        else:
            return
        self.message_until = time.monotonic() + 4
        LOG.info(self.message)

    def ensure_service(self) -> None:
        if UNIT_PATH.exists() or self.installing:
            return
        self.installing = True

        def install() -> None:
            env = {**os.environ, "NANOKVM_APP_DIR": str(APP_DIR), "NANOKVM_APP_ID": APP_NAME}
            try:
                out = subprocess.run(["/bin/bash", str(APP_DIR / "install-service.sh")], env=env,
                                     capture_output=True, text=True, timeout=60)
                LOG.info(f"install-service: rc={out.returncode} {out.stdout.strip()} {out.stderr.strip()}"[:300])
            except (OSError, subprocess.SubprocessError) as exc:
                LOG.error(f"install-service failed: {exc}")
            finally:
                self.installing = False
        threading.Thread(target=install, daemon=True).start()

    def tick(self, dt: float) -> None:
        status = appdata.read_json(self.data / "status.json", {}) or {}
        if self.message and time.monotonic() > self.message_until:
            self.message = ""
        paused = safe_float(self.control().get("paused_until")) > time.time()
        key = (status.get("heartbeat"), self.message, self.installing, paused, int(time.time()) // 10)
        if key != self.key:
            self.key = key
            self.draw(status, paused)

    def draw(self, status: dict, paused: bool) -> None:
        fb = self.ctx.fb
        fb.clear(ui.BG)
        now = time.time()
        alive = safe_float(status.get("heartbeat")) and now - safe_float(status["heartbeat"]) < 3 * (safe_float(status.get("poll_s")) or 20) + 15
        if self.installing:
            state, color = "installing", ui.WARN
        elif not alive:
            state, color = "service down", ui.ERR
        elif status.get("error") or status.get("config_error"):
            state, color = "needs setup", ui.ERR
        elif paused:
            state, color = "paused", ui.WARN
        else:
            state, color = "watching", ui.OK
        ui.draw_header(fb, self.header, "Text Alert", right=state, color=color)

        area = self.body.inset(8, 6)
        lines: list[tuple[str, int, int]] = []
        for problem in (status.get("error"), status.get("config_error")):
            if problem:
                lines += [(line, ui.ERR, 1) for line in ui.wrap(problem, area.w)[:3]]
        phrases = status.get("phrases", [])
        if phrases:
            lines.append((f"Watching {len(phrases)} phrase{'s' if len(phrases) != 1 else ''}:", ui.TEXT_DIM, 1))
            lines += [(f'"{p}"', ui.TEXT, 1) for p in phrases[:3]]
            if len(phrases) > 3:
                lines.append((f"+{len(phrases) - 3} more", ui.TEXT_DIM, 1))
        if self.message:
            lines.append((self.message, ui.ACCENT, 1))
        for event in reversed(status.get("events", [])[-4:]):
            color_ = ui.INFO if event.get("level") == "info" else ui.ERR
            lines.append((f"{ago(now - event.get('t', now)):>4} {event.get('title', '')}", color_, 1))
        y = area.y
        for text, col, scale in lines:
            if y + 8 * scale > area.y + area.h:
                break
            ui.text(fb, area.x, y, ui.truncate(text, area.w, scale), col, scale)
            y += ROW_H
        ui.draw_button(fb, self.test_btn, "Test", ui.SURFACE, accent=ui.INFO, sublabel="send alert")
        ui.draw_button(fb, self.pause_btn, "Resume" if paused else "Pause", ui.SURFACE, accent=ui.WARN,
                       sublabel="alerts" if paused else "1 hour")


@app()
def main(ctx: AppContext) -> None:
    data = appdata.data_dir(APP_NAME)
    appdata.write_json(data / "env.json", dict(ctx.env))
    panel = Panel(ctx, data)
    panel.ensure_service()
    LOG.info("panel opened")
    ctx.run(LOG.guard(panel.tick), fps=FPS, on_tap=LOG.guard(panel.on_tap))


if __name__ == "__main__":
    main()
