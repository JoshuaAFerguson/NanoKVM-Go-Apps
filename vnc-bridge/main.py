#!/usr/bin/env python3
"""VNC Bridge panel: where to connect, who is connected, and a Disconnect-all button.
The VNC server itself runs as a background service (works with this App closed).
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
APP_NAME = "vnc-bridge"
UNIT_PATH = Path("/etc/systemd/system/nanokvm-vnc-bridge.service")
LOG = AppLog(APP_DIR)
HEADER_H, BUTTON_H, GAP, ROW_H = 28, 40, 4, 13
FPS = 4


def ago(seconds: float) -> str:
    seconds = int(max(0, seconds))
    return f"{seconds}s" if seconds < 90 else f"{seconds // 60}m" if seconds < 5400 else f"{seconds // 3600}h"


class Panel:
    def __init__(self, ctx: AppContext, data: Path) -> None:
        self.ctx, self.data = ctx, data
        self.header, rest = ui.screen(ctx).split_top(HEADER_H)
        self.body, buttons = rest.split_bottom(BUTTON_H + GAP)
        self.kick_btn = buttons.inset(GAP, 0).split_top(BUTTON_H)[0]
        self.installing = False
        self.message, self.message_until = "", 0.0
        self.key = None

    def on_tap(self, x: int, y: int) -> None:
        if self.kick_btn.contains(x, y):
            control = appdata.read_json(self.data / "control.json", {}) or {}
            appdata.write_json(self.data / "control.json", {**control, "kick_id": uuid.uuid4().hex})
            self.message, self.message_until = "Disconnecting all clients...", time.monotonic() + 4
            LOG.info("disconnect all requested")

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
        key = (status.get("heartbeat"), self.message, self.installing, int(time.time()) // 10)
        if key != self.key:
            self.key = key
            self.draw(status)

    def draw(self, status: dict) -> None:
        fb = self.ctx.fb
        fb.clear(ui.BG)
        now = time.time()
        alive = status.get("heartbeat") and now - status["heartbeat"] < 15
        clients = status.get("clients", []) if alive else []
        if self.installing:
            state, color = "installing", ui.WARN
        elif not alive:
            state, color = "service down", ui.ERR
        elif status.get("error") and not status.get("listening"):
            state, color = "not listening", ui.ERR
        else:
            state, color = (f"{len(clients)} connected" if clients else "ready"), ui.OK
        ui.draw_header(fb, self.header, "VNC Bridge", right=state, color=color)

        area = self.body.inset(8, 6)
        y = area.y
        lines: list[tuple[str, int, int]] = []
        for addr in status.get("listening", []):
            lines.append((addr, ui.TEXT, 2))
        if not status.get("listening"):
            lines.append(("Not listening", ui.ERR, 2))
        if status.get("error"):
            lines += [(line, ui.ERR, 1) for line in ui.wrap(status["error"], area.w)[:3]]
        mode = f"bind: {status.get('bind', '?')}" + ("  view-only" if status.get("view_only") else "")
        lines.append((mode, ui.TEXT_DIM, 1))
        lines.append(("", ui.TEXT_DIM, 1))
        for c in clients[:3]:
            lines.append((f"{c.get('ip', '?')}  {ago(now - c.get('since', now))}  {c.get('frames', 0)} frames",
                          ui.OK, 1))
        if alive and not clients:
            lines.append(("No clients. Use TigerVNC, Remmina, noVNC...", ui.TEXT_DIM, 1))
        if self.message:
            lines.append((self.message, ui.ACCENT, 1))
        for text, col, scale in lines:
            if y + 8 * scale > area.y + area.h:
                break
            ui.text(fb, area.x, y, ui.truncate(text, area.w, scale), col, scale)
            y += 8 * scale + 5
        ui.draw_button(fb, self.kick_btn, "Disconnect all", ui.SURFACE, accent=ui.ERR)


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
