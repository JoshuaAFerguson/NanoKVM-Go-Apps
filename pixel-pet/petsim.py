"""Pixel Pet simulation: pure functions over an immutable Pet (no device access).

Gentle mode: needs drop to 0 but the pet never dies; good care always recovers it.
Time keeps passing while the App is closed, at CLOSED_RATE and capped, so a few
days away is fine. Light KVM tie-ins come in through Env: company when the target
shows video, sleepiness at night, and sickness when the device runs hot.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, fields, replace

HOUR = 3600.0
STEP_S = 60.0                 # simulation step (pet time)
HATCH_S = 120.0               # an egg hatches after 2 minutes of (pet) time
BABY_UNTIL_H, KID_UNTIL_H = 24, 72
CLOSED_RATE = 0.25            # time passes at a quarter speed while closed...
MAX_CATCHUP_S = 12 * HOUR     # ...and at most 12 pet-hours are simulated (48 h away)
MAX_OPEN_S = 30 * 24 * HOUR   # hard cap for one advance() even while open (clock jumps)
MAX_STEPS = 2000              # long spans use bigger steps so advance() stays fast

DECAY_PER_H = {"fullness": 8.0, "happiness": 5.0, "hygiene": 3.0}
ENERGY_AWAKE_PER_H, ENERGY_NIGHT_PER_H, ENERGY_SLEEP_PER_H = 6.0, 10.0, 25.0
COMPANY_PER_H = 7.0           # happiness gained while the target shows video
POOP_EVERY_H = 3.0
MAX_POOPS = 4

FORMS = ((65.0, "sparkle"), (40.0, "chill"), (0.0, "grumpy"))
STATS = ("fullness", "happiness", "energy", "hygiene")


@dataclass(frozen=True)
class Env:
    video: bool = False   # the KVM's target is showing video (someone's around)
    night: bool = False
    hot: bool = False     # the KVM itself is running hot


@dataclass(frozen=True)
class Pet:
    name: str
    stage: str = "egg"            # egg | baby | kid | adult
    form: str = ""                # adult form: sparkle | chill | grumpy
    last_update: float = 0.0      # wall clock of the last advance()
    egg_s: float = 0.0
    age_s: float = 0.0            # pet-time since hatching
    fullness: float = 80.0
    happiness: float = 80.0
    energy: float = 80.0
    hygiene: float = 100.0
    poops: int = 0
    poop_clock: float = 0.0
    asleep: bool = False
    sick: bool = False
    care_sum: float = 0.0
    care_n: int = 0
    care_clock: float = 0.0

    @staticmethod
    def new(name: str, now: float) -> "Pet":
        return Pet(name=name[:12] or "Pet", last_update=now)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(data, now: float = 0.0) -> "Pet":
        """Tolerant load: wrong types start a fresh egg; out-of-range values are clamped."""
        try:
            kinds = {f.name: f.type for f in fields(Pet)}
            values = {}
            for key, value in data.items():
                if key not in kinds:
                    continue
                expected = {"str": str, "float": (int, float), "int": int, "bool": bool}[kinds[key]]
                if isinstance(value, bool) and kinds[key] != "bool":
                    raise TypeError(key)
                if not isinstance(value, expected):
                    raise TypeError(key)
                values[key] = float(value) if kinds[key] == "float" else value
            pet = Pet(**values)
            if pet.stage not in ("egg", "baby", "kid", "adult"):
                raise ValueError("stage")
            return _sanitize(pet, now)
        except (AttributeError, TypeError, ValueError, KeyError, OverflowError):
            return Pet.new("Pet", now)


def _finite(value: float, default: float) -> float:
    return value if math.isfinite(value) else default


def _sanitize(pet: Pet, now: float) -> Pet:
    """Clamp everything into range so a corrupt save can't crash or hang the App."""
    care_sum = _finite(pet.care_sum, 0.0)
    care_n = max(0, pet.care_n)
    if care_sum < 0 or not care_n:
        care_sum, care_n = 0.0, 0
    return replace(
        pet,
        name=(pet.name.strip() or "Pet")[:12],
        form=pet.form if pet.form in ("", "sparkle", "chill", "grumpy") else "",
        last_update=_finite(pet.last_update, now),
        egg_s=max(0.0, _finite(pet.egg_s, 0.0)),
        age_s=min(max(0.0, _finite(pet.age_s, 0.0)), 10 * 365 * 24 * HOUR),
        poops=min(MAX_POOPS, max(0, pet.poops)),
        poop_clock=max(0.0, _finite(pet.poop_clock, 0.0)),
        care_clock=max(0.0, _finite(pet.care_clock, 0.0)),
        care_sum=care_sum, care_n=care_n,
        **{stat: _clamp(_finite(getattr(pet, stat), 50.0)) for stat in STATS},
    )


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, value))


def hatch(pet: Pet, now: float) -> Pet:
    return replace(pet, stage="baby", egg_s=HATCH_S, last_update=now) if pet.stage == "egg" else pet


def _stage_for(age_s: float) -> str:
    hours = age_s / HOUR
    return "baby" if hours < BABY_UNTIL_H else "kid" if hours < KID_UNTIL_H else "adult"


def _care_form(s: dict) -> str:
    average = s["care_sum"] / s["care_n"] if s["care_n"] > 0 else 50.0
    average = average if math.isfinite(average) else 50.0
    return next((name for floor, name in FORMS if average >= floor), "grumpy")


