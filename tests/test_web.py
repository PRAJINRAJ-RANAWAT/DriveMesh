"""Web/API layer: auth, CSRF, streaming upload/download, Range, accounts, OAuth, isolation."""

import pytest
from fastapi.testclient import TestClient

from drivemesh.web import oauth
from drivemesh.web.app import create_app
from tests.conftest import CHUNK, payload


@pytest.fixture
def app(dm):
    return create_app(dm, start_workers=False)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


def login(c: TestClient, username="owner", password="correct horse battery staple") -> str:
    r = c.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    tok = r.json()["csrf_token"]
    c.headers["X-CSRF-Token"] = tok
    return tok


@pytest.fixture
def authed(client, user, accounts):
    login(client)
    return client


APP_PAGES = ("/app", "/files", "/accounts", "/status")


def test_logged_in_pages_require_auth(client):
    for path in APP_PAGES:
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login", path
    assert client.get("/login").status_code == 200


def test_login_page_renders(client):
    r = client.get("/login")
    assert r.status_code == 200
    html = r.text
    assert 'id="login-form"' in html and 'name="password"' in html and 'role="alert"' in html
    assert "/static/js/login.js" in html


def test_app_pages_render_when_signed_in(client, user):
    login(client)
    for path in APP_PAGES:
        r = client.get(path)
        assert r.status_code == 200, path
        assert 'aria-current="page"' in r.text  # sidebar marks the active page
        assert "/static/css/tokens.css" in r.text


def test_landing_page_renders_for_visitors(client):
    import re

    r = client.get("/")
    assert r.status_code == 200
    html = r.text
    assert "Five Google accounts." in html and "One giant drive." in html
    assert re.search(r'href="/login">Start meshing', html)
    for section in (
        'id="how"',
        'id="durability"',
        'id="security"',
        'id="architecture"',
        'class="cta-band"',
    ):
        assert section in html, section
    assert "What happens if an account dies?" in html and "Built by Prajin" in html
    # strict-CSP friendly: no inline scripts, no inline style attributes, no third-party assets
    assert not re.search(r"<script(?![^>]*\bsrc=)", html)
    assert "style=" not in html
    assert not re.search(r'(?:src|href)="https?://(?!github\.com/PRAJINRAJ-RANAWAT)', html)
    assert "script-src 'self'" in r.headers["content-security-policy"]
    # original artwork only: no Google logo assets
    assert "google-drive" not in html.lower() and ".png" not in html


