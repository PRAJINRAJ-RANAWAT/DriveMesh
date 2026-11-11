"""Deletion and garbage collection.

* delete_file job: purge every replica of a tombstoned (deleting) file, then mark it deleted.
* gc_file job:     same for an aborted upload.
* scan():          abort uploads whose writer lease expired; clean stale pending replicas.
* orphan_sweep():  list each account's dm-* objects and delete those the DB does not know,
                   after a grace period (the 'block report' reconciliation).

Errors are never swallowed: a replica that cannot be deleted keeps its row and the job is
retried with backoff; after max attempts the job is 'dead' and visible on the status page.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, select, update

from drivemesh.backends.base import AuthError, BackendError, NotFoundError
from drivemesh.core.jobs import RetryLater, enqueue, register
from drivemesh.db import KV, Chunk, File, LinkedAccount, RemoteDelete, Replica, object_name

if TYPE_CHECKING:
    from drivemesh.core.system import DriveMesh

log = logging.getLogger(__name__)


def delete_replica_object(dm: DriveMesh, replica: Replica) -> None:
    """Delete a replica's remote object. NotFound counts as success (idempotent)."""
    _delete_remote(dm, replica.account_id, replica.remote_id, object_name(replica.id))


def is_disconnected(dm: DriveMesh, acc: LinkedAccount) -> bool:
    """Can't be reached right now: unhealthy state, or failing health checks while draining
    (a draining account keeps its state, so look at its failure count too)."""
    return (
        acc.state in ("unreachable", "revoked")
        or acc.consecutive_failures >= dm.settings.unreachable_after_failures
    )


def _delete_remote(dm: DriveMesh, account_id: str, remote_id: str | None, name: str) -> None:
    """Delete one stored object. NotFound counts as success (idempotent)."""
    backend = dm.accounts.backend(account_id)
    remote = remote_id or backend.find(name)
    if remote is None:
        return
    with contextlib.suppress(NotFoundError):
        backend.delete(remote)


def purge_replicas(dm: DriveMesh, replicas: list[Replica], *, context: dict[str, Any]) -> list[str]:
    """Delete replicas (remote object, then row) in parallel. Returns error strings.

    Per replica, by the state of its account:
      * removed: we no longer have access; the row is dropped (event `replica_abandoned`).
      * disconnected (unreachable / revoked): the object can't be deleted *now*, so the row
        moves to `remote_deletes` and the caller's delete completes; GC deletes the object
        when the account is reachable again (`drain_remote_deletes`). Nothing is forgotten.
      * reachable: delete now; a failure keeps the row and is returned so the caller retries.
    """
    with dm.session() as s:
        accs = {
            a.id: a
            for a in s.scalars(
                select(LinkedAccount).where(LinkedAccount.id.in_({r.account_id for r in replicas}))
            )
        }
    deferred: dict[str, int] = {}
    lock = threading.Lock()

    def defer(r: Replica, reason: str) -> None:
        with dm.session() as s:
            s.add(
                RemoteDelete(
                    account_id=r.account_id,
                    remote_id=r.remote_id,
                    object_name=object_name(r.id),
                    reason=reason,
                    created_at=dm.now(),
                )
            )
            s.execute(delete(Replica).where(Replica.id == r.id))
        with lock:
            deferred[r.account_id] = deferred.get(r.account_id, 0) + 1

    def one(r: Replica) -> str | None:
        acc = accs.get(r.account_id)
        if acc is None or acc.state == "removed":
            dm.event(
                "replica_abandoned",
                level="warning",
                account_id=r.account_id,
                replica_id=r.id,
                reason="account removed",
                **context,
            )
        elif is_disconnected(dm, acc):
            defer(r, f"account {acc.state}")
            return None
        else:
            try:
                _delete_remote(dm, r.account_id, r.remote_id, object_name(r.id))
            except AuthError as exc:
                dm.accounts.mark_state(r.account_id, "revoked", str(exc))
                defer(r, "access revoked")
                return None
            except BackendError as exc:
                return f"{r.id}@{r.account_id}: {type(exc).__name__}: {exc}"
        with dm.session() as s:
            s.execute(delete(Replica).where(Replica.id == r.id))
        return None

    errors = [e for e in dm.io_pool.map(one, replicas) if e]
    for account_id, n in deferred.items():
        dm.event(
            "remote_deletes_deferred",
            level="warning",
            account_id=account_id,
            objects=n,
            note="account disconnected; deleted there when it reconnects",
            **context,
        )
    return errors


