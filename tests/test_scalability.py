"""Phase 3: erasure coding, S3 backend, balancer, metadata at scale."""

import random

import pytest
from sqlalchemy import func, select

from drivemesh.backends.faulty import FaultyBackend
from drivemesh.core import erasure
from drivemesh.core.balancer import balance
from drivemesh.core.fsck import fsck
from drivemesh.core.workers import health_check, repair, under_replicated
from drivemesh.db import Replica
from drivemesh.errors import InsufficientAccounts, InvalidRequest
from tests.conftest import CHUNK, payload


@pytest.mark.parametrize("k,m", [(1, 1), (2, 1), (4, 2), (6, 3)])
def test_erasure_any_k_of_n_reconstructs(k, m):
    rng = random.Random(k * 10 + m)
    for size in (0, 1, 1000, 65_537):
        obj = rng.randbytes(size)
        shards = erasure.encode(obj, k, m)
        assert len(shards) == k + m
        for _ in range(5):
            keep = rng.sample(range(k + m), k)
            assert erasure.decode({i: shards[i] for i in keep}, k, m, size) == obj


def test_erasure_rejects_bad_params():
    with pytest.raises(InvalidRequest):
        erasure.encode(b"x", 0, 1)
    with pytest.raises(InvalidRequest):
        erasure.decode({0: b"x"}, 2, 1, 1)


@pytest.fixture
def six(dm, user):
    return [dm.accounts.link_memory(user.id, f"ec{i}") for i in range(6)]


def test_ec_file_survives_m_losses_and_repairs(dm, user, six, clock):
    data = payload(5 * CHUNK + 3)
    info = dm.put(user.id, "/ec.bin", data, scheme="ec", ec_k=4, ec_m=2)
    assert info.scheme == "ec"
    with dm.session() as s:
        n = s.scalar(select(func.count()).select_from(Replica))
        stored = s.scalar(select(func.sum(Replica.size)))
    assert n == 6 * info.chunks  # one shard per account per chunk
    assert stored / len(data) < 1.6  # ~1.5x overhead (vs 2.0x for RF=2)
    dead = []
    for a in six[:2]:

        def wrap(b, _d=dead):
            fb = FaultyBackend(b, sleep=lambda s: None)
            fb.dead = True
            _d.append(fb)
            return fb

        dm.accounts.wrap(a.id, wrap)
    assert dm.get_bytes(user.id, "/ec.bin") == data  # any 4 of 6 shards suffice
    for _ in range(3):
        health_check(dm)
    clock.advance(dm.settings.dead_after_seconds + 1)
    dm.accounts.link_memory(user.id, "spare1")
    dm.accounts.link_memory(user.id, "spare2")
    health_check(dm)
    res = repair(dm)
    assert res.repaired == info.chunks and res.unavailable == 0
    assert under_replicated(dm) == []
    assert dm.get_bytes(user.id, "/ec.bin") == data


def test_ec_needs_k_plus_m_accounts(dm, user, accounts):
    with pytest.raises(InsufficientAccounts):
        dm.put(user.id, "/x", b"data", scheme="ec", ec_k=4, ec_m=2)


def test_ec_three_losses_is_unavailable_not_wrong(dm, user, six):
    data = payload(2 * CHUNK)
    dm.put(user.id, "/ec", data, scheme="ec", ec_k=4, ec_m=2)
    for a in six[:3]:
        dm.accounts.wrap(a.id, lambda b: FaultyBackend(b, error_rate=1.0, sleep=lambda s: None))
    from drivemesh.errors import ChunkUnavailable

    with pytest.raises(ChunkUnavailable):
        dm.get_bytes(user.id, "/ec")


