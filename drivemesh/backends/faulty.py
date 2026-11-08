"""Fault-injection wrapper for chaos tests and simulated-network benchmarks."""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable, Iterator

from drivemesh.backends.base import (
    AuthError,
    HealthInfo,
    ObjectInfo,
    PutResult,
    StorageBackend,
    TransientError,
)


class FaultyBackend:
    """Wraps a backend and injects failures.

    * dead: every call raises (TransientError by default, AuthError if dead_auth)
    * error_rate: probability that a call raises TransientError
    * fail_next: number of upcoming calls that raise TransientError
    * latency: fixed seconds added to every call (simulates an HTTPS round trip)
    * bandwidth_bps: bytes/second transfer cap per call (simulates a slow link)
    * corrupt_reads: flip a byte in every object returned by get()
    * crash_after_puts: raise `crash_exc` after N successful puts (simulates process death)
    """

    def __init__(
        self,
        inner: StorageBackend,
        *,
        latency: float = 0.0,
        bandwidth_bps: float | None = None,
        error_rate: float = 0.0,
        seed: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.inner = inner
        self.provider = inner.provider
        self.latency = latency
        self.bandwidth_bps = bandwidth_bps
        self.error_rate = error_rate
        self.dead = False
        self.dead_auth = False
        self.fail_next = 0
        self.corrupt_reads = False
        self.crash_after_puts: int | None = None
        self.crash_exc: type[BaseException] = KeyboardInterrupt
        self.calls: dict[str, int] = {}
        self._rng = random.Random(seed)
        self._sleep = sleep
        self._lock = threading.Lock()

    def _enter(self, op: str, nbytes: int = 0) -> None:
        with self._lock:
            self.calls[op] = self.calls.get(op, 0) + 1
            if self.dead:
                raise (AuthError if self.dead_auth else TransientError)(f"{op}: backend dead")
            if self.fail_next > 0:
                self.fail_next -= 1
                raise TransientError(f"{op}: injected failure")
            fail = self.error_rate and self._rng.random() < self.error_rate
        if fail:
            raise TransientError(f"{op}: injected random failure")
        delay = self.latency
        if self.bandwidth_bps and nbytes:
            delay += nbytes / self.bandwidth_bps
        if delay:
            self._sleep(delay)

    def put(self, name: str, data: bytes) -> PutResult:
        self._enter("put", len(data))
        res = self.inner.put(name, data)
        with self._lock:
            if self.crash_after_puts is not None:
                self.crash_after_puts -= 1
                if self.crash_after_puts < 0:
                    raise self.crash_exc("simulated crash")
        return res

    def get(self, remote_id: str) -> bytes:
        self._enter("get")
        data = self.inner.get(remote_id)
        if self.bandwidth_bps:
            self._sleep(len(data) / self.bandwidth_bps)
        if self.corrupt_reads and data:
            b = bytearray(data)
            b[len(b) // 2] ^= 0x01
            return bytes(b)
        return data

    def delete(self, remote_id: str) -> None:
        self._enter("delete")
        self.inner.delete(remote_id)

    def find(self, name: str) -> str | None:
        self._enter("find")
        return self.inner.find(name)

    def list(self, prefix: str = "dm-") -> Iterator[ObjectInfo]:
        self._enter("list")
        return self.inner.list(prefix)

    def health(self) -> HealthInfo:
        self._enter("health")
        return self.inner.health()
