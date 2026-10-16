#!/usr/bin/env python3
"""Copy shared modules from _shared/ into the Apps that use them.

The Store installs one App directory at a time, so an App cannot import from a
sibling directory. Shared code lives in _shared/ and is copied into each App;
an App "uses" a module when it contains a file with the same name.

Usage:
  python3 _tools/sync_shared.py                 # refresh every existing copy
  python3 _tools/sync_shared.py --check         # exit 1 if any copy is stale (CI)
  python3 _tools/sync_shared.py add <app> <module> [...]   # start using a module
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

from validate_catalog import iter_app_dirs

ROOT = Path(__file__).resolve().parents[1]


def shared_modules(root: Path) -> dict[str, Path]:
    return {p.name: p for p in sorted((root / "_shared").glob("*.py"))}


def find_drift(root: Path) -> list[Path]:
    modules = shared_modules(root)
    stale = []
    for app_dir in iter_app_dirs(root):
        for name, source in modules.items():
            copy = app_dir / name
            if copy.is_file() and copy.read_bytes() != source.read_bytes():
                stale.append(copy.relative_to(root))
    return stale


def sync(root: Path) -> list[Path]:
    stale = find_drift(root)
    modules = shared_modules(root)
    for rel in stale:
        shutil.copyfile(modules[rel.name], root / rel)
    return stale


IMPORT_RE = re.compile(r"^\s*(?:import|from)\s+([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE)


def with_dependencies(modules: dict[str, Path], filenames: list[str]) -> list[str]:
    """filenames plus every shared module they import, transitively (e.g. ui -> font)."""
    out, todo = [], list(filenames)
    while todo:
        filename = todo.pop(0)
        if filename in out:
            continue
        out.append(filename)
        for name in IMPORT_RE.findall(modules[filename].read_text(encoding="utf-8")):
            if f"{name}.py" in modules and f"{name}.py" not in out:
                todo.append(f"{name}.py")
    return out


def add_modules(root: Path, app: str, names: list[str]) -> list[str]:
    modules = shared_modules(root)
    app_dir = root / app
    if app_dir not in iter_app_dirs(root):
        raise ValueError(f"'{app}' is not an App directory in {root}")
    filenames = []
    for name in names:
        filename = name if name.endswith(".py") else f"{name}.py"
        if filename not in modules:
            raise ValueError(f"no shared module '{filename}' (have: {', '.join(modules)})")
        filenames.append(filename)
    added = with_dependencies(modules, filenames)
    for filename in added:
        shutil.copyfile(modules[filename], app_dir / filename)
    return added


def main(argv: list[str]) -> int:
    try:
        if len(argv) > 1 and argv[1] == "add":
            if len(argv) < 4:
                print(__doc__, file=sys.stderr)
                return 2
            added = add_modules(ROOT, argv[2], argv[3:])
            print(f"Added {', '.join(added)} to {argv[2]}")
            return 0
        if len(argv) > 1 and argv[1] == "--check":
            stale = find_drift(ROOT)
            for rel in stale:
                print(f"ERROR {rel} differs from _shared/{rel.name}; run _tools/sync_shared.py", file=sys.stderr)
            print("FAILED" if stale else "OK: shared modules in sync")
            return 1 if stale else 0
        updated = sync(ROOT)
        print(f"Updated {len(updated)} file(s)" + "".join(f"\n  {p}" for p in updated))
        return 0
    except (ValueError, OSError) as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
