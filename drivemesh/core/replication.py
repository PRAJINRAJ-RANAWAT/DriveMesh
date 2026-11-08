"""Writing one encrypted object (chunk or shard) to N accounts, with fallback candidates.

Shared by the writer, the repair worker, the balancer and evacuation.

Protocol per replica:
  1. INSERT replica(state=pending)   -> its id fixes the remote name 'dm-<id>'
  2. backend.put('dm-<id>', obj)      (retried with backoff by ResilientBackend)
  3. UPDATE replica -> ok(remote_id, size, sha256)
A crash between 2 and 3 leaves a pending row whose object GC can find by name.
"""

from __future__ import annotations

import logging
from concurrent.futures import FIRST_COMPLETED, Future, wait
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from drivemesh.backends.base import AuthError, BackendError, QuotaFullError
from drivemesh.crypto import sha256_hex
from drivemesh.db import Replica, new_id, object_name
from drivemesh.placement import Candidate

if TYPE_CHECKING:
    from drivemesh.core.system import DriveMesh

log = logging.getLogger(__name__)


@dataclass
class PlaceResult:
    placed: list[tuple[str, str]] = field(default_factory=list)  # (replica_id, account_id)
    failed_accounts: dict[str, str] = field(default_factory=dict)


def handle_account_error(dm: DriveMesh, account_id: str, exc: BaseException) -> None:
    if isinstance(exc, QuotaFullError):
        dm.accounts.mark_state(account_id, "full", str(exc))
    elif isinstance(exc, AuthError):
        dm.accounts.mark_state(account_id, "revoked", str(exc))


def place_object(
    dm: DriveMesh,
    chunk_id: str,
    obj: bytes,
    ranked: list[Candidate],
    need: int,
    *,
    shard_idx: int = 0,
) -> PlaceResult:
    """Store `obj` on `need` distinct accounts taken in order from `ranked`.

    Uploads run in parallel on dm.io_pool; when a target fails permanently (after retries)
    the next candidate is tried. Returns what was placed; the caller decides if it's enough.
    """
    sha = sha256_hex(obj)
    result = PlaceResult()
    candidates = iter(ranked)
    inflight: dict[Future, tuple[str, str]] = {}

    def launch() -> bool:
        acc = next(candidates, None)
        if acc is None:
            return False
        rid = new_id()
        with dm.session() as s:
            s.add(
                Replica(
                    id=rid,
                    chunk_id=chunk_id,
                    account_id=acc.id,
                    shard_idx=shard_idx,
                    state="pending",
                    created_at=dm.now(),
                )
            )
        backend = dm.accounts.backend(acc.id)
        inflight[dm.io_pool.submit(backend.put, object_name(rid), obj)] = (rid, acc.id)
        return True

    while len(result.placed) < need:
        while len(result.placed) + len(inflight) < need:
            if not launch():
                break
        if not inflight:
            break
        done, _ = wait(list(inflight), return_when=FIRST_COMPLETED)
        for fut in done:
            rid, acc_id = inflight.pop(fut)
            try:
                put = fut.result()
            except BackendError as exc:
                # Leave the replica row 'pending': GC deletes any partial object by name.
                result.failed_accounts[acc_id] = f"{type(exc).__name__}: {exc}"
                handle_account_error(dm, acc_id, exc)
                log.warning(
                    "replica put failed",
                    extra={"account_id": acc_id, "chunk_id": chunk_id, "error": str(exc)[:300]},
                )
                continue
            with dm.session() as s:
                r = s.get(Replica, rid)
                r.remote_id, r.size, r.sha256 = put.remote_id, put.size, sha
                r.state, r.verified_at = "ok", dm.now()
            result.placed.append((rid, acc_id))
    return result
