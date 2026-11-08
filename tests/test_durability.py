"""Phase 2: heartbeats, repair, scrubber, evacuation, backup/restore, safe mode.

These are the in-process chaos tests (C-1..C-4, C-6 unit-level). Process-kill variants live
in bench/chaos.py and are recorded in results.md.
"""

import random

import pytest
from sqlalchemy import select

from drivemesh.backends.faulty import FaultyBackend
from drivemesh.backends.local import MemoryBackend
from drivemesh.core import backup, gc
from drivemesh.core.fsck import fsck
from drivemesh.core.workers import (
    WorkerSet,
    cluster_status,
    health_check,
    repair,
    scrub,
    start_evacuation,
    under_replicated,
)
from drivemesh.db import KV, LinkedAccount, Replica
from drivemesh.errors import Conflict, SafeMode
from tests.conftest import CHUNK, payload


def make_faulty(dm, account):
    holder = {}

    def wrap(b):
        holder["fb"] = FaultyBackend(b, sleep=lambda s: None)
        return holder["fb"]

    dm.accounts.wrap(account.id, wrap)
    dm.accounts.backend(account.id)
    return holder["fb"]


def write_files(dm, user, n=10, size=3 * CHUNK + 7, **kw):
    files = {}
    for i in range(n):
        data = payload(size, seed=i)
        dm.put(user.id, f"/f{i}", data, **kw)
        files[f"/f{i}"] = data
    return files


def assert_all_readable(dm, user, files):
    for path, data in files.items():
        assert dm.get_bytes(user.id, path) == data, path


def test_health_check_updates_capacity_and_states(dm, user):
    a = dm.accounts.link_memory(user.id, "a", capacity_bytes=10 * CHUNK)
    b = dm.accounts.link_memory(user.id, "b")
    fb = make_faulty(dm, b)
    assert health_check(dm) == {a.id: "active", b.id: "active"}
    assert dm.accounts.get(a.id).free_bytes == 10 * CHUNK
    fb.dead = True
    for _ in range(dm.settings.unreachable_after_failures - 1):
        assert health_check(dm)[b.id] == "active"  # tolerate blips
    assert health_check(dm)[b.id] == "unreachable"
    assert dm.accounts.get(b.id).unhealthy_since is not None
    fb.dead = False
    assert health_check(dm)[b.id] == "active"
    fb.dead, fb.dead_auth = True, True
    assert health_check(dm)[b.id] == "revoked"  # auth failures are immediate


