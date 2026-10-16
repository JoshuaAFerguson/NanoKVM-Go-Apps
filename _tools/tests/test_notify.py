"""Tests for _shared/notify.py with a fake transport (no network)."""

import json
import sys
import unittest
from pathlib import Path
from urllib.parse import parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared"))

import notify  # noqa: E402
from notify import NotifyError  # noqa: E402


class Recorder:
    def __init__(self, status=200):
        self.calls = []
        self.status = status

    def __call__(self, url, body, headers, timeout):
        self.calls.append((url, body, dict(headers)))
        return self.status


class ConfigTest(unittest.TestCase):
    def test_nothing_configured(self):
        self.assertEqual(notify.load_channels({}), [])

    def test_all_channels(self):
        env = {"NTFY_URL": "https://ntfy.sh/my-kvm", "DISCORD_WEBHOOK": "https://discord.com/api/webhooks/1/abc",
               "PUSHOVER_TOKEN": "a" * 30, "PUSHOVER_USER": "u" * 30, "HA_WEBHOOK_URL": "http://ha.lan:8123/api/webhook/kvm"}
        self.assertEqual([c.kind for c in notify.load_channels(env)], ["ntfy", "discord", "pushover", "homeassistant"])

    def test_validation(self):
        for env in ({"NTFY_URL": "ftp://x/y"}, {"DISCORD_WEBHOOK": "https://evil.example/api/webhooks/1/a"},
                    {"PUSHOVER_TOKEN": "abc"}, {"HA_WEBHOOK_URL": "http://ha/api/states"},
                    {"NTFY_URL": "https://ntfy.sh/"}):
            with self.assertRaises(NotifyError, msg=env):
                notify.load_channels(env)


class RobustnessTest(unittest.TestCase):
    def test_malformed_urls_are_notify_errors(self):
        for env in ({"NTFY_URL": "http://[::1/x"}, {"NTFY_URL": "https://ntfy.sh/a b"},
                    {"HA_WEBHOOK_URL": "http://ha/api/webhook/x\r\nX: y"}):
            with self.assertRaises(NotifyError, msg=env):
                notify.load_channels(env)

    def test_token_requires_https(self):
        with self.assertRaisesRegex(NotifyError, "https"):
            notify.load_channels({"NTFY_URL": "http://ntfy.lan/t", "NTFY_TOKEN": "tk"})

    def test_any_transport_exception_is_contained(self):
        import http.client

        def bad_status(url, body, headers, timeout):
            raise http.client.BadStatusLine("garbage")
        results = notify.Notifier(notify.load_channels({"NTFY_URL": "https://ntfy.sh/t"}),
                                  transport=bad_status, device="d").send("t", "m")
        self.assertTrue(results["ntfy"].startswith("failed"))

    def test_slow_channel_does_not_block_past_deadline(self):
        import threading
        import time
        gate = threading.Event()

        def slow(url, body, headers, timeout):
            gate.wait(5)
            return 200
        n = notify.Notifier(notify.load_channels({"NTFY_URL": "https://ntfy.sh/t"}), transport=slow, device="d")
        start = time.monotonic()
        results = n.send("t", "m", deadline_s=0.3)
        gate.set()
        self.assertLess(time.monotonic() - start, 2)
        self.assertIn("timed out", results["ntfy"])

    def test_deadline_is_shared_across_channels(self):
        import threading
        import time
        gate = threading.Event()

        def slow(url, body, headers, timeout):
            gate.wait(5)
            return 200
        env = {"NTFY_URL": "https://ntfy.sh/t", "DISCORD_WEBHOOK": "https://discord.com/api/webhooks/1/a",
               "HA_WEBHOOK_URL": "http://ha.lan/api/webhook/x"}
        n = notify.Notifier(notify.load_channels(env), transport=slow, device="d")
        start = time.monotonic()
        n.send("t", "m", deadline_s=0.3)
        gate.set()
        self.assertLess(time.monotonic() - start, 0.9)

    def test_delivered_helper(self):
        self.assertTrue(notify.delivered({"ntfy": "ok", "discord": "HTTP 500"}))
        self.assertFalse(notify.delivered({"ntfy": "failed: x"}))