def _step(s: dict, dt: float, env: Env) -> None:
    """Advance a mutable state dict by dt pet-seconds (used inside advance only)."""
    if s["stage"] == "egg":
        s["egg_s"] += dt
        if s["egg_s"] >= HATCH_S:
            s["stage"] = "baby"
        return
    h = dt / HOUR
    s["age_s"] += dt
    sleep_factor = 0.5 if s["asleep"] else 1.0
    s["fullness"] -= DECAY_PER_H["fullness"] * h * sleep_factor
    s["hygiene"] -= DECAY_PER_H["hygiene"] * h
    mood_drift = -DECAY_PER_H["happiness"] * h
    if env.video and not s["asleep"]:
        mood_drift += COMPANY_PER_H * h
    if s["fullness"] < 20:
        mood_drift -= 6.0 * h
    if s["hygiene"] < 30 or s["poops"] >= 2:
        mood_drift -= 3.0 * h
    if s["sick"]:
        mood_drift -= 4.0 * h
    s["happiness"] += mood_drift
    if s["asleep"]:
        s["energy"] += ENERGY_SLEEP_PER_H * h
        if s["energy"] >= 100 and not env.night:
            s["asleep"] = False
    else:
        s["energy"] -= (ENERGY_NIGHT_PER_H if env.night else ENERGY_AWAKE_PER_H) * h
        if env.night and s["energy"] < 25:
            s["asleep"] = True
    s["poop_clock"] += h
    if s["poop_clock"] >= POOP_EVERY_H and s["fullness"] > 20:
        s["poop_clock"] = 0.0
        s["poops"] = min(MAX_POOPS, s["poops"] + 1)
        s["hygiene"] -= 10
    if env.hot or s["hygiene"] < 10 or s["poops"] >= 3:
        s["sick"] = True
    for stat in STATS:
        s[stat] = _clamp(s[stat])
    s["care_clock"] += h
    if s["care_clock"] >= 1.0:
        s["care_clock"] = 0.0
        s["care_sum"] += (s["fullness"] + s["happiness"] + s["hygiene"]) / 3
        s["care_n"] += 1
    stage = _stage_for(s["age_s"])
    if stage == "adult" and s["stage"] != "adult":
        s["form"] = _care_form(s)
    s["stage"] = stage


def _tend(pet: Pet, now: float) -> Pet:
    """What an attentive owner does (used for simulations and tests)."""
    if pet.sick:
        pet = act(pet, "heal", now)
    if pet.poops or pet.hygiene < 60:
        pet = act(pet, "clean", now)
    if pet.fullness < 60:
        pet = act(pet, "feed", now)
    if pet.energy < 30 and not pet.asleep:
        pet = act(pet, "sleep", now)
    elif pet.asleep and pet.energy > 90:
        pet = act(pet, "sleep", now)
    if not pet.asleep and pet.happiness < 60 and pet.energy > 20:
        pet = act(pet, "play", now)
    return pet


def advance(pet: Pet, now: float, env: Env, open_app: bool, tend: bool = False) -> Pet:
    elapsed = now - pet.last_update
    if not math.isfinite(elapsed) or elapsed < 0:
        return replace(pet, last_update=now)  # clock went backwards / garbage: resync, simulate nothing
    if elapsed == 0:
        return pet
    if open_app:
        simulated = min(elapsed, MAX_OPEN_S)
    else:
        simulated = min(elapsed * CLOSED_RATE, MAX_CATCHUP_S)
    step = max(STEP_S, simulated / MAX_STEPS)
    state = asdict(pet)
    remaining = simulated
    while remaining > 0:
        dt = min(step, remaining)
        _step(state, dt, env)
        if tend:
            state = asdict(_tend(Pet(**state), now))
        remaining -= dt
    state["last_update"] = now
    return Pet(**state)


def act(pet: Pet, action: str, now: float) -> Pet:
    if pet.stage == "egg":
        return pet
    if action == "feed":
        if pet.fullness >= 95:  # stuffed: refuses, a bit grumpy
            return replace(pet, happiness=_clamp(pet.happiness - 5))
        return replace(pet, fullness=_clamp(pet.fullness + 30), asleep=False)
    if action == "play":
        if pet.asleep or pet.energy < 10:
            return pet
        return replace(pet, happiness=_clamp(pet.happiness + 20), energy=_clamp(pet.energy - 10),
                       fullness=_clamp(pet.fullness - 5))
    if action == "clean":
        return replace(pet, poops=0, hygiene=100.0)
    if action == "sleep":
        return replace(pet, asleep=not pet.asleep)
    if action == "heal":
        return replace(pet, sick=False, happiness=_clamp(pet.happiness - 5))
    raise ValueError(f"unknown action {action}")


def mood(pet: Pet) -> str:
    if pet.stage == "egg":
        return "egg"
    if pet.sick:
        return "sick"
    if pet.asleep:
        return "asleep"
    if pet.fullness < 25:
        return "hungry"
    if pet.hygiene < 30 or pet.poops >= 2:
        return "dirty"
    if pet.energy < 20:
        return "sleepy"
    if pet.happiness < 30:
        return "sad"
    return "happy" if pet.happiness >= 70 else "ok"
