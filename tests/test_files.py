"""Write/read/delete paths. Tests named w##_* pin a weakness from 01-current-state.md."""

import tracemalloc

import pytest
from sqlalchemy import select

from drivemesh import crypto
from drivemesh.backends.faulty import FaultyBackend
from drivemesh.backends.local import MemoryBackend
from drivemesh.core.fsck import fsck
from drivemesh.core.gc import scan
from drivemesh.db import Chunk, File, Job, Replica
from drivemesh.errors import AlreadyExists, ChunkUnavailable, InsufficientAccounts, NotFound
from tests.conftest import CHUNK, payload


def all_objects():
    return {name: be for name, be in MemoryBackend._instances.items()}


def object_count():
    return sum(len(b.objects) for b in MemoryBackend._instances.values())


@pytest.mark.parametrize("size", [0, 1, CHUNK - 1, CHUNK, CHUNK + 1, 3 * CHUNK + 5])
def test_roundtrip_sizes(dm, user, accounts, size):
    data = payload(size, seed=size)
    info = dm.put(user.id, "/f.bin", data)
    assert info.size == size
    assert info.chunks == (size + CHUNK - 1) // CHUNK
    assert info.sha256 == crypto.sha256_hex(data)
    assert dm.get_bytes(user.id, "/f.bin") == data
    assert fsck(dm, deep=True).clean


def test_rf2_replicas_on_distinct_accounts(dm, user, accounts):
    dm.put(user.id, "/f", payload(5 * CHUNK))
    with dm.session() as s:
        rows = s.execute(select(Replica.chunk_id, Replica.account_id, Replica.state)).all()
    by_chunk = {}
    for cid, acc, state in rows:
        assert state == "ok"
        by_chunk.setdefault(cid, []).append(acc)
    assert len(by_chunk) == 5
    assert all(len(v) == 2 and len(set(v)) == 2 for v in by_chunk.values())


@pytest.mark.parametrize(
    "start,end",
    [(0, 0), (5, CHUNK + 10), (CHUNK, 2 * CHUNK - 1), (3 * CHUNK, 3 * CHUNK + 4), (100, None)],
)
def test_range_reads(dm, user, accounts, start, end):
    data = payload(3 * CHUNK + 5)
    dm.put(user.id, "/f", data)
    got = b"".join(dm.open_reader(user.id, "/f").iter_range(start, end))
    assert got == data[start : (len(data) if end is None else end + 1)]


def test_w05_same_name_in_different_folders_is_independent(dm, user, accounts):
    dm.mkdir(user.id, "/docs")
    dm.put(user.id, "/a.txt", b"root version")
    dm.put(user.id, "/docs/a.txt", b"docs version")
    assert dm.get_bytes(user.id, "/a.txt") == b"root version"
    assert dm.get_bytes(user.id, "/docs/a.txt") == b"docs version"


def test_w05_same_path_requires_explicit_overwrite(dm, user, accounts):
    dm.put(user.id, "/a", b"v1" * 50000)
    with pytest.raises(AlreadyExists):
        dm.put(user.id, "/a", b"v2")
    info = dm.put(user.id, "/a", b"v2", overwrite=True)
    assert info.version == 2
    assert dm.get_bytes(user.id, "/a") == b"v2"
    dm.jobs.run_until_idle()
    with dm.session() as s:
        states = sorted(s.scalars(select(File.state)).all())
    assert states == ["committed", "deleted"]
    # old version's chunks are purged: only v2's replicas remain
    assert object_count() == 2


def test_w18_remote_names_are_opaque(dm, user, accounts):
    dm.put(user.id, "/secret-plans.pdf", payload(2 * CHUNK))
    for be in MemoryBackend._instances.values():
        for name, (data, _) in be.objects.items():
            assert name.startswith("dm-") and "secret" not in name
            assert b"secret-plans" not in data


