#!/usr/bin/env python3
"""Pixel Pet: a tiny Tamagotchi-style blob that lives on your NanoKVM.

Feed, play, wash and put it to bed with the four buttons; tap the pet to pet
it. It grows from an egg into one of three adult forms depending on how well
it's cared for, never dies (gentle mode), keeps living at quarter speed while
the App is closed, and notices the KVM: it perks up when the target is showing
video, gets sleepy at night, and feels sick if the device runs hot.
"""

from __future__ import annotations

import math
import time
from pathlib import Path

from appbase import AppContext, app

import appdata
import motion
import petsim
import sprites
import ui
import vinsnap
from applog import AppLog
from petsim import Env, Pet

APP_DIR = Path(__file__).resolve().parent
LOG = AppLog(APP_DIR)
HEADER_H, BARS_H, BUTTONS_H, GAP = 24, 18, 46, 4
SIM_EVERY_S, SAVE_EVERY_S = 10.0, 300.0
JUMP_GAP_S = 3 * SIM_EVERY_S      # a longer gap while open means the clock jumped
MAX_EFFECTS = 8
HOT_C = 70.0
NIGHT_HOURS = (22, 7)
FPS = 10
BAR_COLORS = {"fullness": ui.rgb565(255, 150, 60), "happiness": ui.rgb565(240, 90, 150),
              "energy": ui.rgb565(120, 170, 255), "hygiene": ui.rgb565(90, 210, 200)}
BAR_ICONS = {"fullness": "feed", "happiness": "heart", "energy": "sleep", "hygiene": "wash"}
SAYINGS = {"hungry": "I'm hungry!", "dirty": "Ew, messy...", "sleepy": "So sleepy...", "sad": "Play with me?",
           "sick": "I don't feel well", "happy": "", "ok": "", "asleep": "", "egg": "Something's moving..."}


def read_env() -> Env:
    try:
        temp = int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000
    except (OSError, ValueError):
        temp = 0.0
    hour = time.localtime().tm_hour
    night = hour >= NIGHT_HOURS[0] or hour < NIGHT_HOURS[1]
    return Env(video=vinsnap.hdmi_signal(), night=night, hot=temp >= HOT_C)


def draw_runs(fb, cached_runs, x: int, y: int, scale: int) -> None:
    for r, c, length, color in cached_runs:
        fb.fill_rect(x + c * scale, y + r * scale, length * scale, scale, color)


class Transform:
    """Maps sprite cells to screen pixels with separate x/y scale and mirroring,
    anchored at the sprite's bottom centre (so squash keeps its feet on the ground).

    Cell sizes snap to whole pixels: fractional sizes would make some columns a
    pixel wider than others, shifting every frame ("pixel boil"). Snapped, the
    squash/stretch reads as crisp, distinct poses like hand-drawn pixel art.
    """

    def __init__(self, cols: int, rows: int, base: float, sx: float, sy: float,
                 foot_x: float, foot_y: float, facing: int) -> None:
        self.cols, self.rows = cols, rows
        self.cw, self.ch = max(1, round(base * sx)), max(1, round(base * sy))
        self.left = round(foot_x - cols * self.cw / 2)
        self.top = round(foot_y) - rows * self.ch
        self.mirror = facing < 0

    def rect(self, fb, col: float, row: float, w: float, h: float, color: int) -> None:
        if self.mirror:
            col = self.cols - col - w
        x0, x1 = round(self.left + col * self.cw), round(self.left + (col + w) * self.cw)
        y0, y1 = round(self.top + row * self.ch), round(self.top + (row + h) * self.ch)
        fb.fill_rect(x0, y0, max(1, x1 - x0), max(1, y1 - y0), color)

    def box(self) -> ui.Box:
        return ui.Box(round(self.left), round(self.top), round(self.cols * self.cw), round(self.rows * self.ch))


def draw_icon(fb, name: str, x: int, y: int, scale: int) -> None:
    draw_runs(fb, sprites.runs(sprites.ICONS[name], sprites.ICON_PALETTE), x, y, scale)


