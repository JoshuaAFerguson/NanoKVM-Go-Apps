#!/usr/bin/env python3
"""Render an App's screen on your computer, without a device.

Replaces `appbase` with an in-memory 284x240 framebuffer (the verified device
geometry), runs the App for a few frames, optionally injects taps/swipes, and
writes a PPM image (open it in any viewer; on macOS: `open out.ppm`).

Usage:
  python3 _tools/preview_app.py <app-dir> [out.ppm] [--env K=V ...] [--frames N]
                                [--tap X,Y] [--swipe up|down|left|right]

Hardware calls (MCP, sockets) still run for real, so pass a FAKE key via --env
(it lands in shell history) and expect network errors to show as the App would.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import types
from pathlib import Path

WIDTH, HEIGHT = 284, 240
SDK_DIR = Path(__file__).resolve().parents[1] / "_sdk"


class FakeFramebuffer:
    rotate = 90
    width, height = WIDTH, HEIGHT

    def __init__(self) -> None:
        self.pixels = [0] * (WIDTH * HEIGHT)

    def fill_rect(self, x, y, w, h, color):
        x0, y0, x1, y1 = max(0, x), max(0, y), min(WIDTH, x + w), min(HEIGHT, y + h)
        for row in range(y0, y1):
            base = row * WIDTH
            self.pixels[base + x0:base + x1] = [color] * max(0, x1 - x0)

    def clear(self, color=0):
        self.pixels = [color] * (WIDTH * HEIGHT)

    def put_pixel(self, x, y, color):
        self.fill_rect(x, y, 1, 1, color)

    def flush(self):
        pass

    def text_width(self, text, scale=1):
        return len(text) * 8 * scale

    def draw_text(self, x, y, text, color, scale=1):
        import font  # the App's copy of the shared font
        font.draw_text(self, x, y, text, color, scale)

    def text_center(self, cx, y, text, color, scale=1):
        self.draw_text(cx - self.text_width(text, scale) // 2, y, text, color, scale)

    def to_ppm(self, path: Path) -> None:
        out = bytearray()
        for v in self.pixels:
            out += bytes((((v >> 11) & 31) * 255 // 31, ((v >> 5) & 63) * 255 // 63, (v & 31) * 255 // 31))
        path.write_bytes(b"P6 %d %d 255\n" % (WIDTH, HEIGHT) + out)


class FakeContext:
    def __init__(self, env: dict, events: list, frames: int, realtime: bool = False) -> None:
        self.fb = FakeFramebuffer()
        self.env = types.MappingProxyType(env)
        self.width, self.height = WIDTH, HEIGHT
        self._events = events
        self._frames = frames
        self._realtime = realtime

    def poll(self):
        events, self._events = self._events, []
        return events

    def flush(self):
        pass

    def run(self, tick, fps=20.0, on_tap=None, on_swipe=None):
        for frame in range(self._frames):
            # deliver injected gestures after the first frame has drawn
            for kind, x, y in (self.poll() if frame == min(1, self._frames - 1) else []):
                if kind == "tap" and on_tap:
                    on_tap(x, y)
                elif kind != "tap" and on_swipe:
                    on_swipe(kind, x, y)
            if tick(1 / max(fps, 1)) is False:
                break
            if self._realtime:
                import time
                time.sleep(1 / max(fps, 1))


def install_fake_appbase(ctx: FakeContext, app_dir: Path) -> None:
    fake = types.ModuleType("appbase")
    real_font = {}
    if (SDK_DIR / "appbase.py").exists():  # reuse the real glyphs when the SDK is fetched
        spec = importlib.util.spec_from_file_location("_real_appbase", SDK_DIR / "appbase.py")
        real = importlib.util.module_from_spec(spec)
        sys.modules["_real_appbase"] = real  # dataclasses look the module up while loading
        spec.loader.exec_module(real)
        real_font = getattr(real, "_FONT", {})
        for name in ("rgb565", "BLACK", "WHITE", "RED", "GREEN", "BLUE", "YELLOW", "GRAY",
                     "DKGRAY", "ORANGE", "CYAN", "MAGENTA", "NAVY", "Rect"):
            if hasattr(real, name):
                setattr(fake, name, getattr(real, name))
    fake._FONT = real_font
    fake.AppContext = FakeContext
    manifest = json.loads((app_dir / "app.json").read_text())

    def app():
        def decorator(fn):
            def wrapper():
                return fn(ctx)
            wrapper.__app_info__ = types.SimpleNamespace(**{k: manifest.get(k, "") for k in (
                "app_id", "name", "creator", "create_time", "version", "desc", "category", "icon")})
            return wrapper
        return decorator

    fake.app = app
    sys.modules["appbase"] = fake


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("app_dir", type=Path)
    parser.add_argument("out", type=Path, nargs="?")
    parser.add_argument("--env", action="append", default=[], metavar="K=V")
    parser.add_argument("--frames", type=int, default=3)
    parser.add_argument("--realtime", action="store_true", help="sleep 1/fps between frames (for apps with background threads)")
    parser.add_argument("--tap", action="append", default=[], metavar="X,Y")
    parser.add_argument("--swipe", action="append", default=[], choices=["up", "down", "left", "right"])
    args = parser.parse_args(argv[1:])

    app_dir = args.app_dir.resolve()
    manifest = json.loads((app_dir / "app.json").read_text())
    env = {name: spec.get("default", spec.get("value", "")) for name, spec in manifest.get("env", {}).items()}
    for item in args.env:
        if "=" not in item:
            parser.error(f"--env needs K=V, got {item!r}")
        key, value = item.split("=", 1)
        env[key] = value
    events = [("tap", *map(int, t.split(","))) for t in args.tap]
    events += [(kind, WIDTH // 2, HEIGHT // 2) for kind in args.swipe]
    ctx = FakeContext(env, events, args.frames, args.realtime)

    install_fake_appbase(ctx, app_dir)
    sys.path.insert(0, str(app_dir))
    os.chdir(app_dir)
    spec = importlib.util.spec_from_file_location("app_main", app_dir / "main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.main()

    out = (args.out or Path(f"{app_dir.name}-preview.ppm")).resolve()
    ctx.fb.to_ppm(out)
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
