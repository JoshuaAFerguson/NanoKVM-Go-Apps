#!/usr/bin/env python3
"""Claude Code hook: report this session's state to a NanoKVM running AI Monitor.

Install on each machine you run Claude Code on (see ai-monitor/README.md):
  NANOKVM_AI_URL     https://<kvm tailscale ip>:8790     (required)
  NANOKVM_AI_TOKEN   the agent token shown in the AI Monitor App (required)
  NANOKVM_AI_CERT    path to the KVM's certificate to pin (recommended), or
  NANOKVM_AI_INSECURE=1 to skip TLS verification (tailnet only)
  NANOKVM_AI_MACHINE name shown on the KVM (default: hostname)

State lives in ~/.cache/nanokvm-agent-hook. Never breaks Claude Code: no output, always exits 0. Only state *changes* are
sent (tool hooks fire constantly), and after a failure it stays quiet for a
minute, so an unreachable KVM costs at most one short timeout per minute.
"""

from __future__ import annotations

import json
import os
import stat
import time
import re
import socket
import ssl
import sys
import urllib.request

TIMEOUT_S = 2
BACKOFF_S = 60
STATE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "nanokvm-agent-hook")  # per-user, not shared /tmp

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


def _state_dir() -> None:
    """Create the state dir and refuse it unless it's a real 0700 directory we own."""
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    st = os.lstat(STATE_DIR)
    posix = os.name == "posix"  # Windows reports 0o777 for private dirs; its ACLs come from makedirs
    if not stat.S_ISDIR(st.st_mode) or (posix and (st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o077)):
        raise OSError("unsafe hook state directory")


def _write(path: str, value: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(value)


def unchanged(session: str, state: str, message: str) -> bool:
    """True if this session already reported exactly this; records it otherwise."""
    _state_dir()
    path, value = _marker("s-" + session), f"{state}\n{message}"
    try:
        with open(path, encoding="utf-8") as fh:
            if fh.read() == value:
                return True
    except OSError:
        pass
    _write(path, value)
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
                _state_dir()
                _write(_marker("failed"), "")
                os.unlink(_marker("s-" + session))
            except OSError:
                pass
    except Exception:  # never disturb the agent
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
