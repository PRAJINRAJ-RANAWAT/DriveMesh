"""Session auth, CSRF, login throttling and security headers."""

from __future__ import annotations

import hmac
import secrets
import threading
import time
import uuid

from fastapi import Depends, Request
from starlette.middleware.base import BaseHTTPMiddleware

from drivemesh.db import User
from drivemesh.errors import Forbidden, Unauthorized
from drivemesh.logs import request_id_var
from drivemesh.resilience import TokenBucket

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; form-action 'self' https://accounts.google.com; "
    "frame-ancestors 'none'; base-uri 'none'; object-src 'none'"
)


def get_dm(request: Request):
    return request.app.state.dm


def current_user(request: Request) -> User:
    """The caller: a Bearer JWT (API clients) or a valid server-side cookie session (browser)."""
    from drivemesh.web.auth import session_user, verify_access_token

    header = request.headers.get("authorization", "")
    if header[:7].lower() == "bearer ":
        user = verify_access_token(request.app.state.dm, request.app.state.jwt_key, header[7:])
        request.state.auth = "bearer"
        return user
    user = session_user(request)
    if user is None:
        raise Unauthorized("login required")
    request.state.auth = "cookie"
    return user


def ensure_csrf_token(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = request.session["csrf"] = secrets.token_urlsafe(32)
    return tok


def require_csrf(request: Request, user: User = Depends(current_user)) -> User:
    """Synchronizer-token CSRF defence for every state-changing cookie-authenticated request.

    Bearer-token requests are exempt: browsers never attach an Authorization header on their
    own, so a cross-site page cannot forge one."""
    if request.method not in SAFE_METHODS and getattr(request.state, "auth", "") != "bearer":
        sent = request.headers.get("x-csrf-token", "")
        expected = request.session.get("csrf", "")
        if not expected or not hmac.compare_digest(sent, expected):
            raise Forbidden("missing or invalid CSRF token")
    return user


class LoginThrottle:
    """Per-client token bucket for login attempts (brute-force protection)."""

    def __init__(self, per_minute: int):
        self.per_minute = max(1, per_minute)
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        with self._lock:
            b = self._buckets.get(key)
            if b is None:
                if len(self._buckets) > 10_000:
                    self._buckets.clear()
                b = self._buckets[key] = TokenBucket(self.per_minute / 60.0, self.per_minute)
        return b.try_acquire()


class SelectiveGZipMiddleware:
    """gzip for pages, CSS/JS and JSON. Never for encrypted file streams (incompressible, and
    compressing would break Content-Length/Range) or for already-compressed fonts."""

    SKIP = ("/api/v1/files/content", "/static/fonts/")

    def __init__(self, app, minimum_size: int = 700):
        from starlette.middleware.gzip import GZipMiddleware

        self.app = app
        self.gzip = GZipMiddleware(app, minimum_size=minimum_size)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and not scope["path"].startswith(self.SKIP):
            await self.gzip(scope, receive, send)
        else:
            await self.app(scope, receive, send)


class RequestContextMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, hsts: bool = False):
        super().__init__(app)
        self.hsts = hsts

    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        token = request_id_var.set(rid)
        t0 = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers["X-Request-ID"] = rid
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        if request.url.path.startswith("/static/"):
            # Fingerprinted URLs (?v=<content hash>) never change: cache them for a year.
            versioned = "v=" in request.url.query
            response.headers["Cache-Control"] = (
                "public, max-age=31536000, immutable" if versioned else "public, max-age=86400"
            )
        else:
            response.headers.setdefault("Cache-Control", "no-store")
        if self.hsts:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Server-Timing"] = f"app;dur={(time.perf_counter() - t0) * 1000:.1f}"
        return response
