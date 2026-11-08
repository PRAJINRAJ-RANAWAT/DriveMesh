"""DriveMesh benchmark runner.

Usage (each benchmark runs in its own process so peak RSS is per-benchmark):
    python -m bench.run throughput --size-mb 1024 --accounts 3 --rf 2
    python -m bench.run scaling --size-mb 256 --latency 0.1 --account-mbps 10
    python -m bench.run chunksize --size-mb 256 --latency 0.1 --account-mbps 10
    python -m bench.run overhead
    python -m bench.run repair --size-mb 1024 --accounts 4
    python -m bench.run metadata --chunks 1000000
Results: bench/out/<name>-<timestamp>.json (and a one-line summary on stdout).
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from sqlalchemy import func, select

from bench.common import MiB, NetSim, data_stream, dir_bytes, make_system, stream_digest
from drivemesh import benchkit

DEFAULT_WORK = Path("bench/work")


def _upload(dm, uid, path, size, **kw):
    t0 = time.perf_counter()
    info = dm.put(uid, path, data_stream(size), **kw)
    return info, time.perf_counter() - t0


def _download(dm, uid, path):
    import hashlib

    h = hashlib.sha256()
    t0 = time.perf_counter()
    n = 0
    for piece in dm.open_reader(uid, path).iter_range():
        h.update(piece)
        n += len(piece)
    return h.hexdigest(), n, time.perf_counter() - t0


def bench_throughput(a) -> benchkit.Measurement:
    """B-1/B-3: upload+download one file; MB/s and peak RSS (local disk, no network sim)."""
    size = a.size_mb * MiB
    with benchkit.measure(
        "throughput",
        size_mb=a.size_mb,
        accounts=a.accounts,
        rf=a.rf,
        inflight=a.inflight,
        chunk_mib=a.chunk_mib,
        backend="local",
    ) as m:
        dm, uid, _ = make_system(
            a.work,
            a.accounts,
            inflight_chunks=a.inflight,
            chunk_size=a.chunk_mib * MiB,
            account_concurrency=a.inflight,
        )
        info, up = _upload(dm, uid, "/bench.bin", size, rf=a.rf)
        digest, n, down = _download(dm, uid, "/bench.bin")
        assert n == size and digest == info.sha256 == stream_digest(size), "integrity failure"
        m.metrics.update(
            upload_s=up,
            download_s=down,
            upload_MBps=size / up / 1e6,
            download_MBps=size / down / 1e6,
            chunks=info.chunks,
        )
        dm.close()
    return m


def bench_scaling(a) -> benchkit.Measurement:
    """B-2: sequential vs parallel, 1..N accounts, over a *simulated* network."""
    size = a.size_mb * MiB
    net = NetSim(
        a.latency,
        a.account_mbps * 1e6 if a.account_mbps else None,
        a.host_mbps * 1e6 if a.host_mbps else None,
    )
    rows = []
    with benchkit.measure(
        "scaling", size_mb=a.size_mb, rf=1, net=net.describe(), backend="local+simulated-network"
    ) as m:
        modes = [("sequential", 1), ("parallel", a.inflight)]
        if a.parallel_only:
            modes = modes[1:]
        for n_acc in a.account_counts:
            for mode, inflight in modes:
                ups, downs = [], []
                for _trial in range(a.trials):
                    dm, uid, _ = make_system(
                        a.work,
                        n_acc,
                        net=net,
                        inflight_chunks=inflight,
                        account_concurrency=max(1, inflight),
                    )
                    info, up = _upload(dm, uid, "/s.bin", size, rf=1)
                    digest, _, down = _download(dm, uid, "/s.bin")
                    assert digest == info.sha256
                    ups.append(size / up / 1e6)
                    downs.append(size / down / 1e6)
                    dm.close()
                rows.append(
                    {
                        "accounts": n_acc,
                        "mode": mode,
                        "inflight": inflight,
                        "trials": a.trials,
                        "upload_MBps_mean": round(sum(ups) / len(ups), 2),
                        "upload_MBps_min": round(min(ups), 2),
                        "upload_MBps_max": round(max(ups), 2),
                        "download_MBps_mean": round(sum(downs) / len(downs), 2),
                        "download_MBps_min": round(min(downs), 2),
                        "download_MBps_max": round(max(downs), 2),
                    }
                )
                print(json.dumps(rows[-1]), flush=True)
        m.metrics["rows"] = rows
    return m


def bench_chunksize(a) -> benchkit.Measurement:
    """B-4: throughput vs chunk size over a simulated network (ADR-009)."""
    size = a.size_mb * MiB
    net = NetSim(
        a.latency,
        a.account_mbps * 1e6 if a.account_mbps else None,
        a.host_mbps * 1e6 if a.host_mbps else None,
    )
    rows = []
    with benchkit.measure(
        "chunksize",
        size_mb=a.size_mb,
        accounts=a.accounts,
        rf=2,
        net=net.describe(),
        backend="local+simulated-network",
    ) as m:
        for cmib in a.chunk_sizes:
            dm, uid, _ = make_system(
                a.work, a.accounts, net=net, chunk_size=cmib * MiB, inflight_chunks=a.inflight
            )
            info, up = _upload(dm, uid, "/c.bin", size, rf=2)
            digest, _, down = _download(dm, uid, "/c.bin")
            assert digest == info.sha256
            rows.append(
                {
                    "chunk_MiB": cmib,
                    "chunks": info.chunks,
                    "upload_MBps": round(size / up / 1e6, 2),
                    "download_MBps": round(size / down / 1e6, 2),
                    "requests_per_GiB_rf2": 2 * 1024 // cmib,
                }
            )
            print(json.dumps(rows[-1]), flush=True)
            dm.close()
        m.metrics["rows"] = rows
    return m


def bench_overhead(a) -> benchkit.Measurement:
    """Storage overhead: stored object bytes / logical bytes per durability scheme."""
    size = a.size_mb * MiB
    rows = []
    with benchkit.measure("overhead", size_mb=a.size_mb, backend="local") as m:
        for label, kw, n_acc in (
            ("RF=1", {"rf": 1}, 3),
            ("RF=2", {"rf": 2}, 3),
            ("RF=3", {"rf": 3}, 3),
            ("RS(4,2)", {"scheme": "ec", "ec_k": 4, "ec_m": 2}, 6),
            ("RS(6,3)", {"scheme": "ec", "ec_k": 6, "ec_m": 3}, 9),
        ):
            dm, uid, _ = make_system(a.work, n_acc)
            dm.put(uid, "/o.bin", data_stream(size), **kw)
            stored = dir_bytes(a.work)
            rows.append(
                {
                    "scheme": label,
                    "stored_bytes": stored,
                    "logical_bytes": size,
                    "overhead_x": round(stored / size, 4),
                }
            )
            print(json.dumps(rows[-1]), flush=True)
            dm.close()
        m.metrics["rows"] = rows
    return m


def bench_repair(a) -> benchkit.Measurement:
    """B-5: time to restore RF after losing one account."""
    from drivemesh.core.fsck import fsck
    from drivemesh.core.workers import health_check, repair, under_replicated

    size = a.size_mb * MiB
    net = NetSim(
        a.latency,
        a.account_mbps * 1e6 if a.account_mbps else None,
        a.host_mbps * 1e6 if a.host_mbps else None,
    )
    with benchkit.measure(
        "repair",
        size_mb=a.size_mb,
        accounts=a.accounts,
        rf=2,
        files=a.files,
        net=net.describe(),
        backend="local" if not a.latency else "local+simulated-network",
    ) as m:
        dm, uid, accs = make_system(
            a.work,
            a.accounts,
            net=net,
            dead_after_seconds=0,
            unreachable_after_failures=1,
            repair_batch=100_000,
        )
        per_file = size // a.files
        for i in range(a.files):
            dm.put(uid, f"/r{i}.bin", data_stream(per_file, seed=i), rf=2)
        victim = accs[0]
        with dm.session() as s:
            from drivemesh.db import Replica

            lost = s.scalar(
                select(func.count()).select_from(Replica).where(Replica.account_id == victim)
            )
            lost_bytes = (
                s.scalar(select(func.sum(Replica.size)).where(Replica.account_id == victim)) or 0
            )
        import shutil

        shutil.rmtree(a.work / "acct0")  # the account disappears (dir gone)
        health_check(dm)
        t0 = time.perf_counter()
        passes = 0
        while under_replicated(dm):
            repair(dm, limit=100_000)
            passes += 1
            if passes > 20:
                break
        elapsed = time.perf_counter() - t0
        rep = fsck(dm)
        m.metrics.update(
            replicas_lost=lost,
            bytes_lost=lost_bytes,
            repair_s=elapsed,
            repair_MBps=lost_bytes / elapsed / 1e6 if elapsed else None,
            passes=passes,
            fsck_clean=rep.clean,
            under_replicated_after=rep.under_replicated,
        )
        dm.close()
    return m


def bench_metadata(a) -> benchkit.Measurement:
    """Metadata scale: N chunks x 2 replicas; time the repair scan and fsck queries."""
    from drivemesh.core import namespace as ns
    from drivemesh.core.fsck import fsck
    from drivemesh.core.workers import under_replicated
    from drivemesh.db import Chunk, File, Replica, new_id

    url = a.database_url or None
    kw = {"database_url": url} if url else {}
    with benchkit.measure("metadata", chunks=a.chunks, db="postgres" if url else "sqlite") as m:
        dm, uid, accs = make_system(a.work, 3, **kw)
        per_file = 1000
        t0 = time.perf_counter()
        with dm.engine.begin() as conn:
            with dm.session() as s:
                root = ns.root_folder(s, uid).id
            for fstart in range(0, a.chunks, per_file):
                fid = new_id()
                conn.execute(
                    File.__table__.insert(),
                    [
                        dict(
                            id=fid,
                            user_id=uid,
                            folder_id=root,
                            name=f"f{fstart}",
                            version=1,
                            state="committed",
                            size=per_file,
                            sha256="0" * 64,
                            chunk_size=1,
                            chunk_count=per_file,
                            scheme="replica",
                            rf=2,
                            wrapped_dek=b"x",
                            kek_id="bench",
                        )
                    ],
                )
                chunks, reps = [], []
                for i in range(per_file):
                    cid = new_id()
                    chunks.append(dict(id=cid, file_id=fid, idx=i, plain_size=1))
                    n = 1 if (fstart + i) % 997 == 0 else 2  # ~0.1% under-replicated
                    for acc in accs[:n]:
                        reps.append(
                            dict(
                                id=new_id(),
                                chunk_id=cid,
                                account_id=acc,
                                shard_idx=0,
                                remote_id="r",
                                size=1,
                                sha256="s",
                                state="ok",
                            )
                        )
                conn.execute(Chunk.__table__.insert(), chunks)
                conn.execute(Replica.__table__.insert(), reps)
        load_s = time.perf_counter() - t0
        # A production DB has planner statistics (autovacuum/ANALYZE); a fresh bulk load
        # does not, so refresh them before timing the queries.
        from sqlalchemy import text

        with dm.engine.begin() as conn:
            conn.execute(text("ANALYZE"))
        t0 = time.perf_counter()
        ur = under_replicated(dm, limit=None)
        scan_s = time.perf_counter() - t0
        t0 = time.perf_counter()
        rep = fsck(dm)
        fsck_s = time.perf_counter() - t0
        m.metrics.update(
            load_s=load_s,
            under_replicated=len(ur),
            repair_scan_s=scan_s,
            fsck_s=fsck_s,
            fsck_chunks=rep.chunks,
        )
        dm.close()
    return m


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "name", choices=["throughput", "scaling", "chunksize", "overhead", "repair", "metadata"]
    )
    p.add_argument("--work", type=Path, default=DEFAULT_WORK)
    p.add_argument("--out", type=Path, default=Path("bench/out"))
    p.add_argument("--size-mb", type=int, default=256)
    p.add_argument("--accounts", type=int, default=3)
    p.add_argument("--account-counts", type=int, nargs="+", default=[1, 2, 4, 8])
    p.add_argument("--rf", type=int, default=2)
    p.add_argument("--inflight", type=int, default=4)
    p.add_argument("--chunk-mib", type=int, default=8)
    p.add_argument("--chunk-sizes", type=int, nargs="+", default=[1, 4, 8, 16, 32])
    p.add_argument("--latency", type=float, default=0.0)
    p.add_argument("--account-mbps", type=float, default=None)
    p.add_argument("--host-mbps", type=float, default=None)
    p.add_argument("--files", type=int, default=16)
    p.add_argument("--chunks", type=int, default=100_000)
    p.add_argument("--trials", type=int, default=1)
    p.add_argument("--parallel-only", action="store_true")
    p.add_argument(
        "--no-load-aware",
        action="store_true",
        help="disable load-aware placement/reads (for A/B comparison)",
    )
    p.add_argument("--database-url", default=None)
    a = p.parse_args(argv)
    if a.no_load_aware:
        from drivemesh import placement

        placement.load_aware = lambda ranked, need, load, window=2: list(ranked)
        import drivemesh.core.writer as w

        w.placement.load_aware = placement.load_aware
        from drivemesh.resilience import ResilientBackend

        ResilientBackend.pending = property(lambda self: 0, lambda self, v: None)
    fn = globals()[f"bench_{a.name}"]
    m = fn(a)
    path = benchkit.save(m, a.out)
    print(
        json.dumps(
            {
                "saved": str(path),
                "wall_s": round(m.wall_seconds, 2),
                "peak_rss_MiB": round(m.peak_rss_bytes / MiB, 1),
                "baseline_rss_MiB": round(m.baseline_rss_bytes / MiB, 1),
                **{k: v for k, v in m.metrics.items() if k != "rows"},
            },
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
