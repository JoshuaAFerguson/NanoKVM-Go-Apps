"""Runs one macro at a time on a worker thread so the UI never blocks on HTTP."""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from typing import Callable

from macros import Action, friendly_error

TEXT_CHUNK = 40          # type long text in pieces so Cancel works between them
TEXT_TIMEOUT_S = 30      # per chunk; typing is paced by the device


@dataclass(frozen=True)
class RunStatus:
    busy: bool = False
    index: int | None = None  # button index of the running/last action
    ok: bool = True
    message: str = "Ready"
    progress: str = ""
    version: int = 0  # bumps on every change so the UI knows to redraw


class Runner:
    def __init__(self, client, sleep: Callable[[float], None] | None = None,
                 on_result: Callable[[str], None] | None = None) -> None:
        self._client = client
        self._cancel = threading.Event()
        # Default wait returns early on cancel; tests inject a no-op sleep.
        self._sleep = sleep or self._cancel.wait
        self._on_result = on_result
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._status = RunStatus()

    def status(self) -> RunStatus:
        with self._lock:
            return self._status

    def start(self, action: Action, index: int) -> bool:
        with self._lock:
            if self._status.busy:
                return False
            self._cancel.clear()
            self._update_locked(busy=True, index=index, ok=True, message=f"Sending {action.label}", progress="")
            self._thread = threading.Thread(target=self._run, args=(action,), daemon=True)
            self._thread.start()
            return True

    def cancel(self) -> None:
        self._cancel.set()

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _update_locked(self, **changes) -> None:
        self._status = replace(self._status, version=self._status.version + 1, **changes)

    def _update(self, **changes) -> None:
        with self._lock:
            self._update_locked(**changes)

    def _run(self, action: Action) -> None:
        try:
            done = self._type(action) if action.text else self._press(action)
            self._finish(True, f"{'Sent' if done else 'Cancelled'} {action.label}")
        except Exception as exc:  # anything: the UI must never stay stuck on "busy"
            self._finish(False, friendly_error(str(exc)),
                         detail=f"{action.label} failed: {type(exc).__name__}: {exc}")

    def _press(self, action: Action) -> bool:
        for n in range(action.repeat):
            if self._cancel.is_set():
                return False
            self._client.press_keys(list(action.keys), hold_ms=action.hold_ms)
            if action.repeat > 1:
                self._update(progress=f"{n + 1}/{action.repeat}")
                if n + 1 < action.repeat:
                    self._sleep(action.interval_ms / 1000)
        return not self._cancel.is_set()

    def _type(self, action: Action) -> bool:
        chunks = [action.text[i:i + TEXT_CHUNK] for i in range(0, len(action.text), TEXT_CHUNK)]
        for n, chunk in enumerate(chunks):
            if self._cancel.is_set():
                return False
            self._client.type_text(chunk, timeout=TEXT_TIMEOUT_S)
            if len(chunks) > 1:
                self._update(progress=f"{n + 1}/{len(chunks)}")
        return True

    def _finish(self, ok: bool, message: str, detail: str = "") -> None:
        self._update(busy=False, ok=ok, message=message, progress="")
        if self._on_result is not None:
            self._on_result(detail or message)
