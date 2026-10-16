"""Observe the target's screen: HDMI state + a low-quality full-frame JPEG,
averaged into a fixed colour grid (no PyAV)."""

from __future__ import annotations

import time

import jpegdc
import vinsnap
from rules import Observation

QUALITY = 5            # averages don't care about JPEG artifacts; smaller = faster decode
GRID_COLS, GRID_ROWS = 16, 9

CaptureError = vinsnap.SnapshotError
hdmi_signal = vinsnap.hdmi_signal


def downsample(grid: list, cols: int = GRID_COLS, rows: int = GRID_ROWS) -> list:
    """Average an MCU colour grid into a fixed rows x cols grid."""
    src_rows, src_cols = len(grid), len(grid[0])
    out = []
    for r in range(rows):
        y0, y1 = r * src_rows // rows, max(r * src_rows // rows + 1, (r + 1) * src_rows // rows)
        row = []
        for c in range(cols):
            x0, x1 = c * src_cols // cols, max(c * src_cols // cols + 1, (c + 1) * src_cols // cols)
            cells = [grid[y][x] for y in range(y0, y1) for x in range(x0, x1)]
            row.append(tuple(round(sum(px[i] for px in cells) / len(cells)) for i in range(3)))
        out.append(row)
    return out


def observe() -> tuple[Observation, float]:
    """(observation, decode seconds). No signal -> Observation(False) without capturing."""
    if not hdmi_signal():
        return Observation(False), 0.0
    snap = vinsnap.capture(quality=QUALITY)
    start = time.monotonic()
    image = jpegdc.decode(snap.jpeg)
    grid = downsample(image.mcu_rgb())
    return Observation.from_grid(True, image.width, image.height, grid), time.monotonic() - start
