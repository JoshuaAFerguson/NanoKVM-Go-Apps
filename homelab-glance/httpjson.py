"""Small, bounded JSON-over-HTTP client (stdlib only).

Shared module: edit _shared/httpjson.py, then run `python3 _tools/sync_shared.py`.

Guarantees: 512 KB response cap, a whole-request deadline, no env proxies, no
redirects (they would forward the auth header), and error messages that never
include request headers (tokens).
"""

from __future__ import annotations

import http.client
import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

MAX_BYTES = 512 * 1024   # protect the Go's ~35 MB of free RAM
TIMEOUT_S = 5.0          # per request, total (not per socket read)
CHUNK = 16 * 1024


class HttpError(RuntimeError):
    """A service request failed; message is short enough for the screen."""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


def decode(body: bytes, name: str):
    if len(body) > MAX_BYTES:
        raise HttpError(f"{name}: response over {MAX_BYTES // 1024} KB")
    try:
        return json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HttpError(f"{name}: response is not JSON") from exc


_openers: dict = {}


def _opener(verify_tls: bool, ca_file: str):
    key = (verify_tls, ca_file)
    if key not in _openers:
        context = ssl.create_default_context(cafile=ca_file or None)
        if not verify_tls:  # explicit per-service opt-in (*_INSECURE) for self-signed homelab certs
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        _openers[key] = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context), _NoRedirect())
    return _openers[key]


def _shrink_socket_timeout(response, seconds: float) -> None:
    """Best effort: make the next recv wait at most `seconds` (private attrs, may be absent)."""
    sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
    if sock is not None:
        sock.settimeout(max(0.1, seconds))


def _read_with_deadline(response, deadline: float, name: str) -> bytes:
    """read1() does at most one recv per call, so a slow-drip server can't stretch
    one read past the deadline; each recv's timeout is also cut to the time left."""
    chunks, total = [], 0
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            raise HttpError(f"{name}: timed out")
        _shrink_socket_timeout(response, left)
        try:
            chunk = response.read1(CHUNK)
        except TimeoutError:
            raise HttpError(f"{name}: timed out") from None
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > MAX_BYTES:
            raise HttpError(f"{name}: response over {MAX_BYTES // 1024} KB")
        chunks.append(chunk)


def get_json(url: str, name: str, headers: dict | None = None, verify_tls: bool = True, ca_file: str = ""):
    return decode(_get(url, name, {"Accept": "application/json", **(headers or {})}, verify_tls, ca_file), name)


def get_text(url: str, name: str, headers: dict | None = None, verify_tls: bool = True, ca_file: str = "") -> str:
    """Bounded text fetch (e.g. Prometheus /metrics)."""
    return _get(url, name, headers or {}, verify_tls, ca_file).decode("utf-8", "replace")


def _get(url: str, name: str, headers: dict, verify_tls: bool, ca_file: str) -> bytes:
    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):  # no file://, ftp://, ...
        raise HttpError(f"{name}: URL must start with http:// or https://")
    request = urllib.request.Request(url, headers=headers)
    deadline = time.monotonic() + TIMEOUT_S
    try:
        with _opener(verify_tls, ca_file).open(request, timeout=TIMEOUT_S) as response:
            body = _read_with_deadline(response, deadline, name)
    except HttpError:
        raise
    except urllib.error.HTTPError as exc:
        detail = ""
        try:  # e.g. Prometheus puts the PromQL error in the body
            payload = json.loads(exc.read(1024) or b"{}")
            detail = str(payload.get("error") or payload.get("message") or "")[:80] if isinstance(payload, dict) else ""
        except (ValueError, OSError):
            pass
        exc.close()
        hint = {301: "redirect refused - fix the URL", 302: "redirect refused - fix the URL",
                307: "redirect refused - fix the URL", 308: "redirect refused - fix the URL",
                401: "token rejected", 403: "token lacks permission", 404: "not found"}.get(exc.code, "")
        raise HttpError(f"{name}: HTTP {exc.code}" + "".join(f" - {x}" for x in (hint, detail) if x), exc.code) from None
    except ssl.SSLError:
        raise HttpError(f"{name}: TLS error (self-signed? set *_CA_FILE or *_INSECURE)") from None
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        reason = getattr(exc, "reason", None) or type(exc).__name__
        raise HttpError(f"{name}: {reason}") from None
    except ValueError:  # e.g. invalid header value: never echo it (it holds the token)
        raise HttpError(f"{name}: invalid request (check URL/token characters)") from None
    return body
