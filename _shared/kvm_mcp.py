"""Stdlib client for the NanoKVM's local MCP server (keyboard, mouse, screenshot, ...).

Shared module: edit _shared/kvm_mcp.py, then run `python3 _tools/sync_shared.py`
to copy it into every App that uses it. Do not edit the App copies.

The server speaks MCP streamable HTTP at https://127.0.0.1/api/mcp with a
self-signed certificate and `Authorization: Bearer <MCP API key>`.
"""

from __future__ import annotations

import http.client
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

import localtls

DEFAULT_URL = "https://127.0.0.1/api/mcp"
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
PROTOCOL_VERSION = "2025-06-18"
SESSION_LOST_STATUS = 404  # MCP spec: unknown/expired session

# transport(url, body, headers, timeout) -> (status, response_headers, body_text)
Transport = Callable[[str, bytes, dict, float], "tuple[int, dict, str]"]


class McpError(RuntimeError):
    """The MCP server rejected a request, a tool failed, or the reply was unusable."""


class _SessionLost(Exception):
    pass


def _is_loopback(url: str) -> bool:
    return urllib.parse.urlsplit(url).hostname in LOOPBACK_HOSTS


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


# Loopback only (KvmMcp refuses other URLs with this transport), the device's
# own certificate pinned (localtls), env proxies ignored, and redirects refused
# so the bearer key can't be forwarded.
_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    urllib.request.HTTPSHandler(context=localtls.context()),
    _NoRedirect(),
)


def _urllib_transport(url: str, body: bytes, headers: dict, timeout: float) -> tuple[int, dict, str]:
    request = urllib.request.Request(url, body, headers=headers, method="POST")
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read().decode("utf-8", "replace") if exc.fp else ""


def _parse_messages(body: str) -> list[dict]:
    """Plain JSON body, or an SSE stream of `data:` events (multi-line data joined)."""
    if not body.strip():
        return []
    if not any(line.startswith("data:") for line in body.splitlines()):
        return [json.loads(body)]
    messages, data = [], []
    for line in body.splitlines() + [""]:
        if line.startswith("data:"):
            data.append(line[5:].lstrip())
        elif not line.strip() and data:
            messages.append(json.loads("\n".join(data)))
            data = []
    return messages


def _first_text(result: dict) -> str:
    content = result.get("content")
    if not isinstance(content, list) or not content or not isinstance(content[0], dict):
        raise McpError("MCP tool returned a malformed result")
    text = content[0].get("text", "")
    return text if isinstance(text, str) else ""


class KvmMcp:
    """Thread-safe, lazily-initialized MCP session that re-initializes once if it expires."""

    def __init__(self, api_key: str, url: str = DEFAULT_URL, timeout: float = 10.0,
                 transport: Transport | None = None, client_name: str = "nanokvm-app") -> None:
        if not api_key:
            raise ValueError("MCP API key is required (set it in the App's install form)")
        if transport is None and not _is_loopback(url):
            raise ValueError("default transport only talks to the local device (loopback URL)")
        self._api_key = api_key
        self._url = url
        self._timeout = timeout
        self._transport = transport or _urllib_transport
        self._client_name = client_name
        self._session_id = ""
        self._next_id = 0
        self._lock = threading.Lock()

    # -- public API -------------------------------------------------------

    def call(self, name: str, arguments: dict | None = None, timeout: float | None = None) -> Any:
        """Call a tool; returns parsed JSON content, or text if not JSON."""
        result = self._rpc("tools/call", {"name": name, "arguments": arguments or {}}, timeout)
        if not isinstance(result, dict):
            raise McpError(f"MCP {name} returned no result")
        text = _first_text(result)
        if result.get("isError"):
            raise McpError(text or f"{name} failed")
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return text

    def tool_names(self) -> list[str]:
        result = self._rpc("tools/list", {})
        tools = result.get("tools", []) if isinstance(result, dict) else []
        return [t["name"] for t in tools if isinstance(t, dict) and "name" in t]

    def press_keys(self, keys: list[str], hold_ms: int | None = None) -> Any:
        args: dict[str, Any] = {"keys": list(keys)}
        if hold_ms is not None:
            args["holdMs"] = hold_ms
        return self.call("press_keys", args)

    def type_text(self, text: str, delay_ms: int | None = None, timeout: float | None = None) -> Any:
        args: dict[str, Any] = {"text": text}
        if delay_ms is not None:
            args["delayMs"] = delay_ms
        return self.call("type_text", args, timeout)

    # -- protocol ---------------------------------------------------------

    def _rpc(self, method: str, params: dict, timeout: float | None = None) -> Any:
        with self._lock:
            if not self._session_id:
                self._initialize()
            try:
                return self._send(method, params, timeout)
            except _SessionLost:
                self._initialize()
                try:
                    return self._send(method, params, timeout)
                except _SessionLost as exc:
                    raise McpError("MCP session rejected after re-initialize") from exc

    def _initialize(self) -> None:
        self._session_id = ""
        status, headers, _, _ = self._post({
            "method": "initialize",
            "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                       "clientInfo": {"name": self._client_name, "version": "1"}},
        })
        if status in (401, 403):
            raise McpError("MCP API key was rejected; check the App configuration")
        session = next((v for k, v in headers.items() if k.lower() == "mcp-session-id"), "")
        if status != 200 or not session:
            raise McpError(f"MCP initialize failed (HTTP {status})")
        self._session_id = session
        self._post({"method": "notifications/initialized", "params": {}}, notification=True)

    def _send(self, method: str, params: dict, timeout: float | None) -> Any:
        status, _, body, request_id = self._post({"method": method, "params": params}, timeout=timeout)
        if status == SESSION_LOST_STATUS or (status == 400 and "session" in body.lower()):
            raise _SessionLost()
        if status in (401, 403):
            raise McpError("MCP API key was rejected; check the App configuration")
        if status != 200:
            detail = body.strip()[:120]
            raise McpError(f"MCP {method} failed (HTTP {status}){': ' + detail if detail else ''}")
        try:
            messages = _parse_messages(body)
        except json.JSONDecodeError as exc:
            raise McpError(f"MCP {method} returned invalid JSON") from exc
        message = next((m for m in messages if isinstance(m, dict) and m.get("id") == request_id), None)
        if message is None:
            raise McpError(f"MCP {method} returned no response")
        error = message.get("error")
        if error:
            raise McpError(str(error.get("message", error) if isinstance(error, dict) else error))
        return message.get("result")

    def _post(self, payload: dict, notification: bool = False,
              timeout: float | None = None) -> tuple[int, dict, str, int | None]:
        message = {"jsonrpc": "2.0", **payload}
        request_id = None
        if not notification:
            self._next_id += 1
            request_id = message["id"] = self._next_id
        headers = {
            "Authorization": "Bearer " + self._api_key,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        try:
            status, resp_headers, body = self._transport(
                self._url, json.dumps(message).encode(), headers, timeout or self._timeout)
        except (OSError, http.client.HTTPException) as exc:
            raise McpError(f"cannot reach MCP server: {exc}") from exc
        return status, resp_headers, body, request_id
