"""Failed sign-ins per client address: slows down guessing access codes without locking out a class that shares one address."""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable

#: 10 分钟里失败 30 次就先拦下：一个班共用学校的出口地址，偶尔输错不会被锁；6 位数字的访问码要试上半年以上。
FAILURES = 30
WINDOW_SECONDS = 600


class LoginLimiter:
    def __init__(self, failures: int = FAILURES, window: float = WINDOW_SECONDS, clock: Callable[[], float] = time.monotonic) -> None:
        self._failures = failures
        self._window = window
        self._clock = clock
        self._seen: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def blocked(self, address: str) -> bool:
        with self._lock:
            return self._count(address) >= self._failures

    def failed(self, address: str) -> None:
        with self._lock:
            self._count(address)
            self._seen.setdefault(address, deque()).append(self._clock())

    def _count(self, address: str) -> int:
        """窗口外的失败丢掉；一个地址没有近期失败就不留条目，地址再多也不会一直占内存。"""
        recent = self._seen.get(address)
        if recent is None:
            return 0
        now = self._clock()
        while recent and now - recent[0] >= self._window:
            recent.popleft()
        if not recent:
            del self._seen[address]
        return len(recent)


def client_address(handler) -> str:
    """直连时是对端地址；在本机的 nginx 后面时取它转来的 X-Real-IP（只信来自本机的这个头）。"""
    peer = handler.client_address[0]
    forwarded = handler.headers.get("X-Real-IP")
    return forwarded.strip() if forwarded and peer in ("127.0.0.1", "::1") else peer
