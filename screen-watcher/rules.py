"""Screen Watcher detection rules: a pure state machine over observations."""

from __future__ import annotations

from dataclasses import dataclass, field

BSOD_FRACTION = 0.6      # share of cells that must be "BSOD blue"
BLACK_LUMA = 16          # mean luma below this ...
BLACK_SPREAD = 8         # ... with almost no variation = black screen
SAME_TOLERANCE = 10      # max per-channel difference for a cell to count as unchanged


def _luma(rgb) -> float:
    r, g, b = rgb
    return 0.299 * r + 0.587 * g + 0.114 * b


@dataclass(frozen=True)
class Observation:
    signal: bool
    width: int = 0
    height: int = 0
    grid: tuple = ()          # rows of (r, g, b) per cell

    @staticmethod
    def from_grid(signal: bool, width: int, height: int, grid) -> "Observation":
        rows = tuple(tuple(tuple(c) for c in row) for row in grid)
        return Observation(signal, width, height, rows)

    def cells(self):
        return [c for row in self.grid for c in row]


def same_screen(a: Observation, b: Observation, tolerance: int = SAME_TOLERANCE) -> bool:
    """True if every cell matches within `tolerance` (JPEG noise never flips this)."""
    ca, cb = a.cells(), b.cells()
    return bool(ca) and len(ca) == len(cb) and all(
        abs(x - y) <= tolerance for p, q in zip(ca, cb) for x, y in zip(p, q))


def is_bsod(obs: Observation) -> bool:
    cells = obs.cells()
    if not cells:
        return False
    blue = sum(1 for r, g, b in cells if b >= 140 and b - r >= 90 and b - g >= 40)
    return blue / len(cells) >= BSOD_FRACTION


def is_black(obs: Observation) -> bool:
    lumas = [_luma(c) for c in obs.cells()]
    return bool(lumas) and sum(lumas) / len(lumas) < BLACK_LUMA and max(lumas) - min(lumas) < BLACK_SPREAD * 4


@dataclass(frozen=True)
class Settings:
    signal_grace_s: float = 30.0
    bsod: bool = True         # blue wallpapers can false-positive: let users turn it off
    bsod_checks: int = 2
    black_min: float = 0.0    # 0 = off
    freeze_min: float = 0.0   # 0 = off


@dataclass(frozen=True)
class Event:
    kind: str     # signal | bsod | black | frozen
    level: str    # alert | recovered
    title: str
    message: str


TITLES = {"signal": "Video signal lost", "bsod": "Blue screen detected",
          "black": "Screen is black", "frozen": "Screen looks frozen"}
RECOVERED = {"signal": "Video signal is back", "bsod": "Blue screen cleared",
             "black": "Screen is no longer black", "frozen": "Screen is changing again"}


@dataclass
class _Condition:
    since: float | None = None   # when the condition started being true
    count: int = 0               # consecutive true checks
    alerted_at: float | None = None


@dataclass
class Rules:
    settings: Settings
    _conds: dict = field(default_factory=lambda: {k: _Condition() for k in TITLES})
    _seen_signal: bool = False
    _last: Observation | None = None
    _changed_at: float | None = None   # when the screen last visibly changed

    def active(self) -> list[str]:
        return [k for k, c in self._conds.items() if c.alerted_at is not None]

    def snapshot(self) -> dict:
        """State worth surviving a service restart (stored in status.json)."""
        return {"seen_signal": self._seen_signal, "active": self.active()}

    def restore(self, state: dict, now: float) -> None:
        if not isinstance(state, dict):
            return
        self._seen_signal = bool(state.get("seen_signal"))
        for kind in state.get("active") or []:
            if kind in self._conds:
                self._conds[kind] = _Condition(since=now, count=1, alerted_at=now)

    def _step(self, kind: str, true: bool, now: float, ready) -> list[Event]:
        cond = self._conds[kind]
        if not true:
            started, alerted = cond.since, cond.alerted_at
            self._conds[kind] = _Condition()
            if alerted is not None:
                return [Event(kind, "recovered", RECOVERED[kind], f"after {int(now - started)}s")]
            return []
        if cond.since is None:
            cond.since = self._changed_at if kind == "frozen" and self._changed_at is not None else now
        cond.count += 1
        if cond.alerted_at is None and ready(cond):
            cond.alerted_at = now
            return [Event(kind, "alert", TITLES[kind], self._detail(kind, now - cond.since))]
        return []

    @staticmethod
    def _detail(kind: str, duration: float) -> str:
        return {"signal": f"No video from the target for {int(duration)}s",
                "bsod": "The target's screen is mostly BSOD-blue",
                "black": f"The target's screen has been black for {int(duration // 60)} min",
                "frozen": f"Nothing on screen has changed for {int(duration // 60)} min"}[kind]

    def update(self, obs: Observation, now: float) -> list[Event]:
        s = self.settings
        events: list[Event] = []
        if obs.signal:
            self._seen_signal = True
        lost = self._seen_signal and not obs.signal
        events += self._step("signal", lost, now, lambda c: now - c.since >= s.signal_grace_s)
        if not (obs.signal and obs.grid):
            # No picture: screen conditions are unknown, not cleared. Keep them as-is
            # (no false "recovered" while a target reboots) and restart the frozen timer.
            self._last = None
            return events
        bsod = s.bsod and is_bsod(obs)
        black = not bsod and is_black(obs)
        events += self._step("bsod", bsod, now, lambda c: c.count >= s.bsod_checks)
        events += self._step("black", black and s.black_min > 0, now,
                             lambda c: now - c.since >= s.black_min * 60)
        unchanged = not black and self._last is not None and same_screen(obs, self._last)
        if not unchanged:
            self._changed_at = now
        events += self._step("frozen", unchanged and s.freeze_min > 0, now,
                             lambda c: now - c.since >= s.freeze_min * 60)
        self._last = obs
        return events
