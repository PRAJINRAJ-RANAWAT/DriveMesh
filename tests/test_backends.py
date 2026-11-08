"""Backend contract tests: every StorageBackend must pass these."""

import pytest

from drivemesh.backends.base import NotFoundError, PermanentError, QuotaFullError, StorageBackend
from drivemesh.backends.faulty import FaultyBackend
from drivemesh.backends.local import LocalDiskBackend, MemoryBackend


@pytest.fixture(params=["local", "memory", "s3", "gdrive"])
def backend(request, tmp_path) -> StorageBackend:
    if request.param == "local":
        return LocalDiskBackend(tmp_path / "store")
    if request.param == "s3":
        import boto3
        from moto import mock_aws

        from drivemesh.backends.s3 import S3Backend

        m = mock_aws()
        m.start()
        request.addfinalizer(m.stop)
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="dm-test")
        return S3Backend(client, "dm-test", prefix="pool/")
    if request.param == "gdrive":
        from drivemesh.backends.gdrive import GoogleDriveBackend
        from tests.fakes.fake_drive import FakeDrive

        return GoogleDriveBackend(FakeDrive().service, upload_chunk=256 * 1024)
    return MemoryBackend()


def test_contract_put_get_delete(backend):
    assert isinstance(backend, StorageBackend)
    res = backend.put("dm-abc", b"payload")
    assert res.size == 7
    assert backend.get(res.remote_id) == b"payload"
    assert backend.find("dm-abc") == res.remote_id
    assert [o.name for o in backend.list("dm-")] == ["dm-abc"]
    backend.delete(res.remote_id)
    assert backend.find("dm-abc") is None
    with pytest.raises(NotFoundError):
        backend.get(res.remote_id)
    with pytest.raises(NotFoundError):
        backend.delete(res.remote_id)


def test_contract_overwrite_same_name(backend):
    backend.put("dm-x", b"one")
    rid = backend.put("dm-x", b"two").remote_id
    assert backend.get(rid) == b"two"


def test_contract_health(backend):
    h = backend.health()
    assert h.used_bytes is not None and h.used_bytes >= 0


@pytest.mark.parametrize("name", ["../escape", "a/b", ".hidden", "", "x" * 300])
@pytest.mark.parametrize("kind", ["local", "memory"])
def test_unsafe_names_rejected(kind, name, tmp_path):
    backend = LocalDiskBackend(tmp_path) if kind == "local" else MemoryBackend()
    with pytest.raises(PermanentError):
        backend.put(name, b"x")


@pytest.mark.parametrize("kind", ["local", "memory"])
def test_capacity_limit(kind, tmp_path):
    b = LocalDiskBackend(tmp_path, capacity_bytes=10) if kind == "local" else MemoryBackend(10)
    b.put("dm-1", b"12345678")
    with pytest.raises(QuotaFullError):
        b.put("dm-2", b"12345")
    assert b.health().free_bytes == 2


def test_local_writes_are_atomic(tmp_path):
    b = LocalDiskBackend(tmp_path)
    b.put("dm-a", b"x" * 1000)
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".tmp")]


def test_faulty_backend_injection():
    inner = MemoryBackend()
    f = FaultyBackend(inner, sleep=lambda s: None)
    f.put("dm-1", b"abc")
    f.corrupt_reads = True
    assert f.get("dm-1") != b"abc"
    f.dead = True
    from drivemesh.backends.base import TransientError

    with pytest.raises(TransientError):
        f.get("dm-1")


def test_shared_link_serializes_bandwidth():
    import threading
    import time

    from drivemesh.backends.faulty import SharedLink

    link = SharedLink(bps=1_000_000)  # 1 MB/s
    t0 = time.monotonic()
    threads = [threading.Thread(target=link.transfer, args=(100_000,)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.monotonic() - t0
    assert 0.38 < elapsed < 0.7  # 4 x 0.1 s serialized, not 0.1 s in parallel
