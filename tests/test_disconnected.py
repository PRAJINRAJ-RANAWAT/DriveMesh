"""Deleting and unlinking while a storage account is disconnected, and where small files'
copies go.

A disconnected account (unreachable, or its access revoked) can't delete anything right now.
DriveMesh must still finish the user's delete immediately, remember what that account owes,
and delete it there as soon as the account reconnects, without losing track of it.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from drivemesh.backends.faulty import FaultyBackend
from drivemesh.backends.local import MemoryBackend
from drivemesh.core import gc
from drivemesh.core.fsck import fsck
from drivemesh.core.workers import health_check, start_evacuation
from drivemesh.db import Event, File, LinkedAccount, Replica
from drivemesh.errors import InsufficientAccounts
from tests.conftest import CHUNK, payload


def chunk_objects(acc) -> list[str]:
    be = MemoryBackend._instances.get(acc.external_id)
    return [n for n in (be.objects if be else {}) if not n.startswith("dm-meta-")]


def faulty(dm, accounts) -> dict[str, FaultyBackend]:
    for a in accounts:
        dm.accounts.wrap(a.id, lambda inner: FaultyBackend(inner, sleep=lambda s: None))
    return {a.id: dm.accounts.backend(a.id).inner for a in accounts}


def disconnect(dm, fb: FaultyBackend, *, revoked: bool = False) -> None:
    fb.dead, fb.dead_auth = True, revoked
    for _ in range(dm.settings.unreachable_after_failures):
        health_check(dm)


def reconnect(dm, fb: FaultyBackend) -> None:
    fb.dead = fb.dead_auth = False
    health_check(dm)


def holder_with_most_copies(dm, accounts):
    with dm.session() as s:
        counts = dict(
            s.execute(select(Replica.account_id, func.count()).group_by(Replica.account_id)).all()
        )
    return max(accounts, key=lambda a: counts.get(a.id, 0))


def pending_deletes(dm, account_id: str) -> int:
    from drivemesh.db import RemoteDelete

    with dm.session() as s:
        return s.scalar(
            select(func.count())
            .select_from(RemoteDelete)
            .where(RemoteDelete.account_id == account_id)
        )


# ---- deleting removes the copies from every linked account ----------
def test_folder_delete_removes_every_copy_from_every_account(dm, user, accounts):
    dm.mkdir(user.id, "/trip/raw", parents=True)
    dm.put(user.id, "/trip/raw/a.jpg", payload(3 * CHUNK))
    dm.put(user.id, "/trip/b.txt", b"tiny")
    assert sum(len(chunk_objects(a)) for a in accounts) == (3 + 1) * 2
    dm.delete_folder(user.id, "/trip", recursive=True)
    dm.jobs.run_until_idle()
    assert sum(len(chunk_objects(a)) for a in accounts) == 0


# ---- small files: two copies, two different accounts ---------------------------------------------
def test_small_file_copies_land_on_two_different_accounts(dm, user, accounts):
    dm.put(user.id, "/note.txt", b"hi")
    with dm.session() as s:
        holders = list(s.scalars(select(Replica.account_id)))
    assert len(holders) == 2 and len(set(holders)) == 2  # never both copies on one account


def test_upload_refused_with_a_clear_reason_when_too_few_accounts_are_connected(dm, user, accounts):
    fbs = faulty(dm, accounts)
    disconnect(dm, fbs[accounts[1].id])
    disconnect(dm, fbs[accounts[2].id])
    with pytest.raises(InsufficientAccounts) as err:
        dm.put(user.id, "/note.txt", b"hi")
    msg = str(err.value)
    assert "acct-b (unreachable)" in msg and "acct-c (unreachable)" in msg
    assert "Reconnect" in msg
    with dm.session() as s:  # nothing was stored on a single account "for now"
        assert s.scalar(select(func.count()).select_from(Replica)) == 0


# ---- deleting while an account is disconnected ----------
@pytest.mark.parametrize("revoked", [False, True])
def test_delete_finishes_now_and_cleans_the_disconnected_account_on_reconnect(
    dm, user, accounts, revoked
):
    fbs = faulty(dm, accounts)
    dm.put(user.id, "/video.mkv", payload(4 * CHUNK))
    victim = holder_with_most_copies(dm, accounts)
    stranded = len(chunk_objects(victim))
    assert stranded > 0
    disconnect(dm, fbs[victim.id], revoked=revoked)

    dm.delete_file(user.id, "/video.mkv")
    dm.jobs.run_until_idle()
    with dm.session() as s:
        assert s.scalars(select(File.state)).one() == "deleted"  # done for the user, now
    for a in accounts:
        if a.id != victim.id:
            assert chunk_objects(a) == []  # reachable accounts are clean
    assert len(chunk_objects(victim)) == stranded  # can't be reached yet...
    assert pending_deletes(dm, victim.id) == stranded  # ...but nothing is forgotten
    assert dm.accounts.get(victim.id).state == ("revoked" if revoked else "unreachable")

    gc.scan(dm)  # still disconnected: nothing to do yet, nothing lost
    assert pending_deletes(dm, victim.id) == stranded

    if revoked:  # access is restored by linking the same account again
        dm.accounts.link_memory(user.id, victim.external_id)
    reconnect(dm, fbs[victim.id])
    assert dm.accounts.get(victim.id).state == "active"
    gc.scan(dm)
    assert chunk_objects(victim) == []
    assert pending_deletes(dm, victim.id) == 0


# ---- unlinking an account that is already disconnected ----------
def test_unlinking_a_disconnected_account_completes_without_losing_files(dm, user, accounts, clock):
    fbs = faulty(dm, accounts)
    data = payload(3 * CHUNK)
    dm.put(user.id, "/keep.bin", data)
    victim = holder_with_most_copies(dm, accounts)
    disconnect(dm, fbs[victim.id])

    start_evacuation(dm, user.id, victim.id)
    for _ in range(5):  # evacuation may retry once while copies are being re-made
        dm.jobs.run_until_idle()
        clock.advance(400)
    assert dm.accounts.get(victim.id).state == "removed"
    assert dm.get_bytes(user.id, "/keep.bin") == data  # every chunk still has 2 copies
    rep = fsck(dm)
    assert rep.clean and rep.under_replicated == 0
    assert pending_deletes(dm, victim.id) == 0
    with dm.session() as s:
        ev = s.scalars(select(Event).where(Event.kind == "objects_left_on_unlinked_account")).one()
        assert ev.account_id == victim.id
        acc = s.get(LinkedAccount, victim.id)
        assert acc.secret_blob is None


def test_accounts_page_shows_what_a_disconnected_account_still_owes(dm, user, accounts):
    from fastapi.testclient import TestClient

    from drivemesh.web.app import create_app
    from tests.test_web import login

    fbs = faulty(dm, accounts)
    dm.put(user.id, "/x.bin", payload(2 * CHUNK))
    victim = holder_with_most_copies(dm, accounts)
    disconnect(dm, fbs[victim.id])
    dm.delete_file(user.id, "/x.bin")
    dm.jobs.run_until_idle()
    with TestClient(create_app(dm, start_workers=False)) as c:
        login(c)
        rows = {a["id"]: a for a in c.get("/api/v1/accounts").json()["accounts"]}
    assert rows[victim.id]["state"] == "unreachable"
    assert rows[victim.id]["pending_deletes"] == pending_deletes(dm, victim.id) > 0
    assert all(r["pending_deletes"] == 0 for k, r in rows.items() if k != victim.id)
