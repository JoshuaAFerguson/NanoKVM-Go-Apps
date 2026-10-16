"""Tests for media-power/kvmweb.py against a fake NanoKVM web API (localhost)."""

import json
import socketserver
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "media-power"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared"))

import kvmweb  # noqa: E402
from kvmweb import KvmWeb, KvmWebError  # noqa: E402

STATE = {}


class FakeKvm(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, payload, cookie=None, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        if cookie:
            self.send_header("Set-Cookie", f"nano-kvm-token={cookie}; Path=/; HttpOnly")
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        return f"nano-kvm-token={STATE['token']}" in (self.headers.get("Cookie") or "")

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def do_POST(self):  # noqa: N802
        body = self._body()
        STATE["posts"].append((self.path, body))
        if self.path == "/api/auth/login":
            STATE["logins"] = STATE.get("logins", 0) + 1
            if STATE.get("twofa"):
                return self._send({"code": 0, "msg": "", "data": {"requires2FA": True, "challenge": "c1"}})
            if STATE.get("ratelimit"):
                return self._send({"code": -5, "msg": "too many attempts", "data": {"rateLimited": True, "retryAfter": 60}})
            if body == {"username": "admin", "password": "pw"}:
                return self._send({"code": 0, "msg": "", "data": {"token": STATE["token"]}})  # browser-style: token in body
            return self._send({"code": -2, "msg": "invalid username or password", "data": None})
        if not self._authed():
            return self._send("unauthorized", status=401)
        if self.path == "/api/storage/image/mount":
            STATE["mounted"] = body["file"]
            return self._send({"code": 0, "msg": "", "data": None})
        if self.path == "/api/vm/finger-robot/release" and STATE.get("release_fails", 0) > 0:
            STATE["release_fails"] -= 1
            return self._send({"code": -1, "msg": "robot busy", "data": None})
        if self.path in ("/api/vm/finger-robot/press", "/api/vm/finger-robot/release"):
            return self._send({"code": 0, "msg": "", "data": None})
        return self._send({"code": -1, "msg": "not found"}, status=404)

    def do_GET(self):  # noqa: N802
        if not self._authed():
            if STATE.get("expire_once"):
                STATE["expire_once"] = False
            return self._send("unauthorized", status=401)
        if self.path == "/api/storage/image":
            return self._send({"code": 0, "msg": "", "data": {"files": ["/data/debian.iso", "/data/win11.iso"]}})
        if self.path == "/api/storage/image/mounted":
            return self._send({"code": 0, "msg": "", "data": {"file": STATE["mounted"]}})
        if self.path == "/api/vm/finger-robot":
            return self._send({"code": 0, "msg": "", "data": STATE["robot"]})
        return self._send({"code": -1, "msg": "not found"}, status=404)


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "localhost", self.server_address[1]


class KvmWebTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = LocalServer(("127.0.0.1", 0), FakeKvm)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        STATE.clear()
        STATE.update(token="tok1", mounted="", posts=[], robot={"enabled": True})
        self.web = KvmWeb("admin", "pw", base_url=self.url, sleep=lambda s: STATE.setdefault("slept", []).append(s))

    def test_login_then_list_and_mounted(self):
        self.assertEqual(self.web.images(), ["/data/debian.iso", "/data/win11.iso"])
        self.assertEqual(self.web.mounted(), "")

    def test_mount_as_cdrom_and_eject(self):
        self.web.mount("/data/debian.iso")
        self.assertEqual(STATE["posts"][-1], ("/api/storage/image/mount",
                                              {"file": "/data/debian.iso", "cdrom": True, "readOnly": True}))
        self.assertEqual(self.web.mounted(), "/data/debian.iso")
        self.web.eject()
        self.assertEqual(self.web.mounted(), "")

    def test_wrong_password(self):
        web = KvmWeb("admin", "nope", base_url=self.url)
        with self.assertRaisesRegex(KvmWebError, "login rejected"):
            web.images()

    def test_session_expiry_relogs_once(self):
        self.web.images()
        STATE["token"] = "tok2"  # server rotated/expired the session
        self.assertEqual(len(self.web.images()), 2)
        logins = [p for p, _ in STATE["posts"] if p == "/api/auth/login"]
        self.assertEqual(len(logins), 2)

    def test_two_factor_is_reported_clearly(self):
        STATE["twofa"] = True
        with self.assertRaisesRegex(KvmWebError, "2FA"):
            self.web.images()

    def test_power_press_and_long_hold(self):
        self.web.power_press(hold_s=0.5)
        self.web.power_press(hold_s=6)
        paths = [p for p, _ in STATE["posts"] if "finger-robot" in p]
        self.assertEqual(paths, ["/api/vm/finger-robot/press", "/api/vm/finger-robot/release"] * 2)
        self.assertEqual(STATE["slept"], [0.5, 6])

    def test_release_is_sent_even_if_wait_fails(self):
        def boom(s):
            raise KeyboardInterrupt
        web = KvmWeb("admin", "pw", base_url=self.url, sleep=boom)
        with self.assertRaises(KeyboardInterrupt):
            web.power_press(hold_s=1)
        self.assertEqual(STATE["posts"][-1][0], "/api/vm/finger-robot/release")

    def test_wrong_password_is_not_retried(self):
        web = KvmWeb("admin", "nope", base_url=self.url)
        for _ in range(5):
            with self.assertRaises(KvmWebError):
                web.images()
        self.assertEqual(STATE["logins"], 1)  # latched: no more attempts that could lock the web UI

    def test_rate_limit_reported(self):
        STATE["ratelimit"] = True
        with self.assertRaisesRegex(KvmWebError, "rate"):
            self.web.images()

    def test_release_is_retried(self):
        STATE["release_fails"] = 2
        self.web.power_press(hold_s=0.1)
        releases = [p for p, _ in STATE["posts"] if p.endswith("/release")]
        self.assertEqual(len(releases), 3)
        self.assertFalse(self.web.held)

    def test_release_failure_is_loud(self):
        STATE["release_fails"] = 99
        with self.assertRaisesRegex(KvmWebError, "RELEASE FAILED"):
            self.web.power_press(hold_s=0.1)
        self.assertTrue(self.web.held)

    def test_hold_is_interruptible_and_released(self):
        import threading
        web = KvmWeb("admin", "pw", base_url=self.url)  # real (event-based) wait
        t = threading.Thread(target=web.power_press, args=(30,))
        t.start()
        import time
        time.sleep(0.3)
        web.abort_hold()  # app exiting mid-hold
        t.join(5)
        self.assertFalse(t.is_alive())
        self.assertEqual(STATE["posts"][-1][0], "/api/vm/finger-robot/release")

    def test_robot_availability(self):
        self.assertTrue(self.web.robot_available())
        STATE["robot"] = {"enabled": False}
        self.assertFalse(self.web.robot_available())
        STATE["robot"] = None
        self.assertFalse(self.web.robot_available())

    def test_base_url_must_be_loopback_by_default(self):
        with self.assertRaises(ValueError):
            KvmWeb("admin", "pw", base_url="https://192.168.0.72")


class HelpersTest(unittest.TestCase):
    def test_display_name(self):
        self.assertEqual(kvmweb.display_name("/data/ubuntu-24.04.1-live-server-amd64.iso"),
                         "ubuntu-24.04.1-live-server-amd64.iso")


if __name__ == "__main__":
    unittest.main()
