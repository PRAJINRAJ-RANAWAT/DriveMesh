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
    uid = request.session.get("uid")
    if not uid:
        raise Unauthorized("login required")
    dm = request.app.state.dm
    with dm.session() as s:
        user = s.get(User, uid)
    if user is None:
        request.session.clear()
        raise Unauthorized("login required")
    return user


def ensure_csrf_token(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = request.session["csrf"] = secrets.token_urlsafe(32)
    return tok


def require_csrf(request: Request, user: User = Depends(current_user)) -> User:
    """Synchronizer-token CSRF defence for every state-changing request."""
    if request.method not in SAFE_METHODS:
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
            # fonts/CSS/JS are public and versioned with the app; cache them for a day
            response.headers["Cache-Control"] = "public, max-age=86400"
        else:
            response.headers.setdefault("Cache-Control", "no-store")
        if self.hsts:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Server-Timing"] = f"app;dur={(time.perf_counter() - t0) * 1000:.1f}"
        return response
