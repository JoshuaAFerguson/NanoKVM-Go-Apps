"""Pixel Pet movement: wandering, hops, and squash & stretch (pure, time-driven).

Each update() returns a Pose: horizontal offset from centre, lift above the
ground, horizontal/vertical scale and facing. Squash and stretch roughly
preserve volume (sx * sy ~ 1): tall and thin in the air, short and wide on
landing, with a slow breathing squish while idle.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

WALK_SPEED = 16.0          # px per second: an amble, not a sprint
HOP_PERIOD = 0.7           # seconds per hop while walking
HOP_HEIGHT = 4.0
JUMP_HEIGHT = 16.0
JUMP_TIME = 0.6
LAND_SQUASH_S = 0.16
PAUSE_RANGE = (2.5, 6.0)   # lingers between strolls


@dataclass(frozen=True)
class Pose:
    x: float
    lift: float
    sx: float
    sy: float
    facing: int   # 1 = right, -1 = left


class Motion:
    def __init__(self, half_range: float, rng: random.Random | None = None) -> None:
        self.half_range = half_range
        self.rng = rng or random.Random()
        self.x = 0.0
        self.target: float | None = None
        self.facing = 1
        self.pause_until = 0.0
        self.walk_clock = 0.0
        self.jump_at: float | None = None
        self.landed_at = -10.0
        self._was_air = False

    def jump(self, now: float) -> None:
        self.jump_at = now

    def update(self, dt: float, now: float, can_walk: bool, asleep: bool) -> Pose:
        lift = 0.0
        stretch = 1.0
        if self.jump_at is not None and now - self.jump_at < JUMP_TIME:
            phase = (now - self.jump_at) / JUMP_TIME
            lift = JUMP_HEIGHT * math.sin(math.pi * phase)
            stretch = 1.0 + 0.22 * math.sin(math.pi * phase)
        elif self.jump_at is not None:
            self.jump_at = None
        elif can_walk and not asleep:
            lift, stretch = self._walk(dt, now)
        else:
            self.target = None
        airborne = lift > 0.5
        if self._was_air and not airborne:
            self.landed_at = now
        self._was_air = airborne
        since_land = now - self.landed_at
        if not airborne and since_land < LAND_SQUASH_S:   # squash on landing
            stretch = 1.0 - 0.18 * (1 - since_land / LAND_SQUASH_S)
            lift = 0.0
        elif not airborne:                                # idle breathing
            rate, depth = (1.2, 0.05) if asleep else (2.2, 0.03)
            stretch = 1.0 + depth * math.sin(now * rate)
        sy = stretch
        sx = 1.0 / math.sqrt(stretch) if stretch >= 1 else 1.0 + (1.0 - stretch) * 0.8
        return Pose(round(self.x, 2), round(lift, 2), round(sx, 3), round(sy, 3), self.facing)

    def _walk(self, dt: float, now: float) -> tuple[float, float]:
        if self.target is None:
            if now < self.pause_until:
                return 0.0, 1.0
            self.target = self.rng.uniform(-self.half_range, self.half_range)
            self.walk_clock = 0.0
        distance = self.target - self.x
        if abs(distance) < 1.5:
            self.target = None
            self.pause_until = now + self.rng.uniform(*PAUSE_RANGE)
            return 0.0, 1.0
        self.facing = 1 if distance > 0 else -1
        step = min(abs(distance), WALK_SPEED * dt)
        self.x = max(-self.half_range, min(self.half_range, self.x + self.facing * step))
        self.walk_clock += dt
        phase = (self.walk_clock % HOP_PERIOD) / HOP_PERIOD
        hop = math.sin(math.pi * phase)
        return HOP_HEIGHT * hop, 1.0 + 0.14 * hop
