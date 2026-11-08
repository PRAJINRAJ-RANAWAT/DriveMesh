"""Streaming reader with replica selection, verification, fallback and bounded memory.

For each chunk: try replicas best-first; every object is checked against its recorded SHA-256
(catches corruption without the key) and then AES-GCM-decrypted with AAD (catches tampering,
swaps, reordering, truncation). Bad replicas are marked missing/corrupt so repair restores RF.
Chunks are prefetched in a window of `inflight_chunks`, consumed strictly in order.
"""

from __future__ import annotations

import hashlib
import logging
from collections import deque
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select

from drivemesh import crypto
from drivemesh.backends.base import AuthError, BackendError, NotFoundError
from drivemesh.core.accounts import READABLE_STATES
from drivemesh.db import Chunk, File, LinkedAccount, Replica
from drivemesh.errors import ChunkUnavailable, IntegrityFailure, InvalidRequest

if TYPE_CHECKING:
    from drivemesh.core.system import DriveMesh

log = logging.getLogger(__name__)


@dataclass
class ReplicaRef:
    id: str
    account_id: str
    account_state: str
    remote_id: str | None
    sha256: str | None
    shard_idx: int


@dataclass
class ChunkRef:
    id: str
    idx: int
    plain_size: int
    object_size: int | None
    object_sha256: str | None
    replicas: list[ReplicaRef]


