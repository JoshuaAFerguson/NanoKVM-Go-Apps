"""Tests for _shared/applog.py."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared"))

import applog  # noqa: E402
from applog import AppLog  # noqa: E402


class AppLogTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.log = AppLog(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_writes_levelled_lines(self):
        self.log.info("hello")
        self.log.error("bad")
        text = self.log.path.read_text()
        self.assertIn("INFO hello", text)
        self.assertIn("ERROR bad", text)

    def test_guard_logs_traceback_and_reraises(self):
        @self.log.guard
        def boom():
            raise RuntimeError("kaboom")

        with self.assertRaises(RuntimeError):
            boom()
        text = self.log.path.read_text()
        self.assertIn("unhandled error in boom", text)
        self.assertIn("RuntimeError: kaboom", text)

    def test_file_is_trimmed_when_too_big(self):
        self.log.path.write_text("x" * (applog.MAX_BYTES + 10))
        self.log.info("after")
        self.assertLess(self.log.path.stat().st_size, applog.MAX_BYTES)
        self.assertTrue(self.log.path.read_text().endswith("INFO after\n"))


if __name__ == "__main__":
    unittest.main()
