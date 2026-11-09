"""Regression tests for 08-how-it-works §3.6: a GC abort must never overwrite a commit."""

from __future__ import annotations

import threading
import time

from sqlalchemy import event

from drivemesh.core import gc
from tests.conftest import CHUNK, payload


def _is_gc_lease_scan(statement: str) -> bool:
    s = " ".join(statement.split())
    return s.startswith("SELECT") and "files.lease_expires_at <" in s


def test_gc_abort_cannot_overwrite_a_concurrent_commit(dm, user, accounts, clock):
    """Deterministic interleaving.

    1. GC reads the upload as pending with an expired lease.
    2. The writer commits.
    3. GC writes its abort.

    Before the fix, step 3 overwrote 'committed' with 'aborted', and the gc_file job purged
    a committed file's data.
    """
    data = payload(3 * CHUNK + 5)
    w = dm.open_writer(user.id, "/race.bin")
    w.write(data)
    clock.advance(dm.settings.pending_lease_seconds + 1)  # lease expired, writer still alive

    fired: list[bool] = []
    errors: list[BaseException] = []

    def close_writer() -> None:
        try:
            w.close()
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(exc)

    def commit_between_gc_read_and_write(conn, cursor, statement, params, context, many):
        if fired or not _is_gc_lease_scan(statement):
            return
        fired.append(True)
        t = threading.Thread(target=close_writer)
        t.start()
        t.join(30)

    event.listen(dm.engine, "after_cursor_execute", commit_between_gc_read_and_write)
    try:
        gc.scan(dm)
    finally:
        event.remove(dm.engine, "after_cursor_execute", commit_between_gc_read_and_write)
    assert fired, "GC lease scan was not observed"
    assert not errors, errors

    dm.jobs.run_until_idle()
    f = dm.stat(user.id, "/race.bin")
    assert f.state == "committed"
    assert dm.get_bytes(user.id, "/race.bin") == data


class _GatedPuts:
    """Backend wrapper whose puts block until the test opens the gate."""

    def __init__(self, inner, gate: threading.Event):
        self.inner, self.gate, self.provider = inner, gate, inner.provider

    def put(self, name, data):
        self.gate.wait(10)
        return self.inner.put(name, data)

    def __getattr__(self, name):
        return getattr(self.inner, name)


def test_close_keeps_the_lease_alive_while_waiting_for_slow_puts(dm, user, accounts, clock):
    """close() waits for in-flight chunks; it must keep heartbeating meanwhile, or GC aborts a
    live upload whose last puts are slow."""
    gate = threading.Event()
    for a in accounts:
        dm.accounts.wrap(a.id, lambda inner, g=gate: _GatedPuts(inner, g))
    data = payload(CHUNK + 1)
    w = dm.open_writer(user.id, "/slow.bin")
    w.heartbeat_poll_seconds = 0.02
    w.write(data)  # chunk 0 is in flight, blocked on the gate

    result: dict[str, object] = {}

    def close_writer() -> None:
        try:
            result["info"] = w.close()
        except BaseException as exc:  # noqa: BLE001
            result["error"] = exc

    t = threading.Thread(target=close_writer)
    t.start()
    time.sleep(0.1)
    clock.advance(dm.settings.pending_lease_seconds - 10)  # inside close(), lease nearly over
    time.sleep(0.3)  # let close() heartbeat
    clock.advance(20)  # past the original lease
    assert gc.scan(dm)["aborted"] == 0
    gate.set()
    t.join(10)
    assert "error" not in result, result.get("error")
    assert dm.get_bytes(user.id, "/slow.bin") == data
