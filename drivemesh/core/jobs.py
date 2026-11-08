"""Durable job queue in the metadata DB, with leases (visibility timeouts).

Semantics: at-least-once. A job is claimed with a conditional UPDATE that only one worker can
win; a crashed worker's job becomes claimable again once its lease expires. Handlers must be
idempotent. Enqueueing inside the caller's transaction gives a transactional outbox: e.g. a
file is never tombstoned without its purge job being scheduled.
"""

from __future__ import annotations

import json
import logging
import random
import socket
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from drivemesh import metrics
from drivemesh.db import Job

if TYPE_CHECKING:
    from drivemesh.core.system import DriveMesh

log = logging.getLogger(__name__)

Handler = Callable[["DriveMesh", dict[str, Any]], None]
HANDLERS: dict[str, Handler] = {}


def register(kind: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        HANDLERS[kind] = fn
        return fn

    return deco


class RetryLater(Exception):
    """Raised by handlers for an expected, retryable condition."""


def enqueue(
    s: Session,
    kind: str,
    payload: dict[str, Any],
    *,
    now,
    dedupe_key: str | None = None,
    priority: int = 100,
    delay_seconds: float = 0,
    max_attempts: int = 8,
) -> Job | None:
    """Add a job in the caller's transaction. Returns None if an identical job is pending."""
    if dedupe_key is not None:
        existing = s.scalars(select(Job).where(Job.dedupe_key == dedupe_key)).first()
        if existing is not None:
            return None
    job = Job(
        kind=kind,
        payload_json=json.dumps(payload),
        dedupe_key=dedupe_key,
        state="queued",
        priority=priority,
        run_after=now + timedelta(seconds=delay_seconds),
        max_attempts=max_attempts,
        created_at=now,
        updated_at=now,
    )
    s.add(job)
    s.flush()
    return job


class JobQueue:
    def __init__(self, dm: DriveMesh):
        self.dm = dm
        self.owner = f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"

    def claim(self, kinds: tuple[str, ...] | None = None) -> Job | None:
        now = self.dm.now()
        visibility = timedelta(seconds=self.dm.settings.job_visibility_seconds)
        with self.dm.session() as s:
            q = (
                select(Job.id)
                .where(
                    Job.state.in_(("queued", "running")),
                    Job.run_after <= now,
                    (Job.lease_expires_at.is_(None)) | (Job.lease_expires_at < now),
                )
                .order_by(Job.priority, Job.run_after)
                .limit(5)
            )
            if kinds:
                q = q.where(Job.kind.in_(kinds))
            for job_id in s.scalars(q).all():
                res = s.execute(
                    update(Job)
                    .where(
                        Job.id == job_id,
                        Job.state.in_(("queued", "running")),
                        (Job.lease_expires_at.is_(None)) | (Job.lease_expires_at < now),
                    )
                    .values(
                        state="running",
                        lease_owner=self.owner,
                        lease_expires_at=now + visibility,
                        attempts=Job.attempts + 1,
                        updated_at=now,
                    )
                )
                if res.rowcount == 1:
                    s.commit()
                    return s.get(Job, job_id)
        return None

    def _finish(self, job: Job, exc: BaseException | None) -> None:
        now = self.dm.now()
        with self.dm.session() as s:
            j = s.get(Job, job.id)
            if j is None or j.lease_owner != self.owner:
                return  # lease lost to another worker; their outcome wins
            j.lease_owner, j.lease_expires_at, j.updated_at = None, None, now
            if exc is None:
                j.state, j.dedupe_key, j.last_error = "done", None, None
                return
            j.last_error = f"{type(exc).__name__}: {exc}"[:2000]
            if j.attempts >= j.max_attempts:
                j.state, j.dedupe_key = "dead", None
                self.dm.event(
                    "job_dead", level="critical", job_id=j.id, job_kind=j.kind, error=j.last_error
                )
            else:
                j.state = "queued"
                backoff = min(300.0, 2.0**j.attempts) * random.uniform(0.5, 1.0)  # noqa: S311 - jitter
                j.run_after = now + timedelta(seconds=backoff)

    def run_once(self, kinds: tuple[str, ...] | None = None) -> bool:
        job = self.claim(kinds)
        if job is None:
            return False
        handler = HANDLERS.get(job.kind)
        try:
            if handler is None:
                raise RuntimeError(f"no handler for job kind {job.kind!r}")
            handler(self.dm, json.loads(job.payload_json))
        except Exception as exc:  # noqa: BLE001 - recorded on the job, never swallowed silently
            log.warning(
                "job failed",
                extra={
                    "job_id": job.id,
                    "kind": job.kind,
                    "attempt": job.attempts,
                    "error": str(exc)[:500],
                },
            )
            self._finish(job, exc)
        else:
            self._finish(job, None)
        return True

    def run_until_idle(self, max_jobs: int = 10_000, kinds: tuple[str, ...] | None = None) -> int:
        n = 0
        while n < max_jobs and self.run_once(kinds):
            n += 1
        return n

    def counts(self) -> dict[str, int]:
        with self.dm.session() as s:
            rows = s.execute(select(Job.state, func.count()).group_by(Job.state)).all()
        out = {state: int(c) for state, c in rows}
        for state in ("queued", "running", "done", "dead"):
            metrics.JOBS.labels(state).set(out.get(state, 0))
        return out
