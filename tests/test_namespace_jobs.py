import json
import threading
from datetime import timedelta

import pytest
from sqlalchemy import select

from drivemesh.core import namespace as ns
from drivemesh.core.jobs import HANDLERS, JobQueue, enqueue, register
from drivemesh.db import Job
from drivemesh.errors import AlreadyExists, Conflict, InvalidPath, NotFound


@pytest.mark.parametrize(
    "path", ["relative", "/a/../b", "/a/./b", "/a//b", "/nul\x00", "/back\\slash", "/" + "x" * 300]
)
def test_w13_bad_paths_rejected(path):
    with pytest.raises(InvalidPath):
        ns.parts(path)


def test_normalize():
    assert ns.normalize("/a/b/") == "/a/b"
    assert ns.split("/a/b/c.txt") == ("/a/b", "c.txt")
    with pytest.raises(InvalidPath):
        ns.split("/")


def test_mkdir_list_move(dm, user, accounts):
    dm.mkdir(user.id, "/a/b", parents=True)
    with pytest.raises(AlreadyExists):
        dm.mkdir(user.id, "/a")
    dm.put(user.id, "/a/b/f.txt", b"hi")
    folders, files = dm.listdir(user.id, "/a/b")
    assert folders == [] and [f.name for f in files] == ["f.txt"]
    dm.move(user.id, "/a/b/f.txt", "/a/g.txt")
    assert dm.get_bytes(user.id, "/a/g.txt") == b"hi"
    dm.move(user.id, "/a/b", "/c")
    assert [f.name for f in dm.listdir(user.id, "/")[0]] == ["a", "c"]
    with pytest.raises(Conflict):
        dm.move(user.id, "/a", "/a/inside")
    with pytest.raises(NotFound):
        dm.listdir(user.id, "/nope")


def test_file_and_folder_names_cannot_collide(dm, user, accounts):
    dm.mkdir(user.id, "/x")
    with pytest.raises(Conflict):
        dm.put(user.id, "/x", b"data")
    dm.put(user.id, "/y", b"data")
    with pytest.raises(Conflict):
        dm.mkdir(user.id, "/y")


def test_delete_folder_recursive(dm, user, accounts):
    dm.mkdir(user.id, "/p/q", parents=True)
    dm.put(user.id, "/p/q/1", b"one")
    dm.put(user.id, "/p/2", b"two")
    with pytest.raises(Conflict):
        dm.delete_folder(user.id, "/p")
    assert dm.delete_folder(user.id, "/p", recursive=True) == 2
    dm.jobs.run_until_idle()
    assert dm.listdir(user.id, "/") == ([], [])
    with pytest.raises(Conflict):
        dm.delete_folder(user.id, "/")


def test_users_are_isolated(dm, user, accounts):
    other = dm.create_user("intruder", "pw")
    dm.accounts.link_memory(other.id, "intruder-acct")
    dm.put(user.id, "/private", b"mine")
    with pytest.raises(NotFound):
        dm.stat(other.id, "/private")
    assert dm.listdir(other.id, "/") == ([], [])
    assert {a.id for a in dm.accounts.list(other.id)}.isdisjoint({a.id for a in accounts})


def test_authenticate(dm, user):
    assert dm.authenticate("owner", "correct horse battery staple").id == user.id
    from drivemesh.errors import Unauthorized

    for u, p in [("owner", "bad"), ("ghost", "x")]:
        with pytest.raises(Unauthorized):
            dm.authenticate(u, p)


# ---- jobs ----------------------------------------------------------------------------------------


def test_enqueue_dedupes(dm):
    with dm.session() as s:
        assert enqueue(s, "noop", {}, now=dm.now(), dedupe_key="k") is not None
        assert enqueue(s, "noop", {}, now=dm.now(), dedupe_key="k") is None


def test_claim_is_exclusive_under_contention(dm):
    with dm.session() as s:
        for i in range(20):
            enqueue(s, "noop", {"i": i}, now=dm.now())
    claimed: list[str] = []
    lock = threading.Lock()

    def worker():
        q = JobQueue(dm)
        while (job := q.claim()) is not None:
            with lock:
                claimed.append(job.id)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(claimed) == 20 and len(set(claimed)) == 20


def test_expired_lease_is_reclaimed(dm, clock):
    with dm.session() as s:
        enqueue(s, "noop", {}, now=dm.now())
    q1, q2 = JobQueue(dm), JobQueue(dm)
    job = q1.claim()
    assert job is not None and q2.claim() is None  # leased
    clock.advance(dm.settings.job_visibility_seconds + 1)  # worker 1 "crashed"
    again = q2.claim()
    assert again is not None and again.id == job.id and again.attempts == 2


def test_failures_back_off_then_go_dead(dm, clock):
    calls = []

    @register("always_fails")
    def _fails(dm_, payload):
        calls.append(payload)
        raise RuntimeError("boom")

    try:
        with dm.session() as s:
            enqueue(s, "always_fails", {"x": 1}, now=dm.now(), max_attempts=3)
        for _ in range(3):
            assert dm.jobs.run_once()
            clock.advance(400)
        with dm.session() as s:
            job = s.scalars(select(Job)).one()
            assert job.state == "dead" and "boom" in job.last_error
        assert len(calls) == 3
        assert dm.jobs.counts()["dead"] == 1
    finally:
        HANDLERS.pop("always_fails")


def test_job_run_after_respected(dm, clock):
    with dm.session() as s:
        enqueue(s, "noop", {}, now=dm.now(), delay_seconds=60)
    assert dm.jobs.claim() is None
    clock.advance(61)
    assert dm.jobs.claim() is not None
    _ = (json, timedelta)
