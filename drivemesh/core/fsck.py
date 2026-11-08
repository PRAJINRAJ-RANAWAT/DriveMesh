"""Consistency checker: verifies the metadata invariants (and optionally the remote objects).

Used by the CLI, the status page and every chaos test as the 'is the system consistent?' oracle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy import String, and_, case, cast, func, select

from drivemesh.core.accounts import READABLE_STATES
from drivemesh.db import Chunk, File, LinkedAccount, Replica

if TYPE_CHECKING:
    from drivemesh.core.system import DriveMesh


@dataclass
class FsckReport:
    files: int = 0
    chunks: int = 0
    replicas_ok: int = 0
    errors: list[str] = field(default_factory=list)  # durability/consistency violations
    under_replicated: int = 0  # chunks below target but still readable
    unavailable_chunks: int = 0  # chunks with no readable replica
    missing_objects: int = 0  # deep mode: ok replicas whose object is gone

    @property
    def clean(self) -> bool:
        return not self.errors and self.unavailable_chunks == 0 and self.missing_objects == 0


def fsck(dm: DriveMesh, *, user_id: str | None = None, deep: bool = False) -> FsckReport:
    rep = FsckReport()
    # One grouped pass over every chunk (not one query per file): O(chunks) for any file count.
    readable = and_(Replica.state == "ok", LinkedAccount.state.in_(READABLE_STATES))
    unit = case((File.scheme == "ec", cast(Replica.shard_idx, String)), else_=Replica.id)
    healthy = func.count(func.distinct(case((readable, unit), else_=None)))
    with dm.session() as s:
        fq = select(File).where(File.state == "committed")
        if user_id:
            fq = fq.where(File.user_id == user_id)
        files = {f.id: f for f in s.scalars(fq)}
        rep.files = len(files)
        cq = (
            select(Chunk.file_id, Chunk.idx, healthy)
            .join(File, File.id == Chunk.file_id)
            .outerjoin(Replica, Replica.chunk_id == Chunk.id)
            .outerjoin(LinkedAccount, LinkedAccount.id == Replica.account_id)
            .where(File.state == "committed")
            .group_by(Chunk.id, Chunk.file_id, Chunk.idx)
        )
        if user_id:
            cq = cq.where(File.user_id == user_id)
        seen: dict[str, list[int]] = {fid: [] for fid in files}
        for fid, idx, n in s.execute(cq):
            f = files[fid]
            seen[fid].append(idx)
            rep.chunks += 1
            rep.replicas_ok += n
            need_readable = f.ec_k if f.scheme == "ec" else 1
            target = (f.ec_k or 0) + (f.ec_m or 0) if f.scheme == "ec" else f.rf
            if n < need_readable:
                rep.unavailable_chunks += 1
                rep.errors.append(f"{fid}: chunk {idx} has {n} readable replicas")
            elif n < target:
                rep.under_replicated += 1
        for fid, idxs in seen.items():
            if sorted(idxs) != list(range(files[fid].chunk_count or 0)):
                rep.errors.append(
                    f"{fid}: chunk indices {len(idxs)} != count {files[fid].chunk_count}"
                )
        if deep:
            rows = s.execute(
                select(Replica.id, Replica.account_id, Replica.remote_id)
                .join(LinkedAccount, LinkedAccount.id == Replica.account_id)
                .where(Replica.state == "ok", LinkedAccount.state.in_(READABLE_STATES))
            ).all()
    if deep:
        for rid, acc_id, remote in rows:
            try:
                if dm.accounts.backend(acc_id).find(f"dm-{rid}") is None and remote:
                    rep.missing_objects += 1
                    rep.errors.append(f"replica {rid} on {acc_id}: object missing")
            except Exception as exc:  # noqa: BLE001
                rep.errors.append(f"replica {rid} on {acc_id}: check failed: {exc}")
    return rep
