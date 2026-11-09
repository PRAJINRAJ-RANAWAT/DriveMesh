"""GoogleDriveBackend against the real googleapiclient talking to an in-memory fake Drive."""

import pytest

from drivemesh.backends.base import (
    AuthError,
    NotFoundError,
    PermanentError,
    QuotaFullError,
    TransientError,
)
from drivemesh.backends.gdrive import GoogleDriveBackend, map_error
from drivemesh.resilience import ResilientBackend, RetryPolicy
from tests.fakes.fake_drive import FakeDrive


@pytest.fixture
def drive():
    return FakeDrive(quota_limit=10 * 1024 * 1024)


@pytest.fixture
def backend(drive):
    return GoogleDriveBackend(drive.service, upload_chunk=256 * 1024)


def test_put_get_delete_roundtrip(drive, backend):
    data = bytes(range(256)) * 4000  # ~1 MiB -> several resumable requests
    res = backend.put("dm-abc", data)
    assert res.size == len(data)
    assert backend.get(res.remote_id) == data
    assert backend.find("dm-abc") == res.remote_id
    assert [o.name for o in backend.list()] == ["dm-abc"]
    backend.delete(res.remote_id)
    with pytest.raises(NotFoundError):
        backend.get(res.remote_id)
    with pytest.raises(NotFoundError):
        backend.delete(res.remote_id)


def test_objects_live_in_app_folder_with_opaque_names(drive, backend):
    backend.put("dm-1", b"x")
    folder = [f for f in drive.files.values() if f["mimeType"].endswith("folder")]
    assert [f["name"] for f in folder] == ["DriveMesh"]
    chunk = next(f for f in drive.files.values() if f["name"] == "dm-1")
    assert chunk["parents"] == [backend.folder_id]


def test_existing_app_folder_is_reused(drive):
    b1 = GoogleDriveBackend(drive.service)
    b1.put("dm-1", b"x")
    b2 = GoogleDriveBackend(drive.service)
    b2.put("dm-2", b"y")
    assert b1.folder_id == b2.folder_id


def test_health_reports_quota(drive, backend):
    backend.put("dm-1", b"12345")
    h = backend.health()
    assert h.capacity_bytes == 10 * 1024 * 1024 and h.used_bytes == 5
    assert h.free_bytes == 10 * 1024 * 1024 - 5


def test_quota_exceeded_maps_to_quota_full(drive, backend):
    with pytest.raises(QuotaFullError):
        backend.put("dm-big", b"x" * (11 * 1024 * 1024))


@pytest.mark.parametrize(
    "status,reason,expected",
    [
        (503, "backendError", TransientError),
        (500, "", TransientError),
        (429, "rateLimitExceeded", TransientError),
        (403, "userRateLimitExceeded", TransientError),
        (403, "storageQuotaExceeded", QuotaFullError),
        (401, "authError", AuthError),
        (403, "insufficientPermissions", AuthError),
        (404, "notFound", NotFoundError),
        (400, "badRequest", PermanentError),
    ],
)
def test_http_error_mapping(drive, backend, status, reason, expected):
    backend.put("dm-1", b"x")  # creates folder first
    drive.inject(status, reason)
    with pytest.raises(expected):
        backend.find("dm-1")


def test_resumable_upload_resumes_after_transient_error(drive, backend):
    backend.ensure_folder()
    data = b"z" * (1024 * 1024)
    drive.inject(503, "backendError")  # fails the first chunk PUT? (first call = session start)
    # first request (session start) fails -> transient surfaces; ResilientBackend retries
    rb = ResilientBackend(
        backend, account_id="a", policy=RetryPolicy(3, 0, 0), sleep=lambda s: None
    )
    res = rb.put("dm-r", data)
    assert backend.get(res.remote_id) == data


def test_mid_upload_failure_resumes_same_session(drive, backend):
    backend.ensure_folder()
    data = b"q" * (1024 * 1024)

    original = drive.http

    class FlakyHttp:
        def __init__(self):
            self.inner = original()
            self.puts = 0

        def request(self, uri, method="GET", body=None, headers=None, **kw):
            if "/resumable/" in uri:
                self.puts += 1
                if self.puts == 2:  # second chunk of the session fails once
                    from tests.fakes.fake_drive import _error

                    return _error(503, "backendError")
            return self.inner.request(uri, method, body, headers, **kw)

    from googleapiclient.discovery import build

    flaky = FlakyHttp()
    b = GoogleDriveBackend(
        lambda: build("drive", "v3", http=flaky), backend.folder_id, upload_chunk=256 * 1024
    )
    sessions_before = sum(1 for m, u in drive.requests if "uploadType=resumable" in u)
    res = b.put("dm-resume", data)
    sessions_after = sum(1 for m, u in drive.requests if "uploadType=resumable" in u)
    assert b.get(res.remote_id) == data
    assert sessions_after - sessions_before == 1  # resumed, not restarted


