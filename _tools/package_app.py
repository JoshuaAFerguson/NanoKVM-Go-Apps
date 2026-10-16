#!/usr/bin/env python3
"""Package one App directory as a ZIP for Settings > Apps > Installed > Upload ZIP.

The uploader requires exactly one top-level App directory inside the archive.

Usage: python3 _tools/package_app.py <app-dir> [out-dir]   (default out-dir: dist/)
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

from validate_catalog import validate_app

EXCLUDED_NAMES = {"__pycache__", ".DS_Store", ".pytest_cache"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo"}


def is_excluded(path: Path) -> bool:
    return bool(EXCLUDED_NAMES.intersection(path.parts)) or path.suffix in EXCLUDED_SUFFIXES


def package(app_dir: Path, out_dir: Path) -> Path:
    _, errors = validate_app(app_dir)
    if errors:
        raise ValueError(f"{app_dir.name} is invalid:\n  " + "\n  ".join(errors))
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"{app_dir.name}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(app_dir.rglob("*")):
            rel = path.relative_to(app_dir.parent)
            if path.is_file() and not is_excluded(rel):
                zf.write(path, rel.as_posix())
    return zip_path


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    app_dir = Path(argv[1]).resolve()
    out_dir = Path(argv[2]) if len(argv) > 2 else app_dir.parent / "dist"
    try:
        print(f"Wrote {package(app_dir, out_dir)}")
    except (ValueError, OSError) as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
