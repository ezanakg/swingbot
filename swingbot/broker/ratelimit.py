"""Token-bucket rate limiter shared by all broker and data calls."""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

log = logging.getLogger(__name__)


class TokenBucket:
    def __init__(
        self,
        rate_per_sec: float = 1.5,
        capacity: int = 10,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if rate_per_sec <= 0 or capacity <= 0:
            raise ValueError("rate and capacity must be positive")
        self.rate = float(rate_per_sec)
        self.capacity = float(capacity)
        self._tokens = float(capacity)
        self._clock = clock
        self._sleep = sleep
        self._last = clock()
        self._blocked_until: float = 0.0
        self._lock = threading.Lock()
        self.total_waited = 0.0

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
        self._last = now

    def acquire(self, cost: float = 1.0) -> float:
        """Block until ``cost`` tokens are available; return seconds waited."""
        cost = min(float(cost), self.capacity)
        waited = 0.0
        while True:
            with self._lock:
                now = self._clock()
                if now < self._blocked_until:
                    delay = self._blocked_until - now
                else:
                    self._refill()
                    if self._tokens >= cost:
                        self._tokens -= cost
                        self.total_waited += waited
                        return waited
                    delay = (cost - self._tokens) / self.rate
            self._sleep(delay)
            waited += delay

    def penalize(self, seconds: float) -> None:
        """Honour a ``Retry-After`` header: block all callers for ``seconds``."""
        with self._lock:
            self._blocked_until = max(self._blocked_until, self._clock() + max(0.0, seconds))
            log.warning("rate limiter penalised for %.1fs (Retry-After)", seconds)

    @property
    def tokens(self) -> float:
        with self._lock:
            self._refill()
            return self._tokens
