"""Persistent per-App data outside the App directory.

Shared module: edit _shared/appdata.py, then run `python3 _tools/sync_shared.py`.

A Store update replaces /kvmcomm/apps/<app>/, and firmware updates replace
/kvmcomm/*, so state lives in /var/lib/nanokvm-apps/<app>/ instead. Writes are
atomic (temp file + rename) so a crash or power loss never leaves half a file.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

DATA_ROOT = Path("/var/lib/nanokvm-apps")


def data_dir(app: str, root: Path | None = None, allow_fallback: bool = True) -> Path:
    """Create (mode 700) and return the App's data dir.

    With allow_fallback (Apps), falls back to ./.data off-device, e.g. in
    preview_app.py. Services pass allow_fallback=False: if they silently used a
    different directory than the App, their IPC would split.
    """
    base = Path(root) if root is not None else DATA_ROOT
    path = base / app
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        return path
    except OSError:
        if not allow_fallback:
            raise
        fallback = Path.cwd() / ".data" / app  # e.g. preview_app.py on a laptop
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def read_json(path: Path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json(path: Path, data, mode: int = 0o600, durable: bool = True) -> None:
    """Atomic write. durable=False skips fsync (for frequently rewritten heartbeat
    files, to spare flash wear; the rename is still atomic)."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, separators=(",", ":"))
            fh.flush()
            if durable:
                os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
