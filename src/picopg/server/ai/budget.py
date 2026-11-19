"""Per-principal request limit and a server-wide daily token cap for the model."""

from __future__ import annotations

import threading
import time
from collections import deque
from contextvars import ContextVar
from datetime import datetime, timezone


#: 这次请求是谁在用模型：HTTP 层认出身份后设上，模型的每一次调用都按它记账。
current_principal: ContextVar[str | None] = ContextVar("picopg_ai_principal", default=None)


class AiLimited(Exception):
    """超出了这个人每小时的调用数，或全站当天的 token 上限。不是模型失败：不换下一个模型重试。"""


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
            # 一小时没用过的人不留条目，表的大小随在用的人数，而不是随所有用过的人。
            for idle in [key for key, calls in self.requests.items() if key != principal_id and (not calls or now - calls[-1] >= 3600)]:
                del self.requests[idle]
            if len(recent) >= self.per_hour:
                return False
            recent.append(now)
            return True

    def charge(self) -> None:
        """一次模型调用之前：按当前请求的身份放行，不放行就抛 AiLimited。没有身份的（离线评测）记在同一个桶里。"""
        if not self.admit(current_principal.get() or "-"):
            raise AiLimited("AI request limit reached; try again later")

    def spend(self, tokens: int) -> None:
        with self.lock:
            self._roll(self.clock())
            self.tokens += max(tokens, 0)

    def _roll(self, now: float) -> None:
        day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        if day != self.day:
            self.day, self.tokens = day, 0