def test_list_paginates(drive, backend):
    for i in range(5):
        backend.put(f"dm-{i}", b"x")
    backend.put("other", b"y")
    import drivemesh.backends.gdrive as g

    orig = g.GoogleDriveBackend.list
    names = sorted(o.name for o in orig(backend, "dm-"))
    assert names == [f"dm-{i}" for i in range(5)]


def test_refresh_error_maps_to_auth():
    from google.auth.exceptions import RefreshError

    assert isinstance(map_error(RefreshError("invalid_grant")), AuthError)
    assert isinstance(map_error(TimeoutError()), TransientError)
    assert isinstance(map_error(ValueError("x")), PermanentError)


def test_from_secret_requires_refresh_token():
    from tests.conftest import make_settings

    with pytest.raises(AuthError):
        GoogleDriveBackend.from_secret(make_settings(), {}, {})
    b = GoogleDriveBackend.from_secret(
        make_settings(google_client_id="cid", google_client_secret="sec"),
        {"refresh_token": "r", "token": "t"},
        {"folder_id": "F"},
    )
    assert b.folder_id == "F"


def test_full_system_on_fake_drive(dm, user, monkeypatch):
    """End to end: DriveMesh writes/reads through two fake Google Drive accounts."""
    drives = {"g1": FakeDrive(), "g2": FakeDrive()}
    orig_factory = dm.accounts.factory

    def factory(acc, secret):
        if acc.provider == "gdrive":
            return GoogleDriveBackend(drives[acc.external_id].service, upload_chunk=256 * 1024)
        return orig_factory(acc, secret)

    dm.accounts.factory = factory
    for name in drives:
        dm.accounts.link(
            user.id,
            "gdrive",
            name,
            name,
            secret={"refresh_token": "r"},
            email=f"{name}@example.com",
        )
    data = bytes(range(256)) * 1024
    dm.put(user.id, "/doc.bin", data)
    assert dm.get_bytes(user.id, "/doc.bin") == data
    assert all(any(f["name"].startswith("dm-") for f in d.files.values()) for d in drives.values())


def _linked_backend(drive) -> GoogleDriveBackend:
    """A backend built the way accounts are: with the folder id remembered in config_json."""
    first = GoogleDriveBackend(drive.service)
    first.put("dm-1", b"x")
    return GoogleDriveBackend(drive.service, first.folder_id)


@pytest.mark.parametrize("how", ["deleted", "trashed"])
def test_health_fails_when_app_folder_is_gone(drive, how):
    """Regression for 08 M19: health() trusted the cached folder id, so a user deleting the
    DriveMesh folder left the account 'healthy' while every object was gone."""
    b = _linked_backend(drive)
    assert b.health().capacity_bytes  # folder present: healthy
    if how == "deleted":
        del drive.files[b.folder_id]
    else:
        drive.files[b.folder_id]["trashed"] = True
    with pytest.raises(NotFoundError, match="DriveMesh folder"):
        b.health()


def test_missing_app_folder_makes_account_unreachable(dm, user):
    """End to end: three failed probes mark the account unreachable, so repair takes over."""
    from drivemesh.core.workers import health_check

    drives = {"g1": FakeDrive(), "g2": FakeDrive(), "g3": FakeDrive()}
    orig = dm.accounts.factory

    def factory(acc, secret):
        if acc.provider == "gdrive":
            import json

            folder = json.loads(acc.config_json or "{}").get("folder_id")
            return GoogleDriveBackend(drives[acc.external_id].service, folder)
        return orig(acc, secret)

    dm.accounts.factory = factory
    accs = {}
    for name, d in drives.items():
        folder = GoogleDriveBackend(d.service).ensure_folder()
        accs[name] = dm.accounts.link(
            user.id,
            "gdrive",
            name,
            name,
            config={"folder_id": folder},
            secret={"refresh_token": "r"},
        )
    assert set(health_check(dm).values()) == {"active"}
    victim = drives["g1"]
    victim_folder = next(k for k, f in victim.files.items() if f["name"] == "DriveMesh")
    del victim.files[victim_folder]
    for _ in range(dm.settings.unreachable_after_failures):
        states = health_check(dm)
    assert states[accs["g1"].id] == "unreachable"
    assert dm.accounts.get(accs["g1"].id).unhealthy_since is not None
