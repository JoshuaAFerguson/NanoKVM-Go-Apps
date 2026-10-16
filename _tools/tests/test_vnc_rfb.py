"""Tests for vnc-bridge/rfb.py (RFB 3.3/3.7/3.8 encode/decode, pure)."""

import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "vnc-bridge"))

import des  # noqa: E402
import rfb  # noqa: E402


class HandshakeTest(unittest.TestCase):
    def test_version_parsing(self):
        self.assertEqual(rfb.parse_version(b"RFB 003.008\n"), (3, 8))
        self.assertEqual(rfb.parse_version(b"RFB 003.889\n"), (3, 8))  # Apple: treat as 3.8
        self.assertEqual(rfb.parse_version(b"RFB 003.003\n"), (3, 3))
        with self.assertRaises(rfb.RfbError):
            rfb.parse_version(b"HTTP/1.1 200\n")

    def test_auth_check_is_constant_time_and_correct(self):
        challenge = bytes(range(16))
        good = des.vnc_response("pw", challenge)
        self.assertTrue(rfb.check_auth("pw", challenge, good))
        self.assertFalse(rfb.check_auth("pw", challenge, bytes(16)))

    def test_server_init(self):
        msg = rfb.server_init(1920, 1080, "kvm")
        w, h = struct.unpack(">HH", msg[:4])
        self.assertEqual((w, h), (1920, 1080))
        bpp, depth, big, true = msg[4:8]
        self.assertEqual((bpp, depth, true), (32, 24, 1))
        self.assertEqual(msg[-3:], b"kvm")


class ClientMessageTest(unittest.TestCase):
    def test_set_encodings(self):
        body = struct.pack(">xH3i", 3, 7, -223, -23)
        self.assertEqual(rfb.parse_set_encodings(body), [7, -223, -23])

    def test_update_request(self):
        self.assertEqual(rfb.parse_update_request(struct.pack(">B4H", 1, 0, 0, 100, 50)), (True, 0, 0, 100, 50))

    def test_key_event(self):
        self.assertEqual(rfb.parse_key_event(struct.pack(">BxxI", 1, 0xFF0D)), (True, 0xFF0D))

    def test_pointer_event(self):
        self.assertEqual(rfb.parse_pointer_event(struct.pack(">BHH", 5, 10, 20)), (5, 10, 20))

    def test_pixel_format_requires_true_colour_32bpp_for_jpeg(self):
        good = struct.pack(">BBBBHHHBBBxxx", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)
        bad = struct.pack(">BBBBHHHBBBxxx", 8, 8, 0, 1, 7, 7, 3, 0, 3, 6)
        self.assertTrue(rfb.jpeg_compatible(b"\0\0\0" + good))
        self.assertFalse(rfb.jpeg_compatible(b"\0\0\0" + bad))


class EncodeTest(unittest.TestCase):
    def test_compact_length(self):
        self.assertEqual(rfb.compact_length(100), bytes([100]))
        self.assertEqual(rfb.compact_length(200), bytes([0xC8, 0x01]))
        self.assertEqual(rfb.compact_length(20000), bytes([0xA0, 0x9C, 0x01]))
        with self.assertRaises(rfb.RfbError):
            rfb.compact_length(1 << 22)

    def test_tight_jpeg_update(self):
        jpeg = b"\xff\xd8fake\xff\xd9"
        msg = rfb.tight_jpeg_update(1920, 1080, jpeg)
        self.assertEqual(msg[:4], struct.pack(">BxH", 0, 1))
        x, y, w, h, enc = struct.unpack(">HHHHi", msg[4:16])
        self.assertEqual((x, y, w, h, enc), (0, 0, 1920, 1080, rfb.ENC_TIGHT))
        self.assertEqual(msg[16], 0x90)  # JPEG compression control
        self.assertEqual(msg[17], len(jpeg))
        self.assertTrue(msg.endswith(jpeg))

    def test_desktop_size_update(self):
        msg = rfb.desktop_size_update(1280, 720)
        self.assertEqual(struct.unpack(">BxHHHHHi", msg), (0, 1, 0, 0, 1280, 720, rfb.ENC_DESKTOP_SIZE))


class PointerTest(unittest.TestCase):
    def test_absolute_report_scaling_and_buttons(self):
        mouse = rfb.Mouse()
        report = mouse.event(mask=0b001, x=0, y=0, width=1920, height=1080)
        self.assertEqual(report, bytes([1, 0x01, 0, 0, 0, 0, 0]))
        report = mouse.event(mask=0b100, x=1919, y=1079, width=1920, height=1080)  # VNC right
        self.assertEqual(report[1], 0x02)  # HID right
        self.assertEqual(struct.unpack("<HH", report[2:6]), (32767, 32767))

    def test_wheel_on_press_edge_only(self):
        mouse = rfb.Mouse()
        up = mouse.event(mask=0b1000, x=5, y=5, width=100, height=100)
        self.assertEqual(up[6], 1)
        held = mouse.event(mask=0b1000, x=5, y=5, width=100, height=100)
        self.assertEqual(held[6], 0)
        down = mouse.event(mask=0b10000, x=5, y=5, width=100, height=100)
        self.assertEqual(down[6], 0xFF)  # -1


if __name__ == "__main__":
    unittest.main()
