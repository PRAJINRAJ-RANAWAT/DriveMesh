"""Background workers: health checker (heartbeats), repair (re-replication), scrubber,
evacuation, plus the worker loop and cluster status.

Durability rules (deliberately conservative):
  * An account that stops answering is NOT immediately treated as lost: its replicas still
    count as healthy until it has been unhealthy for `dead_after_seconds`. This prevents
    repair storms on network blips.
  * Repair never marks a replica bad just because its account is dead; dead replicas simply
    stop counting. If the account comes back the chunk is over-replicated and repair trims
    the excess. A host-wide network outage therefore cannot "lose" metadata.
  * Only replicas *verified* bad (404 on read / checksum mismatch) are dropped.
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import String, and_, case, cast, func, or_, select

from drivemesh import crypto, metrics, placement
from drivemesh.backends.base import AuthError, BackendError, NotFoundError
from drivemesh.core.accounts import READABLE_STATES
from drivemesh.core.gc import purge_replicas
from drivemesh.core.jobs import RetryLater, enqueue, register
from drivemesh.core.replication import place_object
from drivemesh.db import Chunk, Event, File, Job, LinkedAccount, Replica
from drivemesh.errors import Conflict, NotFound

if TYPE_CHECKING:
    from drivemesh.core.system import DriveMesh

log = logging.getLogger(__name__)


# ---- health checker ------------------------------------------------------------------------------
def health_check(dm: DriveMesh) -> dict[str, str]:
    """Probe every linked account once ('heartbeat'). Returns {account_id: state}."""
    st = dm.settings
    out: dict[str, str] = {}
    with dm.session() as s:
        accounts = list(s.scalars(select(LinkedAccount).where(LinkedAccount.state != "removed")))
    for acc in accounts:
        now = dm.now()
        try:
            h = dm.accounts.backend(acc.id).health()
        except AuthError as exc:
            new_state, err, h = (
                ("draining" if acc.state == "draining" else "revoked"),
                str(exc),
                None,
            )
        except BackendError as exc:
            err, h = f"{type(exc).__name__}: {exc}", None
            fails = acc.consecutive_failures + 1
            new_state = (
                "unreachable"
                if fails >= st.unreachable_after_failures and acc.state != "draining"
                else acc.state
            )
        else:
            err = None
            if acc.state == "draining":
                new_state = "draining"
            elif h.free_bytes is not None and h.free_bytes < st.free_space_reserve + st.chunk_size:
                new_state = "full"
            else:
                new_state = "active"
        with dm.session() as s:
            row = s.get(LinkedAccount, acc.id)
            row.last_health_at = now
            if h is not None:
                row.capacity_bytes, row.used_bytes, row.free_bytes = (
                    h.capacity_bytes,
                    h.used_bytes,
                    h.free_bytes,
                )
                row.last_ok_at, row.consecutive_failures = now, 0
            else:
                row.consecutive_failures += 1
                row.last_error = err[:2000] if err else None
        if new_state != acc.state:
            dm.accounts.mark_state(acc.id, new_state, err)
        out[acc.id] = new_state
    return out


def _dead_cutoff(dm: DriveMesh):
    return dm.now() - timedelta(seconds=dm.settings.dead_after_seconds)


def _counts_as_healthy(cutoff):
    """SQL predicate on LinkedAccount: replicas there still count toward durability."""
    return or_(
        LinkedAccount.state.in_(READABLE_STATES),
        and_(
            LinkedAccount.state.in_(("unreachable", "revoked")),
            LinkedAccount.unhealthy_since > cutoff,
        ),
    )


# ---- repair -------------------------------------------------------------------------------------
@dataclass
class RepairResult:
    scanned: int = 0
    repaired: int = 0
    trimmed: int = 0
    failed: int = 0
    unavailable: int = 0
    details: list[str] = field(default_factory=list)


def _target(f: File) -> int:
    return (f.ec_k or 0) + (f.ec_m or 0) if f.scheme == "ec" else f.rf


def under_replicated_query(dm: DriveMesh, limit: int | None = None):
    cutoff = _dead_cutoff(dm)
    # replicas: count copies; erasure coding: count *distinct* shard indices
    unit = case((File.scheme == "ec", cast(Replica.shard_idx, String)), else_=Replica.id)
    healthy = (
        select(func.count(func.distinct(unit)))
        .join(LinkedAccount, LinkedAccount.id == Replica.account_id)
        .where(Replica.chunk_id == Chunk.id, Replica.state == "ok", _counts_as_healthy(cutoff))
        .correlate(Chunk, File)
        .scalar_subquery()
    )
    target = func.coalesce(File.ec_k + File.ec_m, File.rf)
    q = (
        select(Chunk, File, healthy.label("healthy"))
        .join(File, File.id == Chunk.file_id)
        .where(File.state == "committed", healthy < target)
        .order_by(healthy, Chunk.id)
    )
    if limit:
        q = q.limit(limit)
    return q


def under_replicated(dm: DriveMesh, limit: int | None = None) -> list[tuple[Chunk, File, int]]:
    """Chunks of committed files with fewer healthy replicas than their target,
    fewest-healthy first (HDFS priority queues, flattened)."""
    q = under_replicated_query(dm, limit)
    with dm.session() as s:
        return [(c, f, int(h)) for c, f, h in s.execute(q).all()]


def over_replicated(dm: DriveMesh, limit: int = 200) -> list[tuple[str, int, int]]:
    """(chunk_id, healthy ok replicas, target) for replica-scheme chunks above target."""
    cnt = func.count(Replica.id)
    q = (
        select(Chunk.id, cnt, File.rf)
        .join(File, File.id == Chunk.file_id)
        .join(Replica, Replica.chunk_id == Chunk.id)
        .join(LinkedAccount, LinkedAccount.id == Replica.account_id)
        .where(
            File.state == "committed",
            File.scheme == "replica",
            Replica.state == "ok",
            LinkedAccount.state.in_(("active", "full")),
        )
        .group_by(Chunk.id, File.rf)
        .having(cnt > File.rf)
        .limit(limit)
    )
    with dm.session() as s:
        return [(cid, int(n), int(rf)) for cid, n, rf in s.execute(q).all()]


def _replicas_with_state(dm: DriveMesh, chunk_id: str) -> list[tuple[Replica, str, Any]]:
    with dm.session() as s:
        return list(
            s.execute(
                select(Replica, LinkedAccount.state, LinkedAccount.unhealthy_since)
                .join(LinkedAccount, LinkedAccount.id == Replica.account_id)
                .where(Replica.chunk_id == chunk_id)
            ).all()
        )


def _fetch_verified(dm: DriveMesh, r: Replica) -> bytes | None:
    if not r.remote_id:
        return None
    try:
        data = dm.accounts.backend(r.account_id).get(r.remote_id)
    except NotFoundError:
        _set_replica_state(dm, r.id, "missing")
        return None
    except AuthError as exc:
        dm.accounts.mark_state(r.account_id, "revoked", str(exc))
        return None
    except BackendError:
        return None
    if r.sha256 and crypto.sha256_hex(data) != r.sha256:
        _set_replica_state(dm, r.id, "corrupt")
        return None
    return data


def _set_replica_state(dm: DriveMesh, replica_id: str, state: str) -> None:
    with dm.session() as s:
        r = s.get(Replica, replica_id)
        if r is not None and r.state == "ok":
            r.state = state
            dm.event(
                f"replica_{state}",
                level="warning",
                account_id=r.account_id,
                replica_id=replica_id,
                chunk_id=r.chunk_id,
                source="repair",
            )


def repair_chunk(
    dm: DriveMesh, chunk: Chunk, f: File, *, exclude_accounts: set[str] = frozenset()
) -> str:
    """Bring one chunk back to its target. Returns 'repaired' | 'unavailable' | 'failed' | 'ok'."""
    cutoff = _dead_cutoff(dm)
    rows = _replicas_with_state(dm, chunk.id)
    # Drop rows that were verified bad: the object is wrong or gone.
    bad = [r for r, _, _ in rows if r.state in ("missing", "corrupt")]
    if bad:
        purge_replicas(dm, bad, context={"chunk_id": chunk.id, "reason": "repair"})
        rows = [(r, st, us) for r, st, us in rows if r.state not in ("missing", "corrupt")]

    def healthy(r: Replica, acc_state: str, since) -> bool:
        if r.state != "ok" or r.account_id in exclude_accounts:
            return False
        if acc_state in READABLE_STATES:
            return True
        return acc_state in ("unreachable", "revoked") and since is not None and since > cutoff

    good = [r for r, st, us in rows if healthy(r, st, us)]
    readable = [r for r, st, _ in rows if r.state == "ok" and st in READABLE_STATES]
    holders = {r.account_id for r, _, _ in rows}
    with dm.session() as s:
        candidates = dm.accounts.candidates(s, f.user_id)

    if f.scheme == "ec":
        from drivemesh.core import erasure

        k, m = f.ec_k or 0, f.ec_m or 0
        have = {r.shard_idx for r in good}
        missing_idx = [i for i in range(k + m) if i not in have]
        if not missing_idx:
            return "ok"
        shards: dict[int, bytes] = {}
        for r in readable:
            if len(shards) >= k:
                break
            if r.shard_idx not in shards and (data := _fetch_verified(dm, r)) is not None:
                shards[r.shard_idx] = data
        if len(shards) < k:
            return "unavailable"
        obj = erasure.decode(shards, k, m, chunk.object_size or 0)
        if chunk.object_sha256 and crypto.sha256_hex(obj) != chunk.object_sha256:
            return "failed"
        all_shards = erasure.encode(obj, k, m)
        for i in missing_idx:
            # an old row for this shard index (e.g. on a dead account) blocks nothing: shards are
            # keyed by account, so place on any account not already holding a shard
            ranked = placement.rank(
                f"{chunk.id}:{i}:repair",
                candidates,
                exclude=holders,
                reserve=dm.settings.free_space_reserve,
                min_free=len(all_shards[i]),
            )
            res = place_object(dm, chunk.id, all_shards[i], ranked, 1, shard_idx=i)
            if not res.placed:
                return "failed"
            holders.add(res.placed[0][1])
        return "repaired"

    need = f.rf - len(good)
    if need <= 0:
        return "ok"
    obj = None
    for r in sorted(readable, key=lambda r: r.account_id in exclude_accounts):
        obj = _fetch_verified(dm, r)
        if obj is not None:
            break
    if obj is None:
        return "unavailable"
    ranked = placement.rank(
        f"{chunk.id}:repair",
        candidates,
        exclude=holders,
        reserve=dm.settings.free_space_reserve,
        min_free=len(obj),
    )
    ranked = placement.load_aware(ranked, need, lambda aid: dm.accounts.backend(aid).pending)
    res = place_object(dm, chunk.id, obj, ranked, need)
    return "repaired" if len(res.placed) == need else "failed"


def trim_excess(dm: DriveMesh, chunk_id: str, rf: int) -> int:
    """Delete replicas beyond RF (e.g. after a 'dead' account came back), fullest account first."""
    with dm.session() as s:
        rows = list(
            s.execute(
                select(Replica, LinkedAccount.free_bytes)
                .join(LinkedAccount, LinkedAccount.id == Replica.account_id)
                .where(
                    Replica.chunk_id == chunk_id,
                    Replica.state == "ok",
                    LinkedAccount.state.in_(("active", "full")),
                )
            ).all()
        )
    if len(rows) <= rf:
        return 0
    rows.sort(key=lambda t: t[1] if t[1] is not None else 1 << 62)  # least free first
    victims = [r for r, _ in rows[: len(rows) - rf]]
    errors = purge_replicas(dm, victims, context={"chunk_id": chunk_id, "reason": "excess"})
    return len(victims) - len(errors)


def repair(dm: DriveMesh, limit: int | None = None) -> RepairResult:
    res = RepairResult()
    if dm.safe_mode.active:
        return res
    todo = under_replicated(dm, limit or dm.settings.repair_batch)
    metrics.UNDER_REPLICATED.set(len(todo))

    def one(item) -> str:
        chunk, f, _healthy = item
        try:
            return repair_chunk(dm, chunk, f)
        except Exception as exc:
            log.exception("repair failed", extra={"chunk_id": chunk.id})
            res.details.append(f"{chunk.id}: {exc}")
            return "failed"

    # Chunks are independent, so repair them concurrently (bounded). The queue is ordered
    # fewest-healthy-first, so the most endangered chunks still start first.
    with ThreadPoolExecutor(dm.settings.repair_parallelism, thread_name_prefix="dm-repair") as ex:
        outcomes = list(ex.map(one, todo))
    for outcome in outcomes:
        res.scanned += 1
        if outcome == "repaired":
            res.repaired += 1
        elif outcome == "unavailable":
            res.unavailable += 1
        elif outcome == "failed":
            res.failed += 1
        metrics.REPAIRS.labels(outcome).inc()
    for chunk_id, _n, rf in over_replicated(dm):
        res.trimmed += trim_excess(dm, chunk_id, rf)
    if res.unavailable:
        dm.event("chunks_unavailable", level="critical", count=res.unavailable)
    if res.repaired or res.failed:
        dm.event("repair_pass", repaired=res.repaired, failed=res.failed, trimmed=res.trimmed)
    return res


# ---- scrubber -----------------------------------------------------------------------------------
def scrub(dm: DriveMesh, budget_bytes: int | None = None, limit: int = 100) -> dict[str, int]:
    """Verify replicas oldest-verified-first, up to a byte budget (rate limiting)."""
    if dm.safe_mode.active:
        return {}
    budget = (
        budget_bytes
        if budget_bytes is not None
        else int(dm.settings.scrub_bytes_per_second * dm.settings.scrub_interval)
    )
    with dm.session() as s:
        rows = list(
            s.scalars(
                select(Replica)
                .join(LinkedAccount, LinkedAccount.id == Replica.account_id)
                .where(Replica.state == "ok", LinkedAccount.state.in_(READABLE_STATES))
                .order_by(Replica.verified_at.asc().nulls_first(), Replica.id)
                .limit(limit)
            )
        )
    out = {"verified": 0, "missing": 0, "corrupt": 0, "bytes": 0}
    for r in rows:
        if out["bytes"] >= budget:
            break
        try:
            data = dm.accounts.backend(r.account_id).get(r.remote_id or f"dm-{r.id}")
        except NotFoundError:
            _set_replica_state(dm, r.id, "missing")
            metrics.SCRUB_ERRORS.labels("missing").inc()
            out["missing"] += 1
            continue
        except BackendError:
            continue
        out["bytes"] += len(data)
        metrics.SCRUB_BYTES.inc(len(data))
        if r.sha256 and crypto.sha256_hex(data) != r.sha256:
            _set_replica_state(dm, r.id, "corrupt")
            metrics.SCRUB_ERRORS.labels("corrupt").inc()
            out["corrupt"] += 1
            continue
        with dm.session() as s:
            row = s.get(Replica, r.id)
            if row is not None:
                row.verified_at = dm.now()
        out["verified"] += 1
    return out


# ---- evacuation (unlink) ----------------------------------------------------------------
def start_evacuation(dm: DriveMesh, user_id: str, account_id: str) -> str:
    with dm.session() as s:
        acc = s.get(LinkedAccount, account_id)
        if acc is None or acc.user_id != user_id or acc.state == "removed":
            raise NotFound("account not found")
        if acc.state == "draining":
            job = s.scalars(select(Job).where(Job.dedupe_key == f"evacuate:{account_id}")).first()
            return job.id if job else ""
        remaining = s.scalar(
            select(func.count())
            .select_from(LinkedAccount)
            .where(
                LinkedAccount.user_id == user_id,
                LinkedAccount.id != account_id,
                LinkedAccount.state.in_(("active", "full")),
            )
        )
        need = (
            s.scalar(
                select(func.max(func.coalesce(File.ec_k + File.ec_m, File.rf))).where(
                    File.user_id == user_id, File.state == "committed"
                )
            )
            or 0
        )
        if remaining < need:
            raise Conflict(
                f"unlinking would leave {remaining} accounts but stored files need {need}"
            )
        acc.state = "draining"
        job = enqueue(
            s,
            "evacuate",
            {"account_id": account_id},
            now=dm.now(),
            dedupe_key=f"evacuate:{account_id}",
            priority=50,
            max_attempts=1000,
        )
        job_id = job.id if job else ""
    dm.event("account_draining", user_id=user_id, account_id=account_id)
    return job_id


@register("evacuate")
def evacuate_job(dm: DriveMesh, payload: dict[str, Any]) -> None:
    account_id = payload["account_id"]
    with dm.session() as s:
        rows = list(
            s.execute(
                select(Chunk, File)
                .join(Replica, Replica.chunk_id == Chunk.id)
                .join(File, File.id == Chunk.file_id)
                .where(Replica.account_id == account_id)
                .limit(dm.settings.repair_batch)
            ).all()
        )
    pending = 0
    for chunk, f in rows:
        if f.state == "committed":
            outcome = repair_chunk(dm, chunk, f, exclude_accounts={account_id})
            if outcome not in ("ok", "repaired"):
                pending += 1
                continue
        mine = [r for r, _, _ in _replicas_with_state(dm, chunk.id) if r.account_id == account_id]
        if purge_replicas(dm, mine, context={"reason": "evacuate"}):
            pending += 1
    with dm.session() as s:
        left = s.scalar(
            select(func.count()).select_from(Replica).where(Replica.account_id == account_id)
        )
        if left == 0:
            acc = s.get(LinkedAccount, account_id)
            if acc.provider == "gdrive" and acc.secret_blob:
                _revoke_google(dm, acc)
            acc.state, acc.secret_blob = "removed", None
    if left == 0:
        dm.accounts.invalidate(account_id)
        dm.event("account_removed", level="warning", account_id=account_id)
        return
    raise RetryLater(f"{left} replicas still on draining account ({pending} blocked)")


def _revoke_google(dm: DriveMesh, acc: LinkedAccount) -> None:
    from drivemesh.backends import gdrive

    try:
        secret = dm.accounts.read_secret(acc) or {}
    except Exception:  # noqa: BLE001
        return
    if secret.get("refresh_token"):
        ok = gdrive.revoke_token(secret["refresh_token"])
        dm.event(
            "token_revoked" if ok else "token_revoke_failed",
            level="info" if ok else "warning",
            account_id=acc.id,
        )


# ---- status -------------------------------------------------------------------------------------
def cluster_status(dm: DriveMesh, user_id: str | None = None) -> dict[str, Any]:
    with dm.session() as s:
        aq = select(LinkedAccount).where(LinkedAccount.state != "removed")
        if user_id:
            aq = aq.where(LinkedAccount.user_id == user_id)
        accounts = list(s.scalars(aq))
        acc_ids = [a.id for a in accounts]
        stored_bytes = (
            s.scalar(
                select(func.coalesce(func.sum(Replica.size), 0)).where(
                    Replica.state == "ok", Replica.account_id.in_(acc_ids)
                )
            )
            if acc_ids
            else 0
        )
        per_acc = dict(
            s.execute(
                select(Replica.account_id, func.count())
                .where(Replica.state == "ok")
                .group_by(Replica.account_id)
            ).all()
        )
        rstates = dict(s.execute(select(Replica.state, func.count()).group_by(Replica.state)).all())
        fstates = dict(s.execute(select(File.state, func.count()).group_by(File.state)).all())
        dead_jobs = [
            {"id": j.id, "kind": j.kind, "error": j.last_error}
            for j in s.scalars(select(Job).where(Job.state == "dead").limit(20))
        ]
        events = [
            {
                "ts": e.ts,
                "kind": e.kind,
                "level": e.level,
                "account_id": e.account_id,
                "file_id": e.file_id,
            }
            for e in s.scalars(select(Event).order_by(Event.id.desc()).limit(25))
        ]
        from drivemesh.db import KV

        last_backup = s.get(KV, "last_backup_at")
    for state in ("pending", "ok", "missing", "corrupt", "deleting"):
        metrics.REPLICAS.labels(state).set(rstates.get(state, 0))
    ur = under_replicated(dm)
    return {
        "safe_mode": {"active": dm.safe_mode.active, "reason": dm.safe_mode.reason},
        "accounts": [
            {
                "id": a.id,
                "provider": a.provider,
                "name": a.display_name,
                "email": a.email,
                "state": a.state,
                "capacity_bytes": a.capacity_bytes,
                "used_bytes": a.used_bytes,
                "free_bytes": a.free_bytes,
                "replicas": per_acc.get(a.id, 0),
                "last_health_at": a.last_health_at,
                "last_error": a.last_error,
            }
            for a in accounts
        ],
        "replicas": rstates,
        "stored_bytes": int(stored_bytes or 0),  # all copies, incl. replication overhead
        "files": fstates,
        "under_replicated_chunks": len(ur),
        "unavailable_chunks": sum(1 for _, f, h in ur if h < (f.ec_k or 1)),
        "jobs": dm.jobs.counts(),
        "dead_jobs": dead_jobs,
        "last_backup_at": last_backup.value if last_backup else None,
        "recent_events": events,
    }


# ---- worker loop --------------------------------------------------------------------------------
@dataclass
class Task:
    name: str
    interval: float
    fn: Callable[[], Any]
    respects_safe_mode: bool = True
    last_run: float = 0.0


class WorkerSet:
    """Runs every periodic task in its own daemon thread. `tick()` runs each once (tests)."""

    def __init__(self, dm: DriveMesh):
        from drivemesh.core import backup, gc

        self.dm = dm
        st = dm.settings
        self.tasks = [
            Task("health", st.health_interval, lambda: self._health(), respects_safe_mode=False),
            Task("jobs", st.job_poll_interval, lambda: dm.jobs.run_until_idle(max_jobs=50)),
            Task("repair", st.repair_interval, lambda: repair(dm)),
            Task("gc", st.gc_interval, lambda: (gc.scan(dm), gc.orphan_sweep(dm))),
            Task("scrub", st.scrub_interval, lambda: scrub(dm)),
            Task("backup", st.backup_interval, lambda: backup.backup_if_changed(dm)),
        ]
        try:
            from drivemesh.core.balancer import balance

            self.tasks.append(Task("balancer", st.balancer_interval, lambda: balance(dm)))
        except ImportError:  # pragma: no cover
            pass
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._started_at = time.monotonic()

    def _health(self) -> dict[str, str]:
        states = health_check(self.dm)
        healthy = [s for s in states.values() if s in ("active", "full", "draining")]
        sm = self.dm.safe_mode
        if states and not healthy:
            if not sm.active:
                sm.enter("no reachable storage accounts")
                self.dm.event("safe_mode_entered", level="critical", reason=sm.reason)
        elif sm.active:
            sm.leave()
            self.dm.event("safe_mode_left", accounts=len(states), healthy=len(healthy))
        return states

    def tick(self) -> None:
        for t in self.tasks:
            if t.respects_safe_mode and self.dm.safe_mode.active:
                continue
            t.fn()

    def _loop(self, t: Task) -> None:
        while not self._stop.is_set():
            if not (t.respects_safe_mode and self.dm.safe_mode.active):
                try:
                    t.fn()
                except Exception:
                    log.exception("worker task failed", extra={"task": t.name})
            self._stop.wait(t.interval)

    def start(self) -> None:
        self.dm.safe_mode.enter("startup")
        metrics.SAFE_MODE.set(1)
        try:
            self._health()
        except Exception:
            log.exception("initial health check failed")
        for t in self.tasks:
            th = threading.Thread(target=self._loop, args=(t,), name=f"dm-{t.name}", daemon=True)
            th.start()
            self._threads.append(th)

    def stop(self) -> None:
        self._stop.set()
        for th in self._threads:
            th.join(timeout=5)

    def run_forever(self) -> None:
        self.start()
        stop = threading.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
        stop.wait()
        self.stop()