def drain_remote_deletes(dm: DriveMesh, account_id: str | None = None, limit: int = 1000) -> dict:
    """Delete objects owed to accounts that are reachable again. Returns counts."""
    with dm.session() as s:
        q = (
            select(RemoteDelete, LinkedAccount)
            .join(LinkedAccount, LinkedAccount.id == RemoteDelete.account_id)
            .where(LinkedAccount.state != "removed")
            .order_by(RemoteDelete.created_at)
            .limit(limit)
        )
        if account_id is not None:
            q = q.where(RemoteDelete.account_id == account_id)
        rows = [(rd, acc) for rd, acc in s.execute(q).all() if not is_disconnected(dm, acc)]

    def one(item) -> bool:
        rd, _acc = item
        try:
            _delete_remote(dm, rd.account_id, rd.remote_id, rd.object_name)
        except AuthError as exc:
            dm.accounts.mark_state(rd.account_id, "revoked", str(exc))
            ok, err = False, str(exc)
        except BackendError as exc:
            ok, err = False, f"{type(exc).__name__}: {exc}"
        else:
            ok, err = True, None
        with dm.session() as s:
            row = s.get(RemoteDelete, rd.id)
            if row is not None:
                if ok:
                    s.delete(row)
                else:
                    row.attempts, row.last_error = row.attempts + 1, (err or "")[:2000]
        return ok

    done = sum(dm.io_pool.map(one, rows))
    if done:
        dm.event("remote_deletes_drained", objects=done)
    return {"deleted": done, "failed": len(rows) - done}


def pending_remote_deletes(dm: DriveMesh, account_ids: list[str] | None = None) -> dict[str, int]:
    from sqlalchemy import func

    with dm.session() as s:
        q = select(RemoteDelete.account_id, func.count()).group_by(RemoteDelete.account_id)
        if account_ids is not None:
            q = q.where(RemoteDelete.account_id.in_(account_ids))
        return {aid: int(n) for aid, n in s.execute(q).all()}


def _purge_file(dm: DriveMesh, file_id: str, final_state: str) -> None:
    with dm.session() as s:
        f = s.get(File, file_id)
        if f is None or f.state in ("committed", "pending"):
            return  # resurrected or still being written: never purge live data
        replicas = list(
            s.scalars(
                select(Replica)
                .join(Chunk, Chunk.id == Replica.chunk_id)
                .where(Chunk.file_id == file_id)
            )
        )
    errors = purge_replicas(dm, replicas, context={"file_id": file_id})
    if errors:
        raise RetryLater(f"{len(errors)} replica deletions failed; first: {errors[0]}")
    with dm.session() as s:
        s.execute(delete(Chunk).where(Chunk.file_id == file_id))
        f = s.get(File, file_id)
        f.state = final_state
        if final_state == "deleted" and f.deleted_at is None:
            f.deleted_at = dm.now()
    dm.event("file_purged", file_id=file_id, state=final_state, replicas=len(replicas))


@register("delete_file")
def delete_file_job(dm: DriveMesh, payload: dict[str, Any]) -> None:
    _purge_file(dm, payload["file_id"], "deleted")


@register("gc_file")
def gc_file_job(dm: DriveMesh, payload: dict[str, Any]) -> None:
    _purge_file(dm, payload["file_id"], "aborted")


