"""Macro Pad profiles and actions (pure logic, no device access).

Keys are browser KeyboardEvent.code names, which is what the NanoKVM MCP
`press_keys` tool expects, e.g. ["ControlLeft", "AltLeft", "Delete"].
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

MAX_LABEL = 14
MAX_TEXT = 500
MAX_REPEAT = 200
MIN_INTERVAL_MS = 50
MAX_INTERVAL_MS = 5000
DEFAULT_INTERVAL_MS = 250
MAX_HOLD_MS = 5000
MAX_KEYS = 8
MAX_MACROS = 60

KNOWN_CODES = frozenset(
    [f"Key{c}" for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"]
    + [f"Digit{d}" for d in range(10)]
    + [f"F{n}" for n in range(1, 25)]
    + [f"Numpad{d}" for d in range(10)]
    + [f"{m}{side}" for m in ("Control", "Shift", "Alt", "Meta") for side in ("Left", "Right")]
    + ["Enter", "Escape", "Backspace", "Tab", "Space", "Minus", "Equal", "BracketLeft",
       "BracketRight", "Backslash", "Semicolon", "Quote", "Backquote", "Comma", "Period",
       "Slash", "CapsLock", "PrintScreen", "ScrollLock", "Pause", "Insert", "Home", "PageUp",
       "Delete", "End", "PageDown", "ArrowRight", "ArrowLeft", "ArrowDown", "ArrowUp",
       "NumLock", "NumpadDivide", "NumpadMultiply", "NumpadSubtract", "NumpadAdd",
       "NumpadEnter", "NumpadDecimal", "ContextMenu", "IntlBackslash"]
)

SHORT_NAMES = {
    "ControlLeft": "Ctrl", "ControlRight": "Ctrl", "ShiftLeft": "Shft", "ShiftRight": "Shft",
    "AltLeft": "Alt", "AltRight": "Alt", "Escape": "Esc", "Delete": "Del", "Backspace": "Bksp",
    "ArrowUp": "Up", "ArrowDown": "Down", "ArrowLeft": "Left", "ArrowRight": "Right",
    "PageUp": "PgUp", "PageDown": "PgDn", "PrintScreen": "PrtSc", "Space": "Space",
}


class MacroError(ValueError):
    """A macro definition is invalid (shown to the user on screen)."""


@dataclass(frozen=True)
class Action:
    label: str
    keys: tuple[str, ...] = ()
    text: str = ""
    repeat: int = 1
    interval_ms: int = DEFAULT_INTERVAL_MS
    hold_ms: int | None = None

    @property
    def duration_s(self) -> float:
        return self.repeat * self.interval_ms / 1000


@dataclass(frozen=True)
class Profile:
    name: str
    meta: str  # how the Meta key is labelled on this platform
    actions: tuple[Action, ...]


def _combo(label, *keys):
    return {"label": label, "keys": list(keys)}


def _spam(label, *keys, seconds=15):
    """Tap the key(s) every 250ms during boot (BIOS, boot menus)."""
    return {"label": label, "keys": list(keys), "repeat": seconds * 1000 // DEFAULT_INTERVAL_MS}


def _hold(label, *keys, seconds=15):
    """Keep the key(s) down through boot. Sent as back-to-back 1s holds; if the
    device ignores holdMs this degrades to rapid taps (not hardware-verified)."""
    return {"label": label, "keys": list(keys), "hold_ms": 1000, "interval_ms": 1000, "repeat": seconds}


BUILTIN = (
    ("Windows", "Win", [
        _combo("Ctrl Alt Del", "ControlLeft", "AltLeft", "Delete"),
        _combo("Lock", "MetaLeft", "KeyL"),
        _combo("Task Mgr", "ControlLeft", "ShiftLeft", "Escape"),
        _combo("Run", "MetaLeft", "KeyR"),
        _combo("Desktop", "MetaLeft", "KeyD"),
        _combo("Switch", "AltLeft", "Tab"),
        _combo("Close", "AltLeft", "F4"),
        _combo("Files", "MetaLeft", "KeyE"),
        _combo("Config", "MetaLeft", "KeyI"),
        _combo("GPU Reset", "MetaLeft", "ControlLeft", "ShiftLeft", "KeyB"),
        _spam("BIOS (F2)", "F2"),
        _spam("BIOS (Del)", "Delete"),
        _spam("Boot Menu", "F12"),
    ]),
    ("macOS", "Cmd", [
        _combo("Search", "MetaLeft", "Space"),
        _combo("Lock", "ControlLeft", "MetaLeft", "KeyQ"),
        _combo("Force Quit", "MetaLeft", "AltLeft", "Escape"),
        _combo("Switch", "MetaLeft", "Tab"),
        _combo("Mission Ctl", "ControlLeft", "ArrowUp"),
        _combo("Quit App", "MetaLeft", "KeyQ"),
        _combo("Close Win", "MetaLeft", "KeyW"),
        _combo("Screen Shot", "MetaLeft", "ShiftLeft", "Digit5"),
        _hold("Boot Picker", "AltLeft"),
        _hold("Recover", "MetaLeft", "KeyR"),
    ]),
    ("Linux", "Super", [
        _combo("Shell", "ControlLeft", "AltLeft", "KeyT"),
        _combo("Lock", "MetaLeft", "KeyL"),
        _combo("Ctrl+C", "ControlLeft", "KeyC"),
        _combo("Switch", "AltLeft", "Tab"),
        _combo("Run", "AltLeft", "F2"),
        _combo("Close", "AltLeft", "F4"),
        _combo("TTY1", "ControlLeft", "AltLeft", "F1"),
        _combo("TTY2", "ControlLeft", "AltLeft", "F2"),
        _combo("TTY3", "ControlLeft", "AltLeft", "F3"),
        _spam("GRUB (Esc)", "Escape"),
        _hold("GRUB (Shift)", "ShiftLeft"),
    ]),
    ("Mobile", "Cmd", [
        _combo("iOS Home", "MetaLeft", "KeyH"),
        _combo("iOS Search", "MetaLeft", "Space"),
        _combo("iOS Switch", "MetaLeft", "Tab"),
        _combo("And. Home", "MetaLeft", "Enter"),
        _combo("And. Back", "Escape"),
        _combo("And. Recent", "AltLeft", "Tab"),
        _combo("And. Notifs", "MetaLeft", "KeyN"),
    ]),
    ("Nav", "Meta", [
        _combo("Up", "ArrowUp"), _combo("Enter", "Enter"), _combo("Esc", "Escape"),
        _combo("Left", "ArrowLeft"), _combo("Down", "ArrowDown"), _combo("Right", "ArrowRight"),
        _combo("Tab", "Tab"), _combo("Space", "Space"), _combo("Bksp", "Backspace"),
        _combo("PgUp", "PageUp"), _combo("PgDn", "PageDown"), _combo("F10 Save", "F10"),
    ]),
)


def _require_int(spec: dict, field: str, default: int, low: int, high: int) -> int:
    value = spec.get(field, default)
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise MacroError(f"'{spec.get('label')}': {field} must be an integer {low}..{high}")
    return value


def parse_action(spec: dict) -> Action:
    if not isinstance(spec, dict):
        raise MacroError("each macro must be an object")
    label = spec.get("label")
    if not isinstance(label, str) or not label.strip() or len(label) > MAX_LABEL:
        raise MacroError(f"label must be 1..{MAX_LABEL} characters: {label!r}")
    keys, text = spec.get("keys"), spec.get("text")
    if (keys is None) == (text is None):
        raise MacroError(f"'{label}': set exactly one of 'keys' or 'text'")
    if keys is not None:
        if not isinstance(keys, list) or not keys or not all(isinstance(k, str) for k in keys):
            raise MacroError(f"'{label}': keys must be a non-empty list of key codes")
        if len(keys) > MAX_KEYS:
            raise MacroError(f"'{label}': at most {MAX_KEYS} keys per combo")
        unknown = [k for k in keys if k not in KNOWN_CODES]
        if unknown:
            raise MacroError(f"'{label}': unknown key code(s) {', '.join(unknown)} "
                             "(use KeyboardEvent.code names like ControlLeft, KeyC)")
    if text is not None and (not isinstance(text, str) or not text or len(text) > MAX_TEXT):
        raise MacroError(f"'{label}': text must be 1..{MAX_TEXT} characters")
    if text is not None and any(f in spec for f in ("repeat", "interval_ms", "hold_ms")):
        raise MacroError(f"'{label}': repeat/interval_ms/hold_ms only apply to 'keys' macros")
    hold = spec.get("hold_ms")
    return Action(
        label=label.strip(),
        keys=tuple(keys or ()),
        text=text or "",
        repeat=_require_int(spec, "repeat", 1, 1, MAX_REPEAT),
        interval_ms=_require_int(spec, "interval_ms", DEFAULT_INTERVAL_MS, MIN_INTERVAL_MS, MAX_INTERVAL_MS),
        hold_ms=None if hold is None else _require_int(spec, "hold_ms", 0, 1, MAX_HOLD_MS),
    )


def _profile(name: str, meta: str, specs) -> Profile:
    if not isinstance(specs, list) or not specs:
        raise MacroError(f"profile '{name}' must be a non-empty list of macros")
    if len(specs) > MAX_MACROS:
        raise MacroError(f"profile '{name}' has more than {MAX_MACROS} macros")
    return Profile(name=name, meta=meta, actions=tuple(parse_action(s) for s in specs))


def load_profiles(custom_json: str | None) -> tuple[Profile, ...]:
    """Custom profiles (from the CUSTOM_MACROS setting) first, then the built-ins."""
    builtin = tuple(_profile(name, meta, specs) for name, meta, specs in BUILTIN)
    if not custom_json or not custom_json.strip():
        return builtin
    try:
        data = json.loads(custom_json)
    except json.JSONDecodeError as exc:
        raise MacroError(f"CUSTOM_MACROS is not valid JSON: {exc.msg} (line {exc.lineno})") from exc
    if isinstance(data, list):
        custom = (_profile("Custom", "Meta", data),)
    elif isinstance(data, dict):
        custom = tuple(_profile(_profile_name(name), "Meta", specs) for name, specs in data.items())
    else:
        raise MacroError("CUSTOM_MACROS must be a list of macros or an object of named lists")
    seen = set()
    for profile in custom + builtin:
        key = profile.name.lower()
        if key in seen:
            raise MacroError(f"duplicate profile name '{profile.name}' (names are case-insensitive)")
        seen.add(key)
    return custom + builtin


def _profile_name(name) -> str:
    cleaned = str(name).strip()
    if not cleaned or len(cleaned) > MAX_LABEL:
        raise MacroError(f"profile names must be 1..{MAX_LABEL} characters: {name!r}")
    return cleaned


def start_index(profiles: tuple[Profile, ...], preferred: str | None) -> int:
    wanted = (preferred or "").strip().lower()
    return next((i for i, p in enumerate(profiles) if p.name.lower() == wanted), 0)


def paginate(actions: tuple[Action, ...], per_page: int) -> list[tuple[Action, ...]]:
    return [actions[i:i + per_page] for i in range(0, len(actions), per_page)] or [()]


def describe(action: Action, meta: str = "Meta") -> str:
    """Short second-line hint shown on the button."""
    if action.text:
        return '"' + (action.text if len(action.text) <= 8 else action.text[:8] + "~")
    names = []
    for code in action.keys:
        if code.startswith("Meta"):
            names.append(meta)
        else:
            names.append(SHORT_NAMES.get(code) or code.removeprefix("Key").removeprefix("Digit"))
    combo = "+".join(names)
    if action.hold_ms:
        return f"hold {combo}"
    if action.repeat > 1:
        seconds = action.duration_s
        return f"{combo} {seconds:g}s" if seconds >= 2 else f"{combo} x{action.repeat}"
    return combo


def friendly_error(message: str, limit: int = 60) -> str:
    lowered = message.lower()
    if "hidg" in lowered and "timeout" in lowered:
        return "No target: keyboard not accepted"
    if "api key" in lowered or re.search(r"\bhttp 40[13]\b", lowered):
        return "MCP key rejected - fix it in Settings > Apps"
    return message if len(message) <= limit else message[: limit - 1] + "~"
