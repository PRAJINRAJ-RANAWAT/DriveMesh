"""Chaos experiments against local-disk accounts (real files, real processes, real SIGKILL).

    python -m bench.chaos all --trials 10
    python -m bench.chaos kill-upload --trials 5

Each experiment checks the same oracle afterwards:
  * every committed file reads back with the original SHA-256 (no data loss, no wrong bytes)
  * fsck --deep is clean (every ok replica's object exists; every chunk at its target RF)
  * no orphaned objects remain on any account after GC
Results are written to bench/out/chaos-<ts>.json.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from sqlalchemy import select

from bench.common import make_system, object_names
from drivemesh import benchkit
from drivemesh.core import backup, gc
from drivemesh.core.fsck import fsck
from drivemesh.core.workers import health_check, repair, scrub, under_replicated
from drivemesh.db import Replica

CHUNK = 256 * 1024
FAST = dict(
    chunk_size=CHUNK,
    dead_after_seconds=0,
    unreachable_after_failures=1,
    repair_batch=100_000,
    pending_lease_seconds=1,
    pending_replica_grace_seconds=0,
    orphan_grace_seconds=0,
    restore_quiet_seconds=0,
)


def write_files(dm, uid, rng: random.Random, n: int, rf: int = 2) -> dict[str, str]:
    files = {}
    for i in range(n):
        size = rng.randint(0, 12 * CHUNK)
        data = rng.randbytes(size)
        dm.put(uid, f"/c{i}.bin", data, rf=rf)
        files[f"/c{i}.bin"] = hashlib.sha256(data).hexdigest()
    return files


def verify(dm, uid, files: dict[str, str]) -> dict[str, int]:
    lost = wrong = 0
    for path, digest in files.items():
        try:
            got = hashlib.sha256(dm.get_bytes(uid, path)).hexdigest()
        except Exception:  # noqa: BLE001 - counted as lost
            lost += 1
            continue
        wrong += got != digest
    return {"lost": lost, "wrong_bytes": int(wrong)}


def orphans(dm, work: Path) -> int:
    with dm.session() as s:
        known = set(s.scalars(select(Replica.remote_id).where(Replica.remote_id.is_not(None))))
    return len(object_names(work) - known)


def repair_until_done(dm, max_passes: int = 10) -> int:
    passes = 0
    while under_replicated(dm) and passes < max_passes:
        repair(dm)
        passes += 1
    return passes


def oracle(dm, uid, work: Path, files: dict[str, str]) -> dict:
    gc.scan(dm)
    dm.jobs.run_until_idle()
    rep = fsck(dm, deep=True)
    out = verify(dm, uid, files)
    out.update(
        fsck_clean=rep.clean, under_replicated=rep.under_replicated, orphans=orphans(dm, work)
    )
    return out


# ---- experiments --------------------------------------------------------------------------
def exp_account_loss(work: Path, seed: int, rf: int = 2, n_accounts: int = 4) -> dict:
    rng = random.Random(seed)
    dm, uid, _accs = make_system(work, n_accounts, **FAST)
    files = write_files(dm, uid, rng, 10, rf=rf)
    victims = rng.sample(range(n_accounts), rf - 1)
    for v in victims:
        shutil.rmtree(work / f"acct{v}")  # the account and all its objects vanish
    health_check(dm)
    readable_before_repair = verify(dm, uid, files)
    passes = repair_until_done(dm)
    out = oracle(dm, uid, work, files)
    out.update(
        readable_before_repair=readable_before_repair,
        repair_passes=passes,
        accounts_lost=len(victims),
    )
    dm.close()
    return out


def exp_delete_chunks(work: Path, seed: int) -> dict:
    rng = random.Random(seed)
    dm, uid, _ = make_system(work, 4, **FAST)
    files = write_files(dm, uid, rng, 10)
    deleted = _mutate_one_replica_per_chunk(dm, work, rng, lambda p: p.unlink())
    before = verify(dm, uid, files)
    found = scrub(dm, budget_bytes=1 << 40, limit=1_000_000)
    repair_until_done(dm)
    out = oracle(dm, uid, work, files)
    out.update(objects_deleted=deleted, scrub_missing=found["missing"], reads_before=before)
    dm.close()
    return out


def exp_corrupt_chunks(work: Path, seed: int) -> dict:
    rng = random.Random(seed)
    dm, uid, _ = make_system(work, 4, **FAST)
    files = write_files(dm, uid, rng, 10)

    def flip(p: Path) -> None:
        b = bytearray(p.read_bytes())
        if b:
            b[rng.randrange(len(b))] ^= 1 << rng.randrange(8)
            p.write_bytes(bytes(b))

    corrupted = _mutate_one_replica_per_chunk(dm, work, rng, flip)
    before = verify(dm, uid, files)
    found = scrub(dm, budget_bytes=1 << 40, limit=1_000_000)
    repair_until_done(dm)
    out = oracle(dm, uid, work, files)
    out.update(objects_corrupted=corrupted, scrub_corrupt=found["corrupt"], reads_before=before)
    dm.close()
    return out


def _mutate_one_replica_per_chunk(dm, work: Path, rng: random.Random, fn) -> int:
    with dm.session() as s:
        rows = s.execute(select(Replica.chunk_id, Replica.remote_id)).all()
    by_chunk: dict[str, list[str]] = {}
    for cid, remote in rows:
        by_chunk.setdefault(cid, []).append(remote)
    n = 0
    for cid in sorted(by_chunk):
        if rng.random() < 0.4:
            name = rng.choice(by_chunk[cid])
            path = next(work.glob(f"acct*/{name}"))
            fn(path)
            n += 1
    return n


def _spawn(cmd: list[str], work: Path) -> subprocess.Popen:
    env = dict(os.environ, PYTHONPATH=str(Path.cwd()))
    return subprocess.Popen(
        [sys.executable, "-m", "bench.chaos_worker", *cmd, str(work)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def exp_kill_upload(work: Path, seed: int) -> dict:
    """SIGKILL a process in the middle of a large upload; GC must leave no trace."""
    rng = random.Random(seed)
    dm, uid, _ = make_system(work, 3, **FAST)
    files = write_files(dm, uid, rng, 3)  # pre-existing data must be unaffected
    dm.close()
    baseline = len(object_names(work))
    proc = _spawn(["upload"], work)
    deadline = time.time() + 60
    target = rng.randint(3, 30)  # kill after this many *new* objects of the victim upload
    while time.time() < deadline and len(object_names(work)) - baseline < target:
        time.sleep(0.02)
    new_objects_at_kill = len(object_names(work)) - baseline
    proc.send_signal(signal.SIGKILL)
    proc.wait()
    time.sleep(1.2)  # writer lease (1 s) expires
    dm, uid, _ = make_system(work, 3, fresh=False, **FAST)
    out = oracle(dm, uid, work, files)
    with dm.session() as s:
        from drivemesh.db import File

        states = sorted(s.scalars(select(File.state)).all())
    out.update(
        new_objects_at_kill=new_objects_at_kill, file_states=states, killed_rc=proc.returncode
    )
    dm.close()
    return out


def exp_kill_repair(work: Path, seed: int) -> dict:
    """SIGKILL the repair worker mid-pass; a later pass must converge with no orphans."""
    rng = random.Random(seed)
    dm, uid, _ = make_system(work, 4, **FAST)
    files = write_files(dm, uid, rng, 12)
    dm.close()
    shutil.rmtree(work / f"acct{rng.randrange(4)}")
    with __import__("sqlite3").connect(work / "meta.db") as c:
        total_replicas = c.execute("select count(*) from replicas").fetchone()[0]
    proc = _spawn(["repair"], work)
    # wait until the child has created some new replicas, then kill it at a random point
    deadline = time.time() + 60
    while time.time() < deadline and _replica_count(work) <= total_replicas:
        time.sleep(0.02)
    time.sleep(rng.uniform(0.0, 0.4))
    proc.send_signal(signal.SIGKILL)
    proc.wait()
    dm, uid, _ = make_system(work, 4, fresh=False, **FAST)
    health_check(dm)
    under_at_restart = len(under_replicated(dm))
    with dm.session() as s:
        pending_after_kill = len(s.scalars(select(Replica).where(Replica.state == "pending")).all())
    gc.scan(dm)  # cleans replicas left 'pending' by the killed process (by name)
    repair_until_done(dm)
    out = oracle(dm, uid, work, files)
    out.update(
        pending_replicas_after_kill=pending_after_kill, under_replicated_at_restart=under_at_restart
    )
    dm.close()
    return out


def _replica_count(work: Path) -> int:
    import sqlite3

    try:
        with sqlite3.connect(work / "meta.db", timeout=1) as c:
            return c.execute("select count(*) from replicas").fetchone()[0]
    except sqlite3.Error:
        return 0


def exp_metadata_restore(work: Path, seed: int) -> dict:
    rng = random.Random(seed)
    dm, uid, _ = make_system(work, 3, **FAST)
    files = write_files(dm, uid, rng, 10)
    dm.mkdir(uid, "/dir/sub", parents=True)
    backup.backup_now(dm)
    dm.close()
    for p in work.glob("meta.db*"):
        p.unlink()  # the metadata DB is gone
    t0 = time.perf_counter()
    from drivemesh import crypto
    from drivemesh.config import Settings
    from drivemesh.core.system import DriveMesh
    from drivemesh.db import make_engine

    url = f"sqlite:///{work}/meta.db"
    ring = crypto.KeyRing.parse((work / "bench.key").read_text())
    st = Settings(_env_file=None, database_url=url, env="test", workers_enabled=False, **FAST)
    dm = DriveMesh(st, keyring=ring, engine=make_engine(url))
    src = work / f"acct{rng.randrange(3)}"
    backup.restore_from_local_dir(dm, src)
    restore_s = time.perf_counter() - t0
    uid = dm.get_user("bench").id
    out = oracle(dm, uid, work, files)
    out.update(
        restore_s=restore_s,
        restored_from=src.name,
        dirs_ok=[d.name for d in dm.listdir(uid, "/dir")[0]] == ["sub"],
    )
    dm.close()
    return out


EXPERIMENTS = {
    "account-loss": exp_account_loss,
    "account-loss-rf3": lambda w, s: exp_account_loss(w, s, rf=3, n_accounts=5),
    "delete-chunks": exp_delete_chunks,
    "corrupt-chunks": exp_corrupt_chunks,
    "kill-upload": exp_kill_upload,
    "kill-repair": exp_kill_repair,
    "metadata-restore": exp_metadata_restore,
}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("experiment", choices=[*EXPERIMENTS, "all"])
    p.add_argument("--trials", type=int, default=5)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--work", type=Path, default=Path("bench/work/chaos"))
    p.add_argument("--out", type=Path, default=Path("bench/out"))
    a = p.parse_args(argv)
    names = list(EXPERIMENTS) if a.experiment == "all" else [a.experiment]
    summary = {}
    with benchkit.measure("chaos", trials=a.trials, seed=a.seed, backend="local") as m:
        for name in names:
            results = []
            for t in range(a.trials):
                r = EXPERIMENTS[name](a.work, a.seed * 1000 + t)
                results.append(r)
            agg = {
                "trials": len(results),
                "files_lost": sum(r["lost"] for r in results),
                "wrong_bytes": sum(r["wrong_bytes"] for r in results),
                "fsck_clean": sum(bool(r["fsck_clean"]) for r in results),
                "orphans": sum(r["orphans"] for r in results),
                "under_replicated": sum(r["under_replicated"] for r in results),
            }
            summary[name] = {"aggregate": agg, "runs": results}
            print(json.dumps({name: agg}), flush=True)
        m.metrics["experiments"] = summary
    shutil.rmtree(a.work, ignore_errors=True)
    print(json.dumps({"saved": str(benchkit.save(m, a.out))}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