def test_w10_corrupt_replica_detected_and_fallback(dm, user, accounts, monkeypatch):
    from drivemesh.core.reader import FileReader

    data = payload(2 * CHUNK)
    dm.put(user.id, "/f", data, rf=3)
    # try the victim account first so the corruption is definitely encountered
    monkeypatch.setattr(
        FileReader,
        "_order",
        lambda self, reps: sorted(reps, key=lambda r: r.account_id != accounts[0].id),
    )
    # flip a byte in every object on one account
    victim = MemoryBackend.named("acct-a")
    for name, (obj, ts) in list(victim.objects.items()):
        b = bytearray(obj)
        b[50] ^= 0xFF
        victim.objects[name] = (bytes(b), ts)
    assert dm.get_bytes(user.id, "/f") == data
    with dm.session() as s:
        corrupt = s.scalars(select(Replica).where(Replica.state == "corrupt")).all()
    assert corrupt and all(r.account_id == accounts[0].id for r in corrupt)


def test_w10_swapped_objects_detected(dm, user, accounts):
    data = payload(2 * CHUNK)
    dm.put(user.id, "/f", data, rf=1)
    with dm.session() as s:
        reps = s.execute(
            select(Replica, Chunk.idx).join(Chunk, Chunk.id == Replica.chunk_id).order_by(Chunk.idx)
        ).all()
    (r0, _), (r1, _) = reps
    b0 = dm.accounts.backend(r0.account_id).inner
    b1 = dm.accounts.backend(r1.account_id).inner
    o0, o1 = b0.get(r0.remote_id), b1.get(r1.remote_id)
    b0.put(r0.remote_id, o1)
    b1.put(r1.remote_id, o0)  # a provider swaps two objects
    with pytest.raises(ChunkUnavailable):
        dm.get_bytes(user.id, "/f")  # never returns wrong bytes


def test_all_replicas_lost_raises(dm, user, accounts):
    dm.put(user.id, "/f", payload(CHUNK))
    for be in MemoryBackend._instances.values():
        be.objects.clear()
    with pytest.raises(ChunkUnavailable):
        dm.get_bytes(user.id, "/f")
    with dm.session() as s:
        assert s.scalars(select(Replica.state)).all() == ["missing", "missing"]


def test_w03_failed_upload_leaves_no_orphans(dm, user, accounts, clock):
    faulty = []

    def broken(b):
        fb = FaultyBackend(b, error_rate=1.0, sleep=lambda s: None)
        faulty.append(fb)
        return fb

    for acc in accounts[1:]:
        dm.accounts.wrap(acc.id, broken)
    with pytest.raises(InsufficientAccounts):
        dm.put(user.id, "/f", payload(4 * CHUNK))
    dm.jobs.run_until_idle()
    assert object_count() == 0  # successful replicas on the healthy account are gone
    with dm.session() as s:
        # replicas on the broken accounts are NOT forgotten: their objects can't be verified gone
        assert s.scalars(select(Replica)).all()
    for fb in faulty:
        fb.error_rate = 0.0  # accounts recover
    clock.advance(600)
    dm.jobs.run_until_idle()
    with dm.session() as s:
        assert s.scalars(select(File.state)).all() == ["aborted"]
        assert s.scalars(select(Replica)).all() == []


def test_write_falls_back_to_next_account(dm, user, accounts):
    dm.accounts.wrap(
        accounts[0].id, lambda b: FaultyBackend(b, error_rate=1.0, sleep=lambda s: None)
    )
    data = payload(6 * CHUNK)
    dm.put(user.id, "/f", data)
    assert dm.get_bytes(user.id, "/f") == data
    with dm.session() as s:
        ok_accounts = set(s.scalars(select(Replica.account_id).where(Replica.state == "ok")))
    assert accounts[0].id not in ok_accounts
    dm.jobs.run_until_idle()


def test_transient_errors_are_retried_transparently(dm, user, accounts):
    for acc in accounts:
        dm.accounts.wrap(
            acc.id, lambda b: FaultyBackend(b, error_rate=0.3, seed=7, sleep=lambda s: None)
        )
    dm2_data = payload(8 * CHUNK)
    dm.put(user.id, "/f", dm2_data)
    assert dm.get_bytes(user.id, "/f") == dm2_data


def test_insufficient_accounts_rejected_upfront(dm, user):
    dm.accounts.link_memory(user.id, "only-one")
    with pytest.raises(InsufficientAccounts):
        dm.put(user.id, "/f", b"x")
    assert dm.put(user.id, "/f", b"x", rf=1).size == 1


