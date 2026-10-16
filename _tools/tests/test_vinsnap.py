"""Tests for _shared/vinsnap.py against a fake kvm_vin snapshot socket."""

import json
import os
import socketserver
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared"))

import vinsnap  # noqa: E402

JPEG = b"\xff\xd8" + b"x" * 5000 + b"\xff\xd9"


class FakeVin(socketserver.StreamRequestHandler):
    reply = "ok"

    def handle(self):
        request = json.loads(self.rfile.readline())
        self.server.requests.append(request)
        if self.reply == "ok":
            self.wfile.write(json.dumps({"ok": True, "size": len(JPEG), "width": 1920, "height": 1080}).encode() + b"\n")
            self.wfile.write(JPEG[:100])
            self.wfile.flush()
            self.wfile.write(JPEG[100:])
        elif self.reply == "fail":
            self.wfile.write(b'{"ok": false, "message": "snapshot timeout"}\n')
        elif self.reply == "short":
            self.wfile.write(json.dumps({"ok": True, "size": len(JPEG)}).encode() + b"\n" + JPEG[:10])
        elif self.reply == "list":
            self.wfile.write(b"[1, 2]\n")


class VinSnapTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "snap.sock")
        self.server = socketserver.ThreadingUnixStreamServer(self.path, FakeVin)
        self.server.requests = []
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self._tmp.cleanup()

    def capture(self, reply, **kw):
        FakeVin.reply = reply
        return vinsnap.capture(path=self.path, timeout_s=1.0, **kw)

    def test_full_frame_in_pieces(self):
        snap = self.capture("ok", quality=50)
        self.assertEqual((snap.width, snap.height, snap.jpeg), (1920, 1080, JPEG))
        self.assertEqual(self.server.requests[-1]["quality"], 50)
        self.assertNotIn("x", self.server.requests[-1])  # never a crop request

    def test_quality_is_clamped(self):
        self.capture("ok", quality=500)
        self.assertEqual(self.server.requests[-1]["quality"], 99)

    def test_errors(self):
        for reply, needle in (("fail", "snapshot timeout"), ("short", "truncated"), ("list", "header")):
            with self.assertRaisesRegex(vinsnap.SnapshotError, needle):
                self.capture(reply)

    def test_missing_socket(self):
        with self.assertRaises(vinsnap.SnapshotError):
            vinsnap.capture(path=os.path.join(self._tmp.name, "nope.sock"), timeout_s=0.5)


if __name__ == "__main__":
    unittest.main()