class SendTest(unittest.TestCase):
    def send(self, env, title="Signal lost", message="No video", level="alert"):
        rec = Recorder()
        notifier = notify.Notifier(notify.load_channels(env), transport=rec, device="kvm-go")
        results = notifier.send(title, message, level)
        return rec, results

    def test_ntfy(self):
        rec, results = self.send({"NTFY_URL": "https://ntfy.sh/my-kvm", "NTFY_TOKEN": "tk"})
        url, body, headers = rec.calls[0]
        self.assertEqual(url, "https://ntfy.sh/my-kvm")
        self.assertEqual(body.decode(), "No video")
        self.assertEqual(headers["Title"], "kvm-go: Signal lost")
        self.assertEqual(headers["Authorization"], "Bearer tk")
        self.assertEqual(headers["Priority"], "4")
        self.assertEqual(results, {"ntfy": "ok"})

    def test_ntfy_title_is_header_safe(self):
        rec, _ = self.send({"NTFY_URL": "https://ntfy.sh/t"}, title="Evil\r\nX-Injected: 1 ünï")
        self.assertNotIn("\n", rec.calls[0][2]["Title"])
        self.assertTrue(rec.calls[0][2]["Title"].isascii())

    def test_discord(self):
        rec, _ = self.send({"DISCORD_WEBHOOK": "https://discord.com/api/webhooks/1/abc"})
        payload = json.loads(rec.calls[0][1])
        self.assertIn("Signal lost", payload["content"])
        self.assertEqual(payload["allowed_mentions"], {"parse": []})

    def test_pushover(self):
        rec, _ = self.send({"PUSHOVER_TOKEN": "a" * 30, "PUSHOVER_USER": "u" * 30}, level="recovered")
        form = parse_qs(rec.calls[0][1].decode())
        self.assertEqual(form["token"], ["a" * 30])
        self.assertEqual(form["priority"], ["0"])

    def test_home_assistant(self):
        rec, _ = self.send({"HA_WEBHOOK_URL": "http://ha.lan:8123/api/webhook/kvm"})
        payload = json.loads(rec.calls[0][1])
        self.assertEqual((payload["device"], payload["level"]), ("kvm-go", "alert"))

    def test_one_failing_channel_does_not_block_others(self):
        calls = []

        def transport(url, body, headers, timeout):
            calls.append(url)
            if "ntfy" in url:
                raise OSError("down")
            return 204
        env = {"NTFY_URL": "https://ntfy.sh/t", "HA_WEBHOOK_URL": "http://ha.lan/api/webhook/x"}
        results = notify.Notifier(notify.load_channels(env), transport=transport, device="d").send("t", "m")
        self.assertEqual(len(calls), 2)
        self.assertIn("down", results["ntfy"])
        self.assertEqual(results["homeassistant"], "ok")

    def test_http_error_status_reported(self):
        rec = Recorder(status=401)
        results = notify.Notifier(notify.load_channels({"NTFY_URL": "https://ntfy.sh/t"}), transport=rec,
                                  device="d").send("t", "m")
        self.assertIn("401", results["ntfy"])

    def test_error_text_never_contains_secret(self):
        def transport(url, body, headers, timeout):
            raise ValueError("Invalid header value b'Bearer s3cr3t'")
        results = notify.Notifier(notify.load_channels({"NTFY_URL": "https://ntfy.sh/t", "NTFY_TOKEN": "s3cr3t"}),
                                  transport=transport, device="d").send("t", "m")
        self.assertNotIn("s3cr3t", results["ntfy"])


if __name__ == "__main__":
    unittest.main()
