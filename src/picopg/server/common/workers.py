from __future__ import annotations

import logging
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
