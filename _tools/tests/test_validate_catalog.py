"""Tests for _tools/validate_catalog.py (run: python3 -m unittest discover -s _tools/tests)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from validate_catalog import validate_catalog  # noqa: E402

VALID_MANIFEST = {
    "app_id": "io.github.example.my_app",
    "name": "My App",
    "creator": "Example",
    "create_time": "2026-10-16",
    "version": "1.0.0",
}


def make_app(root, dir_name="my-app", manifest=None, main_src="print('ok')\n", extra=None):
    app_dir = root / dir_name
    app_dir.mkdir()
    if manifest is not None:
        text = manifest if isinstance(manifest, str) else json.dumps(manifest)
        (app_dir / "app.json").write_text(text)
    if main_src is not None:
        (app_dir / "main.py").write_text(main_src)
    for rel, content in (extra or {}).items():
        path = app_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return app_dir


class ValidateCatalogTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def errors(self):
        return validate_catalog(self.root)

    def test_valid_app_has_no_errors(self):
        make_app(self.root, manifest=VALID_MANIFEST)
        self.assertEqual(self.errors(), [])

    def test_underscore_and_dot_dirs_are_ignored(self):
        (self.root / "_tools").mkdir()
        (self.root / ".github").mkdir()
        self.assertEqual(self.errors(), [])

    def test_missing_main_py_is_reported(self):
        make_app(self.root, manifest=VALID_MANIFEST, main_src=None)
        self.assertTrue(any("main.py" in e for e in self.errors()))

    def test_main_py_syntax_error_is_reported(self):
        make_app(self.root, manifest=VALID_MANIFEST, main_src="def broken(:\n")
        self.assertTrue(any("compile" in e for e in self.errors()))

    def test_invalid_json_is_reported(self):
        make_app(self.root, manifest="{not json")
        self.assertTrue(any("app.json" in e for e in self.errors()))

    def test_missing_required_field_is_reported(self):
        manifest = {k: v for k, v in VALID_MANIFEST.items() if k != "creator"}
        make_app(self.root, manifest=manifest)
        self.assertTrue(any("creator" in e for e in self.errors()))

    def test_app_id_must_match_directory(self):
        make_app(self.root, manifest={**VALID_MANIFEST, "app_id": "io.github.example.other"})
        self.assertTrue(any("app_id" in e for e in self.errors()))

    def test_app_id_needs_three_components(self):
        make_app(self.root, manifest={**VALID_MANIFEST, "app_id": "example.my_app"})
        self.assertTrue(any("app_id" in e for e in self.errors()))

    def test_directory_name_must_be_kebab_case(self):
        make_app(self.root, dir_name="My_App",
                 manifest={**VALID_MANIFEST, "app_id": "io.github.example.My_App"})
        self.assertTrue(any("kebab" in e for e in self.errors()))

    def test_bad_create_time_and_version_are_reported(self):
        make_app(self.root, manifest={**VALID_MANIFEST, "create_time": "10/16/2026", "version": "v1"})
        errs = self.errors()
        self.assertTrue(any("create_time" in e for e in errs))
        self.assertTrue(any("version" in e for e in errs))

    def test_declared_script_must_exist(self):
        make_app(self.root, manifest={**VALID_MANIFEST, "pre_script": "pre-install.sh"})
        self.assertTrue(any("pre_script" in e for e in self.errors()))

    def test_declared_script_present_is_ok(self):
        make_app(self.root, manifest={**VALID_MANIFEST, "pre_script": "pre-install.sh"},
                 extra={"pre-install.sh": "#!/usr/bin/env bash\n"})
        self.assertEqual(self.errors(), [])

    def test_path_escaping_app_dir_is_rejected(self):
        make_app(self.root, manifest={**VALID_MANIFEST, "icon": "../secret.png"})
        self.assertTrue(any("icon" in e for e in self.errors()))

    def test_env_entries_must_be_objects_with_valid_names(self):
        make_app(self.root, manifest={**VALID_MANIFEST, "env": {"bad-name": {"label": "x"}, "OK": "nope"}})
        errs = self.errors()
        self.assertTrue(any("bad-name" in e for e in errs))
        self.assertTrue(any("OK" in e for e in errs))

    def test_duplicate_app_ids_are_reported(self):
        make_app(self.root, manifest=VALID_MANIFEST)
        # "my_app" fails kebab-case but still maps to the same app_id suffix.
        make_app(self.root, dir_name="my_app", manifest=VALID_MANIFEST)
        self.assertTrue(any("duplicate" in e for e in self.errors()))


if __name__ == "__main__":
    unittest.main()
