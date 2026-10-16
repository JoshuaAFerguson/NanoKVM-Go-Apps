"""Turn a status Snapshot into dashboard tiles (pure; no device access)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from probes import Hdmi, Memory, Wifi, format_uptime

HOT_C, WARM_C = 75.0, 65.0
LOW_MEM_MB, TIGHT_MEM_MB = 12, 25
COMMON_HEIGHTS = {720: "720p", 1080: "1080p", 1440: "1440p", 2160: "4K"}
STANDARD_WIDTHS = {720: 1280, 1080: 1920, 1440: 2560, 2160: 3840}


class Level(Enum):
    OK = "ok"
    WARN = "warn"
    ERR = "err"


@dataclass(frozen=True)
class Snapshot:
    model: str
    hdmi: Hdmi
    usb_state: str
    usb_speed: str
    wifi: Wifi
    lan_ip: str
    tailscale_ip: str
    cpu: int
    temp_c: float | None
    load: float
    mem: Memory
    uptime_s: float
    disk_free_gb: float
    vin_stuck: bool


@dataclass(frozen=True)
class Tile:
    label: str
    value: str
    detail: str
    level: Level


def video_value(hdmi: Hdmi) -> str:
    if not hdmi.signal:
        return "None"
    name = COMMON_HEIGHTS.get(hdmi.height)
    if name and STANDARD_WIDTHS.get(hdmi.height) == hdmi.width:
        return f"{name}{hdmi.fps}"
    return f"{hdmi.width}w{hdmi.fps}"


def _video(s: Snapshot) -> Tile:
    if not s.hdmi.signal:
        return Tile("Video", "None", "no video from target", Level.WARN)
    return Tile("Video", video_value(s.hdmi), f"{s.hdmi.width}x{s.hdmi.height}", Level.OK)


def _usb(s: Snapshot) -> Tile:
    if s.usb_state == "configured":
        return Tile("USB", "Linked", s.usb_speed or "target enumerated", Level.OK)
    return Tile("USB", "None", s.usb_state or "unknown", Level.WARN)


def _wifi(s: Snapshot) -> Tile:
    if not s.wifi.connected:
        if s.lan_ip:  # wired or otherwise online without Wi-Fi
            return Tile("Wi-Fi", "n/a", "not in use", Level.WARN)
        return Tile("Wi-Fi", "Down", "not connected", Level.ERR)
    level = Level.OK if s.wifi.bars >= 2 else Level.WARN
    return Tile("Wi-Fi", f"{s.wifi.signal_dbm}dBm", f"{s.wifi.ssid} {s.wifi.band}".strip(), level)


def _network(s: Snapshot) -> Tile:
    detail = s.tailscale_ip or "Tailscale off"
    return Tile("LAN / Tailnet", s.lan_ip or "no IP", detail, Level.OK if s.lan_ip else Level.ERR)


def _system(s: Snapshot) -> Tile:
    temp = f"{s.temp_c:.0f}C" if s.temp_c is not None else "?C"
    level = Level.OK
    if s.temp_c is not None and s.temp_c >= HOT_C:
        level = Level.ERR
    elif (s.temp_c is not None and s.temp_c >= WARM_C) or s.vin_stuck:
        level = Level.WARN
    return Tile("System", f"{s.cpu}%", f"{temp} load {s.load:.1f}", level)


def _memory(s: Snapshot) -> Tile:
    level = Level.ERR if s.mem.available_mb < LOW_MEM_MB else (
        Level.WARN if s.mem.available_mb < TIGHT_MEM_MB else Level.OK)
    return Tile("Memory", f"{s.mem.available_mb}MB", f"/{s.mem.total_mb} swap {s.mem.swap_used_mb}", level)


def build_tiles(s: Snapshot) -> list[Tile]:
    return [_video(s), _usb(s), _wifi(s), _network(s), _system(s), _memory(s)]


def footer(s: Snapshot, armed: bool) -> str:
    if s.vin_stuck:
        return "Tap again to restart video" if armed else "Video service stuck - tap to restart"
    return f"up {format_uptime(s.uptime_s)}   disk {s.disk_free_gb:.1f}G free"
