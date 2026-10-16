#!/usr/bin/env python3
"""Text Alert background service (systemd: nanokvm-text-alert.service). Go+ only.

Polls the Go+ Memory Fabric (on-device NPU OCR, via the local MCP server) for new
screen captures, matches each capture's text against WATCH_TEXTS, and notifies.
Each capture is checked once (tracked by its captured_at); history present when
the service starts is skipped, so installing it doesn't replay old screens.
"""

from __future__ import annotations

import os
import socket
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

import appdata
import servicekit
from alerter import Alerter
from applog import AppLog
from kvm_mcp import KvmMcp, McpError
from matcher import Matcher, MatchError, context, parse_phrases

APP_DIR = Path(__file__).resolve().parent
APP_NAME = "text-alert"
UNIT = "nanokvm-text-alert.service"
REQUIRED_TOOLS = ("current_context", "read_recent_capture")
MIN_POLL_S, DEFAULT_POLL_S = 10, 20
MAX_NEW_PER_POLL = 5        # the rest wait for the next poll (not dropped)
SEEN_MEMORY = 100           # capture IDs remembered, so order/format quirks never re-alert
REPROBE_S = 600
LOG = AppLog(APP_DIR, "service-log.txt")


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _order_key(stamp: str):
    """Sort by real time when parseable (formats vary: 'Z', '+00:00', fractions)."""
    try:
        return (0, datetime.fromisoformat(stamp).timestamp(), stamp)
    except ValueError:
        return (1, 0.0, stamp)


def _number(env: dict, key: str, default: float, low: float, high: float) -> float:
    try:
        return max(low, min(high, float(env.get(key) or default)))
    except ValueError:
        return default


