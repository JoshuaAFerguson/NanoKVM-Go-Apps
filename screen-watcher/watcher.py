#!/usr/bin/env python3
"""Screen Watcher background service (systemd: nanokvm-screen-watcher.service).

Every CHECK_S seconds: read HDMI state, grab a low-quality full-frame JPEG,
average it into a 16x9 colour grid (DC-only decode, no PyAV), run the rules,
and push alerts (retried until a channel accepts them). Talks to the
touchscreen App through JSON files in /var/lib/nanokvm-apps/screen-watcher/:
status.json (service -> App), control.json (App -> service), env.json (App's
launcher-provided settings). Disables itself if the App is uninstalled.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

import appdata
import capture
import notify
from applog import AppLog
from rules import Observation, Rules, Settings

APP_DIR = Path(__file__).resolve().parent
APP_NAME = "screen-watcher"
UNIT = "nanokvm-screen-watcher.service"
MIN_CHECK_S, DEFAULT_CHECK_S = 15, 30
MAX_EVENTS = 20
MAX_PENDING = 10
BLIND_AFTER = 10            # consecutive capture failures before a "can't see" alert
GONE_CHECKS, GONE_SECONDS = 3, 60   # App must be missing this long before self-removal
MAX_LOOP_FAILURES = 20
LOG = AppLog(APP_DIR, "service-log.txt")


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def resolve_env(app_dir: Path, data: Path) -> dict:
    """Same precedence as appbase (os.environ > app.json value > default), plus the
    env the touchscreen App last saw from the launcher (env.json) over the top."""
    try:
        specs = _dict(json.loads((app_dir / "app.json").read_text()).get("env"))
    except (OSError, ValueError, AttributeError):
        specs = {}
    env = {name: str(os.environ.get(name, spec.get("value", spec.get("default", ""))))
           for name, spec in specs.items() if isinstance(spec, dict)}
    overlay = _dict(appdata.read_json(data / "env.json", {}))
    env.update({k: v for k, v in overlay.items() if k in specs and isinstance(v, str)})
    return env


def _number(env: dict, key: str, default: float, low: float, high: float) -> float:
    try:
        return max(low, min(high, float(env.get(key) or default)))
    except ValueError:
        return default


def settings_from(env: dict) -> tuple[Settings, float]:
    settings = Settings(signal_grace_s=_number(env, "SIGNAL_GRACE_S", 30, 5, 3600),
                        bsod=(env.get("BSOD_ALERT") or "true").strip().lower() not in ("0", "false", "no", "off"),
                        black_min=_number(env, "BLACK_MIN", 0, 0, 1440),
                        freeze_min=_number(env, "FREEZE_MIN", 0, 0, 1440))
    return settings, _number(env, "CHECK_S", DEFAULT_CHECK_S, MIN_CHECK_S, 3600)


def device_name(env: dict) -> str:
    return (env.get("DEVICE_NAME") or "").strip()[:40] or socket.gethostname()


def remove_self(data: Path) -> None:
    """The App was uninstalled (there's no uninstall hook): disable the service and
    delete its data, which includes notification secrets."""
    LOG.info("App has been gone for a while; disabling service and deleting its data")
    subprocess.run(["systemctl", "disable", UNIT], capture_output=True, timeout=30)
    try:
        Path("/etc/systemd/system", UNIT).unlink()
    except OSError:
        pass
    subprocess.run(["systemctl", "daemon-reload"], capture_output=True, timeout=30)
    shutil.rmtree(data, ignore_errors=True)


class Service:
    def __init__(self, app_dir: Path, data: Path, observe: Callable = capture.observe,
                 hdmi: Callable[[], bool] = capture.hdmi_signal, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time, remover: Callable[[], None] | None = None) -> None:
        self.app_dir, self.data = app_dir, data
        self._observe, self._hdmi, self._clock, self._wall = observe, hdmi, clock, wall
        self._remover = remover or (lambda: remove_self(data))
        self.status = _dict(appdata.read_json(data / "status.json", {}))
        self.events = [e for e in self.status.get("events", []) if isinstance(e, dict)][-MAX_EVENTS:]
        self.pending: list[dict] = [p for p in self.status.get("pending", []) if isinstance(p, dict)][-MAX_PENDING:]
        self.rules = Rules(Settings())
        self.rules.restore(self.status.get("rules", {}), clock())
        self.notifier: notify.Notifier | None = None
        self.config_error = ""
        self.check_s = DEFAULT_CHECK_S
        self.sleep_s = DEFAULT_CHECK_S
        self.env_stamp = None
        self.failures = 0
        self.blind_alerted = False
        self.was_paused = False
        self.gone_since: float | None = None
        self.gone_count = 0

    # -- configuration -------------------------------------------------------

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
        env = resolve_env(self.app_dir, self.data)
        settings, self.check_s = settings_from(env)
        self.rules.settings = settings  # keep detection state across settings changes
        try:
            self.notifier = notify.Notifier(notify.load_channels(env), device=device_name(env))
            self.config_error = ""
        except (notify.NotifyError, ValueError) as exc:
            self.notifier, self.config_error = None, str(exc)
            LOG.warn(f"notification settings: {exc}")
        LOG.info(f"config loaded: check every {self.check_s:g}s, {settings}")

    # -- notifications -------------------------------------------------------------

    def _send(self, title: str, message: str, level: str) -> tuple[bool, str]:
        if self.notifier is None or not self.notifier.channels:
            return False, self.config_error or "no notification channels configured"
        results = self.notifier.send(title, message, level)
        summary = ", ".join(f"{k} {v}" for k, v in results.items())
        LOG.info(f"notify '{title}': {summary}")
        return notify.delivered(results), summary

    def record(self, title: str, level: str, result: str) -> None:
        self.events.append({"t": self._wall(), "title": title, "level": level, "result": result})
        self.events = self.events[-MAX_EVENTS:]

    def emit(self, title: str, message: str, level: str, paused: bool) -> None:
        if paused:
            self.record(title, level, "paused - not sent")
            return
        ok, summary = self._send(title, message, level)
        self.record(title, level, summary)
        if not ok and level == "alert":
            self.pending = (self.pending + [{"title": title, "message": message, "level": level}])[-MAX_PENDING:]

    def retry_pending(self) -> None:
        still = []
        for item in self.pending:
            ok, summary = self._send(item["title"], item["message"] + " (delayed)", item["level"])
            if ok:
                self.record(item["title"] + " (retried)", item["level"], summary)
            else:
                still.append(item)
        self.pending = still

    # -- one check -----------------------------------------------------------------

    def handle_control(self) -> bool:
        """Process control.json; returns True while alerts are paused."""
        control = _dict(appdata.read_json(self.data / "control.json", {}))
        test_id = control.get("test_id")
        if isinstance(test_id, str) and test_id and test_id != self.status.get("test_done"):
            ok, summary = self._send("Test alert", "Screen Watcher notifications are working.", "info")
            self.record("Test alert", "info", summary)
            self.status["test_done"] = test_id
        paused = _float(control.get("paused_until")) > self._wall()
        if self.was_paused and not paused and self.rules.active():
            names = ", ".join(self.rules.active())
            self.emit("Still active after pause", f"Conditions still active: {names}", "alert", paused=False)
        self.was_paused = paused
        self.status["paused_until"] = _float(control.get("paused_until"))
        return paused

    def tick(self) -> None:
        self.reload()
        paused = self.handle_control()
        if not paused:
            self.retry_pending()
        started = self._clock()
        error, decode_s = "", 0.0
        try:
            obs, decode_s = self._observe()
            self.failures = 0
            self.blind_alerted = False
        except Exception as exc:  # one bad frame must not stop the service
            error = f"{type(exc).__name__}: {exc}"[:120]
            obs = Observation(self._hdmi())
            self.failures += 1
            if self.failures in (1, BLIND_AFTER) or self.failures % 100 == 0:
                LOG.warn(f"observe failed x{self.failures}: {error}")
            if self.failures >= BLIND_AFTER and not self.blind_alerted:
                self.blind_alerted = True
                self.emit("Screen Watcher can't see the screen",
                          f"Snapshots have failed {self.failures} times: {error}", "alert", paused)
        if not error:
            for event in self.rules.update(obs, self._clock()):
                self.emit(event.title, event.message, event.level, paused)
        self.write_status(obs, error, decode_s)
        # Stay under ~10% of a core: back off if decoding is slow (4K targets).
        self.sleep_s = max(self.check_s, decode_s * 10) - (self._clock() - started)

    def write_status(self, obs: Observation, error: str, decode_s: float) -> None:
        cells = [c for row in obs.grid for c in row]
        mean = [round(sum(c[i] for c in cells) / len(cells)) for i in range(3)] if cells else None
        self.status.update({
            "heartbeat": self._wall(), "signal": obs.signal, "resolution": [obs.width, obs.height],
            "mean_rgb": mean, "active": self.rules.active(), "rules": self.rules.snapshot(),
            "events": self.events, "pending": self.pending, "error": error, "config_error": self.config_error,
            "check_s": self.check_s, "decode_ms": round(decode_s * 1000),
            "channels": [c.kind for c in self.notifier.channels] if self.notifier else [], "pid": os.getpid(),
        })
        # Heartbeat-only data: no fsync, to spare the flash.
        appdata.write_json(self.data / "status.json", self.status, durable=False)

    # -- lifecycle ------------------------------------------------------------------

    def app_gone(self) -> bool:
        """True only after the App has been missing for GONE_CHECKS checks and
        GONE_SECONDS, so a Store update (remove + re-extract) is never mistaken
        for an uninstall."""
        missing = not (self.app_dir / "app.json").exists() and not (self.app_dir / "watcher.py").exists()
        if not missing:
            self.gone_since, self.gone_count = None, 0
            return False
        now = self._clock()
        self.gone_since = now if self.gone_since is None else self.gone_since
        self.gone_count += 1
        return self.gone_count >= GONE_CHECKS and now - self.gone_since >= GONE_SECONDS

    def run(self) -> int:
        LOG.info(f"service started (pid {os.getpid()})")
        loop_failures = 0
        while True:
            try:
                if self.app_gone():
                    self._remover()
                    return 0
                self.tick()
                loop_failures = 0
            except Exception:
                loop_failures += 1
                LOG.exception(f"check failed ({loop_failures}/{MAX_LOOP_FAILURES})")
                if loop_failures >= MAX_LOOP_FAILURES:
                    return 1  # let systemd restart us cleanly
                self.sleep_s = min(300, self.check_s * loop_failures)
            self._wait(max(1.0, self.sleep_s))

    def _wait(self, seconds: float) -> None:
        """Sleep, but wake early when the App writes control.json (test button)."""
        control = self.data / "control.json"

        def stamp() -> float:
            try:
                return control.stat().st_mtime
            except OSError:
                return 0.0
        before, end = stamp(), time.monotonic() + seconds
        while time.monotonic() < end:
            time.sleep(1.0)
            if stamp() != before:
                return


def main() -> int:
    try:
        data = appdata.data_dir(APP_NAME, allow_fallback=False)
    except OSError as exc:
        LOG.error(f"cannot use /var/lib/nanokvm-apps: {exc}")
        return 1
    return Service(APP_DIR, data).run()


if __name__ == "__main__":
    sys.exit(main())
