"""Metadata backup and restore: the 'standby NameNode' of DriveMesh (ADR-015).

A snapshot is a logical, portable dump of every table (works SQLite <-> Postgres), gzip'd and
sealed with the master key (AES-GCM, context 'meta'), stored as `dm-meta-<ts>` on every
reachable account. Restoring needs the master key and access to any one account.

RPO = backup interval. After a restore the orphan sweep is paused (restore_quiet_seconds) so
objects of files committed after the snapshot are not mistaken for garbage.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select

from drivemesh import crypto
from drivemesh.backends.base import BackendError, StorageBackend
from drivemesh.backends.local import LocalDiskBackend
from drivemesh.db import (
    KV,
    Base,
    Chunk,
    Event,
    File,
    Folder,
    Job,
    LinkedAccount,
    Replica,
    User,
)
from drivemesh.errors import Conflict, NotFound

if TYPE_CHECKING:
    from drivemesh.core.system import DriveMesh

log = logging.getLogger(__name__)
SNAPSHOT_PREFIX = "dm-meta-"
FORMAT = 1
# Insert order respects foreign keys.
TABLES = [User, LinkedAccount, Folder, File, Chunk, Replica, Job, KV]


def _encode(v: Any) -> Any:
    if isinstance(v, bytes):
        return {"__b64__": base64.b64encode(v).decode()}
    if isinstance(v, datetime):
        return {"__dt__": v.isoformat()}
    return v


def _decode(v: Any) -> Any:
    if isinstance(v, dict):
        if "__b64__" in v:
            return base64.b64decode(v["__b64__"])
        if "__dt__" in v:
            return datetime.fromisoformat(v["__dt__"])
    return v


def dump(dm: DriveMesh) -> dict[str, Any]:
    out: dict[str, Any] = {"format": FORMAT, "created_at": dm.now().isoformat(), "tables": {}}
    with dm.session() as s:
        for model in TABLES:
            cols = [c.name for c in model.__table__.columns]
            rows = []
            for obj in s.scalars(select(model)):
                rows.append({c: _encode(getattr(obj, c)) for c in cols})
            if model is KV:
                rows = [r for r in rows if r["key"] not in ("last_backup_at", "last_backup_hash")]
            out["tables"][model.__tablename__] = rows
    return out


def _content_hash(snapshot: dict[str, Any]) -> str:
    tables = {k: v for k, v in snapshot["tables"].items() if k != "jobs"}
    return hashlib.sha256(json.dumps(tables, sort_keys=True).encode()).hexdigest()


def seal_snapshot(dm: DriveMesh, snapshot: dict[str, Any]) -> bytes:
    raw = gzip.compress(json.dumps(snapshot, sort_keys=True).encode(), mtime=0)
    return crypto.seal(dm.keyring, raw, "meta")


def open_snapshot(dm: DriveMesh, blob: bytes) -> dict[str, Any]:
    return json.loads(gzip.decompress(crypto.unseal(dm.keyring, blob, "meta")))


def _set_kv(dm: DriveMesh, key: str, value: str) -> None:
    with dm.session() as s:
        row = s.get(KV, key)
        if row is None:
            s.add(KV(key=key, value=value))
        else:
            row.value = value


def backup_now(dm: DriveMesh, *, snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    snap = snapshot or dump(dm)
    blob = seal_snapshot(dm, snap)
    stamp = dm.now().strftime("%Y%m%dT%H%M%S%f")
    name = f"{SNAPSHOT_PREFIX}{stamp}"
    with dm.session() as s:
        accounts = list(
            s.scalars(select(LinkedAccount).where(LinkedAccount.state.in_(("active", "full"))))
        )
    stored, failed = [], {}
    for acc in accounts:
        backend = dm.accounts.backend(acc.id)
        try:
            backend.put(name, blob)
            stored.append(acc.id)
            _prune(backend, dm.settings.backup_keep)
        except BackendError as exc:
            failed[acc.id] = str(exc)
    if stored:
        _set_kv(dm, "last_backup_at", dm.now().isoformat())
        _set_kv(dm, "last_backup_hash", _content_hash(snap))
    dm.event(
        "metadata_backup",
        level="info" if stored else "error",
        name=name,
        accounts=len(stored),
        failed=len(failed),
        bytes=len(blob),
    )
    return {"name": name, "bytes": len(blob), "stored_on": stored, "failed": failed}


def backup_if_changed(dm: DriveMesh) -> dict[str, Any] | None:
    with dm.session() as s:
        if not s.scalar(
            select(func.count())
            .select_from(LinkedAccount)
            .where(LinkedAccount.state.in_(("active", "full")))
        ):
            return None  # nowhere to store a snapshot yet
    snap = dump(dm)
    with dm.session() as s:
        last = s.get(KV, "last_backup_hash")
    if last is not None and last.value == _content_hash(snap):
        return None
    return backup_now(dm, snapshot=snap)


def _prune(backend: StorageBackend, keep: int) -> None:
    snaps = sorted((o for o in backend.list(SNAPSHOT_PREFIX)), key=lambda o: o.name)
    for o in snaps[:-keep] if keep > 0 else []:
        try:
            backend.delete(o.remote_id)
        except BackendError:
            log.warning("could not prune snapshot", extra={"object_name": o.name})


def latest_snapshot(backend: StorageBackend) -> bytes:
    snaps = sorted(backend.list(SNAPSHOT_PREFIX), key=lambda o: o.name)
    if not snaps:
        raise NotFound("no metadata snapshot found on this account")
    return backend.get(snaps[-1].remote_id)


def restore(dm: DriveMesh, blob: bytes) -> dict[str, Any]:
    snap = open_snapshot(dm, blob)
    if snap.get("format") != FORMAT:
        raise Conflict(f"unsupported snapshot format {snap.get('format')}")
    Base.metadata.create_all(dm.engine)
    with dm.session() as s:
        if s.scalar(select(func.count()).select_from(User)):
            raise Conflict("metadata DB is not empty; restore only into an empty database")
        counts = {}
        for model in TABLES:
            rows = snap["tables"].get(model.__tablename__, [])
            for r in rows:
                obj = model(**{k: _decode(v) for k, v in r.items()})
                if model is Job and obj.state == "running":
                    obj.state, obj.lease_owner, obj.lease_expires_at = "queued", None, None
                s.add(obj)
            s.flush()
            counts[model.__tablename__] = len(rows)
    _set_kv(dm, "restored_at", dm.now().isoformat())
    _set_kv(dm, "restored_from", snap["created_at"])
    dm.event("metadata_restored", level="warning", snapshot_created_at=snap["created_at"], **counts)
    return {"snapshot_created_at": snap["created_at"], "rows": counts}


def restore_from_local_dir(dm: DriveMesh, path: Path) -> dict[str, Any]:
    return restore(dm, latest_snapshot(LocalDiskBackend(path)))


def restore_from_backend(dm: DriveMesh, backend: StorageBackend) -> dict[str, Any]:
    return restore(dm, latest_snapshot(backend))


__all__ = ["Event", "backup_now", "dump", "restore"]
