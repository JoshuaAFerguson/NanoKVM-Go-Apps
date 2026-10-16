"""Push notifications: ntfy, Discord webhook, Pushover, Home Assistant webhook.

Shared module: edit _shared/notify.py, then run `python3 _tools/sync_shared.py`.

Configured from App env settings (all optional; every configured channel gets
each message). Requests have a timeout, ignore env proxies, refuse redirects,
and errors never echo request headers or bodies (they hold secrets).
"""

from __future__ import annotations

import http.client
import json
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable

TIMEOUT_S = 10.0          # per socket operation
SEND_DEADLINE_S = 20.0    # whole send(), all channels in parallel
URL_CHARS_RE = re.compile(r"^[\x21-\x7e]+$")  # no spaces/control characters
PUSHOVER_API = "https://api.pushover.net/1/messages.json"
PUSHOVER_KEY_RE = re.compile(r"^[A-Za-z0-9]{30}$")
TOKEN_RE = re.compile(r"^[\x21-\x7e]{1,512}$")
MAX_MESSAGE = 900
PRIORITY = {"alert": ("4", "1"), "recovered": ("3", "0"), "info": ("3", "0")}  # (ntfy, pushover)
EMOJI_TAG = {"alert": "warning", "recovered": "white_check_mark", "info": "information_source"}

# transport(url, body, headers, timeout) -> HTTP status
Transport = Callable[[str, bytes, dict, float], int]


class NotifyError(ValueError):
    """Notification settings are invalid (shown on screen)."""


@dataclass(frozen=True)
class Channel:
    kind: str
    url: str
    secret: str = ""
    user: str = ""


def _http_url(env: dict, key: str) -> str:
    raw = (env.get(key) or "").strip()
    error = NotifyError(f"{key} must be an http(s) URL without credentials or spaces")
    if not URL_CHARS_RE.match(raw):
        raise error
    try:
        parts = urllib.parse.urlsplit(raw)
        host = parts.hostname
    except ValueError:  # e.g. "Invalid IPv6 URL"
        raise error from None
    if parts.scheme not in ("http", "https") or not host or parts.username or parts.password:
        raise error
    return raw


def load_channels(env: dict) -> list[Channel]:
    channels = []
    if (env.get("NTFY_URL") or "").strip():
        url = _http_url(env, "NTFY_URL")
        if not urllib.parse.urlsplit(url).path.strip("/"):
            raise NotifyError("NTFY_URL must include the topic, e.g. https://ntfy.sh/my-kvm-alerts")
        token = (env.get("NTFY_TOKEN") or "").strip()
        if token and not TOKEN_RE.match(token):
            raise NotifyError("NTFY_TOKEN must be a single token")
        if token and not url.startswith("https://"):
            raise NotifyError("NTFY_URL must use https when NTFY_TOKEN is set")
        channels.append(Channel("ntfy", url, token))
    if (env.get("DISCORD_WEBHOOK") or "").strip():
        url = _http_url(env, "DISCORD_WEBHOOK")
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" or parts.hostname not in ("discord.com", "discordapp.com") \
                or not parts.path.startswith("/api/webhooks/"):
            raise NotifyError("DISCORD_WEBHOOK must be https://discord.com/api/webhooks/...")
        channels.append(Channel("discord", url))
    if (env.get("PUSHOVER_TOKEN") or "").strip() or (env.get("PUSHOVER_USER") or "").strip():
        token, user = (env.get("PUSHOVER_TOKEN") or "").strip(), (env.get("PUSHOVER_USER") or "").strip()
        if not PUSHOVER_KEY_RE.match(token) or not PUSHOVER_KEY_RE.match(user):
            raise NotifyError("PUSHOVER_TOKEN and PUSHOVER_USER must both be 30-character keys")
        channels.append(Channel("pushover", PUSHOVER_API, token, user))
    if (env.get("HA_WEBHOOK_URL") or "").strip():
        url = _http_url(env, "HA_WEBHOOK_URL")
        if "/api/webhook/" not in urllib.parse.urlsplit(url).path:
            raise NotifyError("HA_WEBHOOK_URL must be http(s)://<ha>/api/webhook/<id>")
        channels.append(Channel("homeassistant", url))
    return channels


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ssl.create_default_context()), _NoRedirect())