def test_w04_delete_retries_and_never_silently_drops(dm, user, accounts):
    dm.put(user.id, "/f", payload(2 * CHUNK))
    flaky = {}
    for acc in accounts:
        flaky[acc.id] = None

        def wrap(b, _id=acc.id):
            fb = FaultyBackend(b, sleep=lambda s: None)
            flaky[_id] = fb
            return fb

        dm.accounts.wrap(acc.id, wrap)
    dm.delete_file(user.id, "/f")
    with pytest.raises(NotFound):
        dm.stat(user.id, "/f")  # hidden immediately
    for acc in accounts:
        dm.accounts.backend(acc.id)
        flaky[acc.id].dead = True
    dm.jobs.run_once()
    with dm.session() as s:
        job = s.scalars(select(Job)).one()
        assert job.state == "queued" and "replica deletions failed" in job.last_error
        assert s.scalars(select(File.state)).one() == "deleting"
        assert len(s.scalars(select(Replica)).all()) == 4  # nothing forgotten
    for fb in flaky.values():
        fb.dead = False
    with dm.session() as s:
        s.get(Job, job.id).run_after = dm.now()
    dm.jobs.run_until_idle()
    with dm.session() as s:
        assert s.scalars(select(File.state)).one() == "deleted"
    assert object_count() == 0


def test_abandoned_upload_is_collected_after_lease(dm, user, accounts, clock):
    w = dm.open_writer(user.id, "/big")
    w.write(payload(3 * CHUNK))  # two chunks uploaded, writer then "crashes"
    for f in list(w._inflight):
        f.result()
    assert object_count() > 0
    assert scan(dm)["aborted"] == 0  # lease still valid
    clock.advance(dm.settings.pending_lease_seconds + 1)
    assert scan(dm)["aborted"] == 1
    dm.jobs.run_until_idle()
    assert object_count() == 0
    with pytest.raises(NotFound):
        dm.stat(user.id, "/big")


def test_crash_between_put_and_db_update_is_cleaned_by_name(dm, user, accounts, clock):
    """Simulate: object uploaded but replica row still pending (crash before UPDATE)."""
    dm.put(user.id, "/f", payload(CHUNK))
    with dm.session() as s:
        r = s.scalars(select(Replica)).first()
        r.state, r.remote_id = "pending", None
    clock.advance(dm.settings.pending_replica_grace_seconds + 1)
    scan(dm)
    with dm.session() as s:
        assert len(s.scalars(select(Replica)).all()) == 1
    assert object_count() == 1


def test_w07_read_memory_is_bounded(dm, user, accounts):
    size = 64 * CHUNK  # 4 MiB file, 64 KiB chunks
    dm.put(user.id, "/f", payload(size), rf=1)
    reader = dm.open_reader(user.id, "/f")
    tracemalloc.start()
    total = 0
    for piece in reader.iter_range():
        total += len(piece)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert total == size
    assert peak < 16 * CHUNK  # window of 4 chunks (+ copies), far below the 64-chunk file


def test_w07_write_memory_is_bounded(dm, user, accounts):
    tracemalloc.start()
    w = dm.open_writer(user.id, "/f", rf=1)
    block = payload(CHUNK // 2)
    for _ in range(128):
        w.write(block)
    w.close()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    # MemoryBackend itself retains the 4 MiB of objects; exclude by comparing to stored size
    stored = sum(len(o) for b in MemoryBackend._instances.values() for o, _ in b.objects.values())
    assert peak - stored < 16 * CHUNK


def test_transient_failure_on_read_falls_back(dm, user, accounts):
    data = payload(2 * CHUNK)
    dm.put(user.id, "/f", data)
    for acc in accounts[:2]:
        dm.accounts.wrap(acc.id, lambda b: FaultyBackend(b, error_rate=1.0, sleep=lambda s: None))
    # at most one replica per chunk is on acct-c; some chunks may be unreadable -> either full data
    # or a clean ChunkUnavailable, never wrong bytes
    try:
        got = dm.get_bytes(user.id, "/f")
    except ChunkUnavailable:
        got = None
    assert got in (None, data)
