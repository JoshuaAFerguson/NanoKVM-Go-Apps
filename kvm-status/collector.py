"""Background sampler: reads the probes on a schedule and publishes Snapshots."""

from __future__ import annotations

import dataclasses
import glob
import os
import signal
import threading
import time
from typing import Callable

import netinfo
import probes
from tiles import Snapshot

FAST_S = 2.0       # cheap /proc and /sys reads
WIFI_S = 10.0      # spawns `iw`, so less often (in its own thread)
STALE_S = 3 * FAST_S
VERIFY_S = 0.5     # fresh kvm_vin CPU measurement before a restart
CLK_TCK = os.sysconf("SC_CLK_TCK")
UDC_SPEED_GLOB = "/sys/class/udc/*/current_speed"
VIN = "kvm_vin"


def _vin_ticks(pid: int) -> int | None:
    stat = probes.read_text(f"/proc/{pid}/stat")
    try:
        return probes.proc_ticks(stat) if stat else None
    except (IndexError, ValueError):
        return None


def _identity(pid: int) -> str:
    """Start time (field 22 of /proc/<pid>/stat): tells a reused PID apart."""
    stat = probes.read_text(f"/proc/{pid}/stat")
    try:
        return stat[stat.rindex(")") + 2:].split()[19] if stat else ""
    except (IndexError, ValueError):
        return ""


class Collector:
    def __init__(self, log: Callable[[str], None]) -> None:
        self._log = log
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._reset = threading.Event()  # set by restart; consumed by the sampler thread
        self._snapshot: Snapshot | None = None
        self._published_at = 0.0
        self.version = 0
        self._detector = probes.SpinDetector()
        self._wifi = probes.Wifi(False)
        self._cpu_prev = probes.read_text("/proc/stat")
        self._vin_prev: tuple[int, int, float] | None = None  # pid, ticks, time
        self._failures = 0

    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True).start()
        threading.Thread(target=self._wifi_loop, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> tuple[Snapshot | None, int]:
        with self._lock:
            return self._snapshot, self.version

    def is_stale(self) -> bool:
        with self._lock:
            return self._snapshot is not None and time.monotonic() - self._published_at > STALE_S

    # -- restart (called from a UI-spawned thread) -------------------------

    def _fresh_vin_cpu(self) -> tuple[int | None, str, int]:
        """(pid, process identity, CPU %) of kvm_vin measured over VERIFY_S."""
        pid = probes.find_pid(VIN)
        before = _vin_ticks(pid) if pid else None
        if before is None:
            return None, "", 0
        identity = _identity(pid)
        time.sleep(VERIFY_S)
        after = _vin_ticks(pid)
        return pid, identity, 0 if after is None else round(100 * (after - before) / CLK_TCK / VERIFY_S)

    def restart_video_service(self) -> str:
        """SIGTERM kvm_vin (kvmcomm restarts it in ~7s), but only if it is *still* stuck."""
        if probes.read_hdmi().signal:
            return "Video is back - not restarting"
        pid, identity, cpu = self._fresh_vin_cpu()
        if cpu < probes.SpinDetector().threshold:
            self._reset.set()
            return f"Video service recovered ({cpu}% CPU)"
        if probes.read_hdmi().signal:  # video came back while measuring: leave it alone
            self._reset.set()
            return "Video is back - not restarting"
        # Signal only the process that was measured: same PID, same start time, same name.
        if pid is None or not identity or _identity(pid) != identity \
                or probes.read_text(f"/proc/{pid}/comm").strip() != VIN:
            return "Video service changed - not restarting"
        os.kill(pid, signal.SIGTERM)
        self._reset.set()
        with self._lock:  # hide the warning now rather than after the next sample
            if self._snapshot is not None:
                self._snapshot = dataclasses.replace(self._snapshot, vin_stuck=False)
                self.version += 1
        self._log(f"restarted {VIN} (pid {pid}, {cpu}% CPU)")
        return "Video service restarting..."

    # -- sampling ---------------------------------------------------------

    def _wifi_loop(self) -> None:
        while not self._stop.is_set():
            self._wifi = probes.read_wifi()  # atomic reference swap
            self._stop.wait(WIFI_S)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                snap = self._sample(time.monotonic())
                with self._lock:
                    self._snapshot = snap
                    self._published_at = time.monotonic()
                    self.version += 1
                self._failures = 0
            except Exception as exc:  # keep sampling; a bad read must not kill the dashboard
                self._failures += 1
                if self._failures in (1, 10) or self._failures % 100 == 0:  # rate-limited
                    self._log(f"sample failed x{self._failures}: {type(exc).__name__}: {exc}")
            self._stop.wait(FAST_S)

    def _vin_cpu(self) -> int:
        pid = probes.find_pid(VIN)
        ticks = _vin_ticks(pid) if pid else None
        now = time.monotonic()  # taken right next to the stat read
        if ticks is None:
            self._vin_prev = None
            return 0
        prev, self._vin_prev = self._vin_prev, (pid, ticks, now)
        if prev is None or prev[0] != pid or now <= prev[2]:
            return 0
        return round(100 * (ticks - prev[1]) / CLK_TCK / (now - prev[2]))

    def _sample(self, now: float) -> Snapshot:
        if self._reset.is_set():
            self._reset.clear()
            self._detector = probes.SpinDetector()
            self._vin_prev = None
        cpu_now = probes.read_text("/proc/stat")
        cpu = probes.cpu_percent(self._cpu_prev, cpu_now) if self._cpu_prev and cpu_now else 0
        self._cpu_prev = cpu_now
        hdmi = probes.read_hdmi()
        mem = probes.parse_meminfo(probes.read_text("/proc/meminfo"))
        stuck = self._detector.update(self._vin_cpu(), hdmi.signal, now)
        speeds = sorted(glob.glob(UDC_SPEED_GLOB))
        udc_speed = probes.read_text(speeds[0]).strip() if speeds else ""
        return Snapshot(
            model=probes.model_name(mem.total_mb),
            hdmi=hdmi,
            usb_state=probes.read_usb_state(),
            usb_speed="" if udc_speed == "UNKNOWN" else udc_speed,
            wifi=self._wifi,
            lan_ip=netinfo.interface_ipv4("wlan0") or netinfo.interface_ipv4("eth0"),
            tailscale_ip=netinfo.interface_ipv4("tailscale0"),
            cpu=cpu,
            temp_c=probes.read_temp_c(),
            load=probes.first_number(probes.read_text("/proc/loadavg")),
            mem=mem,
            uptime_s=probes.first_number(probes.read_text("/proc/uptime")),
            disk_free_gb=probes.read_disk_free_gb("/"),
            vin_stuck=stuck,
        )
