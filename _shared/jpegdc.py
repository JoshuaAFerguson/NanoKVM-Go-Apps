"""DC-only baseline JPEG decoder: average colour of every 8x8 block, pure Python.

Shared module: edit _shared/jpegdc.py, then run `python3 _tools/sync_shared.py`.

Each 8x8 block's DC coefficient is its mean sample value, so averages need only
Huffman decoding: no IDCT, upsampling or per-pixel colour conversion. That is
cheap enough for small hardware-cropped snapshots on the NanoKVM without PyAV
(which takes 10-20 s to import on the Go). Supports baseline sequential JPEG
(SOF0/SOF1, 8-bit, Huffman), any H/V sampling, and restart intervals.
"""

from __future__ import annotations

import math
import struct
from array import array
from dataclasses import dataclass

MAX_BLOCKS = 260_000   # 4K 4:2:0 is ~195k blocks; also bounds memory (~4 bytes/block)


class JpegError(ValueError):
    """Not a JPEG this decoder supports, or corrupt/truncated data."""


class _Huffman:
    def __init__(self, counts: bytes, symbols: bytes) -> None:
        self.maxcode = [-1] * 17
        self.offset = [0] * 17
        self.symbols = symbols
        code = k = 0
        for length in range(1, 17):
            n = counts[length - 1]
            if n:
                self.offset[length] = k - code  # symbol index = code + offset
                code += n
                k += n
                self.maxcode[length] = code - 1
            code <<= 1
        if k != len(symbols):
            raise JpegError("bad Huffman table")


