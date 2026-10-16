"""End-to-end tests for vnc-bridge/server.py: a tiny RFB client over localhost."""

import socket
import struct
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "vnc-bridge"))

import vnc_config as config  # noqa: E402
import des  # noqa: E402
import rfb  # noqa: E402
import server  # noqa: E402

JPEG_A = b"\xff\xd8AAAA\xff\xd9"
JPEG_B = b"\xff\xd8BBBBBB\xff\xd9"


class Snap:
    def __init__(self, w, h, jpeg):
        self.width, self.height, self.jpeg = w, h, jpeg


class FakeHidDev:
    def __init__(self):
        self.reports = []

    def write(self, report):
        self.reports.append(report)
        return True

    def set_state(self, report):
        return self.write(report)

    def retry(self):
        return True


class FakeHid:
    def __init__(self):
        self.keyboard, self.mouse = FakeHidDev(), FakeHidDev()

    def retry(self):
        pass


def recv_exact(sock, n):
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ConnectionError("closed")
        data += chunk
    return data


class Client:
    def __init__(self, port, password="secret1", version=b"RFB 003.008\n"):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            self._handshake(password, version)
        except BaseException:
            self.sock.close()
            raise

    def _handshake(self, password, version):
        assert recv_exact(self.sock, 12) == b"RFB 003.008\n"
        self.sock.sendall(version)
        if version == b"RFB 003.003\n":
            assert struct.unpack(">I", recv_exact(self.sock, 4))[0] == 2
        else:
            n = recv_exact(self.sock, 1)[0]
            if n == 0:
                length = struct.unpack(">I", recv_exact(self.sock, 4))[0]
                raise PermissionError(recv_exact(self.sock, length).decode())
            types = recv_exact(self.sock, n)
            assert 2 in types
            self.sock.sendall(bytes([2]))
        challenge = recv_exact(self.sock, 16)
        self.sock.sendall(des.vnc_response(password, challenge))
        if struct.unpack(">I", recv_exact(self.sock, 4))[0] != 0:
            raise PermissionError("auth failed")
        self.sock.sendall(b"\x01")
        self.width, self.height = struct.unpack(">HH", recv_exact(self.sock, 4))
        recv_exact(self.sock, 16)
        name_len = struct.unpack(">I", recv_exact(self.sock, 4))[0]
        self.name = recv_exact(self.sock, name_len)

    def encodings(self, *encs):
        self.sock.sendall(struct.pack(f">BxH{len(encs)}i", 2, len(encs), *encs))

    def request(self, incremental=0):
        self.sock.sendall(struct.pack(">BB4H", 3, incremental, 0, 0, self.width, self.height))

    def read_update(self):
        kind, count = struct.unpack(">BxH", recv_exact(self.sock, 4))
        assert kind == 0
        rects = []
        for _ in range(count):
            x, y, w, h, enc = struct.unpack(">HHHHi", recv_exact(self.sock, 12))
            if enc == rfb.ENC_TIGHT:
                assert recv_exact(self.sock, 1)[0] == 0x90
                length, shift = 0, 0
                for _ in range(3):
                    b = recv_exact(self.sock, 1)[0]
                    length |= (b & 0x7F) << shift
                    shift += 7
                    if not b & 0x80:
                        break
                rects.append((enc, w, h, recv_exact(self.sock, length)))
            else:
                rects.append((enc, w, h, b""))
        return rects

    def key(self, keysym, down):
        self.sock.sendall(struct.pack(">BBxxI", 4, int(down), keysym))

    def pointer(self, mask, x, y):
        self.sock.sendall(struct.pack(">BBHH", 5, mask, x, y))

    def close(self):
        self.sock.close()


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.snaps = [Snap(1920, 1080, JPEG_A)]
        self.hid = FakeHid()
        placeholder = server.Frame(1280, 720, b"\xff\xd8PH\xff\xd9", b"ph", live=False)
        frames = server.FrameSource(lambda q: self.snaps[0], placeholder, quality=50, max_fps=30)
        self.cfg = config.load({"VNC_PASSWORD": "secret1", "VNC_PORT": "5900"})
        self.server = server.VncServer(self.cfg, frames, self.hid, name="test-kvm", log=lambda m: None)
        # an ephemeral port instead of 5900
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(4)
        self.server.listeners.append(self.listener)
        self.port = self.listener.getsockname()[1]
        threading.Thread(target=self.server.accept_loop, args=(self.listener,), daemon=True).start()

    def tearDown(self):
        self.server.stop()

    def test_handshake_and_first_frame(self):
        c = Client(self.port)
        self.assertEqual((c.width, c.height, c.name), (1920, 1080, b"test-kvm"))
        c.encodings(rfb.ENC_TIGHT, rfb.ENC_DESKTOP_SIZE, -26)
        c.request()
        self.assertEqual(c.read_update(), [(rfb.ENC_TIGHT, 1920, 1080, JPEG_A)])
        c.close()

    def test_rfb_33_client(self):
        c = Client(self.port, version=b"RFB 003.003\n")
        c.encodings(rfb.ENC_TIGHT, -26)
        c.request()
        self.assertEqual(c.read_update()[0][3], JPEG_A)
        c.close()

    def test_wrong_password_then_lockout(self):
        for _ in range(server.AUTH_FAILS):
            with self.assertRaises(PermissionError):
                Client(self.port, password="wrongpw")
        with self.assertRaises((PermissionError, ConnectionError, OSError)):
            Client(self.port)  # even the right password is refused while blocked (dropped at accept)

    def test_keyboard_and_mouse_reach_hid(self):
        c = Client(self.port)
        c.encodings(rfb.ENC_TIGHT, -26)
        c.key(0xFFE3, True)
        c.key(ord("c"), True)
        c.key(ord("c"), False)
        c.key(0xFFE3, False)
        c.pointer(1, 960, 540)
        time.sleep(0.3)
        self.assertIn(bytes([0x01, 0, 0x06, 0, 0, 0, 0, 0]), self.hid.keyboard.reports)
        self.assertEqual(self.hid.mouse.reports[-1][:2], bytes([1, 1]))
        c.close()
        time.sleep(0.3)
        self.assertEqual(self.hid.keyboard.reports[-1], bytes(8))  # released on disconnect

    def test_resolution_change_uses_desktop_size(self):
        c = Client(self.port)
        c.encodings(rfb.ENC_TIGHT, rfb.ENC_DESKTOP_SIZE, -26)
        c.request()
        c.read_update()
        self.snaps[0] = Snap(1280, 720, JPEG_B)
        time.sleep(0.1)
        c.request(incremental=1)
        self.assertEqual(c.read_update()[0][:3], (rfb.ENC_DESKTOP_SIZE, 1280, 720, ))
        self.assertEqual(c.read_update()[0], (rfb.ENC_TIGHT, 1280, 720, JPEG_B))
        c.close()

    def test_client_without_tight_is_disconnected(self):
        c = Client(self.port)
        c.encodings(0)  # Raw only
        c.request()
        c.sock.settimeout(2)
        self.assertEqual(c.sock.recv(1), b"")
        c.close()

    def test_view_only_ignores_input(self):
        self.server.config = config.load({"VNC_PASSWORD": "secret1", "VNC_VIEW_ONLY": "true"})
        c = Client(self.port)
        c.key(ord("a"), True)
        time.sleep(0.2)
        self.assertEqual(self.hid.keyboard.reports, [])
        c.close()


