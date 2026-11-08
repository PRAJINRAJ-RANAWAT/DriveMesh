"""Two-phase, streaming, bounded-memory file writer.

Phase 1 (reserve): INSERT files(state=pending) with a wrapped per-file DEK and a lease.
Data:   buffer -> chunks -> AES-GCM(AAD binds file/version/index/last) -> RF replicas.
        At most `inflight_chunks` chunks are in flight; write() blocks when the window is
        full (backpressure all the way to the HTTP socket).
Phase 2 (commit): one transaction verifies every chunk has >= quorum ok replicas, supersedes
        the previous version (enqueueing its purge) and flips this version to committed.
Any failure before commit leaves a pending/aborted file that GC cleans up.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, as_completed, wait
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from drivemesh import crypto, metrics, placement
from drivemesh.core import namespace as ns
from drivemesh.core.jobs import enqueue
from drivemesh.core.replication import place_object
from drivemesh.db import Chunk, File, Replica, new_id
from drivemesh.errors import (
    AlreadyExists,
    Conflict,
    InsufficientAccounts,
    InvalidRequest,
    SafeMode,
    TooLarge,
)

if TYPE_CHECKING:
    from drivemesh.core.system import DriveMesh

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FileInfo:
    id: str
    path: str
    size: int
    sha256: str
    version: int
    chunks: int
    rf: int
    scheme: str


class FileWriter:
    def __init__(
        self,
        dm: DriveMesh,
        user_id: str,
        path: str,
        *,
        overwrite: bool = False,
        rf: int | None = None,
        scheme: str = "replica",
        ec_k: int | None = None,
        ec_m: int | None = None,
    ):
        self.dm = dm
        st = dm.settings
        self.user_id = user_id
        self.path = ns.normalize(path)
        self.overwrite = overwrite
        self.scheme = scheme
        if scheme not in ("replica", "ec"):
            raise InvalidRequest("scheme must be 'replica' or 'ec'")
        if scheme == "ec":
            from drivemesh.core import erasure

            self.ec_k, self.ec_m = ec_k or erasure.DEFAULT_K, ec_m or erasure.DEFAULT_M
            erasure.require_available()
            self.rf = self.ec_k + self.ec_m  # shards per chunk
            self.quorum = self.ec_k + self.ec_m
        else:
            self.ec_k = self.ec_m = None
            self.rf = rf or st.default_rf
            if not 1 <= self.rf <= 5:
                raise InvalidRequest("rf must be between 1 and 5")
            self.quorum = st.write_quorum(self.rf)
        self.chunk_size = st.chunk_size
        self.window = st.inflight_chunks
        self._buf = bytearray()
        self._hash = hashlib.sha256()
        self._size = 0
        self._next_idx = 0
        self._inflight: set[Future] = set()
        self._closed = False
        self._aborted = False
        self._bad_accounts: set[str] = set()
        self._bad_lock = threading.Lock()  # chunk threads update and read it concurrently
        self._last_heartbeat = dm.now()
        self._reserve()
        self._pool = ThreadPoolExecutor(self.window, thread_name_prefix="dm-chunk")

    # ---- phase 1 -----------------------------------------------------------------------------
    def _reserve(self) -> None:
        dm = self.dm
        if dm.safe_mode.active:
            raise SafeMode("system is in safe mode; writes are disabled until health is known")
        parent, name = ns.split(self.path)
        now = dm.now()
        with dm.session() as s:
            folder = ns.resolve_folder(s, self.user_id, parent)
            if ns.child_folder(s, self.user_id, folder.id, name):
                raise Conflict(f"a folder exists at {self.path}")
            if not self.overwrite and ns.live_file(s, self.user_id, folder.id, name):
                raise AlreadyExists(f"file exists: {self.path}")
            version = (
                s.scalar(
                    select(func.max(File.version)).where(
                        File.user_id == self.user_id, File.folder_id == folder.id, File.name == name
                    )
                )
                or 0
            ) + 1
            self.candidates = dm.accounts.candidates(s, self.user_id)
            eligible = placement.rank(
                "probe", self.candidates, reserve=dm.settings.free_space_reserve
            )
            if len(eligible) < self.quorum:
                raise InsufficientAccounts(
                    f"need {self.quorum} writable accounts for this file, have {len(eligible)}"
                )
            self.file_id = new_id()
            self.dek = crypto.new_dek()
            wrapped, kek_id = crypto.wrap_dek(dm.keyring, self.dek, self.file_id)
            self.version = version
            self.folder_id = folder.id
            self.name = name
            s.add(
                File(
                    id=self.file_id,
                    user_id=self.user_id,
                    folder_id=folder.id,
                    name=name,
                    version=version,
                    state="pending",
                    chunk_size=self.chunk_size,
                    scheme=self.scheme,
                    rf=self.rf if self.scheme == "replica" else 1,
                    ec_k=self.ec_k,
                    ec_m=self.ec_m,
                    wrapped_dek=wrapped,
                    kek_id=kek_id,
                    lease_expires_at=now + timedelta(seconds=dm.settings.pending_lease_seconds),
                    created_at=now,
                )
            )

    # ---- data --------------------------------------------------------------------------------
    def write(self, data: bytes) -> None:
        if self._closed:
            raise InvalidRequest("writer is closed")
        if not data:
            return
        self._size += len(data)
        if self._size > self.dm.settings.max_upload_bytes:
            self.abort("too large")
            raise TooLarge(f"upload exceeds {self.dm.settings.max_upload_bytes} bytes")
        self._hash.update(data)
        self._buf += data
        cs = self.chunk_size
        # Emit only when strictly more than one chunk is buffered: the final chunk is held back
        # so it can be encrypted with last=True at close().
        while len(self._buf) > cs:
            chunk = bytes(self._buf[:cs])
            del self._buf[:cs]
            self._submit(chunk, last=False)

    def _submit(self, plain: bytes, last: bool) -> None:
        # Backpressure without head-of-line blocking: wait for *any* chunk to finish (chunks
        # are independent until commit), not the oldest one.
        while len(self._inflight) >= self.window:
            done, self._inflight = wait(self._inflight, return_when=FIRST_COMPLETED)
            for fut in done:
                fut.result()  # raises on chunk failure
        idx = self._next_idx
        self._next_idx += 1
        self._inflight.add(self._pool.submit(self._store_chunk, idx, plain, last))
        self._heartbeat()

    def _heartbeat(self) -> None:
        now = self.dm.now()
        lease = self.dm.settings.pending_lease_seconds
        if (now - self._last_heartbeat).total_seconds() < lease / 3:
            return
        with self.dm.session() as s:
            f = s.get(File, self.file_id)
            if f is not None and f.state == "pending":
                f.lease_expires_at = now + timedelta(seconds=lease)
        self._last_heartbeat = now

    def _store_chunk(self, idx: int, plain: bytes, last: bool) -> None:
        dm = self.dm
        if self._aborted:
            return
        aad = crypto.chunk_aad(self.file_id, self.version, idx, last)
        obj = crypto.encrypt_chunk(self.dek, plain, aad)
        chunk_id = new_id()
        with dm.session() as s:
            s.add(
                Chunk(
                    id=chunk_id,
                    file_id=self.file_id,
                    idx=idx,
                    plain_size=len(plain),
                    object_size=len(obj),
                    object_sha256=crypto.sha256_hex(obj),
                )
            )
        if self.scheme == "ec":
            from drivemesh.core import erasure

            shards = erasure.encode(obj, self.ec_k, self.ec_m)
            placed_accounts: set[str] = set()
            for shard_idx, shard in enumerate(shards):
                ranked = placement.rank(
                    f"{chunk_id}:{shard_idx}",
                    self.candidates,
                    exclude=self._bad_snapshot() | placed_accounts,
                    reserve=dm.settings.free_space_reserve,
                    min_free=len(shard),
                )
                res = place_object(dm, chunk_id, shard, ranked, 1, shard_idx=shard_idx)
                self._mark_bad(res.failed_accounts)
                if not res.placed:
                    raise InsufficientAccounts(f"could not place shard {shard_idx} of chunk {idx}")
                placed_accounts.add(res.placed[0][1])
            return
        ranked = placement.rank(
            chunk_id,
            self.candidates,
            exclude=self._bad_snapshot(),
            reserve=dm.settings.free_space_reserve,
            min_free=len(obj),
        )
        ranked = placement.load_aware(ranked, self.rf, self._load)
        res = place_object(dm, chunk_id, obj, ranked, self.rf)
        self._mark_bad(res.failed_accounts)
        if len(res.placed) < self.quorum:
            raise InsufficientAccounts(
                f"chunk {idx}: stored {len(res.placed)}/{self.rf} replicas "
                f"(quorum {self.quorum}); failures: {res.failed_accounts}"
            )

    def _bad_snapshot(self) -> set[str]:
        with self._bad_lock:
            return set(self._bad_accounts)

    def _mark_bad(self, accounts) -> None:
        with self._bad_lock:
            self._bad_accounts.update(accounts)

    def _load(self, account_id: str) -> int:
        try:
            return self.dm.accounts.backend(account_id).pending
        except Exception:  # noqa: BLE001
            return 1 << 30

    # ---- phase 2 -----------------------------------------------------------------------------
    def close(self) -> FileInfo:
        if self._closed:
            raise InvalidRequest("writer already closed")
        try:
            if self._size > 0:
                self._submit(bytes(self._buf), last=True)
                self._buf = bytearray()
            for fut in as_completed(self._inflight):
                fut.result()
            self._inflight = set()
            info = self._commit()
        except BaseException as exc:
            self.abort(f"{type(exc).__name__}: {exc}")
            raise
        finally:
            self._closed = True
            self._pool.shutdown(wait=True)
        metrics.FILES_COMMITTED.inc()
        return info

    def _commit(self, attempt: int = 0) -> FileInfo:
        dm = self.dm
        n = self._next_idx
        sha = self._hash.hexdigest()
        now = dm.now()
        try:
            with dm.session() as s:
                f = s.get(File, self.file_id, with_for_update=True)
                if f is None or f.state != "pending":
                    raise Conflict("upload was aborted (lease expired or cancelled)")
                counts = dict(
                    s.execute(
                        select(Chunk.idx, func.count(Replica.id))
                        .join(
                            Replica,
                            (Replica.chunk_id == Chunk.id) & (Replica.state == "ok"),
                            isouter=True,
                        )
                        .where(Chunk.file_id == self.file_id)
                        .group_by(Chunk.idx)
                    ).all()
                )
                if sorted(counts) != list(range(n)):
                    raise Conflict(f"chunk set incomplete: have {len(counts)}, expected {n}")
                weak = [i for i, c in counts.items() if c < self.quorum]
                if weak:
                    raise InsufficientAccounts(f"chunks below write quorum: {weak[:5]}")
                old = ns.live_file(s, self.user_id, self.folder_id, self.name)
                if old is not None and old.id != self.file_id:
                    if not self.overwrite:
                        raise AlreadyExists(f"file exists: {self.path}")
                    old.state, old.deleted_at = "deleting", now
                    s.flush()
                    enqueue(
                        s,
                        "delete_file",
                        {"file_id": old.id},
                        now=now,
                        dedupe_key=f"delete_file:{old.id}",
                    )
                f.state, f.size, f.sha256, f.chunk_count = "committed", self._size, sha, n
                f.committed_at, f.lease_expires_at = now, None
        except IntegrityError:
            # Another writer committed the same path concurrently; with overwrite we supersede it.
            if attempt == 0 and self.overwrite:
                return self._commit(attempt=1)
            raise AlreadyExists(f"file exists: {self.path}") from None
        dm.event(
            "file_committed",
            user_id=self.user_id,
            file_id=self.file_id,
            path=self.path,
            size=self._size,
            chunks=n,
        )
        return FileInfo(
            self.file_id, self.path, self._size, sha, self.version, n, self.rf, self.scheme
        )

    def abort(self, reason: str = "aborted") -> None:
        dm = self.dm
        self._closed = True
        for fut in self._inflight:
            fut.cancel()
        self._aborted = True
        pool = getattr(self, "_pool", None)
        if pool is not None:
            # Wait for in-flight chunk tasks so no straggler uploads after GC has run.
            pool.shutdown(wait=True, cancel_futures=True)
        try:
            with dm.session() as s:
                f = s.get(File, self.file_id)
                if f is not None and f.state == "pending":
                    f.state, f.lease_expires_at = "aborted", None
                    enqueue(
                        s,
                        "gc_file",
                        {"file_id": self.file_id},
                        now=dm.now(),
                        dedupe_key=f"gc_file:{self.file_id}",
                    )
            dm.event(
                "upload_aborted",
                level="warning",
                user_id=self.user_id,
                file_id=self.file_id,
                reason=reason[:500],
            )
        except Exception:
            log.exception("abort bookkeeping failed; GC will clean up after lease expiry")