class _Bits:
    """MSB-first bit reader over one unstuffed restart segment."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0  # in bits
        self.end = len(data) * 8

    def bit(self) -> int:
        if self.pos >= self.end:
            raise JpegError("truncated JPEG data")
        b = (self.data[self.pos >> 3] >> (7 - (self.pos & 7))) & 1
        self.pos += 1
        return b

    def bits(self, n: int) -> int:
        value = 0
        for _ in range(n):
            value = (value << 1) | self.bit()
        return value

    def decode(self, table: _Huffman) -> int:
        code = 0
        for length in range(1, 17):
            code = (code << 1) | self.bit()
            if code <= table.maxcode[length]:
                return table.symbols[code + table.offset[length]]
        raise JpegError("bad Huffman code")

    def receive_extend(self, size: int) -> int:
        if size == 0:
            return 0
        value = self.bits(size)
        return value if value >= 1 << (size - 1) else value - (1 << size) + 1


@dataclass
class _Component:
    cid: int
    h: int
    v: int
    tq: int
    dc_table: int = 0
    ac_table: int = 0


@dataclass(frozen=True)
class DcImage:
    width: int
    height: int
    hmax: int
    vmax: int
    planes: tuple  # per component: (h, v, rows of block averages 0..255)

    def _valid(self, comp: int):
        h, v, rows = self.planes[comp]
        bw = math.ceil(math.ceil(self.width * h / self.hmax) / 8)
        bh = math.ceil(math.ceil(self.height * v / self.vmax) / 8)
        return [row[:bw] for row in rows[:bh]]

    def _mean(self, comp: int) -> float:
        values = [x for row in self._valid(comp) for x in row]
        return sum(values) / len(values)

    def mean_rgb(self) -> tuple[int, int, int]:
        if len(self.planes) == 1:
            y = round(self._mean(0))
            return (y, y, y)
        return ycc_to_rgb(self._mean(0), self._mean(1), self._mean(2))

    def luma_stddev(self) -> float:
        values = [x for row in self._valid(0) for x in row]
        mean = sum(values) / len(values)
        return math.sqrt(sum((x - mean) ** 2 for x in values) / len(values))

    def mcu_rgb(self) -> list[list[tuple[int, int, int]]]:
        """Average colour per MCU (16x16 pixels for 4:2:0), row-major."""
        yh, yv, yrows = self.planes[0]
        mcu_rows, mcu_cols = len(yrows) // yv, len(yrows[0]) // yh
        grid = []
        for my in range(mcu_rows):
            row = []
            for mx in range(mcu_cols):
                ys = [yrows[my * yv + j][mx * yh + i] for j in range(yv) for i in range(yh)]
                y = sum(ys) / len(ys)
                if len(self.planes) == 1:
                    row.append((round(y),) * 3)
                    continue
                cb, cr = (self._block_mean(c, mx, my) for c in (1, 2))
                row.append(ycc_to_rgb(y, cb, cr))
            grid.append(row)
        return grid

    def _block_mean(self, comp: int, mx: int, my: int) -> float:
        h, v, rows = self.planes[comp]
        vals = [rows[my * v + j][mx * h + i] for j in range(v) for i in range(h)]
        return sum(vals) / len(vals)


def ycc_to_rgb(y: float, cb: float, cr: float) -> tuple[int, int, int]:
    clamp = lambda x: max(0, min(255, round(x)))  # noqa: E731
    return (clamp(y + 1.402 * (cr - 128)),
            clamp(y - 0.344136 * (cb - 128) - 0.714136 * (cr - 128)),
            clamp(y + 1.772 * (cb - 128)))


def _segments(data: bytes, start: int) -> list[bytes]:
    """Entropy-coded data after SOS, unstuffed and split at RSTn markers."""
    segments, current, i, n = [], bytearray(), start, len(data)
    while i < n:
        b = data[i]
        if b != 0xFF:
            current.append(b)
            i += 1
            continue
        if i + 1 >= n:
            break
        marker = data[i + 1]
        if marker == 0x00:
            current.append(0xFF)
            i += 2
        elif 0xD0 <= marker <= 0xD7:
            segments.append(bytes(current))
            current = bytearray()
            i += 2
        elif marker == 0xFF:
            i += 1  # fill byte
        else:
            break  # EOI or another marker ends the scan
    segments.append(bytes(current))
    return segments


def decode(data: bytes) -> DcImage:
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        raise JpegError("not a JPEG")
    qt: dict[int, int] = {}          # table id -> DC quantizer
    dc_tables: dict[int, _Huffman] = {}
    ac_tables: dict[int, _Huffman] = {}
    comps: list[_Component] = []
    width = height = restart = 0
    i = 2
    try:
        while True:
            while data[i] == 0xFF and data[i + 1] == 0xFF:
                i += 1
            if data[i] != 0xFF:
                raise JpegError("bad marker")
            marker = data[i + 1]
            length = struct.unpack(">H", data[i + 2:i + 4])[0]
            seg = data[i + 4:i + 2 + length]
            if len(seg) != length - 2:
                raise JpegError("truncated header")
            if marker in (0xC0, 0xC1):
                if seg[0] != 8:
                    raise JpegError("only 8-bit JPEG is supported")
                height, width = struct.unpack(">HH", seg[1:5])
                comps = [_Component(seg[6 + 3 * k], seg[7 + 3 * k] >> 4, seg[7 + 3 * k] & 15, seg[8 + 3 * k])
                         for k in range(seg[5])]
                if len(comps) not in (1, 3) or any(not (1 <= c.h <= 4 and 1 <= c.v <= 4) for c in comps):
                    raise JpegError("unsupported component layout")
            elif 0xC2 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                raise JpegError("only baseline JPEG is supported")
            elif marker == 0xDB:
                j = 0
                while j < len(seg):
                    precision, tid = seg[j] >> 4, seg[j] & 15
                    qt[tid] = struct.unpack(">H", seg[j + 1:j + 3])[0] if precision else seg[j + 1]
                    j += 1 + (128 if precision else 64)
            elif marker == 0xC4:
                j = 0
                while j < len(seg):
                    tclass, tid = seg[j] >> 4, seg[j] & 15
                    counts = seg[j + 1:j + 17]
                    total = sum(counts)
                    table = _Huffman(counts, seg[j + 17:j + 17 + total])
                    (ac_tables if tclass else dc_tables)[tid] = table
                    j += 17 + total
            elif marker == 0xDD:
                restart = struct.unpack(">H", seg[:2])[0]
            elif marker == 0xDA:
                by_id = {c.cid: c for c in comps}
                scan = []
                for k in range(seg[0]):
                    c = by_id[seg[1 + 2 * k]]
                    c.dc_table, c.ac_table = seg[2 + 2 * k] >> 4, seg[2 + 2 * k] & 15
                    scan.append(c)
                return _decode_scan(data, i + 2 + length, width, height, comps, scan, qt,
                                    dc_tables, ac_tables, restart)
            elif marker == 0xD9:
                raise JpegError("no image data")
            i += 2 + length
    except (IndexError, KeyError, struct.error) as exc:
        raise JpegError(f"corrupt JPEG ({type(exc).__name__})") from exc


def _decode_scan(data, start, width, height, comps, scan, qt, dc_tables, ac_tables, restart) -> DcImage:
    if not width or not height or not comps or len(scan) != len(comps):
        raise JpegError("unsupported JPEG layout")
    hmax, vmax = max(c.h for c in comps), max(c.v for c in comps)
    mcux, mcuy = math.ceil(width / (8 * hmax)), math.ceil(height / (8 * vmax))
    total = mcux * mcuy
    blocks = total * sum(c.h * c.v for c in comps)
    # Validate size before allocating: a corrupt/hostile header must not cost memory.
    if blocks > MAX_BLOCKS:
        raise JpegError(f"image too large ({width}x{height})")
    segments = _segments(data, start)
    if blocks * 2 > 8 * sum(len(sg) for sg in segments):  # every block needs >= 2 bits
        raise JpegError("truncated JPEG data")
    planes = {c.cid: [array("f", bytes(4 * mcux * c.h)) for _ in range(mcuy * c.v)] for c in comps}
    per_segment = restart or total
    if math.ceil(total / per_segment) > len(segments):
        raise JpegError("truncated JPEG data")
    for mcu in range(total):
        if mcu % per_segment == 0:
            bits = _Bits(segments[mcu // per_segment])
            pred = {c.cid: 0 for c in scan}
        my, mx = divmod(mcu, mcux)
        for c in scan:
            dc_t, ac_t, q = dc_tables[c.dc_table], ac_tables[c.ac_table], qt[c.tq]
            for v in range(c.v):
                for h in range(c.h):
                    pred[c.cid] += bits.receive_extend(bits.decode(dc_t))
                    k = 1
                    while k < 64:  # skip AC coefficients
                        rs = bits.decode(ac_t)
                        run, size = rs >> 4, rs & 15
                        if size == 0:
                            if run != 15:
                                break  # end of block
                            k += 16
                            continue
                        k += run
                        bits.bits(size)
                        k += 1
                    planes[c.cid][my * c.v + v][mx * c.h + h] = pred[c.cid] * q / 8 + 128
    return DcImage(width, height, hmax, vmax, tuple((c.h, c.v, planes[c.cid]) for c in comps))