class HardeningTest(ServerTest):
    def test_slow_handshake_is_cut_off_by_absolute_deadline(self):
        saved, server.HANDSHAKE_TOTAL_S = server.HANDSHAKE_TOTAL_S, 0.6
        try:
            s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
            recv_exact(s, 12)
            start = time.monotonic()
            closed_at = None
            try:
                for ch in b"RFB 003.008\n"[:11]:  # drip, never finish
                    s.sendall(bytes([ch]))
                    time.sleep(0.15)
                s.settimeout(3)
                if s.recv(100) == b"":
                    closed_at = time.monotonic()
            except (BrokenPipeError, ConnectionResetError):
                closed_at = time.monotonic()  # server already hung up: the deadline fired
            self.assertIsNotNone(closed_at)
            self.assertLess(closed_at - start, 2.5)
            s.close()
        finally:
            server.HANDSHAKE_TOTAL_S = saved

    def test_unauthenticated_connections_cannot_take_client_slots(self):
        idle = [socket.create_connection(("127.0.0.1", self.port), timeout=5) for _ in range(2)]
        time.sleep(0.2)
        self.assertEqual(self.server.clients(), [])       # not counted as clients...
        self.assertEqual(self.server.pending_count(), 2)  # ...only as capped pending handshakes
        for s in idle:
            s.close()
        time.sleep(0.3)
        c = Client(self.port)
        self.assertEqual(len(self.server.clients()), 1)
        c.close()

    def test_per_ip_pending_cap(self):
        socks = [socket.create_connection(("127.0.0.1", self.port), timeout=5) for _ in range(4)]
        time.sleep(0.3)
        self.assertEqual(self.server.pending_count(), server.MAX_PENDING_PER_IP)
        for s in socks:
            s.close()

    def test_pending_connections_are_capped(self):
        socks = [socket.create_connection(("127.0.0.1", self.port), timeout=5) for _ in range(12)]
        time.sleep(0.3)
        self.assertLessEqual(self.server.pending_count(), server.MAX_PENDING_TOTAL)
        for s in socks:
            s.close()

    def test_failed_login_does_not_release_other_users_keys(self):
        c = Client(self.port)
        c.encodings(rfb.ENC_TIGHT, -26)
        c.key(0xFFE1, True)  # holding Shift
        time.sleep(0.2)
        before = len(self.hid.keyboard.reports)
        with self.assertRaises(PermissionError):
            Client(self.port, password="wrongpw")
        time.sleep(0.2)
        self.assertEqual(len(self.hid.keyboard.reports), before)
        c.close()

    def test_lockout_counts_in_flight_attempts(self):
        limiter = server.AuthLimiter()
        for _ in range(server.AUTH_FAILS):
            self.assertTrue(limiter.begin("1.2.3.4"))
        self.assertFalse(limiter.begin("1.2.3.4"))  # in-flight attempts count
        limiter.succeeded("1.2.3.4")  # refunds only one attempt
        self.assertTrue(limiter.begin("1.2.3.4"))

    def test_frames_keep_flowing_during_mouse_movement(self):
        c = Client(self.port)
        c.encodings(rfb.ENC_TIGHT, -26)
        c.request()
        stop = threading.Event()

        def wiggle():
            x = 0
            while not stop.is_set():
                c.pointer(0, x % 500, 100)
                x += 7
                time.sleep(0.01)
        threading.Thread(target=wiggle, daemon=True).start()
        c.sock.settimeout(3)
        self.assertEqual(c.read_update()[0][0], rfb.ENC_TIGHT)
        stop.set()
        c.close()

    def test_client_without_jpeg_quality_is_refused(self):
        c = Client(self.port)
        c.encodings(rfb.ENC_TIGHT)  # Tight, but no JPEG quality level
        c.request()
        c.sock.settimeout(2)
        self.assertEqual(c.sock.recv(1), b"")
        c.close()


