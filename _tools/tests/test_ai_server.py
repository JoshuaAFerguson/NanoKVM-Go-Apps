"""End-to-end tests for ai-monitor/aiserver.py: check-ins and the watched MCP proxy."""

import http.client
import json
import socketserver
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ai-monitor"))

import aiserver  # noqa: E402


class FakeMcp(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        if getattr(self.server, "reject", False):
            self.rfile.read(length)
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.server.seen.append((self.headers.get("Authorization"), self.rfile.read(length)))
        body = b"event: message\ndata: " + json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}).encode() + b"\n\n"
        body += b"x" * 200_000  # a large (screenshot-like) response must stream through
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Mcp-Session-Id", "sess-1")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Quiet(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "localhost", self.server_address[1]


class AiServerTest(unittest.TestCase):
    def setUp(self):
        self.upstream = Quiet(("127.0.0.1", 0), FakeMcp)
        self.upstream.seen = []
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.notes = []
        up_port = self.upstream.server_port
        self.hub = aiserver.Hub("agent-token", self.notes.append,
                                upstream=lambda timeout: http.client.HTTPConnection("127.0.0.1", up_port, timeout=5))
        self.server = aiserver.make_server(self.hub, "127.0.0.1", 0, tls=None)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        for s in (self.server, self.upstream):
            s.shutdown()
            s.server_close()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, body=json.dumps(body).encode() if isinstance(body, dict) else body,
                     headers={"Content-Type": "application/json", **(headers or {})})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp, data

    def test_checkin_requires_token_and_notifies_waiting(self):
        payload = {"machine": "mbp", "session": "s1", "state": "waiting", "message": "permission needed"}
        resp, _ = self.request("POST", "/v1/agent", payload, {"Authorization": "Bearer wrong"})
        self.assertEqual(resp.status, 401)
        resp, _ = self.request("POST", "/v1/agent", payload, {"Authorization": "Bearer agent-token"})
        self.assertEqual(resp.status, 200)
        self.assertEqual([n.kind for n in self.notes], ["waiting"])

    def test_checkin_validation_and_size_limit(self):
        resp, _ = self.request("POST", "/v1/agent", {"machine": "mbp", "state": "?"},
                               {"Authorization": "Bearer agent-token"})
        self.assertEqual(resp.status, 400)
        resp, _ = self.request("POST", "/v1/agent", b"x" * 10_000, {"Authorization": "Bearer agent-token"})
        self.assertEqual(resp.status, 413)

    def test_mcp_proxy_streams_and_logs_tool_only(self):
        call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "type_text", "arguments": {"text": "hunter2"}}}
        resp, data = self.request("POST", "/mcp", call, {"Authorization": "Bearer mcp-key"})
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Mcp-Session-Id"), "sess-1")
        self.assertGreater(len(data), 200_000)
        self.assertEqual(self.upstream.seen[-1][0], "Bearer mcp-key")  # key passed through untouched
        recent = self.hub.activity.recent()
        self.assertEqual(recent[0]["tool"], "type_text")
        self.assertNotIn("hunter2", json.dumps(recent))

    def test_kill_switch_blocks_and_records(self):
        self.hub.kill_switch = True
        call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "press_keys"}}
        resp, data = self.request("POST", "/mcp", call)
        self.assertEqual(resp.status, 403)
        self.assertIn("kill switch", data.decode())
        self.assertEqual(self.upstream.seen, [])
        self.assertEqual(self.hub.activity.blocked_calls, 1)

    def test_kill_switch_reads_control_file_and_fails_closed(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "control.json"
            kill = aiserver.KillSwitch(path)
            self.assertFalse(kill.on())               # no file yet: not paused
            path.write_text('{"kill": true}')
            self.assertTrue(kill.on())
            path.write_text('{"kill": false, "x": 1}')
            self.assertFalse(kill.on())
            path.write_text("{corrupt")
            self.assertTrue(kill.on())                # unreadable: fail closed

    def test_idle_connection_does_not_block_others(self):
        import socket as s
        idle = s.create_connection(("127.0.0.1", self.port))  # connects, sends nothing
        try:
            resp, _ = self.request("GET", "/health")
            self.assertEqual(resp.status, 200)
        finally:
            idle.close()

    def test_chunked_and_bad_length_rejected(self):
        resp, _ = self.request("POST", "/v1/agent", b"{}", {"Authorization": "Bearer agent-token",
                                                            "Transfer-Encoding": "chunked"})
        self.assertEqual(resp.status, 411)

    def test_non_ascii_token_is_401_not_crash(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.putrequest("POST", "/v1/agent")
        conn.putheader("Authorization", "Bearer t\xe9st".encode("latin-1").decode("unicode_escape").encode("latin-1"))
        conn.putheader("Content-Length", "2")
        conn.endheaders(b"{}")
        self.assertEqual(conn.getresponse().status, 401)
        conn.close()

    def test_unauthorized_upstream_calls_do_not_flush_audit_log(self):
        self.upstream.reject = True
        call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "press_keys"}}
        self.request("POST", "/mcp", call)
        self.assertEqual(self.hub.activity.recent(), [])
        self.assertEqual(self.hub.unauthorized, 1)

    def test_batch_control_call_at_end_is_logged(self):
        import mcpwatch
        batch = [{"jsonrpc": "2.0", "method": "ping"}] * 30 + [
            {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "type_text"}}]
        self.assertIn("type_text", mcpwatch.describe(json.dumps(batch).encode()))

    def test_rate_limit(self):
        self.hub.checkin_rate._hits["127.0.0.1"] = [__import__("time").time()] * aiserver.CHECKINS_PER_MIN
        resp, _ = self.request("POST", "/v1/agent", {}, {"Authorization": "Bearer agent-token"})
        self.assertEqual(resp.status, 429)


if __name__ == "__main__":
    unittest.main()
