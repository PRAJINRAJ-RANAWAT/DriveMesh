"""Shared setup for benchmarks and chaos runs.

Every run creates a fresh DriveMesh in a work directory with N local-disk accounts, optionally
wrapped in a simulated network (per-op latency + serialized per-account link + optional shared
host uplink). Simulation parameters are *assumptions*, recorded with every result.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from drivemesh import crypto
from drivemesh.backends.faulty import FaultyBackend, SharedLink
from drivemesh.config import Settings
from drivemesh.core.system import DriveMesh
from drivemesh.db import make_engine

MiB = 1 << 20


@dataclass
class NetSim:
    latency: float = 0.0  # seconds per request
    account_bps: float | None = None  # per-account serialized link
    host_bps: float | None = None  # host uplink shared by all accounts

    def describe(self) -> dict:
        return {
            "latency_s": self.latency,
            "account_MBps": self.account_bps and self.account_bps / 1e6,
            "host_MBps": self.host_bps and self.host_bps / 1e6,
        }


def make_system(
    workdir: Path, n_accounts: int, *, net: NetSim | None = None, fresh: bool = True, **settings
) -> tuple[DriveMesh, str, list[str]]:
    if fresh and workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    keyfile = workdir / "bench.key"
    if not keyfile.exists():
        keyfile.write_text(crypto.generate_key_spec("bench"))
    ring = crypto.KeyRing.parse(keyfile.read_text())
    url = settings.pop("database_url", f"sqlite:///{workdir}/meta.db")
    base = dict(
        env="test",
        workers_enabled=False,
        log_json=False,
        log_level="WARNING",
        free_space_reserve=0,
        account_rate=1e6,
        account_burst=1_000_000,
        retry_base_seconds=0.05,
        retry_cap_seconds=1.0,
    )
    base.update(settings)
    st = Settings(_env_file=None, database_url=url, **base)
    dm = DriveMesh(st, keyring=ring, engine=make_engine(url))
    dm.create_schema()
    try:
        user = dm.create_user("bench", "bench")
    except Exception:  # noqa: BLE001 - existing system
        user = dm.get_user("bench")
    accounts = []
    if fresh:
        for i in range(n_accounts):
            acc = dm.accounts.link_local(user.id, str(workdir / f"acct{i}"), f"acct{i}")
            accounts.append(acc.id)
    else:  # reopen: never re-link (that would recreate a deleted account directory)
        accounts = [a.id for a in dm.accounts.list(user.id)]
    if net and (net.latency or net.account_bps or net.host_bps):
        host = SharedLink(net.host_bps) if net.host_bps else None
        for aid in accounts:
            links = tuple(
                x for x in (SharedLink(net.account_bps) if net.account_bps else None, host) if x
            )
            dm.accounts.wrap(
                aid, lambda b, _l=links: FaultyBackend(b, latency=net.latency, links=_l)
            )
    return dm, user.id, accounts


def data_stream(total: int, block: int = MiB, seed: int = 7) -> Iterator[bytes]:
    """Deterministic, incompressible-enough stream without holding the file in memory."""
    base = hashlib.sha256(str(seed).encode()).digest() * (block // 32)
    sent, i = 0, 0
    while sent < total:
        n = min(block, total - sent)
        head = i.to_bytes(8, "big")
        yield (head + base[8:n]) if n > 8 else base[:n]
        sent += n
        i += 1


def stream_digest(total: int, block: int = MiB, seed: int = 7) -> str:
    h = hashlib.sha256()
    for b in data_stream(total, block, seed):
        h.update(b)
    return h.hexdigest()


def dir_bytes(path: Path) -> int:
    return sum(
        p.stat().st_size
        for p in path.rglob("dm-*")
        if p.is_file() and not p.name.startswith("dm-meta-")
    )


def object_names(workdir: Path) -> set[str]:
    return {
        p.name for p in workdir.rglob("dm-*") if p.is_file() and not p.name.startswith("dm-meta-")
    }


def env_info() -> dict:
    return {"cpu_count": os.cpu_count()}
