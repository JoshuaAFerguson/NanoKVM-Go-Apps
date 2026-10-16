"""Tiny file logger for Apps: the launcher discards child stdout/stderr.

Shared module: edit _shared/applog.py, then run `python3 _tools/sync_shared.py`.

Writes to <App dir>/log.txt, trimmed to MAX_BYTES so it can't fill storage.
Read it with: ssh root@<device> cat /kvmcomm/apps/<app>/log.txt
"""

from __future__ import annotations

import sys
import threading
import time
import traceback
from pathlib import Path

MAX_BYTES = 64 * 1024
_LOCK = threading.Lock()  # the UI and worker threads both log


class AppLog:
    def __init__(self, app_dir: Path, name: str = "log.txt") -> None:
        self.path = Path(app_dir) / name

    def write(self, level: str, message: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {level} {message}\n"
        try:
            with _LOCK:
                if self.path.exists() and self.path.stat().st_size > MAX_BYTES:
                    tail = self.path.read_bytes()[-MAX_BYTES // 2:].decode("utf-8", "replace")
                    self.path.write_text(tail, encoding="utf-8")
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(line)
        except OSError as exc:  # logging must never take the App down
            print(f"applog: {exc}: {line}", file=sys.stderr, end="")

    def info(self, message: str) -> None:
        self.write("INFO", message)

    def warn(self, message: str) -> None:
        self.write("WARN", message)

    def error(self, message: str) -> None:
        self.write("ERROR", message)

    def exception(self, context: str) -> None:
        self.write("ERROR", f"{context}\n{traceback.format_exc().rstrip()}")

    def guard(self, fn):
        """Wrap a callback so an exception is logged, then re-raised."""
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception:
                self.exception(f"unhandled error in {getattr(fn, '__name__', fn)}")
                raise
        return wrapper
