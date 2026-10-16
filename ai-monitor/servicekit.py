"""Helpers for Apps that run a background systemd service.

Shared module: edit _shared/servicekit.py, then run `python3 _tools/sync_shared.py`.

- resolve_env: the App's settings, resolved like appbase does, plus the env the
  touchscreen App last saw from the launcher (data/env.json).
- AppPresence: decides the App was uninstalled only after it has been missing
  for a while, so a Store update (remove + re-extract) is never mistaken for it.
- remove_service: disable the unit and delete the service's data (secrets).
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Callable

import appdata

GONE_CHECKS, GONE_SECONDS = 5, 300
APPS_MARKER = "appbase.py"   # the shared SDK always sits next to installed Apps


def resolve_env(app_dir: Path, data: Path) -> dict:
    """os.environ > app.json "value" > "default" (as appbase), then env.json on top."""
    try:
        specs = json.loads((app_dir / "app.json").read_text()).get("env")
    except (OSError, ValueError, AttributeError):
        specs = None
    specs = specs if isinstance(specs, dict) else {}
    env = {name: str(os.environ.get(name, spec.get("value", spec.get("default", ""))))
           for name, spec in specs.items() if isinstance(spec, dict)}
    overlay = appdata.read_json(data / "env.json", {})
    if isinstance(overlay, dict):
        env.update({k: v for k, v in overlay.items() if k in specs and isinstance(v, str)})
    return env


class AppPresence:
    def __init__(self, app_dir: Path, entry: str, clock: Callable[[], float] = time.monotonic) -> None:
        self.app_dir, self.entry, self._clock = app_dir, entry, clock
        self.since: float | None = None
        self.count = 0

    def gone(self) -> bool:
        # A storage hiccup must never look like an uninstall: the apps directory
        # itself has to be healthy (listable, SDK present) while this App is missing.
        try:
            apps_ok = (self.app_dir.parent / APPS_MARKER).exists() and any(self.app_dir.parent.iterdir())
        except OSError:
            apps_ok = False
        missing = apps_ok and not (self.app_dir / "app.json").exists() and not (self.app_dir / self.entry).exists()
        if not missing:
            self.since, self.count = None, 0
            return False
        now = self._clock()
        self.since = now if self.since is None else self.since
        self.count += 1
        return self.count >= GONE_CHECKS and now - self.since >= GONE_SECONDS


PRIVATE_FILES = ("env.json", "status.json", "state.json", "control.json", "agent_token.json")


def remove_service(unit: str, data: Path, log: Callable[[str], None] = print) -> None:
    """The App was uninstalled (there's no uninstall hook): disable the unit and
    delete stored settings and state (secrets, queued alert text)."""
    log(f"App has been gone for a while; disabling {unit} and deleting its stored data")
    for cmd in (["systemctl", "disable", unit], None, ["systemctl", "daemon-reload"]):
        try:
            if cmd is None:
                Path("/etc/systemd/system", unit).unlink()
            else:
                subprocess.run(cmd, capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            pass
    for name in PRIVATE_FILES:
        try:
            (data / name).unlink()
        except OSError:
            pass


def wait(seconds: float, data: Path, step: float = 1.0) -> None:
    """Sleep up to `seconds`, waking early when the App writes control.json."""
    control = data / "control.json"

    def stamp() -> float:
        try:
            return control.stat().st_mtime
        except OSError:
            return 0.0
    before, end = stamp(), time.monotonic() + seconds
    while time.monotonic() < end:
        time.sleep(min(step, max(0.0, end - time.monotonic())))
        if stamp() != before:
            return
