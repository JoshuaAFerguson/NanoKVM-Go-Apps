"""Write USB HID reports to the KVM's gadget devices (keyboard + absolute mouse).

/dev/hidg0: boot keyboard, 8-byte reports. /dev/hidg2: absolute mouse, 7-byte
reports with report ID 1. Writes are non-blocking with a short wait, so a
target that isn't reading USB (asleep, unplugged) drops input instead of
stalling the VNC session. The latest desired state is remembered: if writing
it was dropped (e.g. a key-up), retry() resends it so keys don't stay stuck.
"""

from __future__ import annotations

import errno
import os
import select
import threading

KEYBOARD_DEV = "/dev/hidg0"
MOUSE_DEV = "/dev/hidg2"
WRITE_WAIT_S = 0.05
RELEASE_TRIES = 5


class HidDevice:
    def __init__(self, path: str, report_len: int) -> None:
        self.path = path
        self.report_len = report_len
        self.fd: int | None = None
        self.dropped = 0
        self.state: bytes | None = None
        self.dirty = False
        self._lock = threading.Lock()

    def _open(self) -> None:
        if self.fd is None:
            self.fd = os.open(self.path, os.O_WRONLY | os.O_NONBLOCK)

    def _close(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

    def write(self, report: bytes) -> bool:
        if len(report) != self.report_len:
            raise ValueError(f"{self.path}: report must be {self.report_len} bytes")
        with self._lock:
            try:
                self._open()
                _, ready, _ = select.select([], [self.fd], [], WRITE_WAIT_S)
                if ready:
                    os.write(self.fd, report)
                    return True
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK, errno.ESHUTDOWN, errno.EPIPE):
                    self._close()  # reopen next time (e.g. gadget re-enumerated)
            self.dropped += 1
            return False

    def set_state(self, report: bytes) -> bool:
        """Write the new desired state; remember it for retry() if dropped."""
        self.state = report
        ok = self.write(report)
        self.dirty = not ok
        return ok

    def retry(self) -> bool:
        if not self.dirty or self.state is None:
            return True
        self.dirty = not self.write(self.state)
        return not self.dirty

    def close(self) -> None:
        with self._lock:
            self._close()


class Hid:
    def __init__(self, keyboard: str = KEYBOARD_DEV, mouse: str = MOUSE_DEV) -> None:
        self.keyboard = HidDevice(keyboard, 8)
        self.mouse = HidDevice(mouse, 7)

    def retry(self) -> None:
        self.keyboard.retry()
        self.mouse.retry()

    def close(self) -> None:
        self.keyboard.close()
        self.mouse.close()


def release(device: HidDevice, report: bytes) -> bool:
    """Persistently try to apply a release report (on disconnect)."""
    for _ in range(RELEASE_TRIES):
        if device.set_state(report):
            return True
    return False
