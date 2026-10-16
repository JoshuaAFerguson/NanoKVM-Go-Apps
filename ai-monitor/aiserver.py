"""AI Monitor's network side: agent check-ins and the watched MCP proxy.

POST /v1/agent   Authorization: Bearer <AGENT_TOKEN>   {machine, session, state, ...}
ANY  /mcp        forwarded to the device's own MCP server (same API key),
                 logged by tool name, blocked while the kill switch is on.

Hardening: TLS handshakes run in the per-connection thread (an idle client
can't stall accept()), sockets time out, concurrent connections are capped, and
request bodies must have an exact Content-Length (no chunked uploads).
"""

from __future__ import annotations

import hmac
import http.client
import json
import os
import socket
import socketserver
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

import agents
import mcpwatch

MAX_CHECKIN = 4096
MAX_MCP_BODY = 256 * 1024
CHECKINS_PER_MIN = 120
MCP_PER_MIN = 600
MAX_RATE_IPS = 1024
MAX_CONNECTIONS = 12
SOCKET_TIMEOUT_S = 15
BODY_DEADLINE_S = 20
FORWARD_HEADERS = ("Authorization", "Content-Type", "Accept", "Mcp-Session-Id", "Mcp-Protocol-Version",
                   "Last-Event-ID")
RETURN_HEADERS = ("Content-Type", "Mcp-Session-Id", "Cache-Control")
UPSTREAM_TIMEOUT_S = 60
STREAM_TIMEOUT_S = 300   # GET opens a long-lived SSE stream
NO_BODY_STATUSES = (204, 304)
KILL_ERROR = {"jsonrpc": "2.0", "id": None,
              "error": {"code": -32001, "message": "AI control is paused on the NanoKVM (kill switch)"}}


