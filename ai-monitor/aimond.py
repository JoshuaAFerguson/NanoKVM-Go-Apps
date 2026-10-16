#!/usr/bin/env python3
"""AI Monitor background service (systemd: nanokvm-ai-monitor.service).

- Listens (HTTPS, device certificate) for coding-agent check-ins from hooks
  and serves a watched MCP endpoint that forwards to this KVM's MCP server.
- Polls AI API spend (Anthropic, OpenAI, OpenRouter) and local model servers
  in a worker thread, so slow providers never delay the main loop.
- Publishes everything to status.json for the touchscreen panel. The kill
  switch is read by the HTTP handlers straight from the panel's control.json.
"""

from __future__ import annotations

import hashlib
import http.client
import os
import queue
import secrets
import ssl
import sys
import threading
import time
from pathlib import Path

import aiserver
import appdata
import localllm
import localtls
import netinfo
import servicekit
import spend
from alerter import Alerter
from applog import AppLog

APP_DIR = Path(__file__).resolve().parent
APP_NAME = "ai-monitor"
UNIT = "nanokvm-ai-monitor.service"
SERVER_CERT, SERVER_KEY = "/etc/kvm/server.crt", "/etc/kvm/server.key"
LOOP_S = 1.0
HEARTBEAT_S = 5.0             # status.json is rewritten on change, or at least this often
SPEND_EVERY_S, LOCAL_EVERY_S = 600.0, 15.0
RETRY_BIND_S = 30.0
RETRY_ALERTS_S = 60.0
NOTIFY_COOLDOWN_S = 60.0      # per session and kind: a flapping agent can't spam alerts
MIN_TOKEN_LEN = 24
DEFAULT_PORT = 8790
NEVER = float("-inf")
LOG = AppLog(APP_DIR, "service-log.txt")


def bind_target(mode: str) -> tuple[str, str]:
    """(address, interface): named modes also bind to the interface."""
    if mode == "all":
        return "0.0.0.0", ""
    if mode == "lan":
        iface = netinfo.lan_interface()
        return (netinfo.interface_ipv4(iface), iface) if iface else ("", "")
    if mode == "tailscale":
        ip = netinfo.interface_ipv4("tailscale0")
        return (ip, "tailscale0") if ip else ("", "")
    return mode, ""


def server_tls() -> ssl.SSLContext | None:
    if not (os.path.isfile(SERVER_CERT) and os.path.isfile(SERVER_KEY)):
        return None
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(SERVER_CERT, SERVER_KEY)
    return ctx


def cert_fingerprint() -> str:
    try:
        der = ssl.PEM_cert_to_DER_cert(Path(SERVER_CERT).read_text())
    except (OSError, ValueError):
        return ""
    return hashlib.sha256(der).hexdigest()[:16]


def upstream(timeout: float) -> http.client.HTTPConnection:
    return http.client.HTTPSConnection("127.0.0.1", 443, timeout=timeout, context=localtls.context())