def _urllib_transport(url: str, body: bytes, headers: dict, timeout: float) -> int:
    request = urllib.request.Request(url, body, headers=headers, method="POST")
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        exc.close()
        return exc.code


def _header_safe(text: str, limit: int = 200) -> str:
    """ASCII-only, no CR/LF: header injection and encoding errors are impossible."""
    cleaned = re.sub(r"[\r\n\t]+", " ", text).encode("ascii", "replace").decode()
    return cleaned[:limit]


class Notifier:
    def __init__(self, channels: list[Channel], transport: Transport | None = None, device: str = "NanoKVM") -> None:
        self.channels = channels
        self._transport = transport or _urllib_transport
        self.device = device

    def _request(self, ch: Channel, title: str, message: str, level: str) -> tuple[str, bytes, dict]:
        full_title = f"{self.device}: {title}"
        ntfy_priority, pushover_priority = PRIORITY.get(level, PRIORITY["info"])
        if ch.kind == "ntfy":
            headers = {"Title": _header_safe(full_title), "Priority": ntfy_priority,
                       "Tags": EMOJI_TAG.get(level, "information_source"), "Content-Type": "text/plain; charset=utf-8"}
            if ch.secret:
                headers["Authorization"] = f"Bearer {ch.secret}"
            return ch.url, message.encode(), headers
        if ch.kind == "discord":
            body = {"content": f"**{full_title}**\n{message}"[:1900], "allowed_mentions": {"parse": []}}
            return ch.url, json.dumps(body).encode(), {"Content-Type": "application/json"}
        if ch.kind == "pushover":
            form = {"token": ch.secret, "user": ch.user, "title": full_title[:250], "message": message,
                    "priority": pushover_priority}
            return ch.url, urllib.parse.urlencode(form).encode(), {"Content-Type": "application/x-www-form-urlencoded"}
        body = {"title": full_title, "message": message, "level": level, "device": self.device}
        return ch.url, json.dumps(body).encode(), {"Content-Type": "application/json"}

    def _send_one(self, ch: Channel, title: str, message: str, level: str) -> str:
        try:
            url, body, headers = self._request(ch, title, message, level)
            status = self._transport(url, body, headers, TIMEOUT_S)
            return "ok" if 200 <= status < 300 else f"HTTP {status}"
        except ValueError:  # e.g. invalid header value: may quote a secret, so never echo it
            return "failed: invalid request (check token/URL characters)"
        except OSError as exc:  # network errors (URLError included) are safe to show
            return f"failed: {str(getattr(exc, 'reason', None) or exc)[:80]}"
        except Exception as exc:  # http.client errors etc.: type name only (messages may quote data)
            return f"failed: {type(exc).__name__}"

    def send(self, title: str, message: str, level: str = "alert",
             deadline_s: float = SEND_DEADLINE_S) -> dict[str, str]:
        """Send to every channel in parallel; returns {channel: 'ok' | short error}.

        Never raises and never blocks longer than `deadline_s` (a stuck channel is
        reported as timed out and left to finish in the background).
        """
        message = message[:MAX_MESSAGE]
        results = {ch.kind: "failed: timed out" for ch in self.channels}
        lock = threading.Lock()

        def worker(ch: Channel) -> None:
            outcome = self._send_one(ch, title, message, level)
            with lock:
                results[ch.kind] = outcome

        threads = [threading.Thread(target=worker, args=(ch,), daemon=True) for ch in self.channels]
        for t in threads:
            t.start()
        deadline_at = time.monotonic() + deadline_s  # one shared deadline, not one per channel
        for t in threads:
            t.join(max(0.0, deadline_at - time.monotonic()))
        with lock:
            return dict(results)


def delivered(results: dict[str, str]) -> bool:
    """True if at least one channel accepted the message."""
    return any(v == "ok" for v in results.values())
