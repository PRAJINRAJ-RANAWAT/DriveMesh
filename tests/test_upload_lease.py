"""Regression tests for 08-how-it-works §1.6: a slow but live upload must keep its lease.

Before the fix the lease was renewed only when a whole chunk was submitted, so a client
slower than ~28 KB/s (8 MiB per 300 s lease) had its upload aborted by GC. Behind the HTTP
layer, which buffers 1 MiB before calling write(), the threshold was even lower.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from drivemesh.core import gc
from tests.conftest import CHUNK, payload

STEP = 120.0  # seconds of (fake) time between pieces; 8 steps = 960 s, over three leases


def test_writer_renews_lease_while_bytes_arrive_below_a_chunk(dm, user, accounts, clock):
    data = payload(CHUNK // 2)  # never reaches a chunk boundary before close()
    piece = len(data) // 8
    w = dm.open_writer(user.id, "/trickle.bin")
    for i in range(8):
        w.write(data[i * piece : (i + 1) * piece])
        clock.advance(STEP)
        assert gc.scan(dm)["aborted"] == 0, f"aborted after {(i + 1) * STEP:.0f} s"
    w.close()
    assert dm.get_bytes(user.id, "/trickle.bin") == data


def test_http_upload_renews_lease_while_bytes_trickle_in(dm, user, accounts, clock):
    """The API buffers up to 1 MiB before calling write(); it must flush early when the lease
    is due, so a trickling client keeps its upload alive."""
    from drivemesh.web.api import _stream_upload

    data = payload(32 * 1024)
    pieces = [data[i : i + 4096] for i in range(0, len(data), 4096)]

    class TrickleRequest:
        async def stream(self):
            for p in pieces:
                yield p
                clock.advance(STEP)
                assert gc.scan(dm)["aborted"] == 0, "GC aborted a live upload"

    out = asyncio.run(
        _stream_upload(TrickleRequest(), dm, SimpleNamespace(id=user.id), "/http.bin", {})
    )
    assert out["size"] == len(data)
    assert dm.get_bytes(user.id, "/http.bin") == data
