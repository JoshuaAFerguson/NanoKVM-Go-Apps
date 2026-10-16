#!/usr/bin/env python3
"""Wake-on-LAN Panel: tap a machine to wake it; see which machines are up.

Hosts come from the WOL_HOSTS setting (Name=MAC@IP; ...). Hosts with an IP are
pinged so their tile shows online/offline and how long a wake took. With no
hosts configured, the screen lists LAN neighbours (IP + MAC) to copy from.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from appbase import AppContext, app

import netinfo
import ui
import wol
from applog import AppLog
from monitor import Monitor

APP_DIR = Path(__file__).resolve().parent
LOG = AppLog(APP_DIR)
COLS, ROWS = 2, 3
HEADER_H, FOOTER_H, GAP = 28, 18, 4
FPS = 5


def broadcast_addresses(extra: str) -> tuple[list[str], list[str]]:
    """(valid addresses, rejected user entries): global + local subnet + validated extras."""
    addresses = [wol.GLOBAL_BROADCAST]
    iface = netinfo.lan_interface()
    ip, mask = (netinfo.interface_ipv4(iface), netinfo.interface_netmask(iface)) if iface else ("", "")
    if ip and mask:
        addresses.append(wol.directed_broadcast(ip, mask))
    good, bad = wol.validate_broadcasts(extra.split(","))
    return addresses + good, bad


class Panel:
    def __init__(self, ctx: AppContext, monitor: Monitor) -> None:
        self.ctx = ctx
        self.monitor = monitor
        header, rest = ui.screen(ctx).split_top(HEADER_H)
        body, self.footer = rest.split_bottom(FOOTER_H)
        self.header = header
        self.cells = ui.grid(body.inset(GAP), COLS, ROWS, GAP)
        self.pages = [monitor.hosts[i:i + COLS * ROWS] for i in range(0, len(monitor.hosts), COLS * ROWS)]
        self.page = 0
        self.seen = -1
        self.second = -1
        self.notice = ""
        self.drawn_footer = ""
        self.dirty = True

    def on_tap(self, x: int, y: int) -> None:
        cell = ui.hit(self.cells, x, y)
        hosts = self.pages[self.page]
        if cell is not None and cell < len(hosts):
            threading.Thread(target=self._wake, args=(hosts[cell],), daemon=True).start()

    def _wake(self, host: wol.Host) -> None:
        try:
            self.monitor.wake(host)
        except Exception as exc:  # runs on its own thread, outside LOG.guard
            LOG.exception(f"wake {host.name} failed")
            self.monitor.set_message(f"Send failed: {exc}")

    def on_swipe(self, kind: str, x: int, y: int) -> None:
        if x < ui.EXIT_ZONE_PX or x >= self.ctx.width - ui.EXIT_ZONE_PX or len(self.pages) < 2:
            return
        step = {"up": 1, "down": -1}.get(kind)
        if step:
            self.page = (self.page + step) % len(self.pages)
            self.dirty = True

    def tick(self, dt: float) -> None:
        second = int(time.monotonic())
        waking = self.monitor.interval() < 5
        footer = self.footer_text()
        ticking = waking and second != self.second  # elapsed "waking Ns" counter
        if self.monitor.version != self.seen or self.dirty or ticking or footer != self.drawn_footer:
            self.seen, self.second, self.dirty, self.drawn_footer = self.monitor.version, second, False, footer
            self.draw(time.monotonic(), footer)

    def footer_text(self) -> str:
        return self.monitor.current_message() or self.notice or "Tap a machine to wake it"

    def draw(self, now: float, message: str) -> None:
        fb = self.ctx.fb
        fb.clear(ui.BG)
        right = f"{self.page + 1}/{len(self.pages)}" if len(self.pages) > 1 else ""
        ui.draw_header(fb, self.header, "Wake-on-LAN", right=right, color=ui.OK)
        for box, host in zip(self.cells, self.pages[self.page]):
            state = self.monitor.state(host.name)
            status = wol.status_text(state.up, state.waking_since, now, has_ip=bool(host.ip))
            if state.up:
                accent, color = ui.OK, ui.OK
            elif state.waking_since is not None:
                accent, color = ui.VIOLET, ui.VIOLET
            else:
                accent, color = ui.SURFACE_HI, ui.TEXT_DIM
            ui.draw_button(fb, box, host.name, ui.SURFACE, accent=accent, sublabel=" ")
            hint = box.inset(8, 5).split_bottom(8)[1]
            ui.draw_text_center(fb, hint, status, color, 1)
        ui.text(fb, self.footer.x + 6, self.footer.cy - 4, ui.truncate(message, self.footer.w - 12), ui.TEXT_DIM)


def draw_setup(ctx: AppContext, title: str, lines: list[str], color: int) -> None:
    fb = ctx.fb
    fb.clear(ui.BG)
    header, body = ui.screen(ctx).split_top(HEADER_H)
    ui.draw_header(fb, header, title, color=color)
    for n, line in enumerate(lines[:17]):
        ui.text(fb, body.x + 8, body.y + 8 + n * 12, line, ui.TEXT if n < 3 else ui.TEXT_DIM)


def setup_lines(problem: str) -> list[str]:
    lines = ui.wrap(problem, 268) + ["", "LAN neighbours (IP  MAC):"]
    arp = Path("/proc/net/arp")
    neighbours = wol.parse_arp(arp.read_text()) if arp.exists() else []
    rows = [f"{ip:<15} {mac}" for ip, mac in neighbours]
    return lines + (rows or ["(none seen yet)"])


@app()
def main(ctx: AppContext) -> None:
    try:
        hosts = wol.parse_hosts(ctx.env.get("WOL_HOSTS"))
    except wol.WolError as exc:
        LOG.warn(str(exc))
        draw_setup(ctx, "Fix WOL hosts", setup_lines(str(exc)), ui.ERR)
        ctx.run(lambda dt: None, fps=1)
        return
    if not hosts:
        draw_setup(ctx, "Wake-on-LAN", setup_lines(
            "Add machines in Settings > Apps > Installed > Wake-on-LAN, e.g. NAS=aa:bb:cc:dd:ee:ff@192.168.0.10"),
            ui.OK)
        ctx.run(lambda dt: None, fps=1)
        return

    broadcasts, rejected = broadcast_addresses(ctx.env.get("WOL_BROADCAST", ""))
    LOG.info(f"started: {len(hosts)} hosts, broadcasts {broadcasts}")
    if rejected:
        LOG.warn(f"ignored broadcast entries (IPv4 LAN addresses only): {rejected}")
    monitor = Monitor(hosts, ping=wol.ping_many, send=lambda mac: wol.send_wol(mac, broadcasts), log=LOG.info)
    monitor.start()
    panel = Panel(ctx, monitor)
    if rejected:
        panel.notice = f"Ignored broadcast: {', '.join(rejected)}"
    try:
        ctx.run(LOG.guard(panel.tick), fps=FPS, on_tap=LOG.guard(panel.on_tap), on_swipe=LOG.guard(panel.on_swipe))
    finally:
        monitor.stop()
        LOG.info("stopped")


if __name__ == "__main__":
    main()
