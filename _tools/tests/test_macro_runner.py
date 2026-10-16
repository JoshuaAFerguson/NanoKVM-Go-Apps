"""Tests for macro-pad/runner.py using a fake MCP client."""

import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "macro-pad"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared"))

from kvm_mcp import McpError  # noqa: E402
from macros import Action  # noqa: E402
from runner import Runner  # noqa: E402


class FakeClient:
    def __init__(self, fail_with=None, block=None, crash=None):
        self.presses = []
        self.holds = []
        self.texts = []
        self.fail_with = fail_with
        self.block = block
        self.crash = crash

    def press_keys(self, keys, hold_ms=None):
        if self.block:
            self.block.wait(1)
        if self.crash:
            raise self.crash
        if self.fail_with:
            raise McpError(self.fail_with)
        self.presses.append(tuple(keys))
        self.holds.append(hold_ms)

    def type_text(self, text, delay_ms=None, timeout=None):
        self.texts.append(text)


def run_and_wait(runner, action, index=0):
    runner.start(action, index)
    runner.join(2)
    return runner.status()


class RunnerTest(unittest.TestCase):
    def test_runs_combo_once_and_reports_ok(self):
        client = FakeClient()
        status = run_and_wait(Runner(client, sleep=lambda s: None), Action("L", keys=("MetaLeft", "KeyL")))
        self.assertEqual(client.presses, [("MetaLeft", "KeyL")])
        self.assertTrue(status.ok)
        self.assertFalse(status.busy)

    def test_repeat_presses_n_times(self):
        client = FakeClient()
        run_and_wait(Runner(client, sleep=lambda s: None), Action("B", keys=("F2",), repeat=5, interval_ms=50))
        self.assertEqual(len(client.presses), 5)

    def test_text_action_types(self):
        client = FakeClient()
        run_and_wait(Runner(client, sleep=lambda s: None), Action("T", text="hello"))
        self.assertEqual(client.texts, ["hello"])

    def test_error_is_reported_friendly(self):
        client = FakeClient(fail_with="write /dev/hidg0: i/o timeout")
        status = run_and_wait(Runner(client, sleep=lambda s: None), Action("L", keys=("KeyL",)))
        self.assertFalse(status.ok)
        self.assertIn("target", status.message.lower())

    def test_cancel_stops_repeat_early(self):
        client = FakeClient()
        cancel_after = threading.Event()

        def sleep(_):
            if len(client.presses) >= 3:
                cancel_after.set()
                runner.cancel()

        runner = Runner(client, sleep=sleep)
        status = run_and_wait(runner, Action("B", keys=("F2",), repeat=100, interval_ms=50))
        self.assertLess(len(client.presses), 10)
        self.assertIn("cancel", status.message.lower())

    def test_start_while_busy_is_refused(self):
        gate = threading.Event()
        runner = Runner(FakeClient(block=gate), sleep=lambda s: None)
        self.assertTrue(runner.start(Action("A", keys=("KeyA",)), 0))
        self.assertFalse(runner.start(Action("B", keys=("KeyB",)), 1))
        self.assertEqual(runner.status().index, 0)
        gate.set()
        runner.join(2)

    def test_unexpected_exception_never_leaves_runner_busy(self):
        results = []
        runner = Runner(FakeClient(crash=AttributeError("boom")), sleep=lambda s: None, on_result=results.append)
        status = run_and_wait(runner, Action("A", keys=("KeyA",)))
        self.assertFalse(status.busy)
        self.assertFalse(status.ok)
        self.assertIn("AttributeError", results[-1])

    def test_no_sleep_after_last_repeat(self):
        sleeps = []
        run_and_wait(Runner(FakeClient(), sleep=sleeps.append), Action("B", keys=("F2",), repeat=3, interval_ms=100))
        self.assertEqual(len(sleeps), 2)

    def test_default_wait_is_cancellable(self):
        runner = Runner(FakeClient())
        runner.start(Action("B", keys=("F2",), repeat=3, interval_ms=5000), 0)
        runner.cancel()
        runner.join(2)
        self.assertFalse(runner.status().busy)

    def test_hold_is_forwarded(self):
        client = FakeClient()
        run_and_wait(Runner(client, sleep=lambda s: None), Action("H", keys=("AltLeft",), hold_ms=1000))
        self.assertEqual(client.holds, [1000])

    def test_long_text_is_typed_in_chunks(self):
        client = FakeClient()
        text = "x" * 100
        run_and_wait(Runner(client, sleep=lambda s: None), Action("T", text=text))
        self.assertGreater(len(client.texts), 1)
        self.assertEqual("".join(client.texts), text)

    def test_version_changes_on_every_update(self):
        runner = Runner(FakeClient(), sleep=lambda s: None)
        before = runner.status().version
        run_and_wait(runner, Action("A", keys=("KeyA",)))
        self.assertGreater(runner.status().version, before)


if __name__ == "__main__":
    unittest.main()
