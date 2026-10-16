"""Tests for vnc-bridge/hid.py using a FIFO as a stand-in gadget device."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "vnc-bridge"))

import hid  # noqa: E402


class HidTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fifo = os.path.join(self._tmp.name, "hidg0")
        os.mkfifo(self.fifo)
        self.reader = os.open(self.fifo, os.O_RDONLY | os.O_NONBLOCK)  # a "host" reading reports

    def tearDown(self):
        os.close(self.reader)
        self._tmp.cleanup()

    def test_report_written(self):
        dev = hid.HidDevice(self.fifo, 8)
        self.assertTrue(dev.write(bytes([0, 0, 4, 0, 0, 0, 0, 0])))
        self.assertEqual(os.read(self.reader, 8), bytes([0, 0, 4, 0, 0, 0, 0, 0]))
        dev.close()

    def test_wrong_length_rejected(self):
        with self.assertRaises(ValueError):
            hid.HidDevice(self.fifo, 8).write(b"\0" * 7)

    def test_full_pipe_drops_instead_of_blocking(self):
        dev = hid.HidDevice(self.fifo, 8)
        for _ in range(20000):  # fill the pipe buffer; host isn't reading
            if not dev.write(bytes(8)):
                break
        self.assertGreater(dev.dropped, 0)
        dev.close()

    def drain(self):
        try:
            while os.read(self.reader, 65536):
                pass
        except BlockingIOError:
            pass

    def test_dropped_state_is_resent_on_retry(self):
        dev = hid.HidDevice(self.fifo, 8)
        while dev.write(bytes(8)):  # fill the pipe: the host stops reading
            pass
        self.assertFalse(dev.set_state(bytes([0, 0, 4, 0, 0, 0, 0, 0])))
        self.assertTrue(dev.dirty)
        self.drain()  # the host reads again
        self.assertTrue(dev.retry())
        self.assertFalse(dev.dirty)
        dev.close()

    def test_missing_device_counts_as_dropped(self):
        dev = hid.HidDevice(os.path.join(self._tmp.name, "nope"), 8)
        self.assertFalse(dev.write(bytes(8)))
        self.assertEqual(dev.dropped, 1)


if __name__ == "__main__":
    unittest.main()