class Poller(threading.Thread):
    """Fetches spend and local-server status off the main loop."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.lock = threading.Lock()
        self.env: dict = {}
        self.spend: list[spend.Spend] = []
        self.local: list[localllm.ServerStatus] = []
        self.refresh = threading.Event()
        self.servers = localllm.LocalServers()

    def configure(self, env: dict) -> None:
        with self.lock:
            self.env = dict(env)
        self.refresh.set()  # new keys/URLs: fetch now

    def run(self) -> None:
        spend_at = local_at = NEVER
        while True:
            forced = self.refresh.wait(LOCAL_EVERY_S)
            self.refresh.clear()
            with self.lock:
                env = dict(self.env)
            now = time.monotonic()
            try:
                if forced or now - local_at >= LOCAL_EVERY_S:
                    local_at = now
                    result = self.servers.poll(env, now)
                    with self.lock:
                        self.local = result
                if forced or now - spend_at >= SPEND_EVERY_S:
                    spend_at = now
                    result = spend.fetch_all(env, time.time())
                    with self.lock:
                        self.spend = result
            except Exception:
                LOG.exception("poller error")

    def snapshot(self) -> tuple[list, list]:
        with self.lock:
            return list(self.spend), list(self.local)


class Daemon:
    def __init__(self, data: Path) -> None:
        self.data = data
        self.presence = servicekit.AppPresence(APP_DIR, "aimond.py")
        status = appdata.read_json(data / "status.json", {})
        status = status if isinstance(status, dict) else {}
        self.alerter = Alerter(LOG.info, events=status.get("events"), pending=status.get("pending"))
        self.env: dict = {}
        self.env_stamp = None
        self.notes: queue.Queue = queue.Queue(maxsize=200)
        self.last_note: dict[tuple, float] = {}
        self.hub = aiserver.Hub("", self._enqueue, upstream=upstream, control_path=data / "control.json")
        self.server = None
        self.listening = ""
        self.bind_error = ""
        self.bound: tuple = ()
        self.fingerprint = cert_fingerprint()
        self.poller = Poller()
        self.test_done = self.control().get("test_id")  # don't resend an old test after a restart
        self.last_written: dict | None = None
        self.written_at = NEVER

    # -- configuration -----------------------------------------------------------

    def control(self) -> dict:
        control = appdata.read_json(self.data / "control.json", {})
        return control if isinstance(control, dict) else {}

    def agent_token(self) -> str:
        token = (self.env.get("AGENT_TOKEN") or "").strip()
        if len(token) >= MIN_TOKEN_LEN:
            return token
        if token:
            LOG.warn(f"AGENT_TOKEN is shorter than {MIN_TOKEN_LEN} characters: using the generated token")
        path = self.data / "agent_token.json"
        saved = appdata.read_json(path, {})
        if isinstance(saved, dict) and isinstance(saved.get("token"), str) and len(saved["token"]) >= MIN_TOKEN_LEN:
            return saved["token"]
        token = secrets.token_urlsafe(24)
        appdata.write_json(path, {"token": token})  # atomic, 0600; shown on the panel for hook setup
        LOG.info("generated a new agent token")
        return token

    def reload(self) -> None:
        def mtime(p: Path) -> float:
            try:
                return p.stat().st_mtime
            except OSError:
                return 0.0
        stamp = (mtime(APP_DIR / "app.json"), mtime(self.data / "env.json"))
        if stamp == self.env_stamp:
            return
        env = servicekit.resolve_env(APP_DIR, self.data)
        if env != self.env or self.env_stamp is None:
            self.env = env
            self.alerter.configure(env)
            self.hub.agent_token = self.agent_token()  # in place: open connections see it too
            self.poller.configure(env)
        self.env_stamp = stamp  # only after everything above succeeded

    # -- network -----------------------------------------------------------------

    def _enqueue(self, note) -> None:  # called from HTTP handler threads: never block
        try:
            self.notes.put_nowait(note)
        except queue.Full:
            pass

    def drain_notes(self, now: float) -> None:
        notify = (self.env.get("AGENT_NOTIFY") or "true").lower() not in ("0", "false", "no", "off")
        while True:
            try:
                note = self.notes.get_nowait()
            except queue.Empty:
                return
            s = note.session
            key = (s.machine, s.session, note.kind)
            if not notify or now - self.last_note.get(key, NEVER) < NOTIFY_COOLDOWN_S:
                continue
            self.last_note = {k: t for k, t in self.last_note.items() if now - t < NOTIFY_COOLDOWN_S}
            self.last_note[key] = now
            waiting = note.kind == "waiting"
            title = f"{s.machine}: agent needs you" if waiting else f"{s.machine}: agent finished"
            self.alerter.emit(title, f"{s.project or 'session'}: {s.message or s.state}", "alert" if waiting else "info")

    def wanted_bind(self) -> tuple:
        mode = (self.env.get("AI_BIND") or "tailscale").strip().lower()
        try:
            port = int(self.env.get("AI_PORT") or DEFAULT_PORT)
        except ValueError:
            port = DEFAULT_PORT
        return (mode, *bind_target(mode), port if 0 < port < 65536 else DEFAULT_PORT)

    def stop_server(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        self.listening = ""

    def bind(self, wanted: tuple) -> None:
        mode, address, device, port = wanted
        self.stop_server()
        self.bound, self.bind_error = wanted, ""
        if not address:
            self.bind_error = f"no address for AI_BIND={mode} yet"
            return
        try:
            tls = server_tls()
            if tls is None:  # fail closed: never relay tokens and MCP keys over plain HTTP
                self.bind_error = f"device certificate missing ({SERVER_CERT}): not listening"
                LOG.warn(self.bind_error)
                return
            self.server = aiserver.make_server(self.hub, address, port, tls, device)
        except (OSError, ssl.SSLError) as exc:
            self.bind_error = f"cannot listen on {address}:{port}: {getattr(exc, 'strerror', None) or exc}"
            LOG.warn(self.bind_error)
            return
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.listening = f"{address}:{port}"
        LOG.info(f"listening on https://{self.listening} (bind={mode})")

    # -- control and status --------------------------------------------------------------

    def handle_control(self) -> None:
        test_id = self.control().get("test_id")
        if isinstance(test_id, str) and test_id and test_id != self.test_done:
            self.test_done = test_id
            ok, summary = self.alerter.send("Test alert", "AI Monitor notifications are working.", "info")
            self.alerter.record("Test alert", "info", summary)

    def status(self, wall: float) -> dict:
        hub = self.hub
        with hub.lock:  # handler threads update these
            sessions, counts = hub.registry.sessions(wall), hub.registry.summary(wall)
            mcp = {"active": hub.activity.active(wall), "watching": hub.activity.watching(wall),
                   "blocked": hub.activity.blocked_calls, "unauthorized": hub.unauthorized,
                   "recent": hub.activity.recent(10)}
        mcp["kill"] = hub.kill_switch
        spent, local = self.poller.snapshot()
        return {
            "listening": self.listening, "bind_error": self.bind_error, "fingerprint": self.fingerprint,
            "token_from_setting": len((self.env.get("AGENT_TOKEN") or "").strip()) >= MIN_TOKEN_LEN,
            "agents": [{"machine": s.machine, "session": s.session[:8], "state": s.state, "project": s.project,
                        "message": s.message, "since": s.since} for s in sessions[:20]],
            "agent_counts": counts, "mcp": mcp,
            "spend": [s.__dict__ for s in spent],
            "local": [{"name": s.name, "summary": s.summary, "detail": s.detail, "rows": s.rows[:8],
                       "error": s.error} for s in local],
            "events": self.alerter.events, "pending": self.alerter.pending,
            "channels": self.alerter.channels, "config_error": self.alerter.config_error,
        }

    def write_status(self, now: float) -> None:
        status = self.status(time.time())
        if status == self.last_written and now - self.written_at < HEARTBEAT_S:
            return  # spare the flash: nothing new to say
        self.last_written, self.written_at = status, now
        appdata.write_json(self.data / "status.json", {**status, "heartbeat": time.time(), "pid": os.getpid()},
                           durable=False)

    def run(self) -> int:
        LOG.info(f"service started (pid {os.getpid()})")
        self.poller.start()
        last_bind_try = last_retry = NEVER
        while True:
            try:
                if self.presence.gone():
                    self.stop_server()
                    servicekit.remove_service(UNIT, self.data, LOG.info)
                    return 0
                self.reload()
                now = time.monotonic()
                wanted = self.wanted_bind()  # cheap ioctl; notices a new Tailscale/LAN address
                if wanted != self.bound or (self.server is None and now - last_bind_try >= RETRY_BIND_S):
                    last_bind_try = now
                    self.bind(wanted)
                self.drain_notes(now)
                self.handle_control()
                if self.alerter.pending and now - last_retry >= RETRY_ALERTS_S:
                    last_retry = now
                    self.alerter.retry_pending()
                self.write_status(now)
            except Exception:
                LOG.exception("service loop error")
            servicekit.wait(LOOP_S, self.data)


def main() -> int:
    try:
        data = appdata.data_dir(APP_NAME, allow_fallback=False)
    except OSError as exc:
        LOG.error(f"cannot use /var/lib/nanokvm-apps: {exc}")
        return 1
    return Daemon(data).run()


if __name__ == "__main__":
    sys.exit(main())
