#!/usr/bin/env python3
"""Media & Power: mount ISO images on the KVM's virtual USB drive and press the
target's power button with the Finger Robot, from the touchscreen.

Tap the header to switch between the ISO and Power pages. Anything that acts on
the target (mount, eject, power) needs a second tap within 3 seconds.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from appbase import AppContext, app

import ui
from applog import AppLog
from kvmweb import KvmWeb, KvmWebError, display_name

APP_DIR = Path(__file__).resolve().parent
LOG = AppLog(APP_DIR)
HEADER_H, FOOTER_H, GAP, ROW_H = 28, 18, 4, 30
CONFIRM_S = 3.0
SHORT_PRESS_S, FORCE_OFF_S = 0.5, 6.0
FPS = 6
EXIT_WAIT_S = 15.0  # long enough for a forced-off hold plus release retries
PAGES = ("ISO", "Power")


class Screen:
    def __init__(self, ctx: AppContext, web: KvmWeb) -> None:
        self.ctx, self.web = ctx, web
        self.workers: list[threading.Thread] = []
        self.header, rest = ui.screen(ctx).split_top(HEADER_H)
        self.body, self.footer = rest.split_bottom(FOOTER_H)
        self.page = 0
        self.images: list[str] = []
        self.mounted = ""
        self.robot: bool | None = None
        self.offset = 0
        self.busy = ""
        self.message, self.message_color = "Loading...", ui.TEXT_DIM
        self.armed: tuple[str, float] | None = None   # (action key, expires)
        self.dirty = True
        self._lock = threading.Lock()
        self.run_bg("Loading", self.refresh)

    # -- background work -----------------------------------------------------

    def run_bg(self, label: str, fn) -> None:
        with self._lock:  # one job at a time, even if taps race
            if self.busy:
                return
            self.busy = label
        self.armed = None
        self.dirty = True

        def work() -> None:
            try:
                result = fn()
                self.say(result or "Ready", ui.TEXT_DIM if not result else ui.OK)
            except KvmWebError as exc:
                LOG.warn(f"{label}: {exc}")
                self.say(str(exc), ui.ERR)
            except Exception:
                LOG.exception(label)
                self.say(f"{label} failed (see log)", ui.ERR)
            finally:
                self.busy = ""
                self.dirty = True
        worker = threading.Thread(target=work, daemon=True)
        self.workers = [w for w in self.workers if w.is_alive()] + [worker]
        worker.start()

    def say(self, text: str, color: int) -> None:
        self.message, self.message_color, self.dirty = text, color, True

    def refresh(self) -> str:
        images, mounted = self.web.images(), self.web.mounted()
        robot = self.web.robot_available()
        with self._lock:
            self.images, self.mounted, self.robot = images, mounted, robot
        return ""

    def mount(self, path: str) -> str:
        self.web.mount(path)
        LOG.info(f"mounted {path}")
        self.refresh()
        return f"Mounted {display_name(path)}"

    def eject(self) -> str:
        self.web.eject()
        LOG.info("ejected")
        self.refresh()
        return "Ejected"

    def press(self, hold_s: float, label: str) -> str:
        try:
            self.web.power_press(hold_s)
        except KvmWebError as exc:
            if "RELEASE FAILED" in str(exc):
                LOG.error(str(exc))
            raise
        LOG.info(f"power: {label}")
        return f"{label} sent"

    # -- layout ------------------------------------------------------------------

    def rows(self) -> list[ui.Box]:
        area = self.body.inset(GAP)
        count = (area.h - BUTTON_ROOM) // ROW_H
        return [ui.Box(area.x, area.y + i * ROW_H, area.w, ROW_H - 3) for i in range(count)]

    def eject_box(self) -> ui.Box:
        area = self.body.inset(GAP)
        return ui.Box(area.x, area.y + area.h - (BUTTON_ROOM - 4), area.w, BUTTON_ROOM - 4)

    def power_boxes(self) -> list[ui.Box]:
        return ui.grid(self.body.inset(GAP * 2), 1, 2, GAP * 2)

    # -- input -------------------------------------------------------------------

    def confirm(self, key: str, prompt: str) -> bool:
        """First tap arms an action, a second tap within CONFIRM_S runs it."""
        now = time.monotonic()
        if self.armed and self.armed[0] == key and now < self.armed[1]:
            self.armed = None
            return True
        self.armed = (key, now + CONFIRM_S)
        self.say(prompt, ui.WARN)
        return False

    def on_tap(self, x: int, y: int) -> None:
        if self.header.contains(x, y):
            self.page, self.armed = (self.page + 1) % len(PAGES), None
            self.dirty = True
            return
        if self.busy:
            return
        if PAGES[self.page] == "ISO":
            self.tap_iso(x, y)
        else:
            self.tap_power(x, y)

    def tap_iso(self, x: int, y: int) -> None:
        if self.mounted and self.eject_box().contains(x, y):
            if self.confirm("eject", "Tap again to eject"):
                self.run_bg("Eject", self.eject)
            return
        index = ui.hit(self.rows(), x, y)
        if index is None or self.offset + index >= len(self.images):
            return
        path = self.images[self.offset + index]
        if path == self.mounted:
            return
        if self.confirm(f"mount:{path}", f"Tap again to mount {display_name(path)}"):
            self.run_bg("Mount", lambda: self.mount(path))

    def tap_power(self, x: int, y: int) -> None:
        if not self.robot:
            return
        power, force = self.power_boxes()
        if power.contains(x, y) and self.confirm("power", "Tap again to press Power"):
            self.run_bg("Power", lambda: self.press(SHORT_PRESS_S, "Power press"))
        elif force.contains(x, y) and self.confirm("force", "Tap again to FORCE OFF (6 s hold)"):
            self.run_bg("Force off", lambda: self.press(FORCE_OFF_S, "Force off"))

    def on_swipe(self, kind: str, x: int, y: int) -> None:
        if PAGES[self.page] != "ISO" or x < ui.EXIT_ZONE_PX or x >= self.ctx.width - ui.EXIT_ZONE_PX:
            return
        visible = len(self.rows())
        if kind == "up" and self.offset + visible < len(self.images):
            self.offset += visible
        elif kind == "down":
            self.offset = max(0, self.offset - visible)
        self.dirty = True

    # -- drawing -------------------------------------------------------------------

    def tick(self, dt: float) -> None:
        if self.armed and time.monotonic() > self.armed[1]:
            self.armed = None
            self.say("Cancelled", ui.TEXT_DIM)
        if self.dirty:
            self.dirty = False
            self.draw()

    def draw(self) -> None:
        fb = self.ctx.fb
        fb.clear(ui.BG)
        other = PAGES[(self.page + 1) % len(PAGES)]
        ui.draw_header(fb, self.header, PAGES[self.page], right=f"tap: {other} >", color=ui.INFO)
        (self.draw_iso if PAGES[self.page] == "ISO" else self.draw_power)()
        text = f"{self.busy}..." if self.busy else self.message
        color = ui.TEXT_DIM if self.busy else self.message_color
        ui.text(fb, self.footer.x + 6, self.footer.cy - 4, ui.truncate(text, self.footer.w - 12), color)

    def draw_iso(self) -> None:
        fb = self.ctx.fb
        rows = self.rows()
        if not self.images:
            ui.draw_text_center(fb, self.body, "No images" if self.robot is not None else "", ui.TEXT_DIM, 2)
        for box, path in zip(rows, self.images[self.offset:self.offset + len(rows)]):
            mounted = path == self.mounted
            armed = self.armed and self.armed[0] == f"mount:{path}"
            fb.fill_rect(box.x, box.y, box.w, box.h, ui.SURFACE_HI if armed else ui.SURFACE)
            fb.fill_rect(box.x, box.y, 3, box.h, ui.OK if mounted else ui.SURFACE_HI)
            name = ui.truncate(display_name(path), box.w - 16 - (64 if mounted else 0))
            ui.text(fb, box.x + 9, box.cy - 4, name, ui.TEXT)
            if mounted:
                ui.text(fb, box.x + box.w - 64, box.cy - 4, "mounted", ui.OK)
        if self.mounted:
            armed = self.armed and self.armed[0] == "eject"
            ui.draw_button(fb, self.eject_box(), "Eject", ui.ERR if armed else ui.SURFACE, accent=ui.ERR, max_scale=2)

    def draw_power(self) -> None:
        fb = self.ctx.fb
        if not self.robot:
            lines = ui.wrap("No Finger Robot detected. Attach one to the KVM's power port "
                            "to press the target's power button from here.", self.body.w - 24)
            for n, line in enumerate(lines):
                ui.text(fb, self.body.x + 12, self.body.y + 20 + n * 14, line, ui.TEXT_DIM)
            return
        power, force = self.power_boxes()
        for box, key, label, sub, accent in ((power, "power", "Power", "short press", ui.OK),
                                             (force, "force", "Force off", "hold 6 s", ui.ERR)):
            armed = self.armed and self.armed[0] == key
            ui.draw_button(fb, box, label, accent if armed else ui.SURFACE, accent=accent, sublabel=sub)


BUTTON_ROOM = 44  # space reserved under the image list for the Eject button


def draw_setup(ctx: AppContext, message: str) -> None:
    fb = ctx.fb
    fb.clear(ui.BG)
    header, body = ui.screen(ctx).split_top(HEADER_H)
    ui.draw_header(fb, header, "Media & Power", color=ui.ERR)
    for n, line in enumerate(ui.wrap(message, body.w - 16)[:12]):
        ui.text(fb, body.x + 8, body.y + 10 + n * 14, line, ui.TEXT)


@app()
def main(ctx: AppContext) -> None:
    try:
        web = KvmWeb(ctx.env.get("KVM_USERNAME", "").strip(), ctx.env.get("KVM_PASSWORD", ""))
    except KvmWebError as exc:
        draw_setup(ctx, f"{exc}: Settings > Apps > Installed > Media & Power. "
                        "Use the same login as the KVM's web page.")
        ctx.run(lambda dt: None, fps=1)
        return
    screen = Screen(ctx, web)
    LOG.info("started")
    try:
        ctx.run(LOG.guard(screen.tick), fps=FPS, on_tap=LOG.guard(screen.on_tap), on_swipe=LOG.guard(screen.on_swipe))
    finally:
        # Exiting: refuse new presses, cut a hold short, and wait for a worker that
        # may be about to press, so its own finally releases the button.
        web.close()
        for worker in screen.workers:
            worker.join(EXIT_WAIT_S)
        if web.held:  # never leave the power button pressed
            try:
                web.release()
                LOG.info("released Finger Robot on exit")
            except KvmWebError as exc:
                LOG.error(str(exc))


if __name__ == "__main__":
    main()
