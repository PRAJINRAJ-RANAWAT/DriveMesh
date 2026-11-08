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
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, select

from drivemesh.backends.base import AuthError, BackendError, NotFoundError
from drivemesh.core.jobs import RetryLater, enqueue, register
from drivemesh.db import KV, Chunk, File, LinkedAccount, Replica, object_name

if TYPE_CHECKING:
    from drivemesh.core.system import DriveMesh

log = logging.getLogger(__name__)


def delete_replica_object(dm: DriveMesh, replica: Replica) -> None:
    """Delete a replica's remote object. NotFound counts as success (idempotent)."""
    backend = dm.accounts.backend(replica.account_id)
    remote = replica.remote_id or backend.find(object_name(replica.id))
    if remote is None:
        return
    with contextlib.suppress(NotFoundError):
        backend.delete(remote)


def purge_replicas(dm: DriveMesh, replicas: list[Replica], *, context: dict[str, Any]) -> list[str]:
    """Delete replicas (remote object, then row) in parallel. Returns error strings."""
    with dm.session() as s:
        states = dict(
            s.execute(
                select(LinkedAccount.id, LinkedAccount.state).where(
                    LinkedAccount.id.in_({r.account_id for r in replicas})
                )
            ).all()
        )

    def one(r: Replica) -> str | None:
        acc_state = states.get(r.account_id, "removed")
        if acc_state == "removed":
            dm.event(
                "replica_abandoned",
                level="warning",
                account_id=r.account_id,
                replica_id=r.id,
                reason="account removed",
                **context,
            )
        else:
            try:
                delete_replica_object(dm, r)
            except AuthError as exc:
                dm.accounts.mark_state(r.account_id, "revoked", str(exc))
                if acc_state == "revoked":
                    dm.event(
                        "replica_abandoned",
                        level="warning",
                        account_id=r.account_id,
                        replica_id=r.id,
                        reason="account revoked",
                        **context,
                    )
                else:
                    return f"{r.id}: {exc}"
            except BackendError as exc:
                return f"{r.id}@{r.account_id}: {type(exc).__name__}: {exc}"
        with dm.session() as s:
            s.execute(delete(Replica).where(Replica.id == r.id))
        return None

    errors = [e for e in dm.io_pool.map(one, replicas) if e]
    return errors


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
    aborted = 0
    with dm.session() as s:
        stale = list(
            s.scalars(select(File).where(File.state == "pending", File.lease_expires_at < now))
        )
        for f in stale:
            f.state, f.lease_expires_at = "aborted", None
            enqueue(s, "gc_file", {"file_id": f.id}, now=now, dedupe_key=f"gc_file:{f.id}")
            aborted += 1
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
    for f in stale:
        dm.event("upload_abandoned", level="warning", file_id=f.id, user_id=f.user_id)
    errors = (
        purge_replicas(dm, stale_replicas, context={"reason": "stale"}) if stale_replicas else []
    )
    return {"aborted": aborted, "stale_replicas": len(stale_replicas) - len(errors)}


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
        known_ids = {f"dm-{rid}" for rid in s.scalars(select(Replica.id)).all()}
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
            if obj.name in known_ids or obj.name.startswith("dm-meta-"):
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
