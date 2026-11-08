"""Google Drive backend (scope: drive.file).

* All objects live flat in one app-created folder ("DriveMesh"); names are opaque (dm-<id>).
* Uploads use Drive resumable sessions: a dropped connection resumes from the last
  acknowledged byte instead of resending the object.
* httplib2 is not thread-safe, so each thread builds its own service object.
* HTTP errors are mapped onto the backend error taxonomy; only transient ones get retried
  (by ResilientBackend, with full-jitter backoff).
"""

from __future__ import annotations

import io
import json
import logging
import socket
import ssl
import threading
from collections.abc import Callable, Iterator
from datetime import datetime
from typing import Any

from drivemesh.backends.base import (
    AuthError,
    BackendError,
    HealthInfo,
    NotFoundError,
    ObjectInfo,
    PermanentError,
    PutResult,
    QuotaFullError,
    TransientError,
)

log = logging.getLogger(__name__)

SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/drive.file",
]
FOLDER_MIME = "application/vnd.google-apps.folder"
APP_FOLDER = "DriveMesh"
UPLOAD_CHUNK = 4 * 1024 * 1024  # resumable request size (multiple of 256 KiB)
RATE_REASONS = {"userRateLimitExceeded", "rateLimitExceeded", "backendError", "internalError"}
QUOTA_REASONS = {"storageQuotaExceeded", "quotaExceeded"}
AUTH_REASONS = {"authError", "insufficientPermissions", "appNotAuthorizedToFile", "invalid_grant"}


def _http_error_reason(exc: Any) -> str:
    try:
        details = getattr(exc, "error_details", None) or []
        if details and isinstance(details, list) and isinstance(details[0], dict):
            return str(details[0].get("reason", ""))
        payload = json.loads(exc.content.decode())
        errs = payload.get("error", {}).get("errors") or [{}]
        return str(errs[0].get("reason", ""))
    except Exception:  # noqa: BLE001
        return ""


def map_error(exc: BaseException) -> BackendError:
    from google.auth.exceptions import RefreshError, TransportError
    from googleapiclient.errors import HttpError, ResumableUploadError

    if isinstance(exc, BackendError):
        return exc
    if isinstance(exc, RefreshError):
        return AuthError(f"token refresh failed: {exc}")
    if isinstance(exc, TransportError):
        return TransientError(f"transport: {exc}")
    if isinstance(exc, (HttpError, ResumableUploadError)):
        status = int(getattr(exc.resp, "status", 0) or 0)
        reason = _http_error_reason(exc)
        msg = f"HTTP {status} {reason}".strip()
        if status == 404:
            return NotFoundError(msg)
        if status == 401 or reason in AUTH_REASONS:
            return AuthError(msg)
        if status == 403 and reason in QUOTA_REASONS:
            return QuotaFullError(msg)
        if status in (408, 429) or status >= 500 or reason in RATE_REASONS:
            return TransientError(msg)
        return PermanentError(msg)
    if isinstance(exc, (TimeoutError, ConnectionError, socket.timeout, ssl.SSLError, OSError)):
        return TransientError(f"{type(exc).__name__}: {exc}")
    try:
        import httplib2

        if isinstance(exc, httplib2.HttpLib2Error):
            return TransientError(f"httplib2: {exc}")
    except ImportError:  # pragma: no cover
        pass
    return PermanentError(f"{type(exc).__name__}: {exc}")


def revoke_token(token: str, *, timeout: float = 10.0) -> bool:
    """Best-effort revocation of a refresh/access token at Google (used on unlink)."""
    import urllib.parse
    import urllib.request

    data = urllib.parse.urlencode({"token": token}).encode()
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/revoke",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return 200 <= resp.status < 300
    except Exception:  # noqa: BLE001 - revocation is best effort
        log.warning("token revocation failed")
        return False


