"""Per-principal request limit and a server-wide daily token cap for the model."""

from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime, timezone


class AiBudget:
    def __init__(self, per_hour: int = 120, daily_tokens: int | None = None, clock=time.time) -> None:
        if per_hour < 1 or (daily_tokens is not None and daily_tokens < 1):
            raise ValueError("AI budget limits must be positive")
        self.per_hour = per_hour
        self.daily_tokens = daily_tokens
        self.clock = clock
        self.requests: dict[str, deque[float]] = {}
        self.day = ""
        self.tokens = 0
        self.lock = threading.Lock()

    def admit(self, principal_id: str) -> bool:
        """一次模型请求（一问一答或 agent 的一步）；超出时不发给模型。"""
        now = self.clock()
        with self.lock:
            self._roll(now)
            if self.daily_tokens is not None and self.tokens >= self.daily_tokens:
                return False
            recent = self.requests.setdefault(principal_id, deque())
            while recent and now - recent[0] >= 3600:
                recent.popleft()
            if len(recent) >= self.per_hour:
                return False
            recent.append(now)
            return True

    def spend(self, tokens: int) -> None:
        with self.lock:
            self._roll(self.clock())
            self.tokens += max(tokens, 0)

    def _roll(self, now: float) -> None:
        day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        if day != self.day:
            self.day, self.tokens = day, 0
