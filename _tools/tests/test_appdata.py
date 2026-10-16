"""Tests for _shared/appdata.py."""

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared"))

import appdata  # noqa: E402


class AppDataTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_data_dir_created_private(self):
        path = appdata.data_dir("pet", root=self.root)
        self.assertTrue(path.is_dir())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)

    def test_round_trip_and_private_file(self):
        path = appdata.data_dir("pet", root=self.root) / "state.json"
        appdata.write_json(path, {"hunger": 3})
        self.assertEqual(appdata.read_json(path), {"hunger": 3})
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_missing_or_corrupt_returns_default(self):
        path = self.root / "x.json"
        self.assertEqual(appdata.read_json(path, {"a": 1}), {"a": 1})
        path.write_text("{oops")
        self.assertIsNone(appdata.read_json(path))

    def test_failed_write_leaves_old_file_and_no_temp(self):
        path = self.root / "s.json"
        appdata.write_json(path, {"v": 1})
        with self.assertRaises(TypeError):
            appdata.write_json(path, {"v": object()})
        self.assertEqual(appdata.read_json(path), {"v": 1})
        self.assertEqual([p.name for p in self.root.iterdir()], ["s.json"])


if __name__ == "__main__":
    unittest.main()