class GoogleDriveBackend:
    provider = "gdrive"

    def __init__(
        self,
        service_factory: Callable[[], Any],
        folder_id: str | None = None,
        *,
        upload_chunk: int = UPLOAD_CHUNK,
        on_folder_created: Callable[[str], None] | None = None,
    ):
        self._factory = service_factory
        self._local = threading.local()
        self._folder_lock = threading.Lock()
        self.folder_id = folder_id
        self.upload_chunk = upload_chunk
        self._on_folder_created = on_folder_created

    @classmethod
    def from_credentials(cls, credentials: Any, folder_id: str | None = None, **kw: Any):
        def factory() -> Any:
            import google_auth_httplib2
            import httplib2
            from googleapiclient.discovery import build

            http = google_auth_httplib2.AuthorizedHttp(credentials, http=httplib2.Http(timeout=60))
            return build("drive", "v3", http=http, cache_discovery=False)

        return cls(factory, folder_id, **kw)

    @classmethod
    def from_secret(cls, settings: Any, secret: dict[str, Any], config: dict[str, Any]):
        from google.oauth2.credentials import Credentials

        if not secret.get("refresh_token"):
            raise AuthError("no refresh token stored for this account; re-link it")
        creds = Credentials(
            token=secret.get("token"),
            refresh_token=secret["refresh_token"],
            token_uri="https://oauth2.googleapis.com/token",  # noqa: S106 - public endpoint
            client_id=settings.google_client_id,
            client_secret=(
                settings.google_client_secret.get_secret_value()
                if settings.google_client_secret
                else None
            ),
            scopes=SCOPES,
        )
        return cls.from_credentials(creds, config.get("folder_id"))

    # ---- helpers -----------------------------------------------------------------------------
    @property
    def svc(self) -> Any:
        s = getattr(self._local, "svc", None)
        if s is None:
            s = self._local.svc = self._factory()
        return s

    def _exec(self, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise map_error(exc) from exc

    def ensure_folder(self) -> str:
        if self.folder_id:
            return self.folder_id
        with self._folder_lock:
            if self.folder_id:
                return self.folder_id
            q = f"name = '{APP_FOLDER}' and mimeType = '{FOLDER_MIME}' and trashed = false"
            found = self._exec(
                lambda: self.svc.files().list(q=q, fields="files(id)", pageSize=1).execute()
            ).get("files", [])
            if found:
                self.folder_id = found[0]["id"]
            else:
                body = {"name": APP_FOLDER, "mimeType": FOLDER_MIME}
                self.folder_id = self._exec(
                    lambda: self.svc.files().create(body=body, fields="id").execute()
                )["id"]
                if self._on_folder_created:
                    self._on_folder_created(self.folder_id)
            return self.folder_id

    # ---- StorageBackend ----------------------------------------------------------------------
    def put(self, name: str, data: bytes) -> PutResult:
        from googleapiclient.http import MediaIoBaseUpload

        folder = self.ensure_folder()
        body = {"name": name, "parents": [folder], "appProperties": {"dm": "1"}}
        media = MediaIoBaseUpload(
            io.BytesIO(data),
            mimetype="application/octet-stream",
            chunksize=self.upload_chunk,
            resumable=True,
        )
        request = self._exec(
            lambda: self.svc.files().create(body=body, media_body=media, fields="id,size")
        )
        response = None
        resume_failures = 0
        while response is None:
            try:
                _, response = request.next_chunk()
                resume_failures = 0
            except BaseException as exc:
                err = map_error(exc)
                # Resume the same session on transient errors instead of restarting the object.
                if isinstance(err, TransientError) and resume_failures < 3:
                    resume_failures += 1
                    log.info("resuming upload", extra={"object_name": name, "error": str(err)})
                    continue
                raise err from exc
        return PutResult(remote_id=response["id"], size=int(response.get("size", len(data))))

    def get(self, remote_id: str) -> bytes:
        from googleapiclient.http import MediaIoBaseDownload

        buf = io.BytesIO()

        def run() -> bytes:
            req = self.svc.files().get_media(fileId=remote_id)
            dl = MediaIoBaseDownload(buf, req, chunksize=self.upload_chunk)
            done = False
            while not done:
                _, done = dl.next_chunk()
            return buf.getvalue()

        return self._exec(run)

    def delete(self, remote_id: str) -> None:
        self._exec(lambda: self.svc.files().delete(fileId=remote_id).execute())

    def find(self, name: str) -> str | None:
        folder = self.ensure_folder()
        q = f"name = '{name}' and '{folder}' in parents and trashed = false"
        files = self._exec(
            lambda: self.svc.files().list(q=q, fields="files(id)", pageSize=10).execute()
        ).get("files", [])
        return files[0]["id"] if files else None

    def list(self, prefix: str = "dm-") -> Iterator[ObjectInfo]:
        folder = self.ensure_folder()
        q = f"'{folder}' in parents and name contains '{prefix}' and trashed = false"
        token = None
        while True:
            page = self._exec(
                lambda t=token: (
                    self.svc.files()
                    .list(
                        q=q,
                        pageSize=1000,
                        pageToken=t,
                        fields="nextPageToken, files(id, name, size, createdTime)",
                    )
                    .execute()
                )
            )
            for f in page.get("files", []):
                if not f["name"].startswith(prefix):
                    continue
                created = None
                if f.get("createdTime"):
                    created = datetime.fromisoformat(f["createdTime"].replace("Z", "+00:00"))
                    created = created.replace(tzinfo=None)
                yield ObjectInfo(f["name"], f["id"], int(f.get("size", 0)), created)
            token = page.get("nextPageToken")
            if not token:
                return

    def health(self) -> HealthInfo:
        about = self._exec(
            lambda: self.svc.about().get(fields="storageQuota, user(emailAddress)").execute()
        )
        self.ensure_folder()
        q = about.get("storageQuota", {})
        limit = int(q["limit"]) if q.get("limit") else None
        usage = int(q.get("usage", 0))
        return HealthInfo(limit, usage, None if limit is None else max(limit - usage, 0))
