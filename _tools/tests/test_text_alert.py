"""Tests for text-alert/matcher.py (OCR text matching with cooldowns)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "text-alert"))

import matcher  # noqa: E402
from matcher import Matcher, MatchError  # noqa: E402


class NormalizeTest(unittest.TestCase):
    def test_ignores_case_spaces_and_punctuation(self):
        self.assertEqual(matcher.normalize("Build  Succeeded!"), "buildsucceeded")
        self.assertEqual(matcher.normalize("ERROR 0x0000007B"), "error0x0000007b")

    def test_keeps_non_ascii_letters(self):
        self.assertEqual(matcher.normalize("Größe Ü"), "größeü")


class ParsePhrasesTest(unittest.TestCase):
    def test_semicolons_and_newlines(self):
        self.assertEqual(matcher.parse_phrases("Build succeeded; kernel panic\nInstall complete"),
                         ("Build succeeded", "kernel panic", "Install complete"))

    def test_too_short_or_too_many(self):
        with self.assertRaises(MatchError):
            matcher.parse_phrases("ok")  # normalizes to 2 chars: would match everything
        with self.assertRaises(MatchError):
            matcher.parse_phrases(";".join(f"phrase{i}" for i in range(40)))

    def test_empty(self):
        self.assertEqual(matcher.parse_phrases(""), ())


class MatcherTest(unittest.TestCase):
    def test_matches_ocr_without_spaces(self):
        m = Matcher(("Build succeeded",), cooldown_s=600)
        self.assertEqual(m.check("...12 tests passed.BUILDSUCCEEDEDin 4m", now=0), ["Build succeeded"])

    def test_cooldown_per_phrase(self):
        m = Matcher(("Build succeeded", "Error"), cooldown_s=600)
        self.assertEqual(m.check("Build succeeded", now=0), ["Build succeeded"])
        self.assertEqual(m.check("Build succeeded and Error", now=100), ["Error"])
        self.assertEqual(m.check("Build succeeded", now=601), ["Build succeeded"])

    def test_context_never_leaks_unrelated_text(self):
        self.assertEqual(matcher.context("secret-password-123 and more", "kernel panic"), "")
        snippet = matcher.context("İİ secret-password-123 Kernel panic now", "kernel panic", width=30)
        self.assertIn("Kernel panic", snippet)

    def test_context_fits_long_phrases(self):
        phrase = "Installation of the operating system completed successfully"
        snippet = matcher.context("x" * 300 + phrase + "y" * 300, phrase, width=40)
        self.assertIn(phrase, snippet)

    def test_context_snippet_around_match(self):
        text = "x" * 200 + "Kernel panic - not syncing" + "y" * 200
        snippet = matcher.context(text, "kernel panic", width=60)
        self.assertIn("Kernel panic", snippet)
        self.assertLessEqual(len(snippet), 70)



import json  # noqa: E402
import tempfile  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared"))

import textd  # noqa: E402
from applog import AppLog  # noqa: E402

_LOG_DIR = tempfile.TemporaryDirectory()
textd.LOG = AppLog(Path(_LOG_DIR.name), "service-log.txt")


class FakeMcp:
    def __init__(self, tools=("current_context", "read_recent_capture")):
        self.tools = list(tools)
        self.captures = {}  # captured_at -> text
        self.broken = set()

    def tool_names(self):
        return self.tools

    def call(self, name, args=None):
        if name == "current_context":
            return {"recent_observations": [{"captured_at": at} for at in sorted(self.captures)][-5:]}
        if name == "read_recent_capture":
            if args["at"] in self.broken:
                from kvm_mcp import McpError
                raise McpError("capture expired")
            return {"ocr": {"text": self.captures[args["at"]]}}
        raise AssertionError(name)


class FakeNotifier:
    def __init__(self):
        self.sent = []
        self.channels = [type("C", (), {"kind": "ntfy"})()]

    def send(self, title, message, level="alert"):
        self.sent.append((title, message))
        return {"ntfy": "ok"}


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.app, self.data = root / "app", root / "data"
        self.app.mkdir()
        self.data.mkdir()
        (self.app / "textd.py").write_text("")
        self.write_env({"WATCH_TEXTS": "Build succeeded; kernel panic", "NANOKVM_MCP_KEY": "k"})
        self.mcp = FakeMcp()
        self.t = 0.0

    def tearDown(self):
        self._tmp.cleanup()

    def write_env(self, values):
        env = {k: {"value": v} for k, v in values.items()}
        (self.app / "app.json").write_text(json.dumps({"env": env}))

    def service(self):
        svc = textd.Service(self.app, self.data, mcp_factory=lambda key: self.mcp, clock=lambda: self.t)
        svc.reload()
        svc.alerter.notifier = FakeNotifier()
        return svc

    def test_history_is_skipped_then_new_captures_match(self):
        self.mcp.captures["2026-10-16T10:00:00Z"] = "BUILD SUCCEEDED long ago"
        svc = self.service()
        svc.tick()
        self.assertEqual(svc.alerter.notifier.sent, [])
        self.mcp.captures["2026-10-16T10:05:00Z"] = "...Buildsucceededin 3m 12s"
        svc.tick()
        self.assertEqual([t for t, _ in svc.alerter.notifier.sent], ["Seen on screen: Build succeeded"])
        svc.tick()  # same capture isn't checked twice
        self.assertEqual(len(svc.alerter.notifier.sent), 1)

    def test_last_seen_persists_across_restarts(self):
        self.mcp.captures["a"] = "nothing"
        self.service().tick()
        self.mcp.captures["b"] = "Kernel panic - not syncing"
        svc = self.service()
        svc.tick()
        self.assertEqual(len(svc.alerter.notifier.sent), 1)

    def test_mixed_timestamp_formats_and_late_captures(self):
        self.mcp.captures["2026-10-16T10:00:00.5Z"] = "old"
        svc = self.service()
        svc.tick()
        self.mcp.captures["2026-10-16T10:00:00Z"] = "Kernel panic arrived late"   # sorts oddly as a string
        self.mcp.captures["2026-10-16T10:00:01+00:00"] = "Build succeeded"
        svc.tick()
        self.assertEqual(len(svc.alerter.notifier.sent), 2)
        svc.tick()
        self.assertEqual(len(svc.alerter.notifier.sent), 2)  # nothing re-checked

    def test_failing_capture_read_does_not_block_others(self):
        self.mcp.captures["a"] = "nothing"
        svc = self.service()
        svc.tick()
        self.mcp.captures["b"] = "Kernel panic"
        self.mcp.broken.add("b")
        self.mcp.captures["c"] = "Build succeeded"
        svc.tick()
        self.assertEqual([t for t, _ in svc.alerter.notifier.sent], ["Seen on screen: Build succeeded"])

    def test_corrupt_state_rebaselines(self):
        (self.data / "state.json").write_text('{"seen": 5}')
        self.mcp.captures["a"] = "Kernel panic"
        svc = self.service()
        svc.tick()  # treated as first run: history skipped, no crash
        self.assertEqual(svc.alerter.notifier.sent, [])

    def test_go_without_memory_fabric_reports_requirement(self):
        self.mcp = FakeMcp(tools=("press_keys",))
        svc = self.service()
        svc.tick()
        self.assertIn("Go+", json.loads((self.data / "status.json").read_text())["error"])

    def test_missing_phrases_or_key_are_reported(self):
        self.write_env({"WATCH_TEXTS": "", "NANOKVM_MCP_KEY": "k"})
        svc = self.service()
        svc.tick()
        self.assertIn("WATCH_TEXTS", svc.error)
        self.write_env({"WATCH_TEXTS": "Build succeeded", "NANOKVM_MCP_KEY": ""})
        svc.env_stamp = None
        svc.tick()
        self.assertIn("NANOKVM_MCP_KEY", svc.error)


if __name__ == "__main__":
    unittest.main()