def scan(dm: DriveMesh) -> dict[str, int]:
    """Periodic GC pass. Safe to run concurrently with writers."""
    now = dm.now()
    stale: list[tuple[str, str]] = []
    with dm.session() as s:
        expired = (File.state == "pending", File.lease_expires_at < now)
        for fid, uid in s.execute(select(File.id, File.user_id).where(*expired)).all():
            # Conditional abort: the writer may have committed or heartbeated since the read
            # above. An unconditional write here overwrote 'committed' (08-how-it-works §3.6).
            res = s.execute(
                update(File)
                .where(File.id == fid, *expired)
                .values(state="aborted", lease_expires_at=None)
                .execution_options(synchronize_session=False)
            )
            if res.rowcount != 1:
                continue
            enqueue(s, "gc_file", {"file_id": fid}, now=now, dedupe_key=f"gc_file:{fid}")
            stale.append((fid, uid))
    aborted = len(stale)
    with dm.session() as s:
        # Aborted files whose GC job finished/died without completing get a fresh job.
        for fid in s.scalars(select(File.id).where(File.state == "aborted")).all():
            enqueue(s, "gc_file", {"file_id": fid}, now=now, dedupe_key=f"gc_file:{fid}")
        for fid in s.scalars(select(File.id).where(File.state == "deleting")).all():
            enqueue(s, "delete_file", {"file_id": fid}, now=now, dedupe_key=f"delete_file:{fid}")
        cutoff = now - timedelta(seconds=dm.settings.pending_replica_grace_seconds)
        stale_replicas = list(
            s.scalars(
                select(Replica)
                .join(Chunk, Chunk.id == Replica.chunk_id)
                .join(File, File.id == Chunk.file_id)
                .where(
                    Replica.state.in_(("pending", "missing", "corrupt")),
                    Replica.created_at < cutoff,
                    File.state == "committed",
                )
            )
        )
    for fid, uid in stale:
        dm.event("upload_abandoned", level="warning", file_id=fid, user_id=uid)
    errors = (
        purge_replicas(dm, stale_replicas, context={"reason": "stale"}) if stale_replicas else []
    )
    drained = drain_remote_deletes(dm)
    return {
        "aborted": aborted,
        "stale_replicas": len(stale_replicas) - len(errors),
        "remote_deletes": drained["deleted"],
    }


def orphan_sweep(dm: DriveMesh, *, dry_run: bool = False) -> dict[str, int]:
    """Delete dm-* objects that no replica row references (older than orphan_grace).

    Disabled for `restore_quiet_seconds` after a metadata restore: objects of files committed
    after the snapshot look like orphans but may be the only copy of user data.
    """
    now = dm.now()
    with dm.session() as s:
        restored = s.get(KV, "restored_at")
        if restored is not None:
            from datetime import datetime

            restored_at = datetime.fromisoformat(restored.value)
            if (now - restored_at).total_seconds() < dm.settings.restore_quiet_seconds:
                return {"skipped_after_restore": 1}
        accounts = list(
            s.scalars(
                select(LinkedAccount).where(LinkedAccount.state.in_(("active", "full", "draining")))
            )
        )
        # Known = any replica's remote id, or the deterministic name of a replica whose upload
        # outcome is unknown (remote_id NULL). A duplicate object with a known *name* but an
        # unknown *id* (e.g. a retried put whose first response was lost) is an orphan.
        rows = s.execute(select(Replica.id, Replica.remote_id)).all()
        known_remote = {remote for _, remote in rows if remote}
        unresolved_names = {f"dm-{rid}" for rid, remote in rows if not remote}
    cutoff = now - timedelta(seconds=dm.settings.orphan_grace_seconds)
    found = deleted = 0
    for acc in accounts:
        backend = dm.accounts.backend(acc.id)
        try:
            objects = list(backend.list("dm-"))
        except BackendError as exc:
            log.warning("orphan sweep list failed", extra={"account_id": acc.id, "error": str(exc)})
            continue
        for obj in objects:
            if (
                obj.remote_id in known_remote
                or obj.name in unresolved_names
                or obj.name.startswith("dm-meta-")
            ):
                continue
            if obj.created_at is not None and obj.created_at > cutoff:
                continue
            found += 1
            if dry_run:
                continue
            try:
                backend.delete(obj.remote_id)
                deleted += 1
            except NotFoundError:
                pass
            except BackendError as exc:
                log.warning("orphan delete failed", extra={"account_id": acc.id, "error": str(exc)})
    if found:
        dm.event("orphans_swept", level="warning", found=found, deleted=deleted, dry_run=dry_run)
    return {"found": found, "deleted": deleted}
