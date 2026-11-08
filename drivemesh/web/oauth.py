"""Linking Google accounts with the OAuth 2.0 web-server flow (PKCE, state, offline access).

One DriveMesh user can link many Google accounts; each is identified by its stable Google
'sub' so re-linking refreshes credentials instead of creating a duplicate (W-17).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from drivemesh.backends.gdrive import SCOPES, GoogleDriveBackend
from drivemesh.db import User
from drivemesh.errors import DriveMeshError, Forbidden, InvalidRequest
from drivemesh.web.security import current_user

log = logging.getLogger(__name__)
router = APIRouter()
DRIVE_FILE = "https://www.googleapis.com/auth/drive.file"


class NotConfigured(DriveMeshError):
    code, http_status = "oauth_not_configured", 503


def redirect_uri(settings) -> str:
    return settings.public_base_url.rstrip("/") + "/accounts/callback/google"


def make_flow(settings, state: str | None = None):
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
        scopes=SCOPES,
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


@router.get("/accounts/link/google")
def link_google(request: Request, user: User = Depends(current_user)):
    dm = request.app.state.dm
    flow = make_flow(dm.settings)
    url, state = flow.authorization_url(
        access_type="offline", prompt="consent select_account", include_granted_scopes="false"
    )
    request.session["oauth_state"] = state
    request.session["oauth_verifier"] = flow.code_verifier
    return RedirectResponse(url, status_code=302)


@router.get("/accounts/callback/google")
def callback_google(
    request: Request,
    state: str = "",
    code: str = "",
    error: str = "",
    user: User = Depends(current_user),
):
    dm = request.app.state.dm
    expected = request.session.pop("oauth_state", None)
    verifier = request.session.pop("oauth_verifier", None)
    if error:
        raise InvalidRequest(f"Google returned an error: {error}")
    if not expected or state != expected:
        raise Forbidden("OAuth state mismatch (possible CSRF); start linking again")
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
    return RedirectResponse("/app?linked=1", status_code=303)
