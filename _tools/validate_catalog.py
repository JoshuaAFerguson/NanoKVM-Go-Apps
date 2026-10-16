#!/usr/bin/env python3
"""Validate that every top-level App directory meets the NanoKVM Go Store rules.

The device downloads this repo's default branch as a ZIP and treats each
top-level directory not starting with "_" (or ".") as an App. These checks
mirror the launcher's discovery rules from the upstream `base` branch guide so
a broken App is caught in CI instead of silently missing from the Store.

Usage: python3 _tools/validate_catalog.py [catalog_root]
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any

REQUIRED_FIELDS = ("app_id", "name", "creator", "create_time", "version")
PATH_FIELDS = ("icon", "pre_script", "post_script")
DIR_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
APP_ID_PART_RE = re.compile(r"^[a-z][a-z0-9_]*$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
MIN_APP_ID_PARTS = 3


def iter_app_dirs(root: Path) -> list[Path]:
    return sorted(
        p for p in root.iterdir()
        if p.is_dir() and not p.name.startswith(("_", "."))
    )


def is_safe_relative(rel: str) -> bool:
    path = PurePosixPath(rel)
    return bool(rel) and not path.is_absolute() and ".." not in path.parts


def check_app_id(app_id: Any, dir_name: str) -> list[str]:
    if not isinstance(app_id, str):
        return []  # reported by the required-field check
    parts = app_id.split(".")
    if len(parts) < MIN_APP_ID_PARTS or not all(APP_ID_PART_RE.match(p) for p in parts):
        return [f"app_id '{app_id}' must be reverse-domain with >= {MIN_APP_ID_PARTS} lowercase parts"]
    expected = dir_name.replace("-", "_")
    if parts[-1] != expected:
        return [f"app_id '{app_id}' must end with '.{expected}' to match the directory name"]
    return []


def check_env(env: Any) -> list[str]:
    if env is None:
        return []
    if not isinstance(env, dict):
        return ["env must be an object"]
    errors = []
    for name, spec in env.items():
        if not ENV_NAME_RE.match(name):
            errors.append(f"env key '{name}' must be UPPER_SNAKE_CASE")
        if not isinstance(spec, dict):
            errors.append(f"env '{name}' must be an object (label, default, required, secret, ...)")
    return errors


def check_manifest(manifest: dict, app_dir: Path) -> list[str]:
    errors = [
        f"missing or empty required field '{field}'"
        for field in REQUIRED_FIELDS
        if not isinstance(manifest.get(field), str) or not manifest[field].strip()
    ]
    errors += check_app_id(manifest.get("app_id"), app_dir.name)
    if isinstance(manifest.get("create_time"), str) and not DATE_RE.match(manifest["create_time"]):
        errors.append("create_time must be YYYY-MM-DD")
    if isinstance(manifest.get("version"), str) and not VERSION_RE.match(manifest["version"]):
        errors.append("version must be semver (e.g. 1.0.0)")
    for field in PATH_FIELDS:
        rel = manifest.get(field)
        if rel is None:
            continue
        if not isinstance(rel, str) or not is_safe_relative(rel):
            errors.append(f"{field} must be a relative path inside the App directory")
        elif not (app_dir / rel).is_file():
            errors.append(f"{field} '{rel}' is declared but the file does not exist")
    errors += check_env(manifest.get("env"))
    return errors


def load_manifest(app_dir: Path) -> tuple[dict | None, list[str]]:
    path = app_dir / "app.json"
    if not path.is_file():
        return None, ["app.json is missing"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return None, [f"app.json is not valid JSON: {exc}"]
    if not isinstance(data, dict):
        return None, ["app.json must be a JSON object"]
    return data, []


def check_main(app_dir: Path) -> list[str]:
    main = app_dir / "main.py"
    if not main.is_file():
        return ["main.py is missing"]
    try:
        compile(main.read_text(encoding="utf-8"), str(main), "exec")
    except (SyntaxError, UnicodeDecodeError) as exc:
        return [f"main.py does not compile: {exc}"]
    return []


def validate_app(app_dir: Path) -> tuple[str | None, list[str]]:
    errors = []
    if not DIR_NAME_RE.match(app_dir.name):
        errors.append("directory name must be lowercase kebab-case (e.g. my-app)")
    errors += check_main(app_dir)
    manifest, load_errors = load_manifest(app_dir)
    errors += load_errors
    if manifest is not None:
        errors += check_manifest(manifest, app_dir)
    app_id = manifest.get("app_id") if manifest else None
    return (app_id if isinstance(app_id, str) else None), errors


def validate_catalog(root: Path) -> list[str]:
    errors: list[str] = []
    seen: dict[str, str] = {}
    for app_dir in iter_app_dirs(root):
        app_id, app_errors = validate_app(app_dir)
        errors += [f"{app_dir.name}: {e}" for e in app_errors]
        if app_id and app_id in seen:
            errors.append(f"{app_dir.name}: duplicate app_id '{app_id}' (also in {seen[app_id]})")
        elif app_id:
            seen[app_id] = app_dir.name
    return errors


def main(argv: list[str]) -> int:
    root = Path(argv[1] if len(argv) > 1 else Path(__file__).resolve().parents[1])
    apps = iter_app_dirs(root)
    errors = validate_catalog(root)
    for error in errors:
        print(f"ERROR {error}", file=sys.stderr)
    status = "FAILED" if errors else "OK"
    print(f"{status}: {len(apps)} app(s), {len(errors)} error(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
