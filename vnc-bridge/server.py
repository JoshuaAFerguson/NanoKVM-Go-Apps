"""Threaded RFB server: KVM snapshots out (Tight JPEG passthrough), HID in.

Hardening (an authenticated client can type into the target, so the pre-auth
surface is kept small): one absolute deadline per handshake; capped pending
connections (total and per IP), reserved before a thread starts; only
authenticated sessions take client slots; login attempts count before the
password is checked (no parallel-guessing race); locked-out IPs are dropped at
accept; per-IP log rate limit; TCP keepalive plus an idle timeout.
"""

from __future__ import annotations

import hashlib
import os
import select
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

import keymap
import rfb
from hid import release
from vnc_config import VncConfig

AUTH_FAILS, AUTH_BLOCK_S = 5, 600
HANDSHAKE_TOTAL_S = 10.0
SEND_TIMEOUT_S = 10.0
IDLE_TIMEOUT_S = 300.0
MAX_PENDING_TOTAL, MAX_PENDING_PER_IP = 4, 2
SAME_FRAME_HOLD_S = 2.0       # an unchanged frame is resent at most this often
MSG_SIZES = {0: 19, 2: 3, 3: 9, 4: 7, 5: 5, 6: 7}  # body sizes after the type byte (fixed part)
MAX_CUT_TEXT = 64 * 1024
JPEG_QUALITY_ENCODINGS = range(-32, -22)   # Tight JPEG quality levels 0..9
LOG_PER_IP_PER_MIN = 6
SO_BINDTODEVICE = getattr(socket, "SO_BINDTODEVICE", 25)


@dataclass
class Frame:
    width: int
    height: int
    jpeg: bytes
    digest: bytes
    live: bool = True


class FrameSource:
    """Shares one capture between clients; at most one capture in flight."""

    def __init__(self, capture: Callable, placeholder: Frame, quality: int, max_fps: float,
                 has_signal: Callable[[], bool] = lambda: True) -> None:
        self._capture = capture
        self._has_signal = has_signal
        self._placeholder = placeholder
        self._quality = quality
        self._min_interval = 1.0 / max_fps
        self._lock = threading.Lock()
        self._frame: Frame | None = None
        self._at = 0.0
        self.errors = 0

    def get(self) -> Frame:
        with self._lock:
            now = time.monotonic()
            if self._frame is not None and now - self._at < self._min_interval:
                return self._frame
            if not self._has_signal():  # don't wait out a snapshot timeout with no video
                self._frame, self._at = self._placeholder, now
                return self._frame
            try:
                snap = self._capture(self._quality)
                self._frame = Frame(snap.width, snap.height, snap.jpeg, hashlib.sha1(snap.jpeg).digest())
            except Exception:  # socket gone, timeout, ...: show the placeholder
                self.errors += 1
                self._frame = self._placeholder
            self._at = now
            return self._frame


