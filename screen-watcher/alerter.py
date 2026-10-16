"""Alert delivery for background services: event log, pause, and retry queue.

Shared module: edit _shared/alerter.py, then run `python3 _tools/sync_shared.py`.

Alerts that no channel accepted (network down, service outage) are queued and
retried on later checks; while paused, events are logged but not sent.
"""

from __future__ import annotations

import time
from typing import Callable

import notify

MAX_EVENTS = 20
MAX_PENDING = 10


class Alerter:
    def __init__(self, log: Callable[[str], None], wall: Callable[[], float] = time.time,
                 events: list | None = None, pending: list | None = None) -> None:
        self.notifier: notify.Notifier | None = None
        self.config_error = ""
        self._log = log
        self._wall = wall
        self.events = [e for e in (events or []) if isinstance(e, dict)][-MAX_EVENTS:]
        self.pending = [p for p in (pending or []) if isinstance(p, dict)
                        and {"title", "message", "level"} <= p.keys()][-MAX_PENDING:]

    def configure(self, env: dict) -> None:
        try:
            device = (env.get("DEVICE_NAME") or "").strip()[:40] or None
            self.notifier = notify.Notifier(notify.load_channels(env), **({"device": device} if device else {}))
            self.config_error = ""
        except (notify.NotifyError, ValueError) as exc:
            self.notifier, self.config_error = None, str(exc)
            self._log(f"notification settings: {exc}")

    @property
    def channels(self) -> list[str]:
        return [c.kind for c in self.notifier.channels] if self.notifier else []

    def send(self, title: str, message: str, level: str) -> tuple[bool, str]:
        if self.notifier is None or not self.notifier.channels:
            return False, self.config_error or "no notification channels configured"
        results = self.notifier.send(title, message, level)
        summary = ", ".join(f"{k} {v}" for k, v in results.items())
        self._log(f"notify '{title}': {summary}")
        return notify.delivered(results), summary

    def record(self, title: str, level: str, result: str) -> None:
        self.events = (self.events + [{"t": self._wall(), "title": title, "level": level, "result": result}])[-MAX_EVENTS:]

    def emit(self, title: str, message: str, level: str = "alert", paused: bool = False) -> None:
        if paused:
            self.record(title, level, "paused - not sent")
            return
        ok, summary = self.send(title, message, level)
        self.record(title, level, summary)
        if not ok and level == "alert":
            self.pending = (self.pending + [{"title": title, "message": message, "level": level}])[-MAX_PENDING:]

    def retry_pending(self) -> None:
        still = []
        for item in self.pending:
            ok, summary = self.send(item["title"], item["message"] + " (delayed)", item["level"])
            if ok:
                self.record(item["title"] + " (retried)", item["level"], summary)
            else:
                still.append(item)
        self.pending = still
