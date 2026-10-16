"""Tests for _tools/sync_shared.py."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sync_shared import add_modules, find_drift, sync  # noqa: E402


class SyncSharedTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "_shared").mkdir()
        (self.root / "_shared" / "ui.py").write_text("NEW = 1\n")
        (self.root / "_shared" / "kvm_mcp.py").write_text("MCP = 1\n")
        (self.root / "app-a").mkdir()
        (self.root / "app-a" / "ui.py").write_text("OLD = 1\n")
        (self.root / "app-b").mkdir()
        (self.root / "app-b" / "main.py").write_text("")

    def tearDown(self):
        self._tmp.cleanup()

    def test_find_drift_reports_stale_copies_only(self):
        self.assertEqual(find_drift(self.root), [Path("app-a/ui.py")])

    def test_sync_updates_existing_copies_and_leaves_others(self):
        sync(self.root)
        self.assertEqual((self.root / "app-a" / "ui.py").read_text(), "NEW = 1\n")
        self.assertFalse((self.root / "app-b" / "ui.py").exists())
        self.assertEqual(find_drift(self.root), [])

    def test_add_modules_copies_into_app(self):
        add_modules(self.root, "app-b", ["kvm_mcp"])
        self.assertEqual((self.root / "app-b" / "kvm_mcp.py").read_text(), "MCP = 1\n")

    def test_add_modules_brings_dependencies(self):
        (self.root / "_shared" / "font.py").write_text("GLYPH_PX = 8\n")
        (self.root / "_shared" / "ui.py").write_text("import font\nfrom font import GLYPH_PX\n")
        added = add_modules(self.root, "app-b", ["ui"])
        self.assertEqual(added, ["ui.py", "font.py"])
        self.assertTrue((self.root / "app-b" / "font.py").exists())

    def test_add_unknown_module_fails(self):
        with self.assertRaises(ValueError):
            add_modules(self.root, "app-b", ["nope"])


if __name__ == "__main__":
    unittest.main()
