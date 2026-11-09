"""Authentication state: server-side browser sessions, JWT access tokens and rotating refresh
tokens for API clients, and "sign out everywhere".

* Browser: an HttpOnly, signed cookie carries a random session id. Only its SHA-256 is stored
  (`sessions`), so logout, expiry and "sign out everywhere" take effect server-side.
* API clients: a short-lived HS256 JWT access token (sent as `Authorization: Bearer`) plus an
  opaque refresh token. Refreshing consumes the token and issues a new pair in the same
  family; presenting a consumed token again revokes the whole family (likely theft).
* `users.tokens_valid_after` invalidates every session and JWT issued before it.

All times come from `dm.now()` so tests can drive expiry with a fake clock.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import jwt
from fastapi import Request
from sqlalchemy import select, update

from drivemesh.db import RefreshToken, User, UserSession, new_id
from drivemesh.errors import Unauthorized

if TYPE_CHECKING:
    from drivemesh.core.system import DriveMesh

ISSUER = "drivemesh"
AUDIENCE = "drivemesh-api"
TOUCH_EVERY = timedelta(minutes=5)  # how often a session's last_seen_at is written


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _ts(dt: datetime) -> float:
    return dt.replace(tzinfo=UTC).timestamp()


def _revoked_by_user(user: User, issued_at: datetime | float) -> bool:
    """Issued at or before the user's last "sign out everywhere"? (`<=`: a token minted in
    the same instant as the revocation is revoked too.)"""
    if user.tokens_valid_after is None:
        return False
    if isinstance(issued_at, float):  # JWT iat: compare as numbers, no float→datetime rounding
        return issued_at <= _ts(user.tokens_valid_after)
    return issued_at <= user.tokens_valid_after


# ---- browser sessions ---------------------------------------------------------------------------
def start_session(request: Request, dm: DriveMesh, user: User) -> str:
    """Create a server-side session for `user`, bind it to the cookie, return a CSRF token."""
    from drivemesh.web.security import ensure_csrf_token

    sid = secrets.token_urlsafe(32)
    now = dm.now()
    with dm.session() as s:
        s.add(
            UserSession(
                id_hash=_hash(sid),
                user_id=user.id,
                created_at=now,
                last_seen_at=now,
                expires_at=now + timedelta(days=dm.settings.session_days),
                user_agent=(request.headers.get("user-agent") or "")[:255] or None,
                ip=(request.client.host if request.client else None),
            )
        )
    request.session.clear()  # fresh cookie on login (session fixation defence)
    request.session["sid"] = sid
    return ensure_csrf_token(request)


def session_user(request: Request) -> User | None:
    """The user of a valid, unexpired, unrevoked cookie session, else None (cached per request)."""
    if hasattr(request.state, "session_user"):
        return request.state.session_user
    dm = request.app.state.dm
    user = None
    sid = request.session.get("sid")
    if sid:
        now = dm.now()
        with dm.session() as s:
            row = s.get(UserSession, _hash(sid))
            if row is not None and row.revoked_at is None and row.expires_at > now:
                u = s.get(User, row.user_id)
                if u is not None and not _revoked_by_user(u, row.created_at):
                    user = u
                    if now - row.last_seen_at >= TOUCH_EVERY:
                        row.last_seen_at = now
        if user is None:
            request.session.clear()
    request.state.session_user = user
    return user


def end_session(request: Request, dm: DriveMesh) -> None:
    sid = request.session.get("sid")
    if sid:
        with dm.session() as s:
            row = s.get(UserSession, _hash(sid))
            if row is not None and row.revoked_at is None:
                row.revoked_at = dm.now()
    request.session.clear()


def revoke_everything(dm: DriveMesh, user_id: str) -> None:
    """Sign out everywhere: all sessions, refresh tokens and outstanding JWTs of the user."""
    now = dm.now()
    with dm.session() as s:
        s.get(User, user_id).tokens_valid_after = now
        s.execute(
            update(UserSession)
            .where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        s.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )
    dm.event("sessions_revoked", user_id=user_id)


# ---- API tokens ---------------------------------------------------------------------------------
def issue_tokens(
    dm: DriveMesh, key: str, user_id: str, family_id: str | None = None
) -> dict[str, Any]:
    st = dm.settings
    now = dm.now()
    exp = now + timedelta(minutes=st.access_token_minutes)
    claims = {
        "sub": user_id,
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": _ts(now),
        "exp": _ts(exp),
        "jti": new_id(),
        "typ": "access",
    }
    access = jwt.encode(claims, key, algorithm="HS256")
    raw = secrets.token_urlsafe(32)
    with dm.session() as s:
        s.add(
            RefreshToken(
                token_hash=_hash(raw),
                family_id=family_id or new_id(),
                user_id=user_id,
                created_at=now,
                expires_at=now + timedelta(days=st.refresh_token_days),
            )
        )
    return {
        "token_type": "Bearer",
        "access_token": access,
        "expires_in": int((exp - now).total_seconds()),
        "refresh_token": raw,
    }


def verify_access_token(dm: DriveMesh, key: str, token: str) -> User:
    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=["HS256"],  # never 'none', never asymmetric confusion
            audience=AUDIENCE,
            issuer=ISSUER,
            # Expiry is checked below against dm.now() (injectable clock); the rest by PyJWT.
            options={
                "require": ["exp", "iat", "sub", "iss", "aud"],
                "verify_exp": False,
                "verify_iat": False,
                "verify_nbf": False,
            },
        )
    except jwt.PyJWTError:
        raise Unauthorized("invalid access token") from None
    if claims.get("typ") != "access":
        raise Unauthorized("invalid access token")
    if float(claims["exp"]) <= _ts(dm.now()):
        raise Unauthorized("access token expired")
    with dm.session() as s:
        user = s.get(User, claims["sub"])
    if user is None:
        raise Unauthorized("invalid access token")
    if _revoked_by_user(user, float(claims["iat"])):
        raise Unauthorized("access token revoked")
    return user


def refresh_tokens(dm: DriveMesh, key: str, raw: str) -> dict[str, Any]:
    """Rotate a refresh token. Reusing a consumed one revokes its whole family."""
    now = dm.now()
    reused = False
    with dm.session() as s:
        row = s.scalars(select(RefreshToken).where(RefreshToken.token_hash == _hash(raw))).first()
        if row is None:
            raise Unauthorized("invalid refresh token")
        user = s.get(User, row.user_id)
        if (
            row.revoked_at is not None
            or row.expires_at <= now
            or user is None
            or _revoked_by_user(user, row.created_at)
        ):
            raise Unauthorized("refresh token expired or revoked")
        user_id, family = row.user_id, row.family_id
        consumed = s.execute(
            update(RefreshToken)
            .where(RefreshToken.id == row.id, RefreshToken.used_at.is_(None))
            .values(used_at=now)
            .execution_options(synchronize_session=False)
        ).rowcount
        if consumed != 1:
            reused = True
            s.execute(
                update(RefreshToken)
                .where(RefreshToken.family_id == family, RefreshToken.revoked_at.is_(None))
                .values(revoked_at=now)
                .execution_options(synchronize_session=False)
            )
    if reused:  # committed above, then refuse
        dm.event("refresh_token_reuse", level="warning", user_id=user_id, family=family)
        raise Unauthorized("refresh token was already used; this token chain has been revoked")
    return issue_tokens(dm, key, user_id, family)


def revoke_refresh_token(dm: DriveMesh, raw: str) -> None:
    now = dm.now()
    with dm.session() as s:
        row = s.scalars(select(RefreshToken).where(RefreshToken.token_hash == _hash(raw))).first()
        if row is None:
            return
        s.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == row.family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
            .execution_options(synchronize_session=False)
        )
