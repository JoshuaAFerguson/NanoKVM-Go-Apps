"""What AI agents are doing to the target through this KVM's MCP server.

The AI Monitor offers a watched MCP endpoint that forwards to the device's own
server; every call is logged here by tool name only (never arguments, which can
contain typed text such as passwords).
"""

from __future__ import annotations

import json
import threading
from collections import deque

ACTIVE_S = 30.0              # a control call in the last 30 s = "AI is in control"
MAX_LOG = 100
CONTROL_TOOLS = {"press_keys", "type_text", "click_mouse", "move_mouse", "scroll_mouse"}
VIEW_TOOLS = {"screenshot", "current_context", "read_recent_capture", "search_captures", "speaker_read"}


def describe(body: bytes) -> list[str]:
    """Tool names (or methods) in a JSON-RPC request or batch; never arguments."""
    try:
        message = json.loads(body)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return ["?"]
    items = message if isinstance(message, list) else [message]
    names = []
    for item in items:  # all of them (body size is capped): a control call can't hide at the end of a batch
        if not isinstance(item, dict):
            continue
        method = item.get("method")
        params = item.get("params") if isinstance(item.get("params"), dict) else {}
        name = params.get("name") if method == "tools/call" else method
        names.append(str(name)[:40] if isinstance(name, str) else "?")
    return names or ["?"]


class Activity:
    def __init__(self) -> None:
        self._log: deque = deque(maxlen=MAX_LOG)
        self._lock = threading.Lock()
        self.last_control = -1e18
        self.last_view = -1e18
        self.blocked_calls = 0

    def record(self, tools: list[str], client: str, ok: bool, now: float) -> None:
        with self._lock:
            for tool in tools:
                self._log.appendleft({"t": now, "tool": tool, "client": client, "ok": ok})
                if ok and tool in CONTROL_TOOLS:
                    self.last_control = now
                elif ok and tool in VIEW_TOOLS:
                    self.last_view = now
                if not ok:
                    self.blocked_calls += 1

    def active(self, now: float) -> bool:
        return now - self.last_control <= ACTIVE_S

    def watching(self, now: float) -> bool:
        return now - self.last_view <= ACTIVE_S

    def recent(self, limit: int = 20) -> list[dict]:
        with self._lock:
            return list(self._log)[:limit]
