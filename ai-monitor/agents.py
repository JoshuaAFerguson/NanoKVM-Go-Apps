"""Coding-agent sessions reported by hooks (e.g. Claude Code) on your machines."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

STATES = ("working", "waiting", "idle", "done")
NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,40}$")
MAX_SESSIONS = 40
DONE_TTL_S = 600          # finished sessions linger for 10 min
STALE_S = 2 * 3600        # no news for 2 h: assume the session is gone
MAX_TEXT = 160


class AgentError(ValueError):
    """A check-in was malformed (reported to the caller as HTTP 400)."""


@dataclass(frozen=True)
class AgentSession:
    machine: str
    session: str
    state: str
    project: str = ""
    message: str = ""
    since: float = 0.0        # when the current state began
    updated: float = 0.0


@dataclass(frozen=True)
class Note:
    kind: str                 # waiting | done
    session: AgentSession


def _text(value, field: str, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > limit * 4:
        raise AgentError(f"{field} must be a short string")
    cleaned = "".join(ch if ch.isprintable() else " " for ch in value)
    return " ".join(cleaned.split())[:limit]


def validate(data) -> AgentSession:
    if not isinstance(data, dict):
        raise AgentError("body must be a JSON object")
    machine, session = data.get("machine"), data.get("session")
    if not isinstance(machine, str) or not NAME_RE.match(machine):
        raise AgentError("machine: 1-40 letters, digits, . _ -")
    if not isinstance(session, str) or not 1 <= len(session) <= 64 or not session.isprintable():
        raise AgentError("session: 1-64 printable characters")
    state = data.get("state")
    if state not in STATES:
        raise AgentError(f"state must be one of {', '.join(STATES)}")
    if len(str(data.get("message") or "")) > MAX_TEXT * 4:
        raise AgentError("message too long")
    return AgentSession(machine, session[:64], state, _text(data.get("project"), "project", 40),
                        _text(data.get("message"), "message", MAX_TEXT))


class Registry:
    def __init__(self) -> None:
        self._sessions: dict[tuple[str, str], AgentSession] = {}

    def update(self, incoming: AgentSession, now: float) -> list[Note]:
        key = (incoming.machine, incoming.session)
        previous = self._sessions.get(key)
        changed = previous is None or previous.state != incoming.state
        session = replace(incoming, since=now if changed else previous.since, updated=now,
                          project=incoming.project or (previous.project if previous else ""))
        self._sessions[key] = session
        if len(self._sessions) > MAX_SESSIONS:
            oldest = min(self._sessions, key=lambda k: self._sessions[k].updated)
            del self._sessions[oldest]
        if changed and incoming.state in ("waiting", "done"):
            return [Note(incoming.state, session)]
        return []

    def _expire(self, now: float) -> None:
        for key, s in list(self._sessions.items()):
            if now - s.updated > STALE_S or (s.state == "done" and now - s.updated > DONE_TTL_S):
                del self._sessions[key]

    def sessions(self, now: float) -> list[AgentSession]:
        self._expire(now)
        order = {state: i for i, state in enumerate(("waiting", "working", "idle", "done"))}
        return sorted(self._sessions.values(), key=lambda s: (order[s.state], -s.updated))

    def summary(self, now: float) -> dict:
        counts = {state: 0 for state in STATES}
        for s in self.sessions(now):
            counts[s.state] += 1
        return counts
