#!/usr/bin/env python3
"""AI Monitor panel: AI API spend, coding-agent sessions, AI control of this KVM
(with a kill switch), and local model servers. The work happens in a background
service; this panel shows its status.json and writes control.json.
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
from spend import money

APP_DIR = Path(__file__).resolve().parent
APP_NAME = "ai-monitor"
UNIT_PATH = Path("/etc/systemd/system/nanokvm-ai-monitor.service")
LOG = AppLog(APP_DIR)
HEADER_H, FOOTER_H, GAP = 28, 18, 4
CONFIRM_S = 3.0
STALE_S = 15.0
FPS = 4
TILES = ("spend", "agents", "kvm", "local")
STATE_COLORS = {"waiting": ui.ERR, "working": ui.INFO, "idle": ui.TEXT_DIM, "done": ui.OK}


def clean(status: dict) -> dict:
    """Coerce status.json into the shapes the drawing code expects (never crash the panel)."""
    def dicts(value) -> list:
        return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []

    def num(value) -> float:
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0

    mcp = status.get("mcp") if isinstance(status.get("mcp"), dict) else {}
    counts = status.get("agent_counts") if isinstance(status.get("agent_counts"), dict) else {}
    return {
        "listening": str(status.get("listening") or "?"), "bind_error": str(status.get("bind_error") or ""),
        "fingerprint": str(status.get("fingerprint") or ""),
        "agent_counts": {k: int(num(v)) for k, v in counts.items()},
        "agents": [{"machine": str(a.get("machine", "?")), "project": str(a.get("project") or ""),
                    "state": str(a.get("state", "?")), "since": num(a.get("since"))} for a in dicts(status.get("agents"))],
        "mcp": {"active": bool(mcp.get("active")), "watching": bool(mcp.get("watching")),
                "recent": [{"tool": str(e.get("tool", "?")), "ok": bool(e.get("ok")), "t": num(e.get("t"))}
                           for e in dicts(mcp.get("recent"))]},
        "spend": [{"provider": str(s.get("provider", "?")), "today_usd": num(s.get("today_usd")),
                   "month_usd": num(s.get("month_usd")), "detail": str(s.get("detail") or ""),
                   "error": str(s.get("error") or "")} for s in dicts(status.get("spend"))],
        "local": [{"name": str(s.get("name", "?")), "summary": str(s.get("summary") or ""),
                   "detail": str(s.get("detail") or ""), "error": str(s.get("error") or ""),
                   "rows": [r for r in s.get("rows", []) if isinstance(r, (list, tuple)) and len(r) == 2]
                   if isinstance(s.get("rows"), list) else []} for s in dicts(status.get("local"))],
    }


def ago(seconds: float) -> str:
    seconds = int(max(0, seconds))
    return f"{seconds}s" if seconds < 90 else f"{seconds // 60}m" if seconds < 5400 else f"{seconds // 3600}h"


class Panel:
    def __init__(self, ctx: AppContext, data: Path) -> None:
        self.ctx, self.data = ctx, data
        self.header, rest = ui.screen(ctx).split_top(HEADER_H)
        self.body, self.footer = rest.split_bottom(FOOTER_H)
        self.cells = ui.grid(self.body.inset(GAP), 2, 2, GAP)
        self.page: str | None = None
        self.armed_until = 0.0
        self.installing = False
        self.key = None

    # -- control -----------------------------------------------------------------

    def control(self) -> dict:
        control = appdata.read_json(self.data / "control.json", {})
        return control if isinstance(control, dict) else {}

    def set_kill(self, kill: bool) -> None:
        appdata.write_json(self.data / "control.json", {**self.control(), "kill": kill})
        LOG.info("kill switch " + ("ON" if kill else "off"))

    def kill_button(self) -> ui.Box:
        area = self.body.inset(GAP)
        return ui.Box(area.x, area.y, area.w, 50)

    def on_tap(self, x: int, y: int) -> None:
        if self.header.contains(x, y):
            self.page, self.armed_until = None, 0.0
        elif self.page is None:
            cell = ui.hit(self.cells, x, y)
            if cell is not None:
                self.page = TILES[cell]
        elif self.page == "kvm" and self.kill_button().contains(x, y):
            if not self.control().get("kill"):
                self.set_kill(True)                       # emergency stop: one tap
            elif time.monotonic() < self.armed_until:
                self.set_kill(False)                      # resume: needs a second tap
                self.armed_until = 0.0
            else:
                self.armed_until = time.monotonic() + CONFIRM_S
        elif self.page == "spend" and self.footer.contains(x, y):
            appdata.write_json(self.data / "control.json", {**self.control(), "test_id": uuid.uuid4().hex})
        self.key = None

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

    # -- drawing -------------------------------------------------------------------

    def tick(self, dt: float) -> None:
        status = appdata.read_json(self.data / "status.json", {})
        status = status if isinstance(status, dict) else {}
        beat = status.get("heartbeat")
        alive = isinstance(beat, (int, float)) and time.time() - beat < STALE_S
        armed = time.monotonic() < self.armed_until
        key = (beat, alive, self.page, armed, self.installing, bool(self.control().get("kill")))
        if key != self.key:
            self.key = key
            self.draw(clean(status), armed, alive)

    def draw(self, status: dict, armed: bool, alive: bool) -> None:
        fb = self.ctx.fb
        fb.clear(ui.BG)
        if self.page is None:
            ui.draw_header(fb, self.header, "AI Monitor", right=time.strftime("%H:%M"), color=ui.VIOLET)
            for box, tile in zip(self.cells, TILES):
                self.draw_tile(box, tile, status)
        else:
            getattr(self, f"draw_{self.page}")(status, armed)
        if self.installing:
            foot, color = "Installing service...", ui.WARN
        elif not alive:
            foot, color = "Service not running", ui.ERR
        elif status.get("bind_error"):
            foot, color = status["bind_error"], ui.ERR
        elif self.page == "spend":
            foot, color = "Tap here to send a test alert", ui.TEXT_DIM
        else:
            foot, color = f"https://{status['listening']}", ui.TEXT_DIM
        ui.text(fb, self.footer.x + 6, self.footer.cy - 4, ui.truncate(foot, self.footer.w - 12), color)

    def draw_tile(self, box: ui.Box, tile: str, status: dict) -> None:
        fb = self.ctx.fb
        if tile == "spend":
            items = [s for s in status.get("spend", []) if not s.get("error")]
            if not status.get("spend"):
                return ui.draw_stat_tile(fb, box, "Spend today", "--", "add API keys", ui.SURFACE_HI)
            today = sum(s.get("today_usd", 0) for s in items)
            month = sum(s.get("month_usd", 0) for s in items)
            errors = len(status["spend"]) - len(items)
            ui.draw_stat_tile(fb, box, "Spend today", money(today), f"month {money(month)}",
                              ui.WARN if errors else ui.OK)
        elif tile == "agents":
            counts = status.get("agent_counts", {})
            waiting, working = counts.get("waiting", 0), counts.get("working", 0)
            if waiting:
                ui.draw_stat_tile(fb, box, "Agents", f"{waiting} WAIT", "needs you!", ui.ERR, ui.ERR)
            else:
                total = sum(counts.values())
                ui.draw_stat_tile(fb, box, "Agents", f"{working} busy", f"{total} sessions", ui.INFO)
        elif tile == "kvm":
            mcp = status["mcp"]
            if self.control().get("kill"):  # the panel's own setting: no lag waiting for the service
                ui.draw_stat_tile(fb, box, "KVM control", "PAUSED", "kill switch on", ui.VIOLET, ui.VIOLET)
            elif mcp.get("active"):
                ui.draw_stat_tile(fb, box, "KVM control", "AI ACTIVE", "tap to pause", ui.ERR, ui.ERR)
            elif mcp.get("watching"):
                ui.draw_stat_tile(fb, box, "KVM control", "watching", "AI viewing screen", ui.WARN, ui.WARN)
            else:
                ui.draw_stat_tile(fb, box, "KVM control", "idle", "no AI control", ui.OK)
        else:
            servers = status.get("local", [])
            up = [s for s in servers if not s.get("error")]
            if not servers:
                return ui.draw_stat_tile(fb, box, "Local LLMs", "--", "add server URLs", ui.SURFACE_HI)
            ui.draw_stat_tile(fb, box, "Local LLMs", f"{len(up)}/{len(servers)} up",
                              up[0]["summary"] if up else "all down", ui.OK if len(up) == len(servers) else ui.WARN)

    def rows(self, lines: list[tuple[str, str, int]], start_y: int | None = None) -> None:
        fb = self.ctx.fb
        area = self.body.inset(8, GAP)
        y = start_y if start_y is not None else area.y + 2
        for label, value, color in lines:
            if y + 8 > area.y + area.h:
                break
            value = ui.truncate(value, area.w // 2)
            label_color = ui.TEXT if color else ui.INFO  # colour 0: a full-width value line
            ui.text(fb, area.x, y, ui.truncate(label, area.w - len(value) * 8 - 8), label_color)
            ui.text(fb, area.x + area.w - len(value) * 8, y, value, color)
            y += 13

    def draw_spend(self, status: dict, armed: bool) -> None:
        ui.draw_header(self.ctx.fb, self.header, "< Spend", right="UTC day / month", color=ui.OK)
        lines = []
        for s in status.get("spend", []):
            if s.get("error"):
                lines.append((s["provider"], "error", ui.ERR))
                lines.append(("  " + s["error"].split(": ", 1)[-1], "", 0))  # full width: the reason matters
            else:
                lines.append((s["provider"], f"{money(s['today_usd'])} / {money(s['month_usd'])}", ui.TEXT_DIM))
                if s.get("detail"):
                    lines.append(("  " + s["detail"], "", ui.TEXT_DIM))
        self.rows(lines or [("No providers configured", "", ui.TEXT_DIM)])

    def draw_agents(self, status: dict, armed: bool) -> None:
        ui.draw_header(self.ctx.fb, self.header, "< Agents", right=f"{len(status.get('agents', []))} sessions",
                       color=ui.INFO)
        now = time.time()
        lines = [(f"{a['machine']} {a.get('project') or ''}".strip(), f"{a['state']} {ago(now - a.get('since', now))}",
                  STATE_COLORS.get(a["state"], ui.TEXT_DIM)) for a in status.get("agents", [])[:6]]
        token = appdata.read_json(self.data / "agent_token.json", {})
        token = token.get("token", "") if isinstance(token, dict) else ""
        # setup values are long: give each its own full-width line under its label
        lines += [("", "", ui.TEXT_DIM), ("Hook URL:", "", ui.TEXT_DIM), (f"https://{status.get('listening', '?')}", "", 0),
                  ("Token:", "", ui.TEXT_DIM), (token or "(from AGENT_TOKEN setting)", "", 0),
                  ("Cert SHA-256:", status.get("fingerprint", ""), ui.TEXT_DIM)]
        self.rows(lines)

    def draw_kvm(self, status: dict, armed: bool) -> None:
        fb = self.ctx.fb
        mcp = status["mcp"]
        ui.draw_header(fb, self.header, "< KVM control", right="watched MCP", color=ui.ERR)
        button = self.kill_button()
        if self.control().get("kill"):
            label, bg = ("Tap again to RESUME" if armed else "AI PAUSED - tap to resume"), ui.VIOLET
        else:
            label, bg = "PAUSE AI CONTROL", ui.ERR
        ui.draw_button(fb, button, label, bg, max_scale=2)
        now = time.time()
        lines = [(f"{e.get('tool', '?')}", f"{'blocked ' if not e.get('ok') else ''}{ago(now - e.get('t', now))}",
                  ui.TEXT_DIM if e.get("ok") else ui.ERR) for e in mcp.get("recent", [])[:7]]
        lines = lines or [("No AI calls through :8790/mcp yet", "", ui.TEXT_DIM)]
        lines.append(("Direct /api/mcp is not covered", "", ui.TEXT_DIM))
        self.rows(lines, start_y=button.y + button.h + 8)

    def draw_local(self, status: dict, armed: bool) -> None:
        ui.draw_header(self.ctx.fb, self.header, "< Local LLMs", color=ui.OK)
        lines = []
        for s in status.get("local", []):
            lines.append((s["name"], s.get("error") or f"{s['summary']} {s.get('detail', '')}".strip(),
                          ui.ERR if s.get("error") else ui.OK))
            lines += [("  " + str(r[0]), str(r[1]), ui.TEXT_DIM) for r in s.get("rows", [])[:4]]
        self.rows(lines or [("No servers configured", "", ui.TEXT_DIM)])


@app()
def main(ctx: AppContext) -> None:
    data = appdata.data_dir(APP_NAME)
    if appdata.read_json(data / "env.json") != dict(ctx.env):  # unchanged: don't wake the service
        appdata.write_json(data / "env.json", dict(ctx.env))
    panel = Panel(ctx, data)
    panel.ensure_service()
    LOG.info("panel opened")
    ctx.run(LOG.guard(panel.tick), fps=FPS, on_tap=LOG.guard(panel.on_tap))


if __name__ == "__main__":
    main()
