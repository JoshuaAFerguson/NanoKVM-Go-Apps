"""Client for the NanoKVM's own web API (the same calls the browser UI makes).

Reverse-engineered from the device's web bundle (not an official API):
  POST /api/auth/login {username, password}      -> session cookie nano-kvm-token
  GET  /api/storage/image                        -> data.files: [path, ...]
  GET  /api/storage/image/mounted                -> data.file: path or ""
  POST /api/storage/image/mount {file, cdrom, readOnly}   (file "" = eject)
  GET  /api/vm/finger-robot                      -> Finger Robot config
  POST /api/vm/finger-robot/press | /release
Every reply is {code, msg, data}; code 0 = success.

Only the local device is contacted (loopback, with its own certificate pinned).
"""

from __future__ import annotations

import http.client
import http.cookiejar
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import PurePosixPath
from typing import Callable

import localtls

LOCAL_URL = "https://127.0.0.1"
LOOPBACK = ("127.0.0.1", "localhost", "::1")
TIMEOUT_S = 10.0
MAX_BYTES = 256 * 1024
RELEASE_TRIES = 4
RELEASE_GAP_S = 0.5
AUTH_LATCH_S = 600          # after a rejected login, don't try again for 10 min (protects the web UI from lockout)


class KvmWebError(RuntimeError):
    """Short, user-facing failure (never contains the password)."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


def display_name(path: str) -> str:
    return PurePosixPath(path).name or path


def _looks_like_auth(reply: dict) -> bool:
    message = str(reply.get("msg") or "").lower()
    return any(word in message for word in ("unauthor", "token", "login", "session"))


class KvmWeb:
    def __init__(self, username: str, password: str, base_url: str = LOCAL_URL,
                 sleep: Callable[[float], None] | None = None) -> None:
        if urllib.parse.urlsplit(base_url).hostname not in LOOPBACK:
            raise ValueError("KvmWeb only talks to the local device")
        if not username or not password:
            raise KvmWebError("Set KVM_USERNAME and KVM_PASSWORD in the App settings")
        self._username, self._password = username, password
        self._base = base_url.rstrip("/")
        self._abort = threading.Event()
        self._sleep = sleep or (lambda seconds: self._abort.wait(seconds))  # interruptible hold
        self._jar = http.cookiejar.CookieJar()
        self._logged_in = False
        self._auth_failed: tuple[float, str] | None = None
        self._lock = threading.Lock()
        self.held = False
        # Loopback only; the device's own certificate is pinned (see localtls).
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=localtls.context()),
            urllib.request.HTTPCookieProcessor(self._jar), _NoRedirect())

    # -- transport -------------------------------------------------------------

    def _raw(self, method: str, path: str, body: dict | None = None) -> tuple[int, object]:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self._base + path, data, method=method,
                                         headers={"Content-Type": "application/json", "Accept": "application/json"})
        try:
            with self._opener.open(request, timeout=TIMEOUT_S) as response:
                status, raw = response.status, response.read(MAX_BYTES + 1)
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read(4096) if exc.fp else b""
            exc.close()
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            raise KvmWebError(f"KVM web API unreachable: {getattr(exc, 'reason', None) or type(exc).__name__}") from None
        if len(raw) > MAX_BYTES:
            raise KvmWebError("KVM web API reply too large")
        try:
            return status, json.loads(raw or b"null")
        except ValueError:
            return status, raw.decode("utf-8", "replace")

    def _login(self) -> None:
        if self._auth_failed and time.monotonic() - self._auth_failed[0] < AUTH_LATCH_S:
            raise KvmWebError(self._auth_failed[1])  # don't hammer the login (it rate-limits/locks)
        self._jar.clear()
        status, reply = self._raw("POST", "/api/auth/login", {"username": self._username, "password": self._password})
        if not isinstance(reply, dict):
            raise KvmWebError(f"login failed (HTTP {status})")
        data = reply.get("data") if isinstance(reply.get("data"), dict) else {}
        if data.get("requires2FA") or data.get("challenge"):
            self._latch("2FA is enabled on this KVM; this App can't complete a 2FA login")
        if data.get("rateLimited"):
            self._latch(f"web login is rate-limited; retry in {data.get('retryAfter', '?')}s")
        if reply.get("code") != 0:
            self._latch("login rejected: check KVM_USERNAME/KVM_PASSWORD (retrying in 10 min)")
        token = data.get("token")
        if isinstance(token, str) and token and not any(c.name == "nano-kvm-token" for c in self._jar):
            host = urllib.parse.urlsplit(self._base).hostname
            self._jar.set_cookie(http.cookiejar.Cookie(0, "nano-kvm-token", token, None, False, host, False, False,
                                                       "/", True, False, None, True, None, None, {}))
        self._logged_in = True
        self._auth_failed = None

    def _latch(self, message: str) -> None:
        self._auth_failed = (time.monotonic(), message)
        raise KvmWebError(message)

    def _call(self, method: str, path: str, body: dict | None = None):
        with self._lock:
            if not self._logged_in:
                self._login()
            status, reply = self._raw(method, path, body)
            retried = False
            if status == 401:  # session expired or rotated: log in again once
                self._login()
                status, reply = self._raw(method, path, body)
                retried = True
            if status == 401:
                raise KvmWebError("not authorized; check KVM_USERNAME/KVM_PASSWORD")
            if not isinstance(reply, dict):
                raise KvmWebError(f"{path}: unexpected reply (HTTP {status})")
            if reply.get("code") != 0 and _looks_like_auth(reply) and not retried:
                self._login()  # session expired, reported in the envelope instead of HTTP 401
                status, reply = self._raw(method, path, body)
            if not isinstance(reply, dict) or reply.get("code") != 0:
                message = reply.get("msg") if isinstance(reply, dict) else None
                raise KvmWebError(f"{str(message or 'request failed')[:80]}")
            return reply.get("data")

    # -- API ---------------------------------------------------------------------

    def images(self) -> list[str]:
        data = self._call("GET", "/api/storage/image")
        files = data.get("files") if isinstance(data, dict) else None
        return [f for f in files if isinstance(f, str)] if isinstance(files, list) else []

    def mounted(self) -> str:
        data = self._call("GET", "/api/storage/image/mounted")
        file = data.get("file") if isinstance(data, dict) else ""
        return file if isinstance(file, str) else ""

    def mount(self, path: str, cdrom: bool = True, read_only: bool = True) -> None:
        self._call("POST", "/api/storage/image/mount", {"file": path, "cdrom": cdrom, "readOnly": read_only})

    def eject(self) -> None:
        self.mount("", cdrom=True, read_only=True)

    def robot_available(self) -> bool:
        try:
            data = self._call("GET", "/api/vm/finger-robot")
        except KvmWebError:
            return False
        return isinstance(data, dict) and data.get("enabled") is not False

    def power_press(self, hold_s: float) -> None:
        """Press the target's power button with the Finger Robot, hold, then release.

        The release is always attempted (even if the press timed out or the wait is
        interrupted) and retried; if it still fails, the error says so loudly.
        """
        self._abort.clear()
        self.held = True
        try:
            self._call("POST", "/api/vm/finger-robot/press", {})
            self._sleep(hold_s)
        finally:
            self.release()

    def release(self) -> None:
        last = None
        for attempt in range(RELEASE_TRIES):
            try:
                self._call("POST", "/api/vm/finger-robot/release", {})
                self.held = False
                return
            except KvmWebError as exc:
                last = exc
                time.sleep(RELEASE_GAP_S * (attempt + 1))
        raise KvmWebError(f"RELEASE FAILED - check the Finger Robot ({last})")

    def abort_hold(self) -> None:
        """Cut a running hold short (e.g. the App is exiting); power_press then releases."""
        self._abort.set()