class PetScreen:
    def __init__(self, ctx: AppContext, pet: Pet, save_path: Path) -> None:
        self.ctx, self.pet, self.save_path = ctx, pet, save_path
        screen = ui.screen(ctx)
        self.header, rest = screen.split_top(HEADER_H)
        self.bars, rest = rest.split_top(BARS_H)
        self.stage_box, buttons = rest.split_bottom(BUTTONS_H)
        self.buttons = ui.grid(buttons.inset(GAP, GAP // 2), 4, 1, GAP)
        self.env = read_env()
        self.sim_at = self.save_at = time.monotonic()
        self.effects: list[tuple[str, float, int]] = []   # (kind, started, x offset)
        self.motion = motion.Motion(half_range=self.stage_box.w // 2 - 70)
        self.last_frame = time.monotonic()
        self.wobble_until = 0.0
        self.pet_box = ui.Box(0, 0, 0, 0)
        self._runs_cache: dict = {}

    # -- life ---------------------------------------------------------------------

    def simulate(self, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self.sim_at >= SIM_EVERY_S:
            self.sim_at = now
            self.env = read_env()
            before = self.pet.stage
            # A long gap while open means the wall clock jumped (no RTC + NTP):
            # treat it like time away instead of simulating it at full speed.
            gap = time.time() - self.pet.last_update
            self.pet = petsim.advance(self.pet, time.time(), self.env, open_app=0 <= gap <= JUMP_GAP_S)
            if self.pet.stage != before:
                LOG.info(f"{self.pet.name} grew: {before} -> {self.pet.stage} {self.pet.form}".strip())
                self.add_effect("heart", now)
                self.motion.jump(now)
        if force or now - self.save_at >= SAVE_EVERY_S:
            self.save(durable=force)

    def save(self, durable: bool = True) -> None:
        """Periodic saves skip fsync to spare the flash; actions and exit are durable."""
        self.save_at = time.monotonic()
        try:
            appdata.write_json(self.save_path, self.pet.to_dict(), durable=durable)
        except OSError as exc:
            LOG.warn(f"save failed: {exc}")

    def add_effect(self, kind: str, now: float, offset: int = 0) -> None:
        self.effects = (self.effects + [(kind, now, offset)])[-MAX_EFFECTS:]

    def button_actions(self) -> list[str]:
        p = self.pet
        return ["feed", "heal" if p.sick else "play", "wash", "wake" if p.asleep else "sleep"]

    def on_tap(self, x: int, y: int) -> None:
        now = time.monotonic()
        index = ui.hit(self.buttons, x, y)
        if index is not None:
            if self.pet.stage == "egg":
                self.wobble_until = now + 0.8
                return
            icon = self.button_actions()[index]
            action = {"wash": "clean", "wake": "sleep"}.get(icon, icon)
            refused = petsim.refuses(self.pet, action)
            self.pet = petsim.act(self.pet, action, time.time())
            if refused:  # stuffed, too tired...: no fake success feedback
                self.save()
                return
            if action in ("play", "feed", "heal"):
                self.add_effect("heart", now)
                self.motion.jump(now)
            if action == "clean":
                self.add_effect("wash", now)
            self.save()
        elif self.pet_box.contains(x, y):
            if self.pet.stage == "egg":
                self.wobble_until = now + 0.8
            elif not self.pet.asleep:
                self.add_effect("heart", now, (len(self.effects) % 3 - 1) * 14)
                self.motion.jump(now)
                self.pet = Pet(**{**self.pet.__dict__, "happiness": min(100.0, self.pet.happiness + 1)})

    # -- drawing --------------------------------------------------------------------

    def tick(self, dt: float) -> None:
        self.simulate()
        self.draw()

    def age_text(self) -> str:
        p = self.pet
        if p.stage == "egg":
            return "egg"
        hours = int(p.age_s // 3600)
        age = f"{hours // 24}d {hours % 24}h" if hours >= 24 else f"{hours}h"
        return f"{p.form or p.stage} {age}"

    def draw(self) -> None:
        fb = self.ctx.fb
        fb.clear(ui.BG)
        ui.draw_header(fb, self.header, self.pet.name, right=self.age_text(), color=ui.VIOLET)
        self.draw_bars()
        self.draw_pet()
        for box, icon in zip(self.buttons, self.button_actions()):
            fb.fill_rect(box.x, box.y, box.w, box.h, ui.SURFACE)
            draw_icon(fb, icon, box.cx - 12, box.cy - 12, 3)

    def draw_bars(self) -> None:
        fb = self.ctx.fb
        cells = ui.grid(self.bars.inset(GAP, 3), 4, 1, GAP * 2)
        for box, stat in zip(cells, petsim.STATS):
            draw_icon(fb, BAR_ICONS[stat], box.x, box.y + 1, 1)
            bar = ui.Box(box.x + 11, box.y + 3, box.w - 11, 6)
            fb.fill_rect(bar.x, bar.y, bar.w, bar.h, ui.SURFACE_HI)
            value = getattr(self.pet, stat)
            color = BAR_COLORS[stat] if value >= 25 else ui.ERR
            fb.fill_rect(bar.x, bar.y, max(1, int(bar.w * value / 100)), bar.h, color)

    def draw_pet(self) -> None:
        fb, p, now = self.ctx.fb, self.pet, time.monotonic()
        dt, self.last_frame = now - self.last_frame, now
        rows, palette, face_key = sprites.body(p.stage, p.form)
        area = self.stage_box
        base = max(3, min(7, (area.h - 30) // len(rows)))
        mood = petsim.mood(p)
        can_walk = p.stage != "egg" and not p.asleep and mood not in ("sick", "sleepy")
        pose = self.motion.update(dt, now, can_walk=can_walk, asleep=p.asleep)
        sx, sy, x_off, lift = pose.sx, pose.sy, pose.x, pose.lift
        if p.stage == "egg":  # eggs don't walk; they wobble (tap, or now and then)
            wobble = now < self.wobble_until or int(now) % 5 == 0
            sx, sy = (1.0 + 0.06 * abs(math.sin(now * 14)), 1.0 - 0.05 * abs(math.sin(now * 14))) if wobble else (1, 1)
            x_off, lift = (3 * math.sin(now * 14) if wobble else 0), 0
        ground = area.y + area.h - 8
        cx = area.cx + x_off
        t = Transform(len(rows[0]), len(rows), base, sx, sy, cx, ground - lift, pose.facing)
        self.pet_box = t.box()
        shadow_w = int(len(rows[0]) * base * max(0.5, 1 - lift / 40))   # shadow shrinks as it rises
        fb.fill_rect(int(cx - shadow_w / 2), ground, shadow_w, 3, ui.SURFACE)
        key = (p.stage, p.form)
        if key not in self._runs_cache:
            self._runs_cache = {key: sprites.runs(rows, palette)}
        for r, c, length, color in self._runs_cache[key]:
            t.rect(fb, c, r, length, 1, color)
        if face_key:
            self.draw_face(t, face_key, sprites.face_offset(p.stage, p.form), now)
        for n in range(min(p.poops, petsim.MAX_POOPS)):
            draw_icon(fb, "poop", area.x + area.w - 30 - n * 26, area.y + area.h - 26, 3)
        b = self.pet_box
        self.draw_effects(b.x, b.y, b.w, now)
        saying = SAYINGS.get(mood, "")
        if saying:
            ui.text(fb, area.x + 8, area.y + 4, saying, ui.TEXT_DIM)

    def draw_face(self, t: Transform, key: str, row_offset: int, now: float) -> None:
        fb, p = self.ctx.fb, self.pet
        eye_r, left, right, mouth_r, mouth_c = sprites.FACES[key]
        eye_r += row_offset
        mouth_r += row_offset
        mood = petsim.mood(p)
        blink = (now % 4) < 0.15
        for col in (left, right):
            if p.asleep or blink or mood == "sleepy":
                t.rect(fb, col, eye_r + 1, 2, 0.5, sprites.INK)
            else:
                t.rect(fb, col, eye_r, 2, 2, sprites.INK)
                t.rect(fb, col + 0.5, eye_r + 0.3, 0.6, 0.6, sprites.WHITE)
        if p.form == "grumpy" or mood == "sick":  # brows
            t.rect(fb, left, eye_r - 1, 2, 0.4, sprites.INK)
            t.rect(fb, right, eye_r - 1, 2, 0.4, sprites.INK)
        u = 0.5
        if p.asleep:
            return
        if mood in ("happy", "ok"):
            t.rect(fb, mouth_c - 2 * u, mouth_r, u, u, sprites.INK)
            t.rect(fb, mouth_c + u, mouth_r, u, u, sprites.INK)
            t.rect(fb, mouth_c - u, mouth_r + u, 2 * u, u, sprites.INK)
        elif mood == "hungry":
            t.rect(fb, mouth_c - u, mouth_r, 2 * u, 2 * u, sprites.INK)
        else:
            t.rect(fb, mouth_c - u, mouth_r, 2 * u, u, sprites.INK)
            t.rect(fb, mouth_c - 2 * u, mouth_r + u, u, u, sprites.INK)
            t.rect(fb, mouth_c + u, mouth_r + u, u, u, sprites.INK)

    def draw_effects(self, x: int, y: int, width: int, now: float) -> None:
        fb = self.ctx.fb
        self.effects = [e for e in self.effects if now - e[1] < 1.5]
        for kind, started, offset in self.effects:
            rise = int((now - started) * 40)
            draw_icon(fb, kind, x + width // 2 - 12 + offset, y - 26 - rise, 3)
        if self.pet.asleep:
            for n in range(3):
                phase = (now * 0.8 + n / 3) % 1.0
                ui.text(fb, x + width + 4 + n * 10, int(y + 10 - phase * 30), "z", ui.TEXT_DIM, 2 if n == 1 else 1)
        if self.pet.sick:
            draw_icon(fb, "heal", x - 30, y, 3)


def load_pet(path: Path, name: str) -> Pet:
    now = time.time()
    data = appdata.read_json(path)
    pet = Pet.from_dict(data, now) if isinstance(data, dict) else Pet.new(name, now)
    if name and pet.name != name[:12]:
        pet = Pet(**{**pet.__dict__, "name": name[:12]})
    # catch up on the time the App was closed (slowed down, capped)
    return petsim.advance(pet, now, read_env(), open_app=False)


@app()
def main(ctx: AppContext) -> None:
    save_path = appdata.data_dir("pixel-pet") / "pet.json"
    pet = load_pet(save_path, (ctx.env.get("PET_NAME") or "Blip").strip())
    screen = PetScreen(ctx, pet, save_path)
    screen.save()
    LOG.info(f"opened: {pet.name} ({pet.stage} {pet.form}) mood={petsim.mood(pet)}")
    try:
        ctx.run(LOG.guard(screen.tick), fps=FPS, on_tap=LOG.guard(screen.on_tap))
    finally:
        screen.simulate(force=True)


if __name__ == "__main__":
    main()
