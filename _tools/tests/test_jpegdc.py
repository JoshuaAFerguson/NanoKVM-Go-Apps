"""Tests for _shared/jpegdc.py (DC-only baseline JPEG decoder)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared"))

import jpegdc  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load(name):
    return jpegdc.decode((FIXTURES / name).read_bytes())


def close(a, b, tol=12):
    return all(abs(x - y) <= tol for x, y in zip(a, b))


class DecodeTest(unittest.TestCase):
    def test_solid_colors_average_correctly(self):
        for name, rgb in (("solid_bsod_blue.jpg", (0, 120, 215)), ("solid_black.jpg", (0, 0, 0)),
                          ("solid_white.jpg", (255, 255, 255)), ("solid_red.jpg", (220, 30, 30))):
            with self.subTest(name):
                img = load(name)
                self.assertEqual((img.width, img.height), (64, 64))
                self.assertTrue(close(img.mean_rgb(), rgb), (name, img.mean_rgb()))

    def test_block_grid_keeps_spatial_layout(self):
        img = load("split_black_white.jpg")
        grid = img.mcu_rgb()  # 16x16 cells: 4x4 for a 64x64 image
        self.assertEqual((len(grid), len(grid[0])), (4, 4))
        self.assertTrue(close(grid[0][0], (0, 0, 0), 20))
        self.assertTrue(close(grid[0][3], (255, 255, 255), 20))

    def test_partial_mcus_and_restart_markers(self):
        img = load("odd_size_gray.jpg")  # 50x30: partial MCUs; sips also emits DRI/RSTn
        self.assertEqual((img.width, img.height), (50, 30))
        self.assertTrue(close(img.mean_rgb(), (128, 128, 128)), img.mean_rgb())

    def test_luma_stats(self):
        img = load("split_black_white.jpg")
        self.assertGreater(img.luma_stddev(), 100)
        self.assertLess(load("solid_red.jpg").luma_stddev(), 3)

    def test_rejects_non_baseline_and_garbage(self):
        for bad in (b"", b"not a jpeg", b"\xff\xd8\xff\xc2\x00\x02"):
            with self.assertRaises(jpegdc.JpegError):
                jpegdc.decode(bad)

    def _with_sof(self, width, height, sampling=0x22, comps=3):
        data = bytearray((FIXTURES / "solid_red.jpg").read_bytes())
        i = data.index(b"\xff\xc0")
        data[i + 5:i + 9] = height.to_bytes(2, "big") + width.to_bytes(2, "big")
        if comps == 3:
            data[i + 11] = sampling
        return bytes(data)

    def test_absurd_dimensions_rejected_before_allocating(self):
        import tracemalloc
        tracemalloc.start()
        with self.assertRaises(jpegdc.JpegError):
            jpegdc.decode(self._with_sof(65535, 65535))
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        self.assertLess(peak, 2 * 1024 * 1024)

    def test_zero_sampling_factor_is_jpeg_error(self):
        with self.assertRaises(jpegdc.JpegError):
            jpegdc.decode(self._with_sof(64, 64, sampling=0x00))

    def test_truncated_data_raises(self):
        data = (FIXTURES / "solid_red.jpg").read_bytes()
        with self.assertRaises(jpegdc.JpegError):
            jpegdc.decode(data[: len(data) // 2])


if __name__ == "__main__":
    unittest.main()
