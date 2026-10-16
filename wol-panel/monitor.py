"""Tracks host reachability and wake attempts on a background thread."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, replace
from typing import Callable

from wol import Host

IDLE_POLL_S = 10.0
WAKING_POLL_S = 2.0
WAKE_TIMEOUT_S = 180.0
PING_TIMEOUT_S = 1.0
MESSAGE_S = 30.0


@dataclass(frozen=True)
class HostState:
    up: bool | None = None          # None = no IP to ping / not checked yet
    waking_since: float | None = None


class Monitor:
    def __init__(self, hosts: tuple[Host, ...], ping: Callable, send: Callable[[str], int],
                 clock: Callable[[], float] = time.monotonic, log: Callable[[str], None] = print) -> None:
        self.hosts = hosts
        self._ping = ping
        self._send = send
        self._clock = clock
        self._log = log
        self._lock = threading.Lock()
        self._wake_event = threading.Event()
        self._stop = threading.Event()
        self._states = {h.name: HostState() for h in hosts}
        self._message = ""
        self._message_at = 0.0
        self._error = ""  # persistent until a poll succeeds
        self._failures = 0
        self.version = 0

    # -- read side (UI thread) ----------------------------------------------

    def state(self, name: str) -> HostState:
        with self._lock:
            return self._states[name]

    @property
    def message(self) -> str:
        with self._lock:
            return self._error or self._message

    def current_message(self) -> str:
        """The footer text: a poll error, else the latest message for MESSAGE_S."""
        with self._lock:
            if self._error:
                return self._error
            return self._message if self._clock() - self._message_at < MESSAGE_S else ""

    def interval(self) -> float:
        with self._lock:
            waking = any(s.waking_since is not None for s in self._states.values())
        return WAKING_POLL_S if waking else IDLE_POLL_S

    # -- write side ------------------------------------------------------------

    def set_message(self, message: str) -> None:
        with self._lock:
            self._message, self._message_at = message, self._clock()
            self.version += 1

    def wake(self, host: Host) -> None:
        sent = self._send(host.mac)
        self._log(f"wake {host.name} {host.mac}: {sent} packets")
        with self._lock:
            state = self._states[host.name]
            if not host.ip:
                note = f"Wake sent to {host.name} (no IP to monitor)"
            elif state.up:
                note = f"Wake sent to {host.name} (already online)"
            else:
                note = f"Wake sent to {host.name} ({sent} packets)"
                self._states[host.name] = replace(state, waking_since=self._clock())
            self._message, self._message_at = note, self._clock()
            self.version += 1
        self._wake_event.set()  # poll sooner

    def poll_once(self) -> None:
        ips = [h.ip for h in self.hosts if h.ip]
        results = self._ping(ips, PING_TIMEOUT_S) if ips else {}
        events = []
        with self._lock:  # read-modify-write in one step so a concurrent wake() isn't lost
            now = self._clock()
            for host in self.hosts:
                previous = self._states[host.name]
                up = results.get(host.ip) if host.ip else None
                waking = previous.waking_since
                if waking is not None and up:
                    events.append(f"{host.name} is up after {int(now - waking)}s")
                    waking = None
                elif waking is not None and now - waking > WAKE_TIMEOUT_S:
                    events.append(f"{host.name}: no response after {int(WAKE_TIMEOUT_S)}s")
                    waking = None
                if (up, waking) != (previous.up, previous.waking_since):
                    self._states[host.name] = HostState(up=up, waking_since=waking)
                    self.version += 1
            if events:
                self._message, self._message_at = events[-1], now
                self.version += 1
            if self._error:
                self._error = ""
                self.version += 1
        for event in events:
            self._log(event)

    def run_once(self) -> None:
        try:
            self.poll_once()
            self._failures = 0
        except Exception as exc:  # keep monitoring; show the problem instead of stale status
            self._failures += 1
            with self._lock:
                self._error = f"Status check failed: {exc}"
                self.version += 1
            if self._failures in (1, 10) or self._failures % 100 == 0:
                self._log(f"poll failed x{self._failures}: {type(exc).__name__}: {exc}")

    def run(self) -> None:
        while not self._stop.is_set():
            self._wake_event.clear()  # clear first so a wake() during the poll isn't lost
            self.run_once()
            self._wake_event.wait(self.interval())

    def start(self) -> None:
        threading.Thread(target=self.run, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self._wake_event.set()
