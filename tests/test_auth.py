"""Sign-in with Google (open sign-up), server-side sessions, API tokens (JWT + rotating refresh)
and the multi-user guards that open sign-up needs."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from drivemesh.web import oauth
from drivemesh.web.app import create_app
from tests.conftest import payload
from tests.test_web import FakeFlow, login

SID_COOKIE = "dm_session"


@pytest.fixture
def app(dm):
    return create_app(dm, start_workers=False)


@pytest.fixture
def google(monkeypatch, dm):
    """Fake Google: sign-in returns whatever identity the test puts in `who`."""
    from drivemesh.backends.gdrive import GoogleDriveBackend
    from tests.fakes.fake_drive import FakeDrive

    who = {"sub": "sub-alice", "email": "alice@gmail.com", "email_verified": True}
    drives: dict[str, FakeDrive] = {}
    monkeypatch.setattr(
        oauth,
        "make_flow",
        lambda settings, state=None, scopes=None: FakeFlow(scopes or [oauth.DRIVE_FILE]),
    )
    monkeypatch.setattr(oauth, "verify_id_token", lambda tok, cid: dict(who))
    monkeypatch.setattr(
        oauth,
        "drive_backend_for",
        lambda dm_, creds: GoogleDriveBackend(drives.setdefault(who["sub"], FakeDrive()).service),
    )
    orig = dm.accounts.factory

    def factory(acc, secret):
        if acc.provider == "gdrive":
            return GoogleDriveBackend(drives[acc.external_id].service)
        return orig(acc, secret)

    dm.accounts.factory = factory
    dm.settings.google_client_id = "cid"
    return who


def google_sign_in(c: TestClient):
    r = c.get("/auth/google", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("https://accounts.google.com")
    return c.get("/accounts/callback/google?state=state-xyz&code=abc", follow_redirects=False)


def csrf(c: TestClient) -> None:
    c.headers["X-CSRF-Token"] = c.get("/api/v1/auth/me").json()["csrf_token"]


def link_gmail(c: TestClient):
    c.get("/accounts/link/google", follow_redirects=False)
    return c.get("/accounts/callback/google?state=state-xyz&code=abc", follow_redirects=False)


# ---- Google sign-in -----------------------------------------------------------------------------
def test_google_sign_in_creates_account_then_signs_back_in(app, dm, google):
    with TestClient(app) as c:
        r = google_sign_in(c)
        assert r.status_code == 303 and r.headers["location"] == "/accounts?welcome=1"
        me = c.get("/api/v1/auth/me").json()
        assert me["user"]["email"] == "alice@gmail.com" and me["sign_in"] == "google"
        assert me["is_admin"] is False and me["allow_local_backends"] is False
        started = [f for f in FakeFlow.instances if hasattr(f, "auth_kwargs")][-1]
        assert started.auth_kwargs.get("access_type") is None  # sign-in asks for no Drive grant
    with TestClient(app) as c2:
        assert google_sign_in(c2).headers["location"] == "/app"  # same Google sub → same user
        assert c2.get("/api/v1/auth/me").json()["user"]["id"] == me["user"]["id"]
    assert dm.accounts.list(me["user"]["id"]) == []  # signing in links no storage


def test_sign_in_rejects_state_mismatch_and_unverified_email(app, google):
    with TestClient(app) as c:
        c.get("/auth/google", follow_redirects=False)
        r = c.get("/accounts/callback/google?state=evil&code=abc")
        assert r.status_code == 403 and "state mismatch" in r.text
        assert c.get("/api/v1/auth/me").status_code == 401
    google["email_verified"] = False
    with TestClient(app) as c:
        r = google_sign_in(c)
        assert r.status_code == 400 and c.get("/api/v1/auth/me").status_code == 401


def test_google_only_accounts_cannot_use_password_login(app, dm, google):
    with TestClient(app) as c:
        google_sign_in(c)
        username = c.get("/api/v1/auth/me").json()["user"]["username"]
    with TestClient(app) as c:
        r = c.post("/api/v1/auth/login", json={"username": username, "password": "!"})
        assert r.status_code == 401


def test_linking_storage_requires_a_signed_in_user(app, google):
    with TestClient(app) as c:
        assert c.get("/accounts/link/google", follow_redirects=False).status_code == 303
        r = c.get("/accounts/callback/google?state=x&code=y", follow_redirects=False)
        assert r.status_code == 403  # no flow started: state check fails first


def test_signed_up_users_get_no_operator_powers(app, dm, google, tmp_path, user):
    with TestClient(app) as c:
        google_sign_in(c)
        csrf(c)
        r = c.post("/api/v1/accounts/local", json={"path": str(tmp_path / "x")})
        assert r.status_code == 403 and "operator" in r.json()["error"]["message"]
        assert c.get("/metrics").status_code == 401
    with TestClient(app) as admin:  # the CLI-created owner keeps them
        login(admin)
        assert (
            admin.post("/api/v1/accounts/local", json={"path": str(tmp_path / "y")}).status_code
            == 201
        )
        assert admin.get("/metrics").status_code == 200


# ---- one Gmail, one DriveMesh user --------------------------------------------------------------
def test_a_gmail_can_be_linked_by_only_one_drivemesh_user(app, dm, google):
    with TestClient(app) as alice:
        google_sign_in(alice)
        google.update(sub="sub-shared", email="shared@gmail.com")
        assert link_gmail(alice).status_code == 303
        alice_id = alice.get("/api/v1/auth/me").json()["user"]["id"]
    google.update(sub="sub-bob", email="bob@gmail.com")
    with TestClient(app) as bob:
        google_sign_in(bob)
        google.update(sub="sub-shared", email="shared@gmail.com")
        r = link_gmail(bob)
        assert r.status_code == 409 and "already linked to another DriveMesh account" in r.text
        # once Alice's link is removed, Bob may link it
        (acc,) = dm.accounts.list(alice_id)
        with dm.session() as s:
            from drivemesh.db import LinkedAccount

            s.get(LinkedAccount, acc.id).state = "removed"
        assert link_gmail(bob).status_code == 303


# ---- sessions -----------------------------------------------------------------------------------
def test_logout_revokes_the_session_server_side(app, user):
    with TestClient(app) as c:
        login(c)
        stolen = c.cookies.get(SID_COOKIE)
        assert c.post("/api/v1/auth/logout").status_code == 204
        c.cookies.set(SID_COOKIE, stolen)  # replaying the old cookie must not work
        assert c.get("/api/v1/auth/me").status_code == 401
        assert c.get("/app", follow_redirects=False).status_code == 303


def test_sessions_expire(app, user, clock, dm):
    with TestClient(app) as c:
        login(c)
        clock.advance(dm.settings.session_days * 86400 + 1)
        assert c.get("/api/v1/auth/me").status_code == 401


def test_sign_out_everywhere(app, user, dm):
    with TestClient(app) as a, TestClient(app) as b:
        login(a)
        login(b)
        pair = a.post("/api/v1/auth/tokens").json()
        bearer = {"Authorization": f"Bearer {pair['access_token']}"}
        assert a.post("/api/v1/auth/logout-all").status_code == 204
        assert b.get("/api/v1/auth/me").status_code == 401
        assert a.get("/api/v1/auth/me").status_code == 401
    with TestClient(app) as c:
        assert c.get("/api/v1/fs?path=/", headers=bearer).status_code == 401
        r = c.post("/api/v1/auth/tokens/refresh", json={"refresh_token": pair["refresh_token"]})
        assert r.status_code == 401


# ---- API tokens ---------------------------------------------------------------------------------
def test_bearer_tokens_work_without_cookie_or_csrf(app, user, accounts):
    with TestClient(app) as c:
        login(c)
        pair = c.post("/api/v1/auth/tokens").json()
        assert pair["token_type"] == "Bearer" and pair["expires_in"] == 15 * 60
    with TestClient(app) as api:  # fresh client: no cookie, no CSRF header
        h = {"Authorization": f"Bearer {pair['access_token']}"}
        data = payload(5000)
        assert (
            api.put("/api/v1/files/content?path=/t.bin", content=data, headers=h).status_code == 201
        )
        assert api.get("/api/v1/files/content?path=/t.bin", headers=h).content == data
        assert "csrf_token" not in api.get("/api/v1/auth/me", headers=h).json()
    with TestClient(app) as anon:  # minting tokens needs a signed-in browser session + CSRF
        assert anon.post("/api/v1/auth/tokens").status_code == 401


def test_access_tokens_expire_and_cannot_be_forged(app, user, clock, dm):
    import jwt

    with TestClient(app) as c:
        login(c)
        tok = c.post("/api/v1/auth/tokens").json()["access_token"]
    claims = jwt.decode(tok, options={"verify_signature": False})
    forged = [
        tok[:-4] + ("AAAA" if not tok.endswith("AAAA") else "BBBB"),  # bad signature
        jwt.encode(claims, "wrong-key", algorithm="HS256"),
        jwt.encode(claims, None, algorithm="none"),
        jwt.encode({**claims, "typ": "refresh"}, app.state.jwt_key, algorithm="HS256"),
        jwt.encode({**claims, "aud": "other"}, app.state.jwt_key, algorithm="HS256"),
    ]
    with TestClient(app) as api:
        ok = {"Authorization": f"Bearer {tok}"}
        assert api.get("/api/v1/fs?path=/", headers=ok).status_code == 200
        for bad in forged:
            r = api.get("/api/v1/fs?path=/", headers={"Authorization": f"Bearer {bad}"})
            assert r.status_code == 401, bad
        clock.advance(dm.settings.access_token_minutes * 60 + 1)
        assert api.get("/api/v1/fs?path=/", headers=ok).status_code == 401


def test_refresh_rotates_and_reuse_revokes_the_chain(app, user, dm):
    with TestClient(app) as c:
        login(c)
        first = c.post("/api/v1/auth/tokens").json()
    with TestClient(app) as api:
        second = api.post(
            "/api/v1/auth/tokens/refresh", json={"refresh_token": first["refresh_token"]}
        )
        assert second.status_code == 200
        second = second.json()
        assert second["refresh_token"] != first["refresh_token"]
        # an attacker replays the consumed token: refused, and the whole chain dies
        r = api.post("/api/v1/auth/tokens/refresh", json={"refresh_token": first["refresh_token"]})
        assert r.status_code == 401
        r = api.post("/api/v1/auth/tokens/refresh", json={"refresh_token": second["refresh_token"]})
        assert r.status_code == 401


# ---- isolation for open sign-up -----------------------------------------------------------------
def test_status_is_scoped_to_the_signed_in_user(app, dm, user, accounts, google):
    dm.put(user.id, "/owner-secret.bin", payload(4000))
    with TestClient(app) as c:
        google_sign_in(c)
        st = c.get("/api/v1/cluster/status").json()
        assert st["accounts"] == [] and st["replicas"] == {} and st["files"] == {}
        assert st["jobs"] == {} and st["dead_jobs"] == []
        assert all(e["kind"] in ("login", "user_signed_up") for e in st["recent_events"])
    with TestClient(app) as admin:
        login(admin)
        assert admin.get("/api/v1/cluster/status").json()["replicas"].get("ok", 0) >= 2
