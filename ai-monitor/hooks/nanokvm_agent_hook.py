#!/usr/bin/env python3
"""Claude Code hook: report this session's state to a NanoKVM running AI Monitor.

Install on each machine you run Claude Code on (see ai-monitor/README.md):
  NANOKVM_AI_URL     https://<kvm tailscale ip>:8790     (required)
  NANOKVM_AI_TOKEN   the agent token shown in the AI Monitor App (required)
  NANOKVM_AI_CERT    path to the KVM's certificate to pin (recommended), or
  NANOKVM_AI_INSECURE=1 to skip TLS verification (tailnet only)
  NANOKVM_AI_MACHINE name shown on the KVM (default: hostname)

Never breaks Claude Code: no output, always exits 0. Only state *changes* are
sent (tool hooks fire constantly), and after a failure it stays quiet for a
minute, so an unreachable KVM costs at most one short timeout per minute.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import re
import socket
import ssl
import sys
import urllib.request

TIMEOUT_S = 2
BACKOFF_S = 60
STATE_DIR = os.path.join(tempfile.gettempdir(), f"nanokvm-agent-hook-{os.getuid() if hasattr(os, 'getuid') else 0}")

WAITING_TYPES = {"permission_prompt", "elicitation_dialog", "agent_needs_input"}
EVENT_STATES = {"SessionStart": "idle", "UserPromptSubmit": "working", "PreToolUse": "working",
                "PostToolUse": "working", "SubagentStop": "working", "Stop": "idle", "SessionEnd": "done"}


def state_for(event: dict) -> tuple[str, str] | None:
    name = event.get("hook_event_name")
    if name == "Notification":
        kind = event.get("notification_type", "")
        message = str(event.get("message") or "")
        if kind in WAITING_TYPES or (not kind and "permission" in message.lower()):
            return "waiting", message
        if kind == "idle_prompt":
            return "idle", "waiting for your next prompt"
        return None
    if name in EVENT_STATES:
        return EVENT_STATES[name], "finished its turn" if name == "Stop" else ""
    return None


def machine_name() -> str:
    raw = os.environ.get("NANOKVM_AI_MACHINE") or socket.gethostname().split(".")[0]
    return re.sub(r"[^A-Za-z0-9._-]", "-", raw)[:40] or "machine"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # never forward the token elsewhere
        return None


def _marker(name: str) -> str:
    return os.path.join(STATE_DIR, re.sub(r"[^A-Za-z0-9_-]", "_", name)[:80])


def unchanged(session: str, state: str, message: str) -> bool:
    """True if this session already reported exactly this; records it otherwise."""
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    path, value = _marker("s-" + session), f"{state}\n{message}"
    try:
        with open(path, encoding="utf-8") as fh:
            if fh.read() == value:
                return True
    except OSError:
        pass
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(value)
    return False


def backing_off() -> bool:
    try:
        return time.time() - os.path.getmtime(_marker("failed")) < BACKOFF_S
    except OSError:
        return False


def main() -> int:
    try:
        event = json.load(sys.stdin)
        url, token = os.environ.get("NANOKVM_AI_URL", ""), os.environ.get("NANOKVM_AI_TOKEN", "")
        mapped = state_for(event) if isinstance(event, dict) else None
        if not (url.startswith("https://") and token and mapped) or backing_off():
            return 0  # https only: the token must never travel in clear text
        state, message = mapped
        session = str(event.get("session_id", "?"))[:64]
        if unchanged(session, state, message):
            return 0
        body = {"machine": machine_name(), "session": session, "state": state,
                "project": os.path.basename(str(event.get("cwd") or "")), "message": message[:160]}
        if os.environ.get("NANOKVM_AI_CERT"):
            ctx = ssl.create_default_context(cafile=os.environ["NANOKVM_AI_CERT"])
            ctx.check_hostname = False  # pinned to the KVM's own certificate
        elif os.environ.get("NANOKVM_AI_INSECURE") == "1":
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
        else:
            ctx = ssl.create_default_context()
        request = urllib.request.Request(url.rstrip("/") + "/v1/agent", json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                             urllib.request.HTTPSHandler(context=ctx), _NoRedirect())
        try:
            opener.open(request, timeout=TIMEOUT_S).close()
        except Exception:
            try:  # back off, and let the same state be retried after the back-off
                os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
                open(_marker("failed"), "w").close()
                os.unlink(_marker("s-" + session))
            except OSError:
                pass
    except Exception:  # never disturb the agent
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