def test_balancer_moves_data_to_new_account(dm, user):
    a = dm.accounts.link_memory(user.id, "old", capacity_bytes=200 * CHUNK)
    files = {}
    for i in range(10):
        files[f"/f{i}"] = payload(4 * CHUNK, seed=i)
        dm.put(user.id, f"/f{i}", files[f"/f{i}"], rf=1)
    b = dm.accounts.link_memory(user.id, "new", capacity_bytes=200 * CHUNK)
    health_check(dm)
    before = dm.accounts.get(a.id).used_bytes
    out = balance(dm, max_moves=1000)
    assert out["moved"] > 0
    health_check(dm)
    ua, ub = dm.accounts.get(a.id).used_bytes, dm.accounts.get(b.id).used_bytes
    assert ua < before and ub > 0
    assert abs(ua - ub) / (200 * CHUNK) <= dm.settings.balancer_threshold + 0.05
    for p, d in files.items():
        assert dm.get_bytes(user.id, p) == d
    assert fsck(dm, deep=True).clean
    assert balance(dm)["moved"] == 0  # balanced -> no-op


def test_s3_backend_in_full_system(dm, user):
    import boto3
    from moto import mock_aws

    from drivemesh.backends.s3 import S3Backend

    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="pool")
        orig = dm.accounts.factory

        def factory(acc, secret):
            if acc.provider == "s3":
                return S3Backend(client, "pool", prefix=f"{acc.external_id}/")
            return orig(acc, secret)

        dm.accounts.factory = factory
        dm.accounts.link(
            user.id,
            "s3",
            "bucket-a",
            "s3a",
            config={"bucket": "pool"},
            secret={"access_key_id": "x", "secret_access_key": "y"},
        )
        dm.accounts.link_memory(user.id, "mem")
        data = payload(3 * CHUNK)
        dm.put(user.id, "/mixed", data)
        with dm.session() as s:
            providers = {r.account_id for r in s.scalars(select(Replica))}
        assert len(providers) == 2  # provider diversity: one copy per provider
        assert dm.get_bytes(user.id, "/mixed") == data


def test_s3_error_mapping():
    from botocore.exceptions import ClientError, EndpointConnectionError

    from drivemesh.backends.base import AuthError, NotFoundError, TransientError
    from drivemesh.backends.s3 import map_error

    def ce(code):
        return ClientError({"Error": {"Code": code}}, "op")

    assert isinstance(map_error(ce("NoSuchKey")), NotFoundError)
    assert isinstance(map_error(ce("SlowDown")), TransientError)
    assert isinstance(map_error(ce("AccessDenied")), AuthError)
    assert isinstance(map_error(EndpointConnectionError(endpoint_url="x")), TransientError)


def test_metadata_queries_at_scale(dm, user, accounts):
    """10k chunks x 2 replicas inserted directly; repair scan and fsck stay correct."""
    from drivemesh import crypto
    from drivemesh.core import namespace as ns
    from drivemesh.db import Chunk, File, new_id

    with dm.session() as s:
        root = ns.root_folder(s, user.id)
        for fi in range(100):
            fid = new_id()
            s.add(
                File(
                    id=fid,
                    user_id=user.id,
                    folder_id=root.id,
                    name=f"syn{fi}",
                    version=1,
                    state="committed",
                    size=100 * CHUNK,
                    sha256="0" * 64,
                    chunk_size=CHUNK,
                    chunk_count=100,
                    rf=2,
                    wrapped_dek=b"x",
                    kek_id="k1",
                )
            )
            s.flush()
            cids = []
            for ci in range(100):
                cid = new_id()
                s.add(Chunk(id=cid, file_id=fid, idx=ci, plain_size=CHUNK))
                cids.append(cid)
            s.flush()
            for ci, cid in enumerate(cids):
                for a in accounts[:2] if ci % 50 else accounts[:1]:  # 2 per file under-replicated
                    s.add(
                        Replica(
                            chunk_id=cid, account_id=a.id, remote_id="r", sha256="s", state="ok"
                        )
                    )
    ur = under_replicated(dm)
    assert len(ur) == 200 and all(h == 1 for _, _, h in ur)
    rep = fsck(dm)
    assert rep.chunks == 10_000 and rep.under_replicated == 200
    _ = crypto