class AuthLimiter:
    """Per-IP attempts in a sliding window. An attempt counts when the challenge is
    issued (so parallel connections can't all guess before any failure lands);
    a successful login refunds only its own attempt."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._attempts: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, ip: str) -> list[float]:
        recent = [t for t in self._attempts.get(ip, []) if self._clock() - t < AUTH_BLOCK_S]
        if recent:
            self._attempts[ip] = recent
        else:
            self._attempts.pop(ip, None)
        return recent

    def blocked(self, ip: str) -> bool:
        with self._lock:
            return len(self._prune(ip)) >= AUTH_FAILS

    def begin(self, ip: str) -> bool:
        with self._lock:
            if len(self._prune(ip)) >= AUTH_FAILS:
                return False
            self._attempts.setdefault(ip, []).append(self._clock())
            return True

    def succeeded(self, ip: str) -> None:
        with self._lock:
            attempts = self._attempts.get(ip)
            if attempts:
                attempts.pop()
            self._prune(ip)


@dataclass
class ClientInfo:
    ip: str
    since: float
    frames: int = 0
    keys: int = 0
    stop: threading.Event = field(default_factory=threading.Event)


def _recv_exact(sock: socket.socket, n: int, deadline: float | None = None) -> bytes:
    data = b""
    while len(data) < n:
        if deadline is not None:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("handshake took too long")
            sock.settimeout(min(left, SEND_TIMEOUT_S))
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ConnectionError("client closed")
        data += chunk
    return data


def _enable_keepalive(sock: socket.socket) -> None:
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    for name, value in (("TCP_KEEPIDLE", 30), ("TCP_KEEPINTVL", 10), ("TCP_KEEPCNT", 3)):
        if hasattr(socket, name):
            try:
                sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, name), value)
            except OSError:
                pass


class Session:
    def __init__(self, sock: socket.socket, ip: str, server: "VncServer") -> None:
        self.sock, self.ip, self.server = sock, ip, server
        self.info = ClientInfo(ip, time.time())
        self.authenticated = False
        self.held: set[int] = set()       # keysyms this session holds down
        self.mouse_mask = 0
        self.mouse_xy = (0, 0)
        self.encodings: set[int] | None = None
        self.jpeg_ok = True
        self.width = self.height = 0
        self.wanted = False
        self.incremental = False
        self.sent_digest = b""
        self.sent_at = 0.0
        self.last_activity = time.monotonic()

    # -- handshake ------------------------------------------------------------

    def handshake(self) -> None:
        deadline = time.monotonic() + HANDSHAKE_TOTAL_S
        recv = lambda n: _recv_exact(self.sock, n, deadline)  # noqa: E731
        cfg = self.server.config
        self.sock.settimeout(SEND_TIMEOUT_S)
        self.sock.sendall(b"RFB 003.008\n")
        version = rfb.parse_version(recv(12))
        if version == (3, 3):
            self.sock.sendall(struct.pack(">I", rfb.SEC_VNC))
        else:
            self.sock.sendall(bytes([1, rfb.SEC_VNC]))
            if recv(1)[0] != rfb.SEC_VNC:
                raise rfb.RfbError("client chose an unsupported security type")
        if not self.server.limiter.begin(self.ip):
            raise rfb.RfbError("too many failed logins")
        challenge = os.urandom(16)
        self.sock.sendall(challenge)
        response = recv(16)
        if not rfb.check_auth(cfg.password, challenge, response):
            self.server.log_ip(self.ip, f"VNC auth failed from {self.ip}")
            self._fail(version, "authentication failed")
        self.server.limiter.succeeded(self.ip)
        if not self.server.promote(self):
            self._fail(version, f"server full ({cfg.max_clients} clients connected)")
        self.authenticated = True
        self.sock.sendall(struct.pack(">I", 0))
        recv(1)  # ClientInit (shared flag; we always share)
        frame = self.server.frames.get()
        self.width, self.height = frame.width, frame.height
        self.sock.sendall(rfb.server_init(self.width, self.height, self.server.name))
        self.sock.settimeout(SEND_TIMEOUT_S)

    def _fail(self, version, reason: str) -> None:
        raw = reason.encode()
        self.sock.sendall(struct.pack(">I", 1) + (struct.pack(">I", len(raw)) + raw if version == (3, 8) else b""))
        raise rfb.RfbError(reason)

    # -- messages ---------------------------------------------------------------

    def handle(self, kind: int) -> None:
        if kind not in MSG_SIZES:
            raise rfb.RfbError(f"unknown client message {kind}")
        body = _recv_exact(self.sock, MSG_SIZES[kind])
        if kind == 0:
            self.jpeg_ok = rfb.jpeg_compatible(body)
        elif kind == 2:
            count = struct.unpack(">H", body[1:3])[0]
            if count > 512:
                raise rfb.RfbError("too many encodings")
            self.encodings = set(rfb.parse_set_encodings(body + _recv_exact(self.sock, 4 * count)))
        elif kind == 3:
            incremental, *_ = rfb.parse_update_request(body)
            self.wanted, self.incremental = True, incremental
        elif kind == 4 and not self.server.config.view_only:
            down, keysym = rfb.parse_key_event(body)
            self.server.key(self, keysym, down)
        elif kind == 5 and not self.server.config.view_only:
            mask, x, y = rfb.parse_pointer_event(body)
            self.server.pointer(self, mask, x, y)
        elif kind == 6:  # ClientCutText: discard (no clipboard into the target)
            length = struct.unpack(">xxxI", body)[0]
            if length > MAX_CUT_TEXT:
                raise rfb.RfbError("clipboard text too large")
            while length:
                length -= len(_recv_exact(self.sock, min(4096, length)))

    def maybe_send(self) -> None:
        if not self.wanted or self.encodings is None:
            return  # wait for SetEncodings before the first frame
        if rfb.ENC_TIGHT not in self.encodings or not self.jpeg_ok \
                or not any(e in self.encodings for e in JPEG_QUALITY_ENCODINGS):
            raise rfb.RfbError("client must support Tight encoding with JPEG enabled "
                               "(TigerVNC, Remmina, noVNC...)")
        now = time.monotonic()
        if now - self.sent_at < 1.0 / self.server.config.max_fps:
            return
        frame = self.server.frames.get()
        if self.incremental and frame.digest == self.sent_digest and now - self.sent_at < SAME_FRAME_HOLD_S:
            return  # nothing changed; RFB lets us delay incremental updates
        if (frame.width, frame.height) != (self.width, self.height):
            if rfb.ENC_DESKTOP_SIZE not in self.encodings:
                raise rfb.RfbError("screen resolution changed; reconnect")
            self.sock.sendall(rfb.desktop_size_update(frame.width, frame.height))
            self.width, self.height = frame.width, frame.height
        self.sock.sendall(rfb.tight_jpeg_update(frame.width, frame.height, frame.jpeg))
        self.wanted, self.sent_digest, self.sent_at = False, frame.digest, now
        self.info.frames += 1

    def run(self) -> None:
        try:
            self.handshake()
            self.server.log(f"VNC client connected: {self.ip}")
            period = 1.0 / self.server.config.max_fps
            while not self.info.stop.is_set():
                ready, _, _ = select.select([self.sock], [], [], period / 2)
                if ready:
                    kind = self.sock.recv(1)
                    if not kind:
                        break
                    self.handle(kind[0])
                    self.last_activity = time.monotonic()
                elif time.monotonic() - self.last_activity > IDLE_TIMEOUT_S:
                    raise rfb.RfbError("idle timeout")
                self.server.hid.retry()
                self.maybe_send()  # also while input is streaming in
        except (OSError, ConnectionError, TimeoutError, rfb.RfbError, struct.error) as exc:
            self.server.log_ip(self.ip, f"VNC client {self.ip}: {exc}")
        finally:
            if self.authenticated:
                self.server.release_session(self)
            try:
                self.sock.close()
            except OSError:
                pass


class VncServer:
    def __init__(self, config: VncConfig, frames: FrameSource, hid, name: str = "NanoKVM",
                 log: Callable[[str], None] = print, limiter: AuthLimiter | None = None) -> None:
        self.config, self.frames, self.hid, self.name, self.log = config, frames, hid, name, log
        self.limiter = limiter or AuthLimiter()
        self.sessions: list[Session] = []
        self._pending: dict[str, int] = {}
        self._lock = threading.Lock()
        self._input_lock = threading.Lock()
        self._keyboard = keymap.Keyboard()   # shared: one physical keyboard
        self._mouse = rfb.Mouse()
        self._stop = threading.Event()
        self._log_times: dict[str, list[float]] = {}
        self.listeners: list[socket.socket] = []

    # -- bookkeeping ------------------------------------------------------------

    def clients(self) -> list[ClientInfo]:
        with self._lock:
            return [s.info for s in self.sessions]

    def pending_count(self) -> int:
        with self._lock:
            return sum(self._pending.values())

    def _reserve(self, ip: str) -> bool:
        with self._lock:
            if sum(self._pending.values()) >= MAX_PENDING_TOTAL or self._pending.get(ip, 0) >= MAX_PENDING_PER_IP:
                return False
            self._pending[ip] = self._pending.get(ip, 0) + 1
            return True

    def _unreserve(self, ip: str) -> None:
        with self._lock:
            self._pending[ip] -= 1
            if not self._pending[ip]:
                del self._pending[ip]

    def promote(self, session: Session) -> bool:
        with self._lock:
            if len(self.sessions) >= self.config.max_clients:
                return False
            self.sessions.append(session)
            return True

    def log_ip(self, ip: str, message: str) -> None:
        now = time.monotonic()
        recent = [t for t in self._log_times.get(ip, []) if now - t < 60]
        if len(recent) < LOG_PER_IP_PER_MIN:
            recent.append(now)
            self.log(message)
        self._log_times[ip] = recent
        if len(self._log_times) > 256:  # bound memory under many source IPs
            self._log_times = {k: v for k, v in self._log_times.items() if v}

    def kick_all(self) -> None:
        for info in self.clients():
            info.stop.set()

    # -- shared input -------------------------------------------------------------

    def key(self, session: Session, keysym: int, down: bool) -> None:
        with self._input_lock:
            report = self._keyboard.event(keysym, down, owner=id(session))
            if report is None:
                return
            self.hid.keyboard.set_state(report)
            (session.held.add if down else session.held.discard)(keysym)
            session.info.keys += down

    def pointer(self, session: Session, mask: int, x: int, y: int) -> None:
        with self._input_lock:
            report = self._mouse.event(mask, x, y, session.width, session.height)
            self.hid.mouse.set_state(report)
            session.mouse_mask, session.mouse_xy = mask, (x, y)

    def release_session(self, session: Session) -> None:
        """Release only what this session holds (other clients keep their keys)."""
        with self._input_lock:
            for keysym in list(session.held):
                report = self._keyboard.event(keysym, False, owner=id(session))
                if report is not None:
                    release(self.hid.keyboard, report)
            session.held.clear()
            if session.mouse_mask & 0x07:
                x, y = session.mouse_xy
                release(self.hid.mouse, self._mouse.event(0, x, y, session.width or 1, session.height or 1))
        with self._lock:
            if session in self.sessions:
                self.sessions.remove(session)

    # -- network ---------------------------------------------------------------------

    def listen(self, address: str, device: str = "") -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if device:  # bind to the interface too: an IP alone is reachable from other interfaces
            sock.setsockopt(socket.SOL_SOCKET, SO_BINDTODEVICE, device.encode())
        sock.bind((address, self.config.port))
        sock.listen(2)
        self.listeners.append(sock)
        return sock

    def _serve_one(self, conn: socket.socket, ip: str) -> None:
        try:
            Session(conn, ip, self).run()
        finally:
            self._unreserve(ip)

    def accept_loop(self, sock: socket.socket) -> None:
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([sock], [], [], 1.0)
                if not ready:
                    continue
                conn, (ip, _) = sock.accept()
            except (OSError, ValueError):  # ValueError: listener closed during select
                if self._stop.is_set():
                    return
                time.sleep(1)
                continue
            if self.limiter.blocked(ip) or not self._reserve(ip):
                conn.close()  # locked out or too many pending: no banner, no thread
                continue
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            _enable_keepalive(conn)
            threading.Thread(target=self._serve_one, args=(conn, ip), daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self.kick_all()
        for sock in self.listeners:
            try:
                sock.close()
            except OSError:
                pass
