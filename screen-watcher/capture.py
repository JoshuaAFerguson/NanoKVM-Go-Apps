"""Observe the target's screen: HDMI state + a low-quality JPEG from kvm_vin.

Uses the documented-by-community snapshot socket (/run/kvm/vin_snapshot.sock),
which coexists with the web UI's video stream. Full frames only: crop requests
that fail have been linked to a kvm_vin busy-loop bug, so none are sent.
"""

from __future__ import annotations

import json
import socket
import time
from pathlib import Path

import jpegdc
from rules import Observation

SNAPSHOT_SOCK = "/run/kvm/vin_snapshot.sock"
HDMI_DIR = Path("/proc/lt7911_info")
QUALITY = 5            # averages don't care about JPEG artifacts; smaller = faster decode
MAX_JPEG = 2 * 1024 * 1024
GRID_COLS, GRID_ROWS = 16, 9


class CaptureError(RuntimeError):
    pass


def hdmi_signal() -> bool:
    try:
        return (HDMI_DIR / "status").read_text().strip() == "stable"
    except OSError:
        return False


def capture_jpeg(timeout_s: float = 3.0) -> bytes:
    request = {"version": 1, "cmd": "capture_fresh", "quality": QUALITY, "timeout_ms": 1500}
    deadline = time.monotonic() + 2 * timeout_s
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout_s)
        try:
            sock.connect(SNAPSHOT_SOCK)
            sock.sendall((json.dumps(request) + "\n").encode())
            buf = b""
            while b"\n" not in buf:
                if time.monotonic() > deadline:
                    raise CaptureError("snapshot: timed out")
                chunk = sock.recv(4096)
                if not chunk:
                    raise CaptureError("snapshot socket closed")
                buf += chunk
                if len(buf) > 65536:
                    raise CaptureError("bad snapshot header")
            head, data = buf.split(b"\n", 1)
            meta = json.loads(head)
            if not isinstance(meta, dict):
                raise CaptureError("bad snapshot header")
            if not meta.get("ok"):
                raise CaptureError(f"snapshot failed: {meta.get('message', '?')}")
            size = int(meta.get("size", 0))
            if not 0 < size <= MAX_JPEG:
                raise CaptureError(f"bad snapshot size {size}")
            while len(data) < size:
                if time.monotonic() > deadline:
                    raise CaptureError("snapshot: timed out")
                chunk = sock.recv(min(65536, size - len(data)))
                if not chunk:
                    raise CaptureError("snapshot truncated")
                data += chunk
            return data[:size]
        except (OSError, ValueError, TypeError) as exc:
            raise CaptureError(f"snapshot: {exc}") from exc


def downsample(grid: list, cols: int = GRID_COLS, rows: int = GRID_ROWS) -> list:
    """Average an MCU colour grid into a fixed rows x cols grid."""
    src_rows, src_cols = len(grid), len(grid[0])
    out = []
    for r in range(rows):
        y0, y1 = r * src_rows // rows, max(r * src_rows // rows + 1, (r + 1) * src_rows // rows)
        row = []
        for c in range(cols):
            x0, x1 = c * src_cols // cols, max(c * src_cols // cols + 1, (c + 1) * src_cols // cols)
            cells = [grid[y][x] for y in range(y0, y1) for x in range(x0, x1)]
            row.append(tuple(round(sum(px[i] for px in cells) / len(cells)) for i in range(3)))
        out.append(row)
    return out


def observe() -> tuple[Observation, float]:
    """(observation, decode seconds). No signal -> Observation(False) without capturing."""
    if not hdmi_signal():
        return Observation(False), 0.0
    jpeg = capture_jpeg()
    start = time.monotonic()
    image = jpegdc.decode(jpeg)
    grid = downsample(image.mcu_rgb())
    return Observation.from_grid(True, image.width, image.height, grid), time.monotonic() - start
