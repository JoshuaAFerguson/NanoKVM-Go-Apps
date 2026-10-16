"""Tests for _shared/kvm_mcp.py using a fake transport (no network)."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared"))

from kvm_mcp import KvmMcp, McpError  # noqa: E402


class FakeServer:
    """Minimal streamable-HTTP MCP server double."""

    def __init__(self, tool_results=None, expire_after=None):
        self.calls = []
        self.tool_results = tool_results or {}
        self.sessions = 0
        self.expire_after = expire_after  # expire session after N tool calls

    def __call__(self, url, body, headers, timeout):
        msg = json.loads(body)
        self.calls.append((msg, dict(headers)))
        method = msg["method"]
        if method == "initialize":
            self.sessions += 1
            result = {"serverInfo": {"name": "fake"}}
            return 200, {"Mcp-Session-Id": f"s{self.sessions}"}, self._sse(msg, result)
        if headers.get("Mcp-Session-Id") != f"s{self.sessions}":
            return 404, {}, "session not found"
        if method == "notifications/initialized":
            return 202, {}, ""
        if method == "tools/call":
            tool_calls = sum(1 for m, _ in self.calls if m["method"] == "tools/call")
            if self.expire_after is not None and tool_calls > self.expire_after:
                self.expire_after = None
                self.sessions += 1  # invalidate current session
                return 404, {}, "session not found"
            name = msg["params"]["name"]
            result = self.tool_results.get(name, {"content": [{"type": "text", "text": '{"ok":true}'}]})
            return 200, {}, self._sse(msg, result)
        if method == "tools/list":
            return 200, {}, self._sse(msg, {"tools": [{"name": "press_keys"}, {"name": "type_text"}]})
        return 200, {}, json.dumps({"jsonrpc": "2.0", "id": msg["id"], "error": {"message": "nope"}})

    @staticmethod
    def _sse(msg, result):
        return "event: message\ndata: " + json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}) + "\n\n"


class KvmMcpTest(unittest.TestCase):
    def test_initializes_once_and_sends_bearer_and_session(self):
        server = FakeServer()
        client = KvmMcp("secret", transport=server)
        client.press_keys(["ControlLeft", "KeyC"])
        client.press_keys(["Escape"])
        methods = [m["method"] for m, _ in server.calls]
        self.assertEqual(methods.count("initialize"), 1)
        self.assertEqual(server.calls[0][1]["Authorization"], "Bearer secret")
        self.assertEqual(server.calls[-1][1]["Mcp-Session-Id"], "s1")

    def test_press_keys_arguments(self):
        server = FakeServer()
        KvmMcp("k", transport=server).press_keys(["AltLeft"], hold_ms=500)
        args = server.calls[-1][0]["params"]["arguments"]
        self.assertEqual(args, {"keys": ["AltLeft"], "holdMs": 500})

    def test_type_text_arguments(self):
        server = FakeServer()
        KvmMcp("k", transport=server).type_text("hi", delay_ms=20)
        self.assertEqual(server.calls[-1][0]["params"], {"name": "type_text", "arguments": {"text": "hi", "delayMs": 20}})

    def test_reinitializes_when_session_expires(self):
        server = FakeServer(expire_after=1)
        client = KvmMcp("k", transport=server)
        client.press_keys(["KeyA"])
        result = client.press_keys(["KeyB"])
        self.assertEqual(result, {"ok": True})
        self.assertEqual(server.sessions, 3)  # initial, invalidation, re-init

    def test_tool_error_raises(self):
        server = FakeServer({"press_keys": {"isError": True, "content": [{"type": "text", "text": "bad key"}]}})
        with self.assertRaisesRegex(McpError, "bad key"):
            KvmMcp("k", transport=server).press_keys(["Nope"])

    def test_plain_text_result_is_returned_as_string(self):
        server = FakeServer({"current_context": {"content": [{"type": "text", "text": "Hello screen"}]}})
        self.assertEqual(KvmMcp("k", transport=server).call("current_context"), "Hello screen")

    def test_rpc_error_raises(self):
        with self.assertRaises(McpError):
            KvmMcp("k", transport=FakeServer())._rpc("bogus/method", {})

    def test_list_tools(self):
        self.assertEqual(KvmMcp("k", transport=FakeServer()).tool_names(), ["press_keys", "type_text"])

    def test_remote_url_rejected_with_unverified_default_transport(self):
        with self.assertRaises(ValueError):
            KvmMcp("k", url="https://192.168.0.66/api/mcp")

    def test_empty_or_malformed_result_raises_mcp_error(self):
        for bad in (None, {"content": []}, {"content": ["x"]}, "not a dict"):
            server = FakeServer({"press_keys": bad})
            with self.assertRaises(McpError):
                KvmMcp("k", transport=server).press_keys(["KeyA"])

    def test_http_400_is_reported_not_retried(self):
        server = FakeServer()
        client = KvmMcp("k", transport=server)
        client.tool_names()

        def bad_request(url, body, headers, timeout):
            return 400, {}, "invalid arguments: keys"
        client._transport = bad_request
        with self.assertRaisesRegex(McpError, "invalid arguments"):
            client.press_keys(["KeyA"])

    def test_sse_picks_response_matching_request_id(self):
        def transport(url, body, headers, timeout):
            msg = json.loads(body)
            if msg["method"] == "initialize":
                return 200, {"Mcp-Session-Id": "s"}, "data: " + json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
            if "id" not in msg:
                return 202, {}, ""
            note = json.dumps({"jsonrpc": "2.0", "method": "notifications/progress", "params": {}})
            reply = json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": [{"name": "x"}]}})
            return 200, {}, f"event: message\ndata: {note}\n\nevent: message\ndata: {reply}\n\n"
        self.assertEqual(KvmMcp("k", transport=transport).tool_names(), ["x"])

    def test_per_call_timeout_is_passed_to_transport(self):
        seen = []
        server = FakeServer()

        def transport(url, body, headers, timeout):
            seen.append(timeout)
            return server(url, body, headers, timeout)
        KvmMcp("k", transport=transport, timeout=10).type_text("hello", timeout=42)
        self.assertEqual(seen[-1], 42)

    def test_missing_key_rejected(self):
        with self.assertRaises(ValueError):
            KvmMcp("")


if __name__ == "__main__":
    unittest.main()
