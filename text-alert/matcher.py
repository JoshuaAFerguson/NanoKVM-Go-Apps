"""Match watched phrases in OCR text: forgiving normalisation plus per-phrase cooldowns.

The Go+ OCR often drops spaces and mixes case ("BUILDSUCCEEDEDin"), so both
the phrase and the screen text are reduced to lowercase letters and digits.
"""

from __future__ import annotations

import re

MIN_NORMALIZED = 4      # shorter phrases would match almost any screen
MAX_PHRASES = 20
MAX_PHRASE = 80


class MatchError(ValueError):
    """WATCH_TEXTS is invalid (shown on screen)."""


def _folded(text: str):
    """(original index, folded char) for each letter/digit; per-character lower()
    so positions stay aligned even when lowercasing changes length (e.g. 'İ')."""
    for i, ch in enumerate(text):
        for c in ch.lower():
            if c.isalnum():
                yield i, c


def normalize(text: str) -> str:
    return "".join(c for _, c in _folded(text))


def parse_phrases(text: str | None) -> tuple[str, ...]:
    phrases = tuple(p.strip() for p in re.split(r"[;\n]", text or "") if p.strip())
    if len(phrases) > MAX_PHRASES:
        raise MatchError(f"WATCH_TEXTS: at most {MAX_PHRASES} phrases")
    for phrase in phrases:
        if len(phrase) > MAX_PHRASE:
            raise MatchError(f"WATCH_TEXTS: phrases can be at most {MAX_PHRASE} characters")
        if len(normalize(phrase)) < MIN_NORMALIZED:
            raise MatchError(f"WATCH_TEXTS: '{phrase}' is too short to match reliably "
                             f"(needs {MIN_NORMALIZED}+ letters/digits)")
    return phrases


def context(text: str, phrase: str, width: int = 80) -> str:
    """A snippet of `text` around the first loose match of `phrase`.

    Privacy: returns "" if the match can't be located, never unrelated screen text.
    """
    target = normalize(phrase)
    pairs = list(_folded(text))
    flat = "".join(c for _, c in pairs)
    pos = flat.find(target) if target else -1
    if pos < 0:
        return ""
    first, last = pairs[pos][0], pairs[pos + len(target) - 1][0] + 1
    span = last - first
    window = max(width, span + 20)
    start = max(0, first - (window - span) // 3)
    snippet = " ".join(text[start:start + window].split())
    return ("..." if start else "") + snippet


class Matcher:
    def __init__(self, phrases: tuple[str, ...], cooldown_s: float) -> None:
        self.phrases = phrases
        self.cooldown_s = cooldown_s
        self._normalized = {p: normalize(p) for p in phrases}
        self._last_alert: dict[str, float] = {}

    def check(self, text: str, now: float) -> list[str]:
        flat = normalize(text)
        hits = []
        for phrase, needle in self._normalized.items():
            if needle in flat and now - self._last_alert.get(phrase, -1e18) >= self.cooldown_s:
                self._last_alert[phrase] = now
                hits.append(phrase)
        return hits
