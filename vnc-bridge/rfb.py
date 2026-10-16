"""RFB (VNC) protocol pieces for a JPEG-only server: pure encode/decode helpers.

The bridge only ever sends Tight-encoded JPEG rectangles (the KVM's hardware
JPEG passes straight through, no re-encoding), so clients must support Tight:
TigerVNC, TightVNC, UltraVNC, Remmina, noVNC and Guacamole do; macOS Screen
Sharing and RealVNC Viewer do not.
"""

from __future__ import annotations

import hmac
import struct

import des

ENC_TIGHT = 7
ENC_DESKTOP_SIZE = -223
SEC_NONE, SEC_VNC = 1, 2
MAX_COMPACT = (1 << 22) - 1
NAME_MAX = 64


class RfbError(RuntimeError):
    pass


def parse_version(line: bytes) -> tuple[int, int]:
    if len(line) != 12 or not line.startswith(b"RFB ") or line[-1:] != b"\n":
        raise RfbError("not an RFB client")
    try:
        major, minor = int(line[4:7]), int(line[8:11])
    except ValueError as exc:
        raise RfbError("bad RFB version") from exc
    if major != 3:
        raise RfbError("unsupported RFB major version")
    if minor >= 8:
        return 3, 8
    return (3, 7) if minor == 7 else (3, 3)


def check_auth(password: str, challenge: bytes, response: bytes) -> bool:
    return hmac.compare_digest(des.vnc_response(password, challenge), response)


def server_init(width: int, height: int, name: str) -> bytes:
    # 32bpp, depth 24, little endian, true colour, 0-255 per channel, R/G/B shifts 16/8/0
    pixel_format = struct.pack(">BBBBHHHBBBxxx", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)
    raw_name = name.encode("utf-8", "replace")[:NAME_MAX]
    return struct.pack(">HH", width, height) + pixel_format + struct.pack(">I", len(raw_name)) + raw_name


def jpeg_compatible(set_pixel_format_body: bytes) -> bool:
    """Tight JPEG is only defined for 32bpp, depth 24 true colour."""
    bpp, depth, _, true_colour = set_pixel_format_body[3:7]
    return bpp == 32 and depth == 24 and true_colour == 1


def parse_set_encodings(body: bytes) -> list[int]:
    count = struct.unpack(">H", body[1:3])[0]
    return list(struct.unpack(f">{count}i", body[3:3 + 4 * count]))


def parse_update_request(body: bytes) -> tuple[bool, int, int, int, int]:
    incremental, x, y, w, h = struct.unpack(">B4H", body[:9])
    return bool(incremental), x, y, w, h


def parse_key_event(body: bytes) -> tuple[bool, int]:
    down, keysym = struct.unpack(">BxxI", body[:7])
    return bool(down), keysym


def parse_pointer_event(body: bytes) -> tuple[int, int, int]:
    return struct.unpack(">BHH", body[:5])


def compact_length(n: int) -> bytes:
    if not 0 <= n <= MAX_COMPACT:
        raise RfbError("JPEG too large for one Tight rectangle")
    out = bytearray([n & 0x7F])
    if n > 0x7F:
        out[0] |= 0x80
        out.append((n >> 7) & 0x7F)
        if n > 0x3FFF:
            out[1] |= 0x80
            out.append((n >> 14) & 0xFF)
    return bytes(out)


def tight_jpeg_update(width: int, height: int, jpeg: bytes) -> bytes:
    header = struct.pack(">BxH", 0, 1) + struct.pack(">HHHHi", 0, 0, width, height, ENC_TIGHT)
    return header + bytes([0x90]) + compact_length(len(jpeg)) + jpeg


def desktop_size_update(width: int, height: int) -> bytes:
    return struct.pack(">BxH", 0, 1) + struct.pack(">HHHHi", 0, 0, width, height, ENC_DESKTOP_SIZE)


# VNC button mask: 1=left 2=middle 4=right 8=wheel up 16=wheel down.
# KVM /dev/hidg2 absolute mouse report (ID 1): [1, buttons(1=L 2=R 4=M), x16, y16, wheel] little endian.
_BUTTONS = ((0x01, 0x01), (0x02, 0x04), (0x04, 0x02))


class Mouse:
    def __init__(self) -> None:
        self.last_mask = 0

    def event(self, mask: int, x: int, y: int, width: int, height: int) -> bytes:
        buttons = sum(hid for vnc, hid in _BUTTONS if mask & vnc)
        pressed = mask & ~self.last_mask
        wheel = 1 if pressed & 0x08 else (-1 if pressed & 0x10 else 0)
        self.last_mask = mask
        ax = round(min(max(x, 0), width - 1) * 32767 / max(1, width - 1))
        ay = round(min(max(y, 0), height - 1) * 32767 / max(1, height - 1))
        return struct.pack("<BBHHb", 1, buttons, ax, ay, wheel)
