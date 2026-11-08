"""Balancer: evens out utilisation across a user's accounts (HDFS balancer analog).

Moves replicas from the most- to the least-utilised active account when the utilisation
spread exceeds `balancer_threshold`. Every move is copy -> mark ok -> delete old, so the
replica count never drops below its target, and a crash mid-move only leaves an extra
replica (trimmed later by repair).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import TYPE_CHECKING

from sqlalchemy import select

from drivemesh import crypto
from drivemesh.core.gc import purge_replicas
from drivemesh.core.replication import place_object
from drivemesh.db import Chunk, File, LinkedAccount, Replica
from drivemesh.placement import Candidate

if TYPE_CHECKING:
    from drivemesh.core.system import DriveMesh

log = logging.getLogger(__name__)


def _utilisation(a: LinkedAccount) -> float | None:
    if not a.capacity_bytes or a.used_bytes is None:
        return None
    return a.used_bytes / a.capacity_bytes


def balance(dm: DriveMesh, max_moves: int | None = None) -> dict[str, int]:
    if dm.safe_mode.active:
        return {"moved": 0}
    st = dm.settings
    budget = max_moves if max_moves is not None else st.balancer_batch
    with dm.session() as s:
        accounts = list(s.scalars(select(LinkedAccount).where(LinkedAccount.state == "active")))
    by_user: dict[str, list[LinkedAccount]] = defaultdict(list)
    for a in accounts:
        if _utilisation(a) is not None:
            by_user[a.user_id].append(a)
    moved = bytes_moved = 0
    for accs in by_user.values():
        if len(accs) < 2:
            continue
        accs.sort(key=_utilisation)
        low, high = accs[0], accs[-1]
        spread = _utilisation(high) - _utilisation(low)
        if spread <= st.balancer_threshold:
            continue
        # move about half the gap, in bytes, measured against the source
        target_bytes = int(spread / 2 * min(high.capacity_bytes, low.capacity_bytes))
        with dm.session() as s:
            low_chunks = set(
                s.scalars(select(Replica.chunk_id).where(Replica.account_id == low.id))
            )
            rows = list(
                s.execute(
                    select(Replica)
                    .join(Chunk, Chunk.id == Replica.chunk_id)
                    .join(File, File.id == Chunk.file_id)
                    .where(
                        Replica.account_id == high.id,
                        Replica.state == "ok",
                        File.state == "committed",
                    )
                    .order_by(Replica.created_at)
                    .limit(budget * 4)
                ).scalars()
            )
        target = [Candidate(low.id, low.provider, low.free_bytes, "active")]
        for r in rows:
            if moved >= budget or bytes_moved >= target_bytes:
                break
            if r.chunk_id in low_chunks or not r.remote_id:
                continue
            try:
                obj = dm.accounts.backend(high.id).get(r.remote_id)
            except Exception:  # noqa: BLE001 - skip; scrubber/repair handle bad replicas
                continue
            if r.sha256 and crypto.sha256_hex(obj) != r.sha256:
                continue
            res = place_object(dm, r.chunk_id, obj, target, 1, shard_idx=r.shard_idx)
            if not res.placed:
                break  # target unhappy; try again next pass
            purge_replicas(dm, [r], context={"reason": "balance"})
            moved += 1
            bytes_moved += len(obj)
            low_chunks.add(r.chunk_id)
    if moved:
        dm.event("balancer_pass", moved=moved, bytes=bytes_moved)
    return {"moved": moved, "bytes": bytes_moved}
