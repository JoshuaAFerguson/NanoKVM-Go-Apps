#!/usr/bin/env python3
"""VNC Bridge background service (systemd: nanokvm-vnc-bridge.service).

Serves the KVM's target over VNC: hardware JPEG snapshots go out as Tight
rectangles (no re-encoding), keyboard/mouse come back as USB HID reports.
Talks to the touchscreen App via JSON files in /var/lib/nanokvm-apps/vnc-bridge/:
status.json (service -> App), control.json (App -> service: disconnect all),
env.json (settings the App saw from the launcher).
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
from pathlib import Path

import appdata
import vnc_config as config
import netinfo
import server
import servicekit
import vinsnap
from applog import AppLog
from hid import Hid

APP_DIR = Path(__file__).resolve().parent
APP_NAME = "vnc-bridge"
UNIT = "nanokvm-vnc-bridge.service"
STATUS_EVERY_S = 2.0
RETRY_BIND_S = 30.0
LOG = AppLog(APP_DIR, "service-log.txt")


def placeholder() -> server.Frame:
    jpeg = (APP_DIR / "assets" / "no-video.jpg").read_bytes()
    return server.Frame(1280, 720, jpeg, b"placeholder", live=False)


def bind_targets(bind: str) -> list[tuple[str, str]]:
    """(address, interface) pairs. Named modes also bind to the interface, because a
    socket bound only to an IP is reachable through other interfaces on Linux."""
    if bind == "all":
        return [("0.0.0.0", "")]
    if bind == "tailscale":
        ip = netinfo.interface_ipv4("tailscale0")
        return [(ip, "tailscale0")] if ip else []
    if bind == "lan":
        iface = netinfo.lan_interface()
        return [(netinfo.interface_ipv4(iface), iface)] if iface else []
    return [(bind, "")]


class Daemon:
    def __init__(self, data: Path) -> None:
        self.data = data
        self.presence = servicekit.AppPresence(APP_DIR, "vncd.py")
        self.limiter = server.AuthLimiter()  # survives reconfiguration
        self.targets: list[tuple[str, str]] = []
        self.vnc: server.VncServer | None = None
        self.config: config.VncConfig | None = None
        self.listening: list[str] = []
        self.error = ""
        self.env_stamp = None
        self.kick_seen = None

    def _stamp(self):
        def mtime(p: Path) -> float:
            try:
                return p.stat().st_mtime
            except OSError:
                return 0.0
        return mtime(APP_DIR / "app.json"), mtime(self.data / "env.json")

    def reconfigure(self) -> None:
        stamp = self._stamp()
        if stamp == self.env_stamp:
            return
        self.env_stamp = stamp
        try:
            new = config.load(servicekit.resolve_env(APP_DIR, self.data))
        except config.ConfigError as exc:
            self.stop_server()
            self.error = str(exc)
            LOG.warn(f"config: {exc}")
            return
        if new == self.config and self.listening:
            return
        self.stop_server()
        self.config = new
        frames = server.FrameSource(lambda q: vinsnap.capture(quality=q), placeholder(), new.quality, new.max_fps,
                                    has_signal=vinsnap.hdmi_signal)
        self.vnc = server.VncServer(new, frames, Hid(), name=socket.gethostname(), log=LOG.info,
                                    limiter=self.limiter)
        if new.bind == "all":
            LOG.warn("VNC_BIND=all: reachable on every interface; VNC auth is weak - prefer tailscale")
        self.start_listening()

    def start_listening(self) -> None:
        if self.vnc is None or self.config is None:
            return
        targets = bind_targets(self.config.bind)
        self.targets = targets
        if not targets:
            self.error = f"no address for VNC_BIND={self.config.bind} yet (is it up?)"
            return
        self.listening, self.error = [], ""
        for address, device in targets:
            try:
                sock = self.vnc.listen(address, device)
            except OSError as exc:
                self.error = f"cannot listen on {address}:{self.config.port}: {exc.strerror or exc}"
                LOG.warn(self.error)
                continue
            threading.Thread(target=self.vnc.accept_loop, args=(sock,), daemon=True).start()
            self.listening.append(f"{address}:{self.config.port}")
        if self.listening:
            LOG.info(f"listening on {', '.join(self.listening)} (bind={self.config.bind}, "
                     f"quality={self.config.quality}, view_only={self.config.view_only})")

    def stop_server(self) -> None:
        if self.vnc is not None:
            self.vnc.stop()
            self.vnc.hid.close()
        self.vnc, self.listening, self.targets = None, [], []

    def rebind_if_moved(self) -> None:
        """Tailscale/LAN addresses can change (DHCP, re-login): follow them."""
        if self.vnc is None or self.config is None or self.config.bind not in ("tailscale", "lan"):
            return
        current = bind_targets(self.config.bind)
        if current != self.targets and (current or self.listening):
            LOG.info(f"bind address changed {self.targets} -> {current}; restarting listener")
            self.env_stamp = None  # forces a clean rebuild on the next reconfigure()
            self.stop_server()
            self.config = None

    def handle_control(self) -> None:
        control = appdata.read_json(self.data / "control.json", {})
        kick = control.get("kick_id") if isinstance(control, dict) else None
        if isinstance(kick, str) and kick != self.kick_seen:
            if self.kick_seen is not None and self.vnc is not None:
                LOG.info("disconnecting all VNC clients (requested from the panel)")
                self.vnc.kick_all()
            self.kick_seen = kick

    def write_status(self) -> None:
        clients = [{"ip": c.ip, "since": c.since, "frames": c.frames, "keys": c.keys}
                   for c in (self.vnc.clients() if self.vnc else [])]
        status = {"heartbeat": time.time(), "listening": self.listening, "error": self.error,
                  "clients": clients, "pid": os.getpid(),
                  "bind": self.config.bind if self.config else "", "view_only": bool(self.config and self.config.view_only),
                  "signal": vinsnap.hdmi_signal()}
        appdata.write_json(self.data / "status.json", status, durable=False)

    def run(self) -> int:
        LOG.info(f"service started (pid {os.getpid()})")
        last_bind_try = 0.0
        while True:
            try:
                if self.presence.gone():
                    self.stop_server()
                    servicekit.remove_service(UNIT, self.data, LOG.info)
                    return 0
                self.rebind_if_moved()
                self.reconfigure()
                if self.vnc is not None and not self.listening and time.monotonic() - last_bind_try > RETRY_BIND_S:
                    last_bind_try = time.monotonic()
                    self.start_listening()
                self.handle_control()
                self.write_status()
            except Exception:
                LOG.exception("service loop error")
            time.sleep(STATUS_EVERY_S)


def main() -> int:
    try:
        data = appdata.data_dir(APP_NAME, allow_fallback=False)
    except OSError as exc:
        LOG.error(f"cannot use /var/lib/nanokvm-apps: {exc}")
        return 1
    return Daemon(data).run()


if __name__ == "__main__":
    sys.exit(main())
