"""Client for the KVM video service's snapshot socket (kvm_vin).

Shared module: edit _shared/vinsnap.py, then run `python3 _tools/sync_shared.py`.

Protocol (observed on current firmware; not an official API): send one JSON
line {"version":1,"cmd":"capture_fresh","quality":Q,"timeout_ms":T}, receive a
JSON line with "ok" and "size", then `size` bytes of JPEG. Coexists with the
web UI's video stream. Only full frames are requested: failed crop requests
have been linked to a kvm_vin busy-loop bug.
"""

from __future__ import annotations

import json
import socket
import time
from dataclasses import dataclass
from pathlib import Path

SNAPSHOT_SOCK = "/run/kvm/vin_snapshot.sock"
HDMI_DIR = Path("/proc/lt7911_info")
MAX_JPEG = 4 * 1024 * 1024
MAX_HEADER = 65536


class SnapshotError(RuntimeError):
    pass


@dataclass(frozen=True)
class Snapshot:
    width: int
    height: int
    jpeg: bytes


def hdmi_signal() -> bool:
    try:
        return (HDMI_DIR / "status").read_text().strip() == "stable"
    except OSError:
        return False


def capture(quality: int = 30, timeout_s: float = 3.0, path: str = SNAPSHOT_SOCK) -> Snapshot:
    request = {"version": 1, "cmd": "capture_fresh", "quality": max(1, min(99, int(quality))),
               "timeout_ms": int(timeout_s * 500)}
    deadline = time.monotonic() + 2 * timeout_s
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout_s)
        try:
            sock.connect(path)
            sock.sendall((json.dumps(request) + "\n").encode())
            buf = b""
            while b"\n" not in buf:
                if time.monotonic() > deadline:
                    raise SnapshotError("snapshot: timed out")
                chunk = sock.recv(4096)
                if not chunk:
                    raise SnapshotError("snapshot socket closed")
                buf += chunk
                if len(buf) > MAX_HEADER:
                    raise SnapshotError("bad snapshot header")
            head, data = buf.split(b"\n", 1)
            meta = json.loads(head)
            if not isinstance(meta, dict):
                raise SnapshotError("bad snapshot header")
            if not meta.get("ok"):
                raise SnapshotError(f"snapshot failed: {str(meta.get('message', '?'))[:80]}")
            size = int(meta.get("size", 0))
            if not 0 < size <= MAX_JPEG:
                raise SnapshotError(f"bad snapshot size {size}")
            while len(data) < size:
                if time.monotonic() > deadline:
                    raise SnapshotError("snapshot: timed out")
                chunk = sock.recv(min(65536, size - len(data)))
                if not chunk:
                    raise SnapshotError("snapshot truncated")
                data += chunk
            return Snapshot(int(meta.get("width", 0)), int(meta.get("height", 0)), data[:size])
        except (OSError, ValueError, TypeError) as exc:
            raise SnapshotError(f"snapshot: {exc}") from exc
