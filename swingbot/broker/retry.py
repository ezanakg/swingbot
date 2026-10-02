"""Error classification, exponential backoff with full jitter, and the adapter-level circuit breaker."""
from __future__ import annotations

import logging
import random
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, TypeVar

from swingbot.enums import ErrorClass

log = logging.getLogger(__name__)
T = TypeVar("T")


class BrokerError(Exception):
    error_class: ErrorClass = ErrorClass.CLIENT_ERROR

    def __init__(self, message: str, *, status: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class TransientError(BrokerError):
    error_class = ErrorClass.TRANSIENT


class AuthError(BrokerError):
    error_class = ErrorClass.AUTH


class ClientError(BrokerError):
    error_class = ErrorClass.CLIENT_ERROR


class BrokerSchemaDrift(BrokerError):
    error_class = ErrorClass.SCHEMA_DRIFT


class AmbiguousError(BrokerError):
    """A mutating request (order POST) timed out after being sent: the order may or may not exist."""

    error_class = ErrorClass.AMBIGUOUS


class BrokerUnavailable(BrokerError):
    error_class = ErrorClass.UNAVAILABLE


class MfaRequired(AuthError):
    """MFA could not be satisfied non-interactively (distinct exit code in the CLI)."""


def classify_http(status: int, body: str = "", *, mutating_sent: bool = False) -> BrokerError:
    """Map an HTTP status to a typed error."""
    snippet = (body or "")[:300]
    if status == 429:
        return TransientError(f"rate limited (429): {snippet}", status=status)
    if status >= 500:
        return TransientError(f"server error {status}: {snippet}", status=status)
    if status in (401,):
        return AuthError(f"unauthorized ({status}): {snippet}", status=status)
    if status == 403:
        return AuthError(f"forbidden (403): {snippet}", status=status)
    if status in (408,):
        return (AmbiguousError if mutating_sent else TransientError)(f"request timeout: {snippet}", status=status)
    return ClientError(f"client error {status}: {snippet}", status=status)


def classify_exception(exc: BaseException, *, mutating_sent: bool = False) -> BrokerError:
    """Wrap arbitrary exceptions raised by the HTTP layer into a typed BrokerError."""
    if isinstance(exc, BrokerError):
        return exc
    name = type(exc).__name__
    text = f"{name}: {exc}"
    if name in ("Timeout", "ReadTimeout", "ConnectTimeout"):
        return AmbiguousError(text) if mutating_sent else TransientError(text)
    if name in ("ConnectionError", "ChunkedEncodingError", "SSLError", "ProxyError", "RemoteDisconnected",
                "ProtocolError", "ConnectionResetError", "OSError"):
        return AmbiguousError(text) if mutating_sent else TransientError(text)
    if isinstance(exc, (KeyError, TypeError, ValueError, AttributeError)):
        return BrokerSchemaDrift(f"unexpected response shape: {text}")
    return ClientError(text)


@dataclass(frozen=True)
class RetryPolicy:
    base_sec: float = 1.0
    factor: float = 2.0
    max_attempts: int = 5
    cap_sec: float = 60.0
    jitter: bool = True


def backoff_delay(attempt: int, policy: RetryPolicy, rng: random.Random | None = None) -> float:
    """Full-jitter exponential backoff for the given zero-based attempt number."""
    upper = min(policy.cap_sec, policy.base_sec * (policy.factor ** attempt))
    if not policy.jitter:
        return upper
    return (rng or random).uniform(0.0, upper)


def with_retry(
    fn: Callable[[], T],
    policy: RetryPolicy,
    *,
    on_auth: Callable[[], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
    describe: str = "call",
) -> T:
    """Run ``fn`` with classified retries.

    * ``TransientError``: exponential backoff (``Retry-After`` honoured) up to ``max_attempts``.
    * ``AuthError``: call ``on_auth`` once, then retry once.
    * everything else propagates immediately (``ClientError``, ``BrokerSchemaDrift``, ``AmbiguousError``).
    """
    reauthed = False
    attempt = 0
    while True:
        try:
            return fn()
        except TransientError as exc:
            attempt += 1
            if attempt >= policy.max_attempts:
                log.error("%s: giving up after %d transient failures: %s", describe, attempt, exc)
                raise
            delay = exc.retry_after if exc.retry_after is not None else backoff_delay(attempt - 1, policy, rng)
            delay = min(delay, policy.cap_sec)
            log.warning("%s: transient failure (%s); retry %d/%d in %.1fs", describe, exc, attempt,
                        policy.max_attempts - 1, delay)
            sleep(delay)
        except AuthError as exc:
            if reauthed or on_auth is None:
                raise
            log.warning("%s: auth failure (%s); re-authenticating once", describe, exc)
            on_auth()
            reauthed = True


class AdapterCircuitBreaker:
    """Opens after ``failures`` classified failures within ``window_sec``; stays open ``open_sec``."""

    def __init__(self, failures: int = 10, window_sec: float = 300.0, open_sec: float = 600.0,
                 clock: Callable[[], float] = time.monotonic):
        self.failures = failures
        self.window_sec = window_sec
        self.open_sec = open_sec
        self._clock = clock
        self._events: deque[float] = deque()
        self._opened_at: float | None = None
        self.trip_count = 0

    def record_failure(self) -> None:
        now = self._clock()
        self._events.append(now)
        while self._events and now - self._events[0] > self.window_sec:
            self._events.popleft()
        if len(self._events) >= self.failures and self._opened_at is None:
            self._opened_at = now
            self.trip_count += 1
            log.error("broker circuit breaker OPEN: %d failures in %.0fs; failing fast for %.0fs",
                      len(self._events), self.window_sec, self.open_sec)

    def record_success(self) -> None:
        if self._opened_at is None:
            self._events.clear()

    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        if self._clock() - self._opened_at >= self.open_sec:
            log.warning("broker circuit breaker half-open: allowing calls again")
            self._opened_at = None
            self._events.clear()
            return False
        return True

    def check(self) -> None:
        if self.is_open():
            raise BrokerUnavailable("broker circuit breaker is open; failing fast")

    def seconds_remaining(self) -> float:
        if self._opened_at is None:
            return 0.0
        return max(0.0, self.open_sec - (self._clock() - self._opened_at))