class FrameSourceTest(unittest.TestCase):
    def test_no_signal_serves_placeholder_without_capturing(self):
        calls = []
        placeholder = server.Frame(1280, 720, b"ph", b"ph", live=False)
        frames = server.FrameSource(lambda q: calls.append(q), placeholder, 50, 30, has_signal=lambda: False)
        self.assertIs(frames.get(), placeholder)
        self.assertEqual(calls, [])

    def test_capture_failure_falls_back_to_placeholder(self):
        def broken(q):
            raise OSError("socket gone")
        placeholder = server.Frame(1280, 720, b"ph", b"ph", live=False)
        frames = server.FrameSource(broken, placeholder, 50, 30)
        self.assertIs(frames.get(), placeholder)
        self.assertEqual(frames.errors, 1)


class ConfigTest(unittest.TestCase):
    def test_password_rules(self):
        for bad in ("", "short", "waytoolongpassword", "pässwort1"):
            with self.assertRaises(config.ConfigError):
                config.load({"VNC_PASSWORD": bad})

    def test_bind_and_ranges(self):
        self.assertEqual(config.load({"VNC_PASSWORD": "secret1"}).bind, "tailscale")
        self.assertEqual(config.load({"VNC_PASSWORD": "secret1", "VNC_BIND": "10.0.0.5"}).bind, "10.0.0.5")
        for env in ({"VNC_BIND": "example.com"}, {"VNC_PORT": "80"}, {"VNC_QUALITY": "5"}):
            with self.assertRaises(config.ConfigError):
                config.load({"VNC_PASSWORD": "secret1", **env})


if __name__ == "__main__":
    unittest.main()