class KillSwitch:
    """Reads the panel's control.json directly (cached by mtime), so a tap takes
    effect on the next request regardless of what the service loop is doing.
    Fails closed: a control file that exists but can't be parsed means paused."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.forced: bool | None = None   # tests / no control file
        self._stamp: tuple | None = None
        self._value = False
        self._lock = threading.Lock()

    def on(self) -> bool:
        if self.forced is not None or self.path is None:
            return bool(self.forced)
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            return False
        except OSError:
            return True
        stamp = (st.st_mtime_ns, st.st_size, st.st_ino)
        with self._lock:
            if stamp != self._stamp:
                try:
                    control = json.loads(Path(self.path).read_text(encoding="utf-8"))
                    self._value = bool(control.get("kill")) if isinstance(control, dict) else True
                except (OSError, ValueError):
                    self._value = True
                self._stamp = stamp
            return self._value


class RateLimiter:
    def __init__(self, per_min: int) -> None:
        self.per_min = per_min
        self._hits: dict[str, list[float]] = {}

    def allow(self, ip: str, now: float) -> bool:
        recent = [t for t in self._hits.get(ip, []) if now - t < 60]
        if ip not in self._hits and len(self._hits) >= MAX_RATE_IPS:
            self._hits = {k: v for k, v in self._hits.items() if v and now - v[-1] < 60}
            if len(self._hits) >= MAX_RATE_IPS:
                return False  # too many distinct clients: refuse new ones rather than forget old ones
        if len(recent) >= self.per_min:
            self._hits[ip] = recent
            return False
        self._hits[ip] = recent + [now]
        return True


class Hub:
    """Shared state the HTTP handlers update. One Hub lives as long as the service;
    settings (token) change in place, so open connections always see current state."""

    def __init__(self, agent_token: str, notify: Callable[[agents.Note], None],
                 upstream: Callable[[float], http.client.HTTPConnection], upstream_path: str = "/api/mcp",
                 control_path: Path | None = None) -> None:
        self.agent_token = agent_token
        self.notify = notify          # must be cheap and non-blocking (e.g. queue.put)
        self.upstream = upstream      # timeout -> connection to the device MCP server
        self.upstream_path = upstream_path
        self.registry = agents.Registry()
        self.activity = mcpwatch.Activity()
        self.kill = KillSwitch(control_path)
        self.unauthorized = 0
        self.lock = threading.Lock()
        self.checkin_rate = RateLimiter(CHECKINS_PER_MIN)
        self.mcp_rate = RateLimiter(MCP_PER_MIN)

    @property
    def kill_switch(self) -> bool:
        return self.kill.on()

    @kill_switch.setter
    def kill_switch(self, value: bool) -> None:
        self.kill.forced = value

    def allow(self, ip: str, now: float, kind: str = "checkin") -> bool:
        with self.lock:
            return (self.checkin_rate if kind == "checkin" else self.mcp_rate).allow(ip, now)

    def record(self, tools: list[str], ip: str, ok: bool, now: float) -> None:
        with self.lock:
            self.activity.record(tools, ip, ok=ok, now=now)


class Handler(BaseHTTPRequestHandler):
    server_version = "nanokvm-ai-monitor"
    protocol_version = "HTTP/1.1"
    timeout = SOCKET_TIMEOUT_S
    hub: Hub = None  # set by make_server

    def setup(self) -> None:
        tls = getattr(self.server, "tls", None)
        self.request.settimeout(SOCKET_TIMEOUT_S)
        if tls is not None:  # handshake here, in this connection's own thread
            self.request = tls.wrap_socket(self.request, server_side=True)
        super().setup()

    def finish(self) -> None:
        try:
            super().finish()
        finally:
            self.request.close()  # the TLS wrapper owns the descriptor now

    def log_message(self, *args) -> None:  # no per-request logging (flash wear, privacy)
        pass

    def _json(self, status: int, payload) -> None:
        body = json.dumps(payload).encode()
        if status >= 400:
            self.close_connection = True  # the request body may be unread: never reuse the connection
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _body(self, limit: int) -> tuple[int, bytes]:
        """(0, body) or (http error status, b'')."""
        if self.headers.get("Transfer-Encoding"):
            return 411, b""
        raw = (self.headers.get("Content-Length") or "0").strip()
        if not raw.isascii() or not raw.isdigit():
            return 400, b""
        length = int(raw)
        if length > limit:
            return 413, b""
        chunks, remaining, deadline = [], length, time.monotonic() + BODY_DEADLINE_S
        while remaining:
            if time.monotonic() > deadline:
                return 408, b""
            chunk = self.rfile.read(min(remaining, 16384))
            if not chunk:
                return 400, b""
            chunks.append(chunk)
            remaining -= len(chunk)
        return 0, b"".join(chunks)

    # -- agent check-ins -------------------------------------------------------

    def _checkin(self) -> None:
        hub, ip, now = self.hub, self.client_address[0], time.time()
        if not hub.allow(ip, now):
            return self._json(429, {"error": "slow down"})
        token = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        expected = hub.agent_token
        if not expected or not hmac.compare_digest(token.encode("utf-8", "replace"), expected.encode()):
            return self._json(401, {"error": "bad token"})
        status, body = self._body(MAX_CHECKIN)
        if status:
            return self._json(status, {"error": "bad body"})
        try:
            session = agents.validate(json.loads(body or b"null"))
        except (ValueError, RecursionError, agents.AgentError) as exc:
            return self._json(400, {"error": str(exc)[:100]})
        with hub.lock:
            notes = hub.registry.update(session, now)
        for note in notes:
            hub.notify(note)
        return self._json(200, {"ok": True})

    # -- watched MCP proxy ---------------------------------------------------------

    def _proxy(self) -> None:
        hub, ip, now = self.hub, self.client_address[0], time.time()
        if not hub.allow(ip, now, "mcp"):
            return self._json(429, {"error": "slow down"})
        body = b""
        if self.command == "POST":
            status, body = self._body(MAX_MCP_BODY)
            if status:
                return self._json(status, {"error": "bad body"})
        tools = mcpwatch.describe(body) if body else [self.command.lower()]
        if hub.kill_switch:
            hub.record(tools, ip, ok=False, now=now)
            return self._json(403, KILL_ERROR)
        headers = {h: self.headers[h] for h in FORWARD_HEADERS if self.headers.get(h)}
        conn = None
        try:
            conn = hub.upstream(STREAM_TIMEOUT_S if self.command == "GET" else UPSTREAM_TIMEOUT_S)
            conn.request(self.command, hub.upstream_path, body=body or None, headers=headers)
            resp = conn.getresponse()
        except (OSError, http.client.HTTPException) as exc:
            if conn is not None:
                conn.close()
            return self._json(502, {"error": f"device MCP unreachable: {type(exc).__name__}"})
        try:
            if resp.status in (401, 403):  # unauthenticated noise must not flush the real audit log
                with hub.lock:
                    hub.unauthorized += 1
            else:
                hub.record(tools, ip, ok=200 <= resp.status < 300, now=now)
            self._relay(resp)
        finally:
            conn.close()

    def _relay(self, resp: http.client.HTTPResponse) -> None:
        chunked = self.request_version == "HTTP/1.1" and resp.status not in NO_BODY_STATUSES
        self.send_response(resp.status)
        for name in RETURN_HEADERS:
            if resp.getheader(name):
                self.send_header(name, resp.getheader(name))
        if chunked:
            self.send_header("Transfer-Encoding", "chunked")
        else:
            self.close_connection = True
            self.send_header("Connection", "close")
            if resp.status in NO_BODY_STATUSES:
                self.send_header("Content-Length", "0")
        self.end_headers()
        if resp.status in NO_BODY_STATUSES:
            return
        try:
            while True:  # stream (screenshots can be large; SSE can be long-lived)
                chunk = resp.read1(65536)
                if not chunk:
                    break
                if self.hub.kill_switch:  # paused mid-stream: cut it off
                    self.close_connection = True
                    return
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk) if chunked else chunk)
                self.wfile.flush()
            if chunked:
                self.wfile.write(b"0\r\n\r\n")
        except (OSError, http.client.HTTPException):
            self.close_connection = True

    # -- routing ----------------------------------------------------------------------

    def _route(self, allowed: tuple[str, ...]) -> None:
        if self.path == "/mcp":
            return self._proxy()
        if self.path == "/v1/agent" and "checkin" in allowed:
            return self._checkin()
        if self.path == "/health" and "health" in allowed:
            return self._json(200, {"ok": True})
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 (http.server API)
        self._route(("checkin",))

    def do_GET(self) -> None:  # noqa: N802
        self._route(("health",))

    def do_DELETE(self) -> None:  # noqa: N802
        self._route(())


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16
    tls: ssl.SSLContext | None = None

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.slots = threading.BoundedSemaphore(MAX_CONNECTIONS)

    def server_bind(self) -> None:  # skip HTTPServer's reverse-DNS lookup (can stall)
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "localhost", self.server_address[1]

    def process_request(self, request, client_address) -> None:
        if not self.slots.acquire(blocking=False):  # at capacity: drop rather than queue threads
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address) -> None:  # failed handshakes, resets: not news
        pass


def make_server(hub: Hub, address: str, port: int, tls: ssl.SSLContext | None,
                device: str = "") -> Server:
    handler = type("BoundHandler", (Handler,), {"hub": hub})
    server = Server((address, port), handler, bind_and_activate=False)
    server.allow_reuse_address = True
    server.tls = tls
    try:
        if device:  # bind to the interface too: an IP alone is reachable via other interfaces
            server.socket.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_BINDTODEVICE", 25), device.encode())
        server.server_bind()
        server.server_activate()
    except OSError:
        server.server_close()
        raise
    return server
