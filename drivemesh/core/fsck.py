"""Consistency checker: verifies the metadata invariants (and optionally the remote objects).

Used by the CLI, the status page and every chaos test as the 'is the system consistent?' oracle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy import func, select

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
    with dm.session() as s:
        fq = select(File).where(File.state == "committed")
        if user_id:
            fq = fq.where(File.user_id == user_id)
        files = list(s.scalars(fq))
        rep.files = len(files)
        for f in files:
            healthy = dict(
                s.execute(
                    select(Chunk.idx, func.count(Replica.id))
                    .join(
                        Replica,
                        (Replica.chunk_id == Chunk.id) & (Replica.state == "ok"),
                        isouter=True,
                    )
                    .join(LinkedAccount, LinkedAccount.id == Replica.account_id, isouter=True)
                    .where(Chunk.file_id == f.id)
                    .where((LinkedAccount.state.in_(READABLE_STATES)) | (Replica.id.is_(None)))
                    .group_by(Chunk.idx)
                ).all()
            )
            all_idx = list(s.scalars(select(Chunk.idx).where(Chunk.file_id == f.id)))
            rep.chunks += len(all_idx)
            if sorted(all_idx) != list(range(f.chunk_count or 0)):
                rep.errors.append(f"{f.id}: chunk indices {len(all_idx)} != count {f.chunk_count}")
            need_readable = f.ec_k if f.scheme == "ec" else 1
            target = (f.ec_k or 0) + (f.ec_m or 0) if f.scheme == "ec" else f.rf
            for idx in all_idx:
                n = healthy.get(idx, 0)
                rep.replicas_ok += n
                if n < need_readable:
                    rep.unavailable_chunks += 1
                    rep.errors.append(f"{f.id}: chunk {idx} has {n} readable replicas")
                elif n < target:
                    rep.under_replicated += 1
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
