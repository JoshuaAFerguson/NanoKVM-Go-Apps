"""Tests for ai-monitor/agents.py and mcpwatch.py (pure logic)."""

import json
import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ai-monitor"))

import agents  # noqa: E402
import mcpwatch  # noqa: E402
from agents import AgentError, Registry  # noqa: E402


def event(**kw):
    base = {"machine": "mbp", "session": "s1", "state": "working", "project": "kvm-apps"}
    base.update(kw)
    return base


class ValidateTest(unittest.TestCase):
    def test_valid(self):
        e = agents.validate(event(message="Claude needs your permission to use Bash"))
        self.assertEqual((e.machine, e.state), ("mbp", "working"))

    def test_rejects_bad_fields(self):
        for bad in (event(state="hacking"), event(machine=""), event(machine="a b/c"), event(session="x" * 200),
                    event(project=5), "not a dict", event(message="m" * 1000)):
            with self.assertRaises(AgentError, msg=bad):
                agents.validate(bad)

    def test_strips_control_characters_from_text(self):
        e = agents.validate(event(message="hi\\x1b[31m\\nthere"))
        self.assertTrue(e.message.isprintable())


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.r = Registry()

    def test_waiting_transition_is_announced_once(self):
        self.assertEqual(self.r.update(agents.validate(event()), now=0), [])
        notes = self.r.update(agents.validate(event(state="waiting", message="needs permission")), now=10)
        self.assertEqual([n.kind for n in notes], ["waiting"])
        self.assertEqual(self.r.update(agents.validate(event(state="waiting")), now=20), [])

    def test_summary_counts_and_waiting_first(self):
        self.r.update(agents.validate(event(session="a", state="working")), now=0)
        self.r.update(agents.validate(event(session="b", state="waiting")), now=1)
        self.r.update(agents.validate(event(machine="desk", session="c", state="idle")), now=2)
        summary = self.r.summary(now=3)
        self.assertEqual((summary["working"], summary["waiting"], summary["idle"]), (1, 1, 1))
        self.assertEqual(self.r.sessions(now=3)[0].state, "waiting")

    def test_done_and_stale_sessions_expire(self):
        self.r.update(agents.validate(event(session="a", state="done")), now=0)
        self.r.update(agents.validate(event(session="b", state="working")), now=0)
        self.assertEqual(len(self.r.sessions(now=agents.DONE_TTL_S + 1)), 1)
        self.assertEqual(len(self.r.sessions(now=agents.STALE_S + 1)), 0)

    def test_capacity_evicts_oldest(self):
        for n in range(agents.MAX_SESSIONS + 5):
            self.r.update(agents.validate(event(session=f"s{n}")), now=n)
        self.assertEqual(len(self.r.sessions(now=agents.MAX_SESSIONS + 5)), agents.MAX_SESSIONS)


class McpWatchTest(unittest.TestCase):
    def test_tool_names_extracted_without_arguments(self):
        body = json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                           "params": {"name": "type_text", "arguments": {"text": "secret password"}}}).encode()
        self.assertEqual(mcpwatch.describe(body), ["type_text"])
        batch = json.dumps([{"method": "tools/call", "params": {"name": "screenshot"}}, {"method": "tools/list"}]).encode()
        self.assertEqual(mcpwatch.describe(batch), ["screenshot", "tools/list"])
        self.assertEqual(mcpwatch.describe(b"garbage"), ["?"])

    def test_activity_log_and_active_window(self):
        log = mcpwatch.Activity()
        log.record(["click_mouse"], "100.64.0.5", ok=True, now=100)
        self.assertTrue(log.active(now=110))
        self.assertFalse(log.active(now=100 + mcpwatch.ACTIVE_S + 1))
        self.assertEqual(log.recent()[0]["tool"], "click_mouse")
        self.assertNotIn("arguments", json.dumps(log.recent()))

    def test_only_tool_calls_count_as_control(self):
        log = mcpwatch.Activity()
        log.record(["tools/list"], "1.2.3.4", ok=True, now=0)
        self.assertFalse(log.active(now=1))
        log.record(["press_keys"], "1.2.3.4", ok=True, now=2)
        self.assertTrue(log.active(now=3))

    def test_log_is_bounded(self):
        log = mcpwatch.Activity()
        for n in range(500):
            log.record(["screenshot"], "x", ok=True, now=n)
        self.assertLessEqual(len(log.recent(limit=1000)), mcpwatch.MAX_LOG)



sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ai-monitor" / "hooks"))
import nanokvm_agent_hook as hook  # noqa: E402


class HookMappingTest(unittest.TestCase):
    def test_event_mapping(self):
        self.assertEqual(hook.state_for({"hook_event_name": "UserPromptSubmit"})[0], "working")
        self.assertEqual(hook.state_for({"hook_event_name": "Stop"})[0], "idle")
        self.assertEqual(hook.state_for({"hook_event_name": "SessionEnd"})[0], "done")
        waiting = hook.state_for({"hook_event_name": "Notification", "notification_type": "permission_prompt",
                                  "message": "Claude needs your permission to use Bash"})
        self.assertEqual(waiting[0], "waiting")
        self.assertIsNone(hook.state_for({"hook_event_name": "Notification", "notification_type": "auth_success"}))
        self.assertIsNone(hook.state_for({"hook_event_name": "PreCompact"}))

    def test_hook_never_fails(self):
        import io
        saved = sys.stdin
        try:
            sys.stdin = io.StringIO("not json")
            self.assertEqual(hook.main(), 0)
        finally:
            sys.stdin = saved

    def test_only_state_changes_are_sent_and_failures_back_off(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            saved = hook.STATE_DIR
            hook.STATE_DIR = tmp
            try:
                self.assertFalse(hook.unchanged("s1", "working", ""))
                self.assertTrue(hook.unchanged("s1", "working", ""))      # repeat tool call: skipped
                self.assertFalse(hook.unchanged("s1", "waiting", "perm"))  # change: sent
                self.assertFalse(hook.backing_off())
                open(hook._marker("failed"), "w").close()
                self.assertTrue(hook.backing_off())
            finally:
                hook.STATE_DIR = saved

    def test_plain_http_url_is_never_used(self):
        import io
        import os
        saved_in, env = sys.stdin, dict(os.environ)
        os.environ.update(NANOKVM_AI_URL="http://192.0.2.1:8790", NANOKVM_AI_TOKEN="t" * 32)
        try:
            sys.stdin = io.StringIO('{"hook_event_name": "Stop", "session_id": "x"}')
            with unittest.mock.patch("urllib.request.OpenerDirector.open") as opened:
                self.assertEqual(hook.main(), 0)
            opened.assert_not_called()
        finally:
            sys.stdin = saved_in
            os.environ.clear()
            os.environ.update(env)

    def test_machine_name_sanitized(self):
        import os
        os.environ["NANOKVM_AI_MACHINE"] = "my laptop/2"
        try:
            self.assertEqual(hook.machine_name(), "my-laptop-2")
        finally:
            del os.environ["NANOKVM_AI_MACHINE"]


if __name__ == "__main__":
    unittest.main()
