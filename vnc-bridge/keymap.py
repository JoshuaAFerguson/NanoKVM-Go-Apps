"""X11 keysyms (what VNC clients send) -> USB HID boot-keyboard reports (US layout).

The KVM's /dev/hidg0 is a boot keyboard: 8-byte reports of
[modifier bits, reserved, key1..key6].
"""

from __future__ import annotations

MAX_KEYS = 6
SHIFT_BIT = 0x02

_UNSHIFTED = {"\n": 0x28, " ": 0x2C, "-": 0x2D, "=": 0x2E, "[": 0x2F, "]": 0x30, "\\": 0x31,
              ";": 0x33, "'": 0x34, "`": 0x35, ",": 0x36, ".": 0x37, "/": 0x38}
_SHIFTED = {"!": 0x1E, "@": 0x1F, "#": 0x20, "$": 0x21, "%": 0x22, "^": 0x23, "&": 0x24, "*": 0x25,
            "(": 0x26, ")": 0x27, "_": 0x2D, "+": 0x2E, "{": 0x2F, "}": 0x30, "|": 0x31, ":": 0x33,
            '"': 0x34, "~": 0x35, "<": 0x36, ">": 0x37, "?": 0x38}

_SPECIAL = {
    0xFF0D: 0x28, 0xFF1B: 0x29, 0xFF08: 0x2A, 0xFF09: 0x2B, 0xFFE5: 0x39,      # Return Esc BS Tab Caps
    0xFF61: 0x46, 0xFF14: 0x47, 0xFF13: 0x48, 0xFF63: 0x49, 0xFF50: 0x4A,      # Print Scroll Pause Ins Home
    0xFF55: 0x4B, 0xFFFF: 0x4C, 0xFF57: 0x4D, 0xFF56: 0x4E, 0xFF53: 0x4F,      # PgUp Del End PgDn Right
    0xFF51: 0x50, 0xFF54: 0x51, 0xFF52: 0x52, 0xFF7F: 0x53, 0xFFAF: 0x54,      # Left Down Up NumLock KP/
    0xFFAA: 0x55, 0xFFAD: 0x56, 0xFFAB: 0x57, 0xFF8D: 0x58, 0xFFB0: 0x62,      # KP* KP- KP+ KPEnter KP0
    0xFFAE: 0x63, 0xFF67: 0x65,                                                # KP. Menu
}
_SPECIAL.update({0xFFBE + i: 0x3A + i for i in range(12)})   # F1-F12
_SPECIAL.update({0xFFCA + i: 0x68 + i for i in range(12)})   # F13-F24
_SPECIAL.update({0xFFB1 + i: 0x59 + i for i in range(9)})    # KP_1-KP_9

_MODIFIERS = {
    0xFFE3: 0x01, 0xFFE1: 0x02, 0xFFE9: 0x04, 0xFFEB: 0x08, 0xFFE7: 0x08,      # Ctrl Shift Alt Super Meta (L)
    0xFFE4: 0x10, 0xFFE2: 0x20, 0xFFEA: 0x40, 0xFE03: 0x40, 0xFFEC: 0x80, 0xFFE8: 0x80,  # (R), AltGr
}
ISO_LEFT_TAB = 0xFE20


def modifier_bit(keysym: int) -> int | None:
    return _MODIFIERS.get(keysym)


def lookup(keysym: int) -> tuple[int, bool] | None:
    """(HID usage, needs shift) for a non-modifier keysym, or None if unmapped."""
    if keysym in _SPECIAL:
        return _SPECIAL[keysym], False
    if keysym == ISO_LEFT_TAB:
        return 0x2B, True
    if 0x20 <= keysym <= 0x7E:
        ch = chr(keysym)
        if "a" <= ch <= "z":
            return 0x04 + ord(ch) - ord("a"), False
        if "A" <= ch <= "Z":
            return 0x04 + ord(ch) - ord("A"), True
        if "1" <= ch <= "9":
            return 0x1E + ord(ch) - ord("1"), False
        if ch == "0":
            return 0x27, False
        if ch in _UNSHIFTED:
            return _UNSHIFTED[ch], False
        if ch in _SHIFTED:
            return _SHIFTED[ch], True
    return None


class Keyboard:
    """Tracks held keys and builds boot-keyboard reports from VNC key events.

    Several VNC clients share one physical keyboard, so each held key and
    modifier remembers which clients (owners) hold it: it is released only when
    the last of them lets go.
    """

    def __init__(self) -> None:
        self._mods: dict[int, set] = {}             # modifier bit -> owners
        self._keys: dict[int, dict] = {}  # usage -> {owner: needs_shift}, in press order

    @property
    def modifiers(self) -> int:
        bits = 0
        for bit, owners in self._mods.items():
            if owners:
                bits |= bit
        return bits

    @property
    def pressed(self) -> list[tuple[int, bool]]:
        return [(usage, any(owners.values())) for usage, owners in self._keys.items()]

    def _report(self) -> bytes:
        mods = self.modifiers
        if any(shift for _, shift in self.pressed) and not mods & (SHIFT_BIT | 0x20):
            mods |= SHIFT_BIT
        keys = list(self._keys)[:MAX_KEYS]
        return bytes([mods, 0] + keys + [0] * (MAX_KEYS - len(keys)))

    def event(self, keysym: int, down: bool, owner=None) -> bytes | None:
        """Report to write for this event, or None if the keysym isn't mapped."""
        bit = modifier_bit(keysym)
        if bit is not None:
            owners = self._mods.get(bit, set())
            self._mods[bit] = owners | {owner} if down else owners - {owner}
            return self._report()
        mapped = lookup(keysym)
        if mapped is None:
            return None
        usage, needs_shift = mapped  # release matches by usage, not case
        owners = dict(self._keys.pop(usage, {}))
        if down:
            owners[owner] = needs_shift
        else:
            owners.pop(owner, None)
        if owners:
            self._keys[usage] = owners  # (re)press moves it to the end
        return self._report()

    def release_all(self) -> bytes:
        self._mods, self._keys = {}, {}
        return bytes(8)
