"""Phase 5: configuration hardening, HSTS, token revocation, crypto-at-rest checks."""

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from drivemesh.core.workers import start_evacuation
from drivemesh.db import LinkedAccount
from drivemesh.web.app import create_app
from tests.conftest import make_settings


def test_production_problems_flag_unsafe_settings():
    s = make_settings()
    problems = " ".join(s.production_problems())
    for needle in (
        "SESSION_SECRET",
        "MASTER_KEYS",
        "SECURE_COOKIES",
        "LOCAL_BACKENDS",
        "https://",
        "Postgres",
    ):
        assert needle in problems
    ok = make_settings(
        session_secret="x" * 40,
        master_keys="k1:" + "A" * 43 + "=",
        secure_cookies=True,
        allow_local_backends=False,
        public_base_url="https://dm.example.com",
        database_url="postgresql+psycopg://u:p@h/db",
    )
    assert ok.production_problems() == []


def test_app_refuses_unsafe_production_config(dm):
    dm.settings.env = "prod"
    with pytest.raises(RuntimeError, match="unsafe production configuration"):
        create_app(dm, start_workers=False)


def test_hsts_only_with_secure_cookies(dm):
    with TestClient(create_app(dm, start_workers=False)) as c:
        assert "strict-transport-security" not in c.get("/login").headers
    dm.settings.secure_cookies = True
    dm.settings.session_secret = SecretStr("s" * 40)
    with TestClient(create_app(dm, start_workers=False), base_url="https://testserver") as c:
        assert c.get("/login").headers["strict-transport-security"].startswith("max-age=")


def test_session_cookie_flags(dm, user):
    dm.settings.secure_cookies = True
    with TestClient(create_app(dm, start_workers=False), base_url="https://testserver") as c:
        r = c.post(
            "/api/v1/auth/login",
            json={"username": "owner", "password": "correct horse battery staple"},
        )
        cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie


def test_unlinking_google_account_revokes_token(dm, user, accounts, monkeypatch):
    from drivemesh.backends import gdrive
    from drivemesh.backends.local import MemoryBackend

    revoked = []
    monkeypatch.setattr(gdrive, "revoke_token", lambda tok: revoked.append(tok) or True)
    orig = dm.accounts.factory
    dm.accounts.factory = lambda acc, secret: (
        MemoryBackend.named("g") if acc.provider == "gdrive" else orig(acc, secret)
    )
    g = dm.accounts.link(user.id, "gdrive", "sub-9", "g", secret={"refresh_token": "rt-9"})
    start_evacuation(dm, user.id, g.id)
    dm.jobs.run_until_idle()
    assert revoked == ["rt-9"]
    with dm.session() as s:
        acc = s.get(LinkedAccount, g.id)
        assert acc.state == "removed" and acc.secret_blob is None


def test_secrets_never_stored_in_plaintext(dm, user):
    dm.accounts.link(
        user.id, "gdrive", "sub-1", "g", secret={"refresh_token": "SUPER-SECRET-REFRESH"}
    )
    raw = dm.engine.raw_connection()
    try:
        cur = raw.cursor()
        cur.execute("SELECT secret_blob FROM linked_accounts")
        blobs = [bytes(r[0]) for r in cur.fetchall() if r[0] is not None]
    finally:
        raw.close()
    assert blobs and all(b"SUPER-SECRET" not in b for b in blobs)
    with dm.session() as s:
        assert s.scalars(select(LinkedAccount)).first().secret_blob.startswith(b"DMS1")


def test_password_hash_is_salted(dm):
    a = dm.create_user("a", "same")
    b = dm.create_user("b", "same")
    assert a.password_hash != b.password_hash and "same" not in a.password_hash


def test_postgres_urls_are_normalized():
    from drivemesh.db import normalize_url

    assert normalize_url("postgres://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalize_url("postgresql://u:p@h/db?sslmode=require").startswith("postgresql+psycopg")
    assert normalize_url("sqlite:///x.db") == "sqlite:///x.db"
