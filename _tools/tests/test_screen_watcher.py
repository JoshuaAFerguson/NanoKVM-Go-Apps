"""Tests for screen-watcher/watcher.py and capture.py pure helpers."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "screen-watcher"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared"))

import capture  # noqa: E402
import watcher  # noqa: E402
from applog import AppLog  # noqa: E402

_LOG_DIR = tempfile.TemporaryDirectory()
watcher.LOG = AppLog(Path(_LOG_DIR.name), "service-log.txt")  # never write into the repo's App dir


class EnvTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.app = Path(self._tmp.name) / "app"
        self.data = Path(self._tmp.name) / "data"
        self.app.mkdir()
        self.data.mkdir()
        manifest = {"env": {"CHECK_S": {"default": "30"}, "NTFY_URL": {"value": "https://ntfy.sh/a"},
                            "BLACK_MIN": {"default": "0"}}}
        (self.app / "app.json").write_text(json.dumps(manifest))

    def tearDown(self):
        self._tmp.cleanup()
        os.environ.pop("CHECK_S", None)

    def test_value_then_default(self):
        env = watcher.resolve_env(self.app, self.data)
        self.assertEqual((env["CHECK_S"], env["NTFY_URL"]), ("30", "https://ntfy.sh/a"))

    def test_os_environ_wins_then_overlay_wins(self):
        os.environ["CHECK_S"] = "60"
        self.assertEqual(watcher.resolve_env(self.app, self.data)["CHECK_S"], "60")
        (self.data / "env.json").write_text(json.dumps({"CHECK_S": "90", "UNKNOWN": "x"}))
        env = watcher.resolve_env(self.app, self.data)
        self.assertEqual(env["CHECK_S"], "90")
        self.assertNotIn("UNKNOWN", env)

    def test_settings_are_clamped(self):
        settings, check_s = watcher.settings_from({"CHECK_S": "1", "BLACK_MIN": "junk", "SIGNAL_GRACE_S": "99999"})
        self.assertEqual(check_s, watcher.MIN_CHECK_S)
        self.assertEqual(settings.black_min, 0)
        self.assertEqual(settings.signal_grace_s, 3600)


class DownsampleTest(unittest.TestCase):
    def test_fixed_grid_and_averaging(self):
        grid = [[(0, 0, 0)] * 60 + [(200, 200, 200)] * 60 for _ in range(68)]  # 1080p MCU grid
        out = capture.downsample(grid, cols=2, rows=1)
        self.assertEqual(out, [[(0, 0, 0), (200, 200, 200)]])

    def test_tiny_source_grid(self):
        out = capture.downsample([[(10, 20, 30)]], cols=16, rows=9)
        self.assertEqual((len(out), len(out[0])), (9, 16))
        self.assertEqual(out[4][7], (10, 20, 30))


if __name__ == "__main__":
    unittest.main()


from rules import Observation  # noqa: E402

BLUE_GRID = [[(0, 120, 215)] * 16 for _ in range(9)]


class FakeNotifier:
    def __init__(self, outcomes=None):
        self.sent = []
        self.outcomes = list(outcomes or [])
        self.channels = [type("C", (), {"kind": "ntfy"})()]

    def send(self, title, message, level="alert"):
        self.sent.append((title, level))
        return {"ntfy": self.outcomes.pop(0) if self.outcomes else "ok"}


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.app, self.data = root / "app", root / "data"
        self.app.mkdir()
        self.data.mkdir()
        (self.app / "app.json").write_text(json.dumps({"env": {"CHECK_S": {"default": "30"}}}))
        (self.app / "watcher.py").write_text("")
        self.t = 1000.0
        self.frames = []
        self.removed = []

    def tearDown(self):
        self._tmp.cleanup()

    def clock(self):
        return self.t

    def observe(self):
        frame = self.frames.pop(0) if self.frames else Observation(False)
        if isinstance(frame, Exception):
            raise frame
        return frame, 0.1

    def service(self, notifier=None):
        svc = watcher.Service(self.app, self.data, observe=self.observe, hdmi=lambda: True,
                              clock=self.clock, wall=self.clock, remover=lambda: self.removed.append(1))
        svc.reload()
        svc.notifier = notifier or FakeNotifier()
        return svc

    def blue(self):
        return Observation.from_grid(True, 1920, 1080, BLUE_GRID)

    def test_alert_is_retried_until_delivered(self):
        notifier = FakeNotifier(outcomes=["failed: dns", "failed: dns", "ok"])
        svc = self.service(notifier)
        self.frames = [self.blue(), self.blue(), self.blue(), self.blue()]
        for _ in range(4):
            svc.tick()
            self.t += 30
        titles = [t for t, _ in notifier.sent]
        self.assertEqual(titles.count("Blue screen detected"), 3)
        self.assertEqual(svc.pending, [])

    def test_bad_ipc_files_do_not_crash(self):
        (self.data / "control.json").write_text("[1, 2]")
        (self.data / "status.json").write_text('"nope"')
        svc = self.service()
        svc.tick()
        self.assertTrue((self.data / "status.json").exists())

    def test_capture_errors_mean_blind_not_no_signal(self):
        notifier = FakeNotifier()
        svc = self.service(notifier)
        self.frames = [RuntimeError("socket gone")] * 12
        for _ in range(12):
            svc.tick()
            self.t += 30
        status = json.loads((self.data / "status.json").read_text())
        self.assertTrue(status["signal"])
        self.assertIn("socket gone", status["error"])
        self.assertIn("Screen Watcher can't see the screen", [t for t, _ in notifier.sent])

    def test_uninstall_needs_repeated_misses(self):
        svc = self.service()
        (self.app / "app.json").unlink()
        (self.app / "watcher.py").unlink()
        self.assertFalse(svc.app_gone())
        self.t += 30
        self.assertFalse(svc.app_gone())
        (self.app / "app.json").write_text("{}")  # came back: an update, not an uninstall
        (self.app / "watcher.py").write_text("")
        self.assertFalse(svc.app_gone())
        (self.app / "app.json").unlink()
        (self.app / "watcher.py").unlink()
        for _ in range(4):
            self.t += 30
            gone = svc.app_gone()
        self.assertTrue(gone)

    def test_resume_after_pause_summarises_active_conditions(self):
        notifier = FakeNotifier()
        svc = self.service(notifier)
        (self.data / "control.json").write_text(json.dumps({"paused_until": self.t + 100}))
        self.frames = [self.blue(), self.blue(), self.blue()]
        svc.tick()
        self.t += 30
        svc.tick()
        self.assertEqual(notifier.sent, [])
        self.t += 200
        svc.tick()
        self.assertIn(("Still active after pause", "alert"), notifier.sent)
