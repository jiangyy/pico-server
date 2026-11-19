from __future__ import annotations

import logging
import time
from collections.abc import Callable
from threading import Event, Thread
from typing import Protocol


logger = logging.getLogger("picopg.server.worker")


class LogConsumer(Protocol):
    def run_once(self) -> object: ...


class ServerWorkerThread:
    """Wakes feature consumers after appends and once on startup."""

    def __init__(self, consumers: tuple[LogConsumer, ...]) -> None:
        self._consumers = consumers
        self._wake = Event()
        self._stop = Event()
        self._thread = Thread(target=self._run, name="picopg-log-worker", daemon=True)

    def start(self) -> None:
        self._thread.start()
        self._wake.set()

    def notify_append(self, _log_id: str) -> None:
        self._wake.set()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread.is_alive():
            self._thread.join()

    def _run(self) -> None:
        delay: float | None = None
        while not self._stop.is_set():
            self._wake.wait(delay)
            self._wake.clear()
            if self._stop.is_set():
                return
            failed = False
            for consumer in self._consumers:
                try:
                    consumer.run_once()
                except Exception:
                    logger.exception("request Log consumer failed")
                    failed = True
            delays = [
                next_delay
                for consumer in self._consumers
                if callable(next_wake_delay := getattr(consumer, "next_wake_delay", None))
                if (next_delay := next_wake_delay()) is not None
            ]
            delay = min(delays) if delays else None
            if failed:
                delay = min(1.0, delay) if delay is not None else 1.0


class Retry:
    """按 Log 隔离失败：一个 Log 出了基础设施错误（磁盘、git、超时）只退避它自己，别的 Log 照常处理。
    同一个错误只记一次日志，退避从 1 秒翻倍到 60 秒。"""

    def __init__(self, clock: Callable[[], float] = time.monotonic, first: float = 1.0, longest: float = 60.0) -> None:
        self._clock = clock
        self._first = first
        self._longest = longest
        self._waiting: dict[str, tuple[float, float, str]] = {}

    def ready(self, key: str) -> bool:
        entry = self._waiting.get(key)
        return entry is None or entry[0] <= self._clock()

    def failed(self, key: str, error: BaseException) -> None:
        previous = self._waiting.get(key)
        delay = self._first if previous is None else min(previous[1] * 2, self._longest)
        message = f"{type(error).__name__}: {error}"
        if previous is None or previous[2] != message:
            logger.error("%s failed; retrying in %.0fs", key, delay, exc_info=error)
        self._waiting[key] = (self._clock() + delay, delay, message)

    def succeeded(self, key: str) -> None:
        if self._waiting.pop(key, None) is not None:
            logger.info("%s recovered", key)

    def next_delay(self) -> float | None:
        if not self._waiting:
            return None
        return max(0.0, min(at for at, _, _ in self._waiting.values()) - self._clock())