class Service:
    def __init__(self, app_dir: Path, data: Path, mcp_factory: Callable[[str], object] | None = None,
                 clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time) -> None:
        self.app_dir, self.data = app_dir, data
        self._mcp_factory = mcp_factory or (lambda key: KvmMcp(key, client_name="text-alert"))
        self._clock, self._wall = clock, wall
        self.status = _dict(appdata.read_json(data / "status.json", {}))
        self.state = _dict(appdata.read_json(data / "state.json", {}))
        self.alerter = Alerter(LOG.info, wall, self.status.get("events"), self.status.get("pending"))
        self.presence = servicekit.AppPresence(app_dir, "textd.py", clock)
        self.mcp = None
        self.matcher: Matcher | None = None
        self.poll_s = DEFAULT_POLL_S
        self.error = ""
        self.env_stamp = None
        self.supported: bool | None = None
        self.probed_at = 0.0
        self.last_logged_error = ""

    def reload(self) -> None:
        def mtime(p: Path) -> float:
            try:
                return p.stat().st_mtime
            except OSError:
                return 0.0
        stamp = (mtime(self.app_dir / "app.json"), mtime(self.data / "env.json"))
        if stamp == self.env_stamp:
            return
        self.env_stamp = stamp
        env = servicekit.resolve_env(self.app_dir, self.data)
        self.poll_s = _number(env, "POLL_S", DEFAULT_POLL_S, MIN_POLL_S, 3600)
        self.alerter.configure({**env, "DEVICE_NAME": (env.get("DEVICE_NAME") or "").strip() or socket.gethostname()})
        self.error, self.matcher, self.mcp, self.supported = "", None, None, None
        try:
            phrases = parse_phrases(env.get("WATCH_TEXTS"))
            if not phrases:
                self.error = "Add phrases to WATCH_TEXTS"
                return
            self.matcher = Matcher(phrases, _number(env, "COOLDOWN_MIN", 10, 0, 1440) * 60)
            key = (env.get("NANOKVM_MCP_KEY") or "").strip()
            if not key:
                self.error = "Set NANOKVM_MCP_KEY (Settings > MCP)"
                return
            self.mcp = self._mcp_factory(key)
        except (MatchError, ValueError) as exc:
            self.error = str(exc)
        LOG.info(f"config loaded: {len(self.matcher.phrases) if self.matcher else 0} phrases, poll {self.poll_s:g}s")

    def handle_control(self) -> bool:
        control = _dict(appdata.read_json(self.data / "control.json", {}))
        test_id = control.get("test_id")
        if isinstance(test_id, str) and test_id and test_id != self.status.get("test_done"):
            ok, summary = self.alerter.send("Test alert", "Text Alert notifications are working.", "info")
            self.alerter.record("Test alert", "info", summary)
            self.status["test_done"] = test_id
        try:
            return float(control.get("paused_until") or 0) > self._wall()
        except (TypeError, ValueError):
            return False

    def new_captures(self) -> list[dict]:
        """Captures not checked before, oldest first. Tracked as a set of capture IDs
        (captured_at strings), so mixed timestamp formats or late arrivals can't cause
        repeats or misses; one unreadable capture never blocks the others."""
        ctx = _dict(self.mcp.call("current_context"))
        stamps = sorted({o["captured_at"] for o in ctx.get("recent_observations") or []
                         if isinstance(o, dict) and isinstance(o.get("captured_at"), str)}, key=_order_key)
        seen = self.state.get("seen")
        if not isinstance(seen, list) or not all(isinstance(x, str) for x in seen):  # first run / corrupt
            self.state = {"seen": stamps[-SEEN_MEMORY:]}
            self.state_dirty = True
            return []
        known = set(seen)
        fresh = [st for st in stamps if st not in known]
        if len(fresh) > MAX_NEW_PER_POLL:
            LOG.info(f"{len(fresh)} new captures; checking {MAX_NEW_PER_POLL} now, the rest next poll")
        captures, done = [], []
        for stamp in fresh[:MAX_NEW_PER_POLL]:
            done.append(stamp)
            try:
                capture = _dict(self.mcp.call("read_recent_capture", {"at": stamp, "include_raw_text": True}))
            except McpError as exc:
                LOG.warn(f"skipping capture {stamp}: {exc}")
                continue
            captures.append({"at": stamp, "text": str(_dict(capture.get("ocr")).get("text") or "")})
        if done:
            self.state = {"seen": (seen + done)[-SEEN_MEMORY:]}
            self.state_dirty = True
        return captures

    def tick(self) -> None:
        self.reload()
        paused = self.handle_control()
        if not paused:
            self.alerter.retry_pending()
        checked = 0
        self.state_dirty = False
        if self.mcp is not None and self.matcher is not None:
            try:
                if self.supported is None or (not self.supported and self._clock() - self.probed_at > REPROBE_S):
                    names = self.mcp.tool_names()  # re-probed: Memory Fabric may be enabled later
                    self.supported, self.probed_at = all(t in names for t in REQUIRED_TOOLS), self._clock()
                if not self.supported:
                    self.error = "Requires NanoKVM Go+ with Memory Fabric enabled"
                else:
                    for capture in self.new_captures():
                        checked += 1
                        for phrase in self.matcher.check(capture["text"], self._clock()):
                            self.alerter.emit(f"Seen on screen: {phrase}", context(capture["text"], phrase),
                                              "alert", paused)
                    self.error = ""
                    if self.state_dirty:  # only when something changed: spare the flash
                        appdata.write_json(self.data / "state.json", self.state)
            except McpError as exc:
                self.error = f"MCP: {exc}"[:120]
        if self.error and self.error != self.last_logged_error:
            LOG.warn(self.error)
        self.last_logged_error = self.error
        self.status.update({
            "heartbeat": self._wall(), "error": self.error, "events": self.alerter.events,
            "pending": self.alerter.pending, "channels": self.alerter.channels,
            "config_error": self.alerter.config_error, "phrases": list(self.matcher.phrases) if self.matcher else [],
            "last_capture": (self.state.get("seen") or [""])[-1], "checked": checked, "poll_s": self.poll_s,
            "paused": paused, "pid": os.getpid(),
        })
        appdata.write_json(self.data / "status.json", self.status, durable=False)

    def run(self) -> int:
        LOG.info(f"service started (pid {os.getpid()})")
        failures = 0
        while True:
            try:
                if self.presence.gone():
                    servicekit.remove_service(UNIT, self.data, LOG.info)
                    return 0
                self.tick()
                failures = 0
            except Exception:
                failures += 1
                LOG.exception(f"check failed ({failures})")
                if failures >= 20:
                    return 1
            servicekit.wait(self.poll_s, self.data)  # Test/Resume act immediately


def main() -> int:
    try:
        data = appdata.data_dir(APP_NAME, allow_fallback=False)
    except OSError as exc:
        LOG.error(f"cannot use /var/lib/nanokvm-apps: {exc}")
        return 1
    return Service(APP_DIR, data).run()


if __name__ == "__main__":
    sys.exit(main())
