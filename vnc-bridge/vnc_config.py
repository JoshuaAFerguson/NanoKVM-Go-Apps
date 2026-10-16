"""VNC Bridge settings from the App's env (validated)."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass

MIN_PASSWORD, MAX_PASSWORD = 6, 8   # VNC auth only ever uses the first 8 characters


class ConfigError(ValueError):
    """Shown on screen and in the log; names the setting to fix."""


@dataclass(frozen=True)
class VncConfig:
    password: str
    port: int = 5900
    bind: str = "tailscale"     # tailscale | lan | all | <IPv4>
    quality: int = 50
    max_fps: float = 10.0
    view_only: bool = False
    max_clients: int = 2


def _int(env: dict, key: str, default: int, low: int, high: int) -> int:
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{key} must be a whole number") from None
    if not low <= value <= high:
        raise ConfigError(f"{key} must be between {low} and {high}")
    return value


def load(env: dict) -> VncConfig:
    password = env.get("VNC_PASSWORD") or ""
    if not MIN_PASSWORD <= len(password) <= MAX_PASSWORD or not password.isprintable() or not password.isascii():
        raise ConfigError(f"VNC_PASSWORD must be {MIN_PASSWORD}-{MAX_PASSWORD} printable ASCII characters "
                          "(VNC uses only the first 8)")
    bind = (env.get("VNC_BIND") or "tailscale").strip().lower()
    if bind not in ("tailscale", "lan", "all"):
        try:
            bind = str(ipaddress.IPv4Address(bind))
        except ipaddress.AddressValueError:
            raise ConfigError("VNC_BIND must be tailscale, lan, all, or an IPv4 address") from None
    view_only = (env.get("VNC_VIEW_ONLY") or "").strip().lower() in ("1", "true", "yes", "on")
    return VncConfig(password=password, port=_int(env, "VNC_PORT", 5900, 1024, 65535), bind=bind,
                     quality=_int(env, "VNC_QUALITY", 50, 10, 90),
                     max_fps=float(_int(env, "VNC_MAX_FPS", 10, 1, 30)), view_only=view_only)
