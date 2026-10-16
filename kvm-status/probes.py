"""KVM Status data sources: pure parsers plus thin readers for /proc and /sys.

Everything here reads files or makes one cheap `iw` call; no heavy subprocesses,
because the NanoKVM Go has only ~35 MB of free RAM.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

HDMI_DIR = Path("/proc/lt7911_info")
GO_PLUS_MIN_MB = 200  # Go has 128 MB (119 visible), Go+ 256 MB+ (246 visible)


@dataclass(frozen=True)
class Hdmi:
    signal: bool
    width: int = 0
    height: int = 0
    fps: int = 0

    @property
    def summary(self) -> str:
        return f"{self.width}x{self.height} @{self.fps}" if self.signal else "No signal"


@dataclass(frozen=True)
class Memory:
    total_mb: int
    available_mb: int
    swap_used_mb: int


@dataclass(frozen=True)
class Wifi:
    connected: bool
    ssid: str = ""
    signal_dbm: int = 0
    freq_mhz: int = 0

    @property
    def band(self) -> str:
        return "5G" if self.freq_mhz >= 4900 else "2.4G" if self.freq_mhz else ""

    @property
    def bars(self) -> int:
        return signal_bars(self.signal_dbm) if self.connected else 0


def _int(value: str, default: int = 0) -> int:
    try:
        return int(value.strip())
    except (ValueError, AttributeError):
        return default


# -- parsers ------------------------------------------------------------------

def parse_hdmi(fields: dict) -> Hdmi:
    if fields.get("status", "").strip() != "stable":
        return Hdmi(False)
    return Hdmi(True, _int(fields.get("width")), _int(fields.get("height")), _int(fields.get("fps")))


def parse_meminfo(text: str) -> Memory:
    values = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        values[key] = _int(rest.split()[0]) if rest.split() else 0
    swap_used = values.get("SwapTotal", 0) - values.get("SwapFree", 0)
    return Memory(values.get("MemTotal", 0) // 1024, values.get("MemAvailable", 0) // 1024, swap_used // 1024)


def model_name(total_mb: int) -> str:
    return "NanoKVM Go+" if total_mb >= GO_PLUS_MIN_MB else "NanoKVM Go"


def _cpu_totals(text: str) -> tuple[int, int]:
    # user nice system idle iowait irq softirq steal; guest columns are already in user/nice
    fields = [int(v) for v in text.split("\n", 1)[0].split()[1:9]]
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
    return sum(fields), idle


def cpu_percent(before: str, after: str) -> int:
    total_a, idle_a = _cpu_totals(before)
    total_b, idle_b = _cpu_totals(after)
    total = total_b - total_a
    return 0 if total <= 0 else round(100 * (total - (idle_b - idle_a)) / total)


def proc_ticks(stat: str) -> int:
    """utime + stime from /proc/<pid>/stat (the comm field may contain spaces)."""
    after_comm = stat.rsplit(")", 1)[1].split()
    return int(after_comm[11]) + int(after_comm[12])


def signal_bars(dbm: int) -> int:
    for bars, floor in ((4, -55), (3, -65), (2, -75), (1, -85)):
        if dbm >= floor:
            return bars
    return 0


def parse_iw_link(text: str) -> Wifi:
    if not text.startswith("Connected"):
        return Wifi(False)
    fields = {}
    for line in text.splitlines()[1:]:
        key, _, value = line.strip().partition(":")
        fields[key] = value.strip()
    signal = (fields.get("signal") or "0").split() or ["0"]
    return Wifi(True, fields.get("SSID", ""), _int(signal[0]), int(first_number(fields.get("freq", ""))))


def first_number(text: str) -> float:
    """First whitespace-separated number in text, or 0 (for /proc/loadavg, /proc/uptime, ...)."""
    try:
        return float(text.split()[0])
    except (IndexError, ValueError):
        return 0.0


def format_uptime(seconds: float) -> str:
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


class SpinDetector:
    """Flags kvm_vin when it burns CPU for `hold_s` with no video input (known firmware bug)."""

    def __init__(self, threshold: int = 80, hold_s: float = 30) -> None:
        self.threshold = threshold
        self.hold_s = hold_s
        self._since: float | None = None

    def update(self, cpu: int, signal: bool, now: float) -> bool:
        if cpu < self.threshold or signal:
            self._since = None
            return False
        if self._since is None:
            self._since = now
        return now - self._since >= self.hold_s


# -- readers (device only) ----------------------------------------------------------

def read_text(path, default: str = "") -> str:
    try:
        return Path(path).read_text(errors="replace")
    except OSError:
        return default


def read_hdmi() -> Hdmi:
    return parse_hdmi({name: read_text(HDMI_DIR / name) for name in ("status", "width", "height", "fps")})


def read_usb_state() -> str:
    """'configured' when the target has enumerated the KVM's USB devices."""
    for udc in sorted(Path("/sys/class/udc").glob("*")):
        return read_text(udc / "state").strip() or "unknown"
    return "no gadget"


def read_wifi(interface: str = "wlan0") -> Wifi:
    try:
        out = subprocess.run(["iw", "dev", interface, "link"], capture_output=True, text=True, timeout=3)
        return parse_iw_link(out.stdout)
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return Wifi(False)


def find_pid(name: str) -> int | None:
    with os.scandir("/proc") as entries:
        for entry in entries:
            if entry.name.isdigit() and read_text(f"/proc/{entry.name}/comm").strip() == name:
                return int(entry.name)
    return None


def read_temp_c() -> float | None:
    value = read_text("/sys/class/thermal/thermal_zone0/temp").strip()
    return int(value) / 1000 if value.isdigit() else None


def read_disk_free_gb(path: str = "/") -> float:
    try:
        st = os.statvfs(path)
    except OSError:
        return 0.0
    return st.f_bavail * st.f_frsize / 1e9