class FileReader:
    def __init__(self, dm: DriveMesh, file: File):
        if file.state != "committed":
            raise InvalidRequest("file is not readable")
        self.dm = dm
        self.file = file
        self.size = file.size or 0
        self.chunk_size = file.chunk_size
        self.dek = crypto.unwrap_dek(dm.keyring, file.wrapped_dek, file.kek_id, file.id)
        self.chunks = self._load_chunks()
        self.last_idx = (file.chunk_count or 0) - 1

    def _load_chunks(self) -> list[ChunkRef]:
        with self.dm.session() as s:
            chunks = list(
                s.scalars(select(Chunk).where(Chunk.file_id == self.file.id).order_by(Chunk.idx))
            )
            rows = s.execute(
                select(Replica, LinkedAccount.state)
                .join(LinkedAccount, LinkedAccount.id == Replica.account_id)
                .join(Chunk, Chunk.id == Replica.chunk_id)
                .where(Chunk.file_id == self.file.id, Replica.state == "ok")
            ).all()
        by_chunk: dict[str, list[ReplicaRef]] = {}
        for r, acc_state in rows:
            by_chunk.setdefault(r.chunk_id, []).append(
                ReplicaRef(r.id, r.account_id, acc_state, r.remote_id, r.sha256, r.shard_idx)
            )
        return [
            ChunkRef(
                c.id, c.idx, c.plain_size, c.object_size, c.object_sha256, by_chunk.get(c.id, [])
            )
            for c in chunks
        ]

    # ---- replica handling ----------------------------------------------------------------------
    def _order(self, replicas: list[ReplicaRef]) -> list[ReplicaRef]:
        def key(r: ReplicaRef) -> tuple[int, int, float]:
            state_rank = {"active": 0, "full": 1, "draining": 1}.get(r.account_state, 2)
            try:
                b = self.dm.accounts.backend(r.account_id)
                pending, lat = b.pending, b.latency_ewma or 0.0
            except Exception:  # noqa: BLE001
                pending, lat = 1 << 30, float("inf")
            return state_rank, pending, lat

        return sorted(replicas, key=key)

    def _mark(self, ref: ReplicaRef, state: str, reason: str) -> None:
        with self.dm.session() as s:
            r = s.get(Replica, ref.id)
            if r is not None and r.state == "ok":
                r.state = state
        self.dm.event(
            f"replica_{state}",
            level="warning",
            file_id=self.file.id,
            account_id=ref.account_id,
            replica_id=ref.id,
            reason=reason[:300],
        )

    def _fetch_verified(self, ref: ReplicaRef) -> bytes | None:
        """Download one stored object and check its hash. Returns None on failure."""
        if not ref.remote_id:
            return None
        try:
            data = self.dm.accounts.backend(ref.account_id).get(ref.remote_id)
        except NotFoundError:
            self._mark(ref, "missing", "object not found")
            return None
        except AuthError as exc:
            self.dm.accounts.mark_state(ref.account_id, "revoked", str(exc))
            return None
        except BackendError as exc:
            log.warning(
                "replica read failed", extra={"replica_id": ref.id, "error": str(exc)[:300]}
            )
            return None
        if ref.sha256 and crypto.sha256_hex(data) != ref.sha256:
            self._mark(ref, "corrupt", "sha256 mismatch")
            return None
        return data

    def fetch_object(self, chunk: ChunkRef) -> bytes:
        """Return the verified encrypted object for a chunk (replica or erasure-coded)."""
        if self.file.scheme == "ec":
            return self._fetch_ec(chunk)
        for ref in self._order(chunk.replicas):
            if ref.account_state not in READABLE_STATES and ref.account_state != "unreachable":
                continue
            data = self._fetch_verified(ref)
            if data is not None:
                return data
        raise ChunkUnavailable(f"chunk {chunk.idx} of {self.file.id}: no readable replica")

    def _fetch_ec(self, chunk: ChunkRef) -> bytes:
        from drivemesh.core import erasure

        k, m = self.file.ec_k or 0, self.file.ec_m or 0
        shards: dict[int, bytes] = {}
        for ref in self._order(chunk.replicas):
            if len(shards) >= k:
                break
            if ref.shard_idx in shards:
                continue
            data = self._fetch_verified(ref)
            if data is not None:
                shards[ref.shard_idx] = data
        if len(shards) < k:
            raise ChunkUnavailable(f"chunk {chunk.idx}: only {len(shards)}/{k} shards readable")
        obj = erasure.decode(shards, k, m, chunk.object_size or 0)
        if chunk.object_sha256 and crypto.sha256_hex(obj) != chunk.object_sha256:
            raise IntegrityFailure(f"chunk {chunk.idx}: reconstructed object hash mismatch")
        return obj

    def read_chunk(self, chunk: ChunkRef) -> bytes:
        last = chunk.idx == self.last_idx
        aad = crypto.chunk_aad(self.file.id, self.file.version, chunk.idx, last)
        tried: set[str] = set()
        while True:
            obj = self.fetch_object(chunk)
            try:
                plain = crypto.decrypt_chunk(self.dek, obj, aad)
            except crypto.CryptoError:
                # Hash matched what we recorded but AEAD fails: the recorded object itself is
                # wrong for this position (e.g. swapped). Mark the replica used and try others.
                bad = next((r for r in chunk.replicas if r.sha256 == crypto.sha256_hex(obj)), None)
                if self.file.scheme == "ec" or bad is None or bad.id in tried:
                    raise IntegrityFailure(f"chunk {chunk.idx}: authentication failed") from None
                tried.add(bad.id)
                self._mark(bad, "corrupt", "AEAD authentication failed")
                chunk.replicas = [r for r in chunk.replicas if r.id != bad.id]
                continue
            if len(plain) != chunk.plain_size:
                raise IntegrityFailure(f"chunk {chunk.idx}: size mismatch")
            return plain

    # ---- streaming -------------------------------------------------------------------------------
    def iter_range(self, start: int = 0, end: int | None = None) -> Iterator[bytes]:
        if self.size == 0:
            return
        end = self.size - 1 if end is None else min(end, self.size - 1)
        if start < 0 or start > end:
            raise InvalidRequest("invalid range")
        cs = self.chunk_size
        first, last = start // cs, end // cs
        full = start == 0 and end == self.size - 1
        digest = hashlib.sha256() if full else None
        pool = ThreadPoolExecutor(self.dm.settings.inflight_chunks, thread_name_prefix="dm-read")
        window: deque[tuple[int, Future]] = deque()
        nxt = first
        try:
            while nxt <= last and len(window) < self.dm.settings.inflight_chunks:
                window.append((nxt, pool.submit(self.read_chunk, self.chunks[nxt])))
                nxt += 1
            while window:
                i, fut = window.popleft()
                data = fut.result()
                if nxt <= last:
                    window.append((nxt, pool.submit(self.read_chunk, self.chunks[nxt])))
                    nxt += 1
                if digest is not None:
                    digest.update(data)
                    if i == last and digest.hexdigest() != self.file.sha256:
                        self.dm.event("file_hash_mismatch", level="critical", file_id=self.file.id)
                        raise IntegrityFailure("whole-file SHA-256 mismatch")
                lo = start - i * cs if i == first else 0
                hi = end - i * cs + 1 if i == last else len(data)
                yield data[lo:hi]
        finally:
            for _, fut in window:
                fut.cancel()
            pool.shutdown(wait=False, cancel_futures=True)

    def read_all(self) -> bytes:
        return b"".join(self.iter_range())
