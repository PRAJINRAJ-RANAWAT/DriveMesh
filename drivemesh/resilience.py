"""Retries with full-jitter exponential backoff, per-account token buckets and concurrency caps.

Only TransientError is retried. Everything else (NotFound, Auth, QuotaFull, Permanent)
surfaces immediately so callers can react (mark replica missing, account revoked/full...).
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import TypeVar

from drivemesh import metrics
from drivemesh.backends.base import (
    BackendError,
    HealthInfo,
    ObjectInfo,
    PermanentError,
    PutResult,
    StorageBackend,
    TransientError,
)

log = logging.getLogger(__name__)
T = TypeVar("T")


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 6
    base: float = 0.5
    cap: float = 32.0

    def backoff(self, attempt: int, rng: random.Random | None = None) -> float:
        """Full jitter: uniform(0, min(cap, base * 2**attempt)). attempt starts at 0."""
        upper = min(self.cap, self.base * (2**attempt))
        return (rng or random).uniform(0, upper)


def call_with_retry(
    fn: Callable[[], T],
    policy: RetryPolicy,
    *,
    sleep: Callable[[float], None] = time.sleep,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
    rng: random.Random | None = None,
) -> T:
    attempt = 0
    while True:
        try:
            return fn()
        except BackendError as exc:
            if not exc.retryable or attempt + 1 >= policy.max_attempts:
                raise
            delay = policy.backoff(attempt, rng)
            if on_retry:
                on_retry(attempt, exc, delay)
            sleep(delay)
            attempt += 1
        except (TimeoutError, ConnectionError) as exc:  # stdlib network errors are transient
            if attempt + 1 >= policy.max_attempts:
                raise TransientError(str(exc)) from exc
            delay = policy.backoff(attempt, rng)
            if on_retry:
                on_retry(attempt, exc, delay)
            sleep(delay)
            attempt += 1


class TokenBucket:
    """Classic token bucket: `rate` tokens/second, up to `burst` stored."""

    def __init__(
        self,
        rate: float,
        burst: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.rate = float(rate)
        self.burst = float(burst)
        self.tokens = float(burst)
        self._clock = clock
        self._sleep = sleep
        self._last = clock()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = self._clock()
        self.tokens = min(self.burst, self.tokens + (now - self._last) * self.rate)
        self._last = now

    def try_acquire(self) -> bool:
        with self._lock:
            self._refill()
            if self.tokens >= 1 - 1e-9:
                self.tokens -= 1
                return True
            return False

    def acquire(self) -> None:
        while True:
            with self._lock:
                self._refill()
                if self.tokens >= 1 - 1e-9:
                    self.tokens -= 1
                    return
                wait = max((1 - self.tokens) / self.rate, 1e-6)
            self._sleep(wait)


class ResilientBackend:
    """Applies concurrency cap + rate limit + retry + metrics to every call of a backend."""

    def __init__(
        self,
        inner: StorageBackend,
        *,
        account_id: str,
        policy: RetryPolicy,
        concurrency: int = 4,
        rate: float = 8.0,
        burst: int = 16,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.inner = inner
        self.account_id = account_id
        self.provider = inner.provider
        self.policy = policy
        self._sem = threading.BoundedSemaphore(concurrency)
        self._bucket = TokenBucket(rate, burst, sleep=sleep)
        self._sleep = sleep
        self.latency_ewma: float | None = None
        self.pending = 0  # calls queued or running: the load signal for placement/reads
        self._pending_lock = threading.Lock()

    def _call(self, op: str, fn: Callable[[], T]) -> T:
        def attempt() -> T:
            self._bucket.acquire()
            with self._sem:
                return fn()

        def on_retry(n: int, exc: BaseException, delay: float) -> None:
            metrics.BACKEND_RETRIES.labels(self.provider, type(exc).__name__).inc()
            log.info(
                "backend retry",
                extra={
                    "account_id": self.account_id,
                    "op": op,
                    "attempt": n + 1,
                    "delay": round(delay, 3),
                    "error": str(exc)[:200],
                },
            )

        t0 = time.perf_counter()
        with self._pending_lock:
            self.pending += 1
        try:
            result = call_with_retry(attempt, self.policy, sleep=self._sleep, on_retry=on_retry)
        except BackendError as exc:
            metrics.BACKEND_ERRORS.labels(self.provider, type(exc).__name__).inc()
            raise
        except Exception as exc:  # unknown SDK errors become PermanentError, never swallowed
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            metrics.BACKEND_ERRORS.labels(self.provider, "unexpected").inc()
            raise PermanentError(f"{op}: {type(exc).__name__}: {exc}") from exc
        finally:
            with self._pending_lock:
                self.pending -= 1
        elapsed = time.perf_counter() - t0
        metrics.BACKEND_OP_SECONDS.labels(op, self.provider).observe(elapsed)
        self.latency_ewma = (
            elapsed if self.latency_ewma is None else 0.8 * self.latency_ewma + 0.2 * elapsed
        )
        return result

    def put(self, name: str, data: bytes) -> PutResult:
        res = self._call("put", lambda: self.inner.put(name, data))
        metrics.BYTES_UPLOADED.labels(self.provider).inc(len(data))
        return res

    def get(self, remote_id: str) -> bytes:
        data = self._call("get", lambda: self.inner.get(remote_id))
        metrics.BYTES_DOWNLOADED.labels(self.provider).inc(len(data))
        return data

    def delete(self, remote_id: str) -> None:
        self._call("delete", lambda: self.inner.delete(remote_id))

    def find(self, name: str) -> str | None:
        return self._call("find", lambda: self.inner.find(name))

    def list(self, prefix: str = "dm-") -> Iterator[ObjectInfo]:
        return iter(self._call("list", lambda: list(self.inner.list(prefix))))

    def health(self) -> HealthInfo:
        return self._call("health", self.inner.health)
