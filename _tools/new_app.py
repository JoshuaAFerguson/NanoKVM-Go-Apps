#!/usr/bin/env python3
"""Scaffold a new App from _template/ with a matching app_id.

Usage: python3 _tools/new_app.py <kebab-case-name> ["Display Name"]
"""

from __future__ import annotations

import datetime
import json
import shutil
import sys
from pathlib import Path

from sync_shared import add_modules
from validate_catalog import DIR_NAME_RE

ROOT = Path(__file__).resolve().parents[1]
APP_ID_PREFIX = "io.github.joshuaaferguson"
STARTER_MODULES = ["ui", "font", "applog"]


def scaffold(dir_name: str, display_name: str) -> Path:
    if not DIR_NAME_RE.match(dir_name):
        raise ValueError("name must be lowercase kebab-case, e.g. weather-panel")
    target = ROOT / dir_name
    if target.exists():
        raise ValueError(f"{target} already exists")
    shutil.copytree(ROOT / "_template", target)
    try:
        _personalize(target, dir_name, display_name)
    except Exception:
        shutil.rmtree(target, ignore_errors=True)  # don't leave a half-built App behind
        raise
    return target


def _personalize(target: Path, dir_name: str, display_name: str) -> None:
    manifest_path = target / "app.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    updated = {
        **manifest,
        "app_id": f"{APP_ID_PREFIX}.{dir_name.replace('-', '_')}",
        "name": display_name,
        "create_time": datetime.date.today().isoformat(),
    }
    manifest_path.write_text(json.dumps(updated, indent=2) + "\n", encoding="utf-8")
    add_modules(ROOT, dir_name, STARTER_MODULES)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    dir_name = argv[1]
    display_name = argv[2] if len(argv) > 2 else dir_name.replace("-", " ").title()
    try:
        print(f"Created {scaffold(dir_name, display_name)}")
    except (ValueError, OSError) as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
