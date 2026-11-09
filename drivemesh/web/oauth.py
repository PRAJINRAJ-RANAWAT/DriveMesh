"""Google OAuth 2.0 web-server flow (PKCE + state) for two purposes, one redirect URI:

* **Sign in / sign up** (`/auth/google`): OpenID Connect with `openid email` only. The verified
  ID token's `sub` identifies the DriveMesh user; a new `sub` creates an account (open sign-up).
  Google authenticates the person (password, 2FA); DriveMesh stores no password for them.
* **Link storage** (`/accounts/link/google`): `drive.file` + offline access. Each linked Gmail
  is identified by its stable `sub`, so re-linking refreshes credentials (W-17), and one Gmail
  can belong to only one DriveMesh user.

Only the person who can sign in to a Gmail can approve either flow: DriveMesh never sees
Google passwords and receives tokens only after that account's owner consents.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from drivemesh.backends.gdrive import SCOPES, GoogleDriveBackend
from drivemesh.db import User
from drivemesh.errors import DriveMeshError, Forbidden, InvalidRequest, RateLimited
from drivemesh.web.auth import session_user, start_session
from drivemesh.web.security import current_user

log = logging.getLogger(__name__)
router = APIRouter()
DRIVE_FILE = "https://www.googleapis.com/auth/drive.file"
LOGIN_SCOPES = ["openid", "https://www.googleapis.com/auth/userinfo.email"]


class NotConfigured(DriveMeshError):
    code, http_status = "oauth_not_configured", 503


def redirect_uri(settings) -> str:
    return settings.public_base_url.rstrip("/") + "/accounts/callback/google"


def make_flow(settings, state: str | None = None, scopes: list[str] | None = None):
    """Factored out so tests can substitute a fake flow."""
    from google_auth_oauthlib.flow import Flow

    if not settings.google_client_id or not settings.google_client_secret:
        raise NotConfigured("Google OAuth is not configured (see MANUAL_STEPS.md)")
    config = {
        "web": {
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret.get_secret_value(),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri(settings)],
        }
    }
    return Flow.from_client_config(
        config,
        scopes=scopes or SCOPES,
        state=state,
        redirect_uri=redirect_uri(settings),
        autogenerate_code_verifier=True,
    )


def verify_id_token(token: str, client_id: str) -> dict[str, Any]:
    from google.auth.transport.requests import Request as GoogleRequest
    from google.oauth2 import id_token

    return id_token.verify_oauth2_token(token, GoogleRequest(), client_id)


def drive_backend_for(dm, creds) -> GoogleDriveBackend:
    return GoogleDriveBackend.from_credentials(creds)


def _remember(request: Request, flow, state: str, purpose: str) -> None:
    request.session["oauth_state"] = state
    request.session["oauth_verifier"] = flow.code_verifier
    request.session["oauth_purpose"] = purpose


@router.get("/auth/google")
def sign_in_with_google(request: Request):
    """'Continue with Google': sign in, or create a DriveMesh account on first use."""
    if session_user(request) is not None:
        return RedirectResponse("/app", status_code=303)
    flow = make_flow(request.app.state.dm.settings, scopes=LOGIN_SCOPES)
    url, state = flow.authorization_url(prompt="select_account", include_granted_scopes="false")
    _remember(request, flow, state, "login")
    return RedirectResponse(url, status_code=302)


@router.get("/accounts/link/google")
def link_google(request: Request, user: User = Depends(current_user)):
    dm = request.app.state.dm
    flow = make_flow(dm.settings)
    url, state = flow.authorization_url(
        access_type="offline", prompt="consent select_account", include_granted_scopes="false"
    )
    _remember(request, flow, state, "link")
    return RedirectResponse(url, status_code=302)


def _finish_sign_in(request: Request, dm, state: str, code: str, verifier: str | None):
    client = request.client.host if request.client else "unknown"
    if not request.app.state.login_throttle.allow(client):
        raise RateLimited("too many sign-in attempts; wait a minute and try again")
    flow = make_flow(dm.settings, state=state, scopes=LOGIN_SCOPES)
    flow.code_verifier = verifier
    flow.fetch_token(code=code)
    claims = verify_id_token(flow.credentials.id_token, dm.settings.google_client_id)
    if not claims.get("email_verified"):
        raise InvalidRequest("this Google account's email address is not verified")
    user, created = dm.get_or_create_google_user(claims["sub"], claims.get("email"))
    start_session(request, dm, user)
    dm.event("login", user_id=user.id, client=client, method="google")
    if created:
        dm.event("user_signed_up", user_id=user.id, method="google")
    return RedirectResponse("/accounts?welcome=1" if created else "/app", status_code=303)


@router.get("/accounts/callback/google")
def callback_google(request: Request, state: str = "", code: str = "", error: str = ""):
    dm = request.app.state.dm
    expected = request.session.pop("oauth_state", None)
    verifier = request.session.pop("oauth_verifier", None)
    purpose = request.session.pop("oauth_purpose", "link")
    if error:
        raise InvalidRequest(f"Google returned an error: {error}")
    if not expected or state != expected:
        raise Forbidden("OAuth state mismatch (possible CSRF); start again")
    if purpose == "login":
        return _finish_sign_in(request, dm, state, code, verifier)
    user = current_user(request)  # linking storage needs a signed-in DriveMesh user
    flow = make_flow(dm.settings, state=state)
    flow.code_verifier = verifier
    flow.fetch_token(code=code)
    creds = flow.credentials
    granted = set(getattr(creds, "granted_scopes", None) or creds.scopes or [])
    if DRIVE_FILE not in granted:
        raise InvalidRequest("Drive access was not granted; tick the Drive checkbox and retry")
    if not creds.refresh_token:
        raise InvalidRequest(
            "Google did not return a refresh token; remove DriveMesh at "
            "myaccount.google.com/permissions and link again"
        )
    claims = verify_id_token(creds.id_token, dm.settings.google_client_id)
    sub, email = claims["sub"], claims.get("email")
    backend = drive_backend_for(dm, creds)
    folder_id = backend.ensure_folder()
    acc = dm.accounts.link(
        user.id,
        "gdrive",
        sub,
        email or f"google:{sub[:8]}",
        email=email,
        config={"folder_id": folder_id},
        secret={"refresh_token": creds.refresh_token, "token": creds.token},
    )
    try:
        from drivemesh.core.workers import health_check

        health_check(dm)
    except Exception:
        log.exception("post-link health check failed")
    log.info("google account linked", extra={"account_id": acc.id})
    return RedirectResponse("/accounts?linked=1", status_code=303)
