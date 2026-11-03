"""Persist login attempts BEFORE submitting credentials, including across restarts."""
from __future__ import annotations

import json
import math
from pathlib import Path
import time

from .config import write_private_json
from .portal import PortalError


class AuthGuard:
    MIN_INTERVAL = 300
    WINDOW = 3600
    MAX_ATTEMPTS = 3

    def __init__(self, path: Path | None = None, *, clock=time.time):
        self.path = path
        self.clock = clock
        self.data = {"attempts": [], "blocked": False, "updated_at": 0}

    def _load(self):
        try:
            if self.path and self.path.exists():
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            data = self.data
            if not isinstance(data, dict) or set(data) != {"attempts", "blocked", "updated_at"}:
                raise ValueError
            if not isinstance(data["blocked"], bool) or not isinstance(data["attempts"], list):
                raise ValueError
            values = [data["updated_at"], *data["attempts"]]
            if (not all(type(t) in (int, float) and math.isfinite(t) and t >= 0 for t in values)
                    or data["attempts"] != sorted(data["attempts"])
                    or not all(t <= data["updated_at"] for t in data["attempts"])):
                raise ValueError
        except (ValueError, OSError, TypeError):
            raise PortalError("Auth guard is unreadable/invalid; refusing login. Inspect auth_guard.json; do not delete it to force retries.") from None

    def _save(self):
        if self.path:
            try:
                write_private_json(self.path, self.data, durable=True)
            except OSError:
                raise PortalError("Cannot persist auth guard; refusing further login attempts.") from None

    def _status(self, now) -> tuple[str, float]:
        if now < self.data["updated_at"]:
            return "auth_clock_error", 0
        if self.data["blocked"]:
            return "auth_blocked", 0
        attempts = [t for t in self.data["attempts"] if now - t < self.WINDOW]
        until = attempts[-1] + self.MIN_INTERVAL if attempts else now
        if len(attempts) >= self.MAX_ATTEMPTS:
            until = max(until, attempts[-self.MAX_ATTEMPTS] + self.WINDOW)
        if until > now:
            return "auth_cooldown", until - now
        return "", 0

    def inspect(self) -> tuple[str, float]:
        """Read the gate without reserving an attempt or changing any file."""
        self._load()
        return self._status(self.clock())

    def reserve(self) -> tuple[str, float]:
        self._load()
        now = self.clock()
        state, retry_after = self._status(now)
        if state:
            return state, retry_after
        attempts = [t for t in self.data["attempts"] if now - t < self.WINDOW]
        self.data.update(attempts=[*attempts, now], updated_at=now)
        self._save()
        return "", 0

    def block(self):
        self.data["blocked"] = True
        self._save()

    def reset_block(self):
        # Clearing a corrected password/captcha incident never clears rate limits.
        self._load()
        self.data["blocked"] = False
        self._save()
