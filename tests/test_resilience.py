import random

import pytest

from drivemesh.backends.base import NotFoundError, PermanentError, TransientError
from drivemesh.backends.local import MemoryBackend
from drivemesh.resilience import ResilientBackend, RetryPolicy, TokenBucket, call_with_retry


def test_full_jitter_bounds():
    p = RetryPolicy(max_attempts=10, base=0.5, cap=32)
    rng = random.Random(0)
    for attempt in range(10):
        for _ in range(50):
            d = p.backoff(attempt, rng)
            assert 0 <= d <= min(32, 0.5 * 2**attempt)


def test_retries_transient_then_succeeds():
    calls, sleeps = [], []

    def fn():
        calls.append(1)
        if len(calls) < 3:
            raise TransientError("503")
        return "ok"

    assert call_with_retry(fn, RetryPolicy(5, 1, 8), sleep=sleeps.append) == "ok"
    assert len(calls) == 3 and len(sleeps) == 2


def test_gives_up_after_max_attempts():
    calls = []

    def fn():
        calls.append(1)
        raise TransientError("429")

    with pytest.raises(TransientError):
        call_with_retry(fn, RetryPolicy(4, 0, 0), sleep=lambda s: None)
    assert len(calls) == 4


@pytest.mark.parametrize("exc", [NotFoundError, PermanentError])
def test_non_retryable_errors_surface_immediately(exc):
    calls = []

    def fn():
        calls.append(1)
        raise exc("nope")

    with pytest.raises(exc):
        call_with_retry(fn, RetryPolicy(5, 0, 0), sleep=lambda s: None)
    assert len(calls) == 1


def test_stdlib_network_errors_are_transient():
    with pytest.raises(TransientError):
        call_with_retry(
            lambda: (_ for _ in ()).throw(ConnectionResetError()),
            RetryPolicy(2, 0, 0),
            sleep=lambda s: None,
        )


def test_token_bucket_rate():
    t = [0.0]
    slept = []

    def sleep(d):
        slept.append(d)
        t[0] += d

    b = TokenBucket(rate=10, burst=2, clock=lambda: t[0], sleep=sleep)
    for _ in range(12):
        b.acquire()
    # 2 burst tokens free, the remaining 10 need ~1 s at 10/s
    assert abs(t[0] - 1.0) < 1e-6
    assert not b.try_acquire()


def test_resilient_backend_wraps_unknown_errors():
    class Boom(MemoryBackend):
        def get(self, remote_id):
            raise ValueError("sdk exploded")

    rb = ResilientBackend(Boom(), account_id="a", policy=RetryPolicy(2, 0, 0), sleep=lambda s: None)
    with pytest.raises(PermanentError, match="sdk exploded"):
        rb.get("x")
    rb2 = ResilientBackend(MemoryBackend(), account_id="b", policy=RetryPolicy(2, 0, 0))
    rb2.put("dm-1", b"abc")
    assert rb2.get("dm-1") == b"abc"
    assert rb2.latency_ewma is not None