def test_full_account_detected(dm, user):
    a = dm.accounts.link_memory(user.id, "tiny", capacity_bytes=CHUNK // 2)
    assert health_check(dm)[a.id] == "full"


def test_c1_survives_account_loss_and_repairs(dm, user, accounts, clock):
    """C-1: lose one of three accounts (RF=2): everything stays readable; repair restores RF."""
    files = write_files(dm, user)
    victim = accounts[0]
    fb = make_faulty(dm, victim)
    fb.dead = True
    assert_all_readable(dm, user, files)  # reads fall back immediately
    for _ in range(3):
        health_check(dm)
    assert dm.accounts.get(victim.id).state == "unreachable"
    assert repair(dm).repaired == 0  # not dead yet: no repair storm on a blip
    clock.advance(dm.settings.dead_after_seconds + 1)
    res = repair(dm)
    assert res.repaired > 0 and res.unavailable == 0 and res.failed == 0
    assert under_replicated(dm) == []
    with dm.session() as s:
        live = s.scalars(
            select(Replica.account_id).where(Replica.state == "ok", Replica.account_id != victim.id)
        ).all()
    assert len(live) == 2 * sum((len(d) + CHUNK - 1) // CHUNK for d in files.values())
    assert_all_readable(dm, user, files)
    # the account comes back: chunks are over-replicated and get trimmed to RF
    fb.dead = False
    health_check(dm)
    res = repair(dm)
    assert res.trimmed > 0
    rep = fsck(dm, deep=True)
    assert rep.clean and rep.under_replicated == 0


def test_c1_rf3_survives_two_losses(dm, user, clock):
    accs = [dm.accounts.link_memory(user.id, f"x{i}") for i in range(5)]
    files = write_files(dm, user, n=5, rf=3)
    for a in accs[:2]:
        make_faulty(dm, a).dead = True
    assert_all_readable(dm, user, files)
    for _ in range(3):
        health_check(dm)
    clock.advance(dm.settings.dead_after_seconds + 1)
    assert repair(dm).unavailable == 0
    assert under_replicated(dm) == []


def test_c2_deleted_chunks_detected_by_scrub_and_repaired(dm, user, accounts):
    files = write_files(dm, user)
    rng = random.Random(42)
    deleted = 0
    with dm.session() as s:
        reps = s.execute(select(Replica.chunk_id, Replica.account_id, Replica.remote_id)).all()
    by_chunk = {}
    for cid, acc, remote in reps:
        by_chunk.setdefault(cid, []).append((acc, remote))
    for cid in sorted(by_chunk):
        if rng.random() < 0.3:  # lose RF-1 = 1 replica of ~30% of chunks
            acc, remote = rng.choice(by_chunk[cid])
            dm.accounts.backend(acc).inner.delete(remote)
            deleted += 1
    assert deleted > 0
    assert_all_readable(dm, user, files)  # fallback covers single-replica loss
    found = scrub(dm, budget_bytes=1 << 40, limit=10_000)
    assert found["missing"] > 0
    repair(dm)
    rep = fsck(dm, deep=True)
    assert rep.clean and rep.under_replicated == 0


def test_c3_corrupted_chunks_detected_and_repaired(dm, user, accounts):
    files = write_files(dm, user)
    victim = MemoryBackend.named("acct-b")
    for name, (obj, ts) in list(victim.objects.items()):
        b = bytearray(obj)
        b[len(b) // 3] ^= 0x40
        victim.objects[name] = (bytes(b), ts)
    res = scrub(dm, budget_bytes=1 << 40, limit=10_000)
    assert res["corrupt"] == len(victim.objects)
    repair(dm)
    assert fsck(dm, deep=True).clean
    # the corrupted objects were deleted from acct-b during repair
    with dm.session() as s:
        assert not s.scalars(select(Replica).where(Replica.state == "corrupt")).all()
    assert_all_readable(dm, user, files)


def test_scrub_respects_budget(dm, user, accounts):
    write_files(dm, user, n=3)
    res = scrub(dm, budget_bytes=CHUNK, limit=1000)
    # the budget is checked before each object, so at most one object overshoots it
    from drivemesh.crypto import CHUNK_OVERHEAD

    assert res["bytes"] <= CHUNK + CHUNK + CHUNK_OVERHEAD
    with dm.session() as s:
        total = len(s.scalars(select(Replica)).all())
    assert res["verified"] < total  # it stopped early instead of scanning everything


def test_chunk_with_no_healthy_replica_is_reported(dm, user, accounts, clock):
    dm.put(user.id, "/one", payload(CHUNK), rf=1)
    with dm.session() as s:
        acc_id = s.scalars(select(Replica.account_id)).one()
    make_faulty(dm, dm.accounts.get(acc_id)).dead = True
    for _ in range(3):
        health_check(dm)
    clock.advance(dm.settings.dead_after_seconds + 1)
    assert repair(dm).unavailable == 1
    st = cluster_status(dm, user.id)
    assert st["unavailable_chunks"] == 1


def test_unlink_with_evacuation(dm, user, accounts):
    files = write_files(dm, user)
    victim = accounts[1]
    start_evacuation(dm, user.id, victim.id)
    assert dm.accounts.get(victim.id).state == "draining"
    dm.jobs.run_until_idle()
    acc = dm.accounts.get(victim.id)
    assert acc.state == "removed" and acc.secret_blob is None
    assert len(MemoryBackend.named("acct-b").objects) == 0
    assert_all_readable(dm, user, files)
    rep = fsck(dm, deep=True)
    assert rep.clean and rep.under_replicated == 0


def test_unlink_refused_if_it_would_break_rf(dm, user, accounts):
    write_files(dm, user, n=1, rf=3)
    with pytest.raises(Conflict):
        start_evacuation(dm, user.id, accounts[0].id)


def test_c6_metadata_wipe_and_restore(tmp_path, keyring, clock):
    """C-6: back up metadata to the accounts, lose the DB, restore from one account."""
    from drivemesh.core.system import DriveMesh
    from drivemesh.db import make_engine
    from tests.conftest import make_settings

    url1 = f"sqlite:///{tmp_path}/first.db"
    dm1 = DriveMesh(
        make_settings(database_url=url1),
        keyring=keyring,
        engine=make_engine(url1),
        clock=clock,
        sleep=lambda s: None,
    )
    dm1.create_schema()
    u = dm1.create_user("owner", "pw")
    for name in ("a", "b", "c"):
        dm1.accounts.link_local(u.id, str(tmp_path / name), name)
    files = write_files(dm1, u, n=5)
    dm1.mkdir(u.id, "/empty-dir")
    res = backup.backup_now(dm1)
    assert len(res["stored_on"]) == 3
    assert backup.backup_if_changed(dm1) is None  # unchanged -> skipped
    dm1.close()
    (tmp_path / "first.db").unlink()  # metadata DB lost

    url2 = f"sqlite:///{tmp_path}/second.db"
    dm2 = DriveMesh(
        make_settings(database_url=url2),
        keyring=keyring,
        engine=make_engine(url2),
        clock=clock,
        sleep=lambda s: None,
    )
    out = backup.restore_from_local_dir(dm2, tmp_path / "b")
    assert out["rows"]["files"] == 5
    assert_all_readable(dm2, u, files)
    assert [f.name for f in dm2.listdir(u.id, "/")[0]] == ["empty-dir"]
    assert fsck(dm2, deep=True).clean
    # orphan sweep is paused after restore
    assert gc.orphan_sweep(dm2) == {"skipped_after_restore": 1}
    with pytest.raises(Conflict):
        backup.restore_from_local_dir(dm2, tmp_path / "a")  # refuses non-empty DB
    with dm2.session() as s:
        assert s.get(KV, "restored_at") is not None
    dm2.close()


def test_backup_with_wrong_key_fails(dm, user, accounts):
    from drivemesh import crypto

    dm.put(user.id, "/x", b"data")
    backup.backup_now(dm)
    blob = backup.latest_snapshot(dm.accounts.backend(accounts[0].id))
    dm.keyring = crypto.KeyRing.parse(crypto.generate_key_spec("other"))
    with pytest.raises(crypto.KeyRingError):
        backup.open_snapshot(dm, blob)


def test_backup_pruning_keeps_last_n(dm, user, accounts, clock):
    for i in range(7):
        dm.put(user.id, f"/f{i}", b"x")
        clock.advance(1)
        backup.backup_now(dm)
    be = MemoryBackend.named("acct-a")
    assert len([n for n in be.objects if n.startswith("dm-meta-")]) == dm.settings.backup_keep


def test_orphan_sweep_removes_unknown_objects_after_grace(dm, user, accounts, clock):
    dm.put(user.id, "/keep", payload(CHUNK))
    be = MemoryBackend.named("acct-a")
    be.put("dm-ffffffffffffffffffffffffffffffff", b"orphan")
    assert gc.orphan_sweep(dm)["found"] == 0  # too young
    clock.advance(dm.settings.orphan_grace_seconds + 10)
    for b in MemoryBackend._instances.values():  # objects' timestamps come from real time
        for k, (d, _ts) in list(b.objects.items()):
            b.objects[k] = (d, clock() - __import__("datetime").timedelta(days=2))
    out = gc.orphan_sweep(dm)
    assert out == {"found": 1, "deleted": 1}
    assert dm.get_bytes(user.id, "/keep") == payload(CHUNK)


def test_safe_mode_blocks_writes_until_health_known(dm, user, accounts):
    ws = WorkerSet(dm)
    dm.safe_mode.enter("startup")
    with pytest.raises(SafeMode):
        dm.put(user.id, "/x", b"data")
    ws._health()
    assert not dm.safe_mode.active
    dm.put(user.id, "/x", b"data")
    # all accounts unreachable -> safe mode again (no repair storm)
    for a in accounts:
        make_faulty(dm, a).dead = True
    for _ in range(3):
        ws._health()
    assert dm.safe_mode.active
    assert repair(dm).scanned == 0


def test_worker_tick_runs_everything(dm, user, accounts):
    write_files(dm, user, n=2)
    ws = WorkerSet(dm)
    ws.tick()
    with dm.session() as s:
        assert s.get(KV, "last_backup_at") is not None
    st = cluster_status(dm, user.id)
    assert st["replicas"]["ok"] > 0 and len(st["accounts"]) == 3


def test_worker_threads_start_and_stop(dm, user, accounts):
    ws = WorkerSet(dm)
    ws.start()
    ws.stop()
    assert not dm.safe_mode.active


def test_rewrap_rotates_master_key(dm, user, accounts):
    from drivemesh import crypto
    from drivemesh.core.keys import rewrap_all

    files = write_files(dm, user, n=2)
    dm.accounts.link(user.id, "gdrive", "sub-1", "g", secret={"refresh_token": "r"})
    old_spec = ",".join(
        f"{k}:{__import__('base64').urlsafe_b64encode(v).decode()}"
        for k, v in dm.keyring.keys.items()
    )
    dm.keyring = crypto.KeyRing.parse(old_spec + "," + crypto.generate_key_spec("k2"), "k2")
    out = rewrap_all(dm)
    assert out["files"] == 2 and out["accounts"] == 1 and out["remaining_old"] == 0
    # drop the old key entirely: everything must still decrypt
    dm.keyring = crypto.KeyRing(keys={"k2": dm.keyring.keys["k2"]}, active_id="k2")
    dm.accounts._cache.clear()
    assert_all_readable(dm, user, files)
    with dm.session() as s:
        g = s.scalars(select(LinkedAccount).where(LinkedAccount.provider == "gdrive")).one()
        assert dm.accounts.read_secret(g) == {"refresh_token": "r"}


def test_events_with_reserved_log_keys_do_not_crash(dm, user, caplog):
    """Regression: dm.event(name=...) crashed the backup worker (found by the Docker smoke test)."""
    import logging

    with caplog.at_level(logging.INFO, logger="drivemesh"):
        dm.event("metadata_backup", name="dm-meta-x", msg="hello", args=1)
    assert any(getattr(r, "name_", None) == "dm-meta-x" for r in caplog.records)


def test_backup_skipped_without_accounts(dm, user):
    assert backup.backup_if_changed(dm) is None