def test_landing_cta_points_to_app_when_signed_in(client, user):
    login(client)
    html = client.get("/").text
    assert 'href="/app">Start meshing' in html and ">Dashboard<" in html
    r = client.get("/login", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/app"


def test_landing_numbers_match_results():
    """Every number on the landing page must be backed by docs/design/results.md."""
    from pathlib import Path

    from drivemesh.web import facts

    results = Path("docs/design/results.md").read_text()
    assert "Total: 70 trials, 0 files lost" in results
    assert f"**{facts.TEST_COUNT} tests**" in results and f"**{facts.COVERAGE}**" in results
    assert (
        "RS(4,2) | 100,663,680 | 1.5000\u00d7" in results
        and "RF=2 | 134,218,240 | 2.0000\u00d7" in results
    )


def test_static_assets_are_cacheable_pages_are_not(client):
    for asset in ("/static/css/tokens.css", "/static/css/landing.css", "/static/js/landing.js"):
        r = client.get(asset)
        assert r.status_code == 200 and r.headers["cache-control"].startswith("public"), asset
    assert client.get("/static/fonts/geist-mono-latin.woff2").status_code == 200
    assert client.get("/login").headers["cache-control"] == "no-store"


def test_fs_listing_reports_replica_health(authed, dm, accounts):
    authed.put("/api/v1/files/content?path=/h.bin", content=payload(2 * CHUNK))
    files = authed.get("/api/v1/fs?path=/").json()["files"]
    assert files[0]["health"] == "healthy"
    from drivemesh.backends.local import MemoryBackend

    for be in MemoryBackend._instances.values():  # lose every copy on one account
        be.objects.clear()
        break
    from drivemesh.core.fsck import fsck

    authed.get("/api/v1/files/content?path=/h.bin")  # reads mark missing replicas
    state = authed.get("/api/v1/fs?path=/").json()["files"][0]["health"]
    assert state in ("healthy", "under_replicated")
    assert fsck(dm).files == 1
    me = authed.get("/api/v1/auth/me").json()
    assert me["chunk_size"] == CHUNK
    cap = authed.get("/api/v1/cluster/status").json()["capacity"]
    assert set(cap) == {"total_bytes", "used_bytes", "free_bytes"}


def test_security_headers(client):
    r = client.get("/login")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-request-id"]


def test_login_logout_and_me(client, user):
    r = client.post("/api/v1/auth/login", json={"username": "owner", "password": "nope"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"
    assert client.get("/api/v1/auth/me").status_code == 401
    tok = login(client)
    me = client.get("/api/v1/auth/me").json()
    assert me["user"]["username"] == "owner" and me["csrf_token"] == tok
    assert client.get("/", follow_redirects=False).status_code == 200
    assert client.post("/api/v1/auth/logout").status_code == 204
    assert client.get("/api/v1/auth/me").status_code == 401


def test_login_is_rate_limited(dm, user):
    dm.settings.login_rate_per_minute = 3
    with TestClient(create_app(dm, start_workers=False)) as c:
        codes = [
            c.post("/api/v1/auth/login", json={"username": "owner", "password": "x"}).status_code
            for _ in range(5)
        ]
    assert codes[:3] == [401, 401, 401] and codes[3:] == [429, 429]


def test_csrf_required_for_mutations(authed):
    authed.headers.pop("X-CSRF-Token")
    r = authed.post("/api/v1/folders", json={"path": "/x"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"
    r = authed.put("/api/v1/files/content?path=/a", content=b"data")
    assert r.status_code == 403
    authed.headers["X-CSRF-Token"] = "wrong"
    assert authed.delete("/api/v1/files?path=/a").status_code == 403


def test_upload_download_roundtrip(authed):
    data = payload(5 * CHUNK + 123)
    r = authed.put("/api/v1/files/content?path=/docs/a.bin", content=data)
    assert r.status_code == 404  # parent folder must exist
    assert authed.post("/api/v1/folders", json={"path": "/docs"}).status_code == 201
    r = authed.put("/api/v1/files/content?path=/docs/a.bin", content=data)
    assert r.status_code == 201, r.text
    info = r.json()
    assert info["size"] == len(data) and info["chunks"] == 6 and info["rf"] == 2
    r = authed.get("/api/v1/files/content?path=/docs/a.bin")
    assert r.status_code == 200 and r.content == data
    assert r.headers["content-length"] == str(len(data))
    assert "filename*=UTF-8''a.bin" in r.headers["content-disposition"]
    assert r.headers["etag"] == f'"{info["sha256"]}"'
    listing = authed.get("/api/v1/fs?path=/docs").json()
    assert [f["name"] for f in listing["files"]] == ["a.bin"]


def test_streamed_upload_without_content_length(authed):
    data = payload(3 * CHUNK)

    def gen():
        for i in range(0, len(data), 10_000):
            yield data[i : i + 10_000]

    r = authed.put("/api/v1/files/content?path=/s.bin", content=gen())
    assert r.status_code == 201, r.text
    assert authed.get("/api/v1/files/content?path=/s.bin").content == data


@pytest.mark.parametrize(
    "header,expected",
    [
        ("bytes=0-9", (0, 9)),
        ("bytes=100-", (100, None)),
        ("bytes=-50", (-50, None)),
        (f"bytes={CHUNK - 5}-{CHUNK + 5}", (CHUNK - 5, CHUNK + 5)),
    ],
)
def test_range_requests(authed, header, expected):
    data = payload(2 * CHUNK + 77)
    authed.put("/api/v1/files/content?path=/r", content=data)
    r = authed.get("/api/v1/files/content?path=/r", headers={"Range": header})
    assert r.status_code == 206
    a, b = expected
    want = data[a:] if b is None else data[a : b + 1]
    assert r.content == want
    assert r.headers["content-range"].endswith(f"/{len(data)}")


def test_unsatisfiable_range(authed):
    authed.put("/api/v1/files/content?path=/r", content=b"0123456789")
    r = authed.get("/api/v1/files/content?path=/r", headers={"Range": "bytes=50-60"})
    assert r.status_code == 416 and r.headers["content-range"] == "bytes */10"


def test_overwrite_semantics(authed):
    authed.put("/api/v1/files/content?path=/v", content=b"one")
    r = authed.put("/api/v1/files/content?path=/v", content=b"two")
    assert r.status_code == 409 and r.json()["error"]["code"] == "already_exists"
    r = authed.put("/api/v1/files/content?path=/v&overwrite=true", content=b"two")
    assert r.status_code == 201 and r.json()["version"] == 2
    assert authed.get("/api/v1/files/content?path=/v").content == b"two"


def test_upload_size_limit(authed, dm):
    dm.settings.max_upload_bytes = 1000
    r = authed.put("/api/v1/files/content?path=/big", content=b"x" * 2000)
    assert r.status_code == 413
    r = authed.put("/api/v1/files/content?path=/big2", content=iter([b"x" * 600, b"x" * 600]))
    assert r.status_code == 413
    assert authed.get("/api/v1/files/meta?path=/big2").status_code == 404


def test_w13_traversal_paths_rejected(authed):
    for bad in ("../../etc/passwd", "/a/../../b", "/x\x00y"):
        r = authed.put("/api/v1/files/content", params={"path": bad}, content=b"x")
        assert r.status_code == 400, bad


def test_move_delete_and_meta(authed):
    authed.post("/api/v1/folders", json={"path": "/a/b", "parents": True})
    authed.put("/api/v1/files/content?path=/a/b/f", content=payload(CHUNK + 1))
    meta = authed.get("/api/v1/files/meta?path=/a/b/f").json()
    assert len(meta["chunk_replicas"]) == 2
    assert all(len(c["replicas"]) == 2 for c in meta["chunk_replicas"])
    assert authed.post("/api/v1/fs/move", json={"src": "/a/b/f", "dst": "/a/g"}).status_code == 200
    assert authed.delete("/api/v1/files?path=/a/g").status_code == 202
    assert authed.get("/api/v1/files/meta?path=/a/g").status_code == 404
    r = authed.delete("/api/v1/folders?path=/a")
    assert r.status_code == 409
    assert authed.delete("/api/v1/folders?path=/a&recursive=true").status_code == 202


def test_accounts_and_unlink(authed, dm, tmp_path, accounts):
    accs = authed.get("/api/v1/accounts").json()["accounts"]
    assert len(accs) == 3
    r = authed.post("/api/v1/accounts/local", json={"path": str(tmp_path / "disk")})
    assert r.status_code == 201 and r.json()["state"] == "active"
    authed.put("/api/v1/files/content?path=/f", content=payload(2 * CHUNK))
    r = authed.delete(f"/api/v1/accounts/{accounts[0].id}")
    assert r.status_code == 202 and r.json()["state"] == "draining"
    dm.jobs.run_until_idle()
    states = {a["id"]: a["state"] for a in authed.get("/api/v1/accounts").json()["accounts"]}
    assert accounts[0].id not in states  # removed accounts are hidden
    assert authed.get("/api/v1/files/content?path=/f").content == payload(2 * CHUNK)


def test_local_backends_can_be_disabled(authed, dm, tmp_path):
    dm.settings.allow_local_backends = False
    r = authed.post("/api/v1/accounts/local", json={"path": str(tmp_path)})
    assert r.status_code == 403


def test_users_are_isolated_over_http(app, dm, user, accounts):
    other = dm.create_user("mallory", "pw")
    dm.accounts.link_memory(other.id, "m1")
    with TestClient(app) as a, TestClient(app) as b:
        login(a)
        a.put("/api/v1/files/content?path=/secret", content=b"top secret")
        login(b, "mallory", "pw")
        assert b.get("/api/v1/files/content?path=/secret").status_code == 404
        assert b.get("/api/v1/fs?path=/").json()["files"] == []
        assert b.delete(f"/api/v1/accounts/{accounts[0].id}").status_code == 404
        ids = {x["id"] for x in b.get("/api/v1/accounts").json()["accounts"]}
        assert ids.isdisjoint({x.id for x in accounts})
        st = b.get("/api/v1/cluster/status").json()
        assert len(st["accounts"]) == 1


def test_cluster_status_and_probes(authed, dm):
    authed.put("/api/v1/files/content?path=/x", content=b"data")
    st = authed.get("/api/v1/cluster/status").json()
    assert st["replicas"]["ok"] == 2 and st["safe_mode"]["active"] is False
    assert authed.get("/healthz").json() == {"ok": True}
    assert authed.get("/readyz").status_code == 200
    dm.safe_mode.enter("test")
    assert authed.get("/readyz").status_code == 503
    r = authed.put("/api/v1/files/content?path=/y", content=b"data")
    assert r.status_code == 503 and r.json()["error"]["code"] == "safe_mode"
    dm.safe_mode.leave()
    assert authed.get("/status").status_code == 200


def test_metrics_requires_auth(client, dm, user):
    assert client.get("/metrics").status_code == 401
    login(client)
    r = client.get("/metrics")
    assert r.status_code == 200 and "dm_bytes_uploaded" in r.text
    from pydantic import SecretStr

    dm.settings.metrics_token = SecretStr("t0k")
    with TestClient(create_app(dm, start_workers=False)) as c:
        assert c.get("/metrics").status_code == 401
        assert c.get("/metrics", headers={"Authorization": "Bearer t0k"}).status_code == 200


def test_owner_bootstrap(dm):
    from pydantic import SecretStr

    dm.settings.owner_username = "boss"
    dm.settings.owner_password = SecretStr("pw")
    with TestClient(create_app(dm, start_workers=False)) as c:
        login(c, "boss", "pw")


def test_app_starts_workers(dm, user, accounts):
    with TestClient(create_app(dm, start_workers=True)) as c:
        assert c.app.state.workers is not None
        assert c.get("/readyz").status_code == 200


# ---- OAuth -------------------------------------------------------------------------------------
class FakeCreds:
    def __init__(self, scopes):
        self.refresh_token = "refresh-1"
        self.token = "access-1"
        self.id_token = "idtok"
        self.granted_scopes = scopes
        self.scopes = scopes


class FakeFlow:
    instances = []

    def __init__(self, scopes):
        self.code_verifier = "verifier-123"
        self.credentials = FakeCreds(scopes)
        self.fetched = None
        FakeFlow.instances.append(self)

    def authorization_url(self, **kw):
        self.auth_kwargs = kw
        return "https://accounts.google.com/o/oauth2/auth?fake=1", "state-xyz"

    def fetch_token(self, code):
        self.fetched = code


@pytest.fixture
def fake_google(monkeypatch, dm):
    from drivemesh.backends.gdrive import GoogleDriveBackend
    from tests.fakes.fake_drive import FakeDrive

    drives = {}
    scopes = {"value": [oauth.DRIVE_FILE, "openid", "email"]}
    sub = {"value": "google-sub-1"}
    monkeypatch.setattr(oauth, "make_flow", lambda settings, state=None: FakeFlow(scopes["value"]))
    monkeypatch.setattr(
        oauth,
        "verify_id_token",
        lambda tok, cid: {"sub": sub["value"], "email": f"{sub['value']}@gmail.com"},
    )

    def backend_for(dm_, creds):
        d = drives.setdefault(sub["value"], FakeDrive())
        return GoogleDriveBackend(d.service)

    monkeypatch.setattr(oauth, "drive_backend_for", backend_for)
    orig = dm.accounts.factory

    def factory(acc, secret):
        if acc.provider == "gdrive":
            return GoogleDriveBackend(drives[acc.external_id].service)
        return orig(acc, secret)

    dm.accounts.factory = factory
    dm.settings.google_client_id = "cid"
    return {"scopes": scopes, "sub": sub, "drives": drives}


def test_oauth_link_flow(client, user, dm, fake_google):
    login(client)
    r = client.get("/accounts/link/google", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("https://accounts.google.com")
    assert FakeFlow.instances[-1].auth_kwargs["access_type"] == "offline"
    r = client.get("/accounts/callback/google?state=state-xyz&code=abc", follow_redirects=False)
    assert r.status_code == 303, r.text
    accs = client.get("/api/v1/accounts").json()["accounts"]
    assert [a["email"] for a in accs] == ["google-sub-1@gmail.com"]
    acc = dm.accounts.get(accs[0]["id"])
    assert dm.accounts.read_secret(acc) == {"refresh_token": "refresh-1", "token": "access-1"}
    assert b"refresh-1" not in acc.secret_blob  # encrypted at rest
    # re-linking the same Google account updates it instead of duplicating (W-17)
    client.get("/accounts/link/google", follow_redirects=False)
    client.get("/accounts/callback/google?state=state-xyz&code=abc", follow_redirects=False)
    assert len(client.get("/api/v1/accounts").json()["accounts"]) == 1
    # a second Google account is a second, distinct account
    fake_google["sub"]["value"] = "google-sub-2"
    client.get("/accounts/link/google", follow_redirects=False)
    client.get("/accounts/callback/google?state=state-xyz&code=abc", follow_redirects=False)
    assert len(client.get("/api/v1/accounts").json()["accounts"]) == 2
    data = payload(3 * CHUNK)
    assert client.put("/api/v1/files/content?path=/g", content=data).status_code == 201
    assert client.get("/api/v1/files/content?path=/g").content == data


def test_oauth_rejects_state_mismatch(client, user, fake_google):
    login(client)
    client.get("/accounts/link/google", follow_redirects=False)
    r = client.get("/accounts/callback/google?state=evil&code=abc")
    assert r.status_code == 403 and "state mismatch" in r.text


def test_oauth_requires_drive_scope(client, user, fake_google):
    fake_google["scopes"]["value"] = ["openid", "email"]
    login(client)
    client.get("/accounts/link/google", follow_redirects=False)
    r = client.get("/accounts/callback/google?state=state-xyz&code=abc")
    assert r.status_code == 400 and "Drive access was not granted" in r.text


def test_oauth_not_configured(client, user, dm):
    login(client)
    r = client.get("/accounts/link/google")
    assert r.status_code == 503


def test_upload_progress_reports_stored_chunks(authed, app):
    seen = []
    original = app.state.upload_progress.__class__.__setitem__

    class Spy(dict):
        def __setitem__(self, k, v):
            seen.append(v)
            original(self, k, v)

    app.state.upload_progress = Spy()
    r = authed.put(
        "/api/v1/files/content?path=/p.bin",
        content=payload(3 * CHUNK + 1),
        headers={"X-Upload-Id": "test-upload-0001"},
    )
    assert r.status_code == 201
    assert seen and sorted(seen[0]) == [0, 1, 2, 3]  # every chunk reported as stored
    assert app.state.upload_progress == {}  # cleaned up after the request
    assert authed.get("/api/v1/uploads/test-upload-0001").status_code == 404


def test_static_assets_are_fingerprinted_and_compressed(client):
    import re

    html = client.get("/").text
    m = re.search(r'href="(/static/css/tokens\.css\?v=[0-9a-f]{10})"', html)
    assert m, "templates must emit content-hashed asset URLs"
    r = client.get(m.group(1), headers={"Accept-Encoding": "gzip"})
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert r.headers.get("content-encoding") == "gzip"
    page = client.get("/", headers={"Accept-Encoding": "gzip"})
    assert (
        page.headers.get("content-encoding") == "gzip"
        and page.headers["cache-control"] == "no-store"
    )


def test_file_downloads_are_never_gzipped(authed):
    authed.put("/api/v1/files/content?path=/z.bin", content=b"a" * 50_000)
    r = authed.get("/api/v1/files/content?path=/z.bin", headers={"Accept-Encoding": "gzip"})
    assert "content-encoding" not in r.headers and r.headers["content-length"] == "50000"
