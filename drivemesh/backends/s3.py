"""S3-compatible backend (AWS S3, MinIO, Cloudflare R2, Backblaze B2 S3 API...).

Optional: requires the 's3' extra (boto3). Objects are stored under an optional key prefix.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
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

_TRANSIENT = {
    "SlowDown",
    "RequestTimeout",
    "InternalError",
    "ServiceUnavailable",
    "Throttling",
    "ThrottlingException",
    "RequestTimeTooSkewed",
    "500",
    "503",
}
_AUTH = {
    "AccessDenied",
    "InvalidAccessKeyId",
    "SignatureDoesNotMatch",
    "ExpiredToken",
    "InvalidToken",
    "403",
}
_NOT_FOUND = {"NoSuchKey", "404", "NotFound", "NoSuchBucket"}


def map_error(exc: BaseException) -> BackendError:
    from botocore.exceptions import (
        ClientError,
        EndpointConnectionError,
        ReadTimeoutError,
    )
    from botocore.exceptions import (
        ConnectionError as BotoConnectionError,
    )

    if isinstance(exc, BackendError):
        return exc
    if isinstance(exc, ClientError):
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in _NOT_FOUND:
            return NotFoundError(code)
        if code in _AUTH:
            return AuthError(code)
        if code in _TRANSIENT:
            return TransientError(code)
        if code in ("QuotaExceeded", "EntityTooLarge"):
            return QuotaFullError(code)
        return PermanentError(code or str(exc))
    if isinstance(
        exc,
        (
            EndpointConnectionError,
            ReadTimeoutError,
            BotoConnectionError,
            TimeoutError,
            ConnectionError,
        ),
    ):
        return TransientError(str(exc))
    return PermanentError(f"{type(exc).__name__}: {exc}")


class S3Backend:
    provider = "s3"

    def __init__(
        self, client: Any, bucket: str, prefix: str = "", capacity_bytes: int | None = None
    ):
        self.client = client  # boto3 clients are thread-safe
        self.bucket = bucket
        self.prefix = prefix
        self.capacity_bytes = capacity_bytes
        self._used: int | None = None
        self._lock = threading.Lock()

    @classmethod
    def from_config(cls, cfg: dict[str, Any], secret: dict[str, Any]) -> S3Backend:
        import boto3

        client = boto3.client(
            "s3",
            endpoint_url=cfg.get("endpoint_url"),
            region_name=cfg.get("region", "us-east-1"),
            aws_access_key_id=secret.get("access_key_id"),
            aws_secret_access_key=secret.get("secret_access_key"),
        )
        return cls(client, cfg["bucket"], cfg.get("prefix", ""), cfg.get("capacity_bytes"))

    def _key(self, name: str) -> str:
        return f"{self.prefix}{name}"

    def _call(self, fn):
        try:
            return fn()
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise map_error(exc) from exc

    def put(self, name: str, data: bytes) -> PutResult:
        if self.capacity_bytes is not None:
            used = self.health().used_bytes or 0
            if used + len(data) > self.capacity_bytes:
                raise QuotaFullError("configured capacity reached")
        key = self._key(name)
        self._call(lambda: self.client.put_object(Bucket=self.bucket, Key=key, Body=data))
        with self._lock:
            if self._used is not None:
                self._used += len(data)
        return PutResult(remote_id=key, size=len(data))

    def get(self, remote_id: str) -> bytes:
        return self._call(
            lambda: self.client.get_object(Bucket=self.bucket, Key=remote_id)["Body"].read()
        )

    def delete(self, remote_id: str) -> None:
        # S3 deletes are idempotent and never 404; HEAD first to honour the contract.
        self._call(lambda: self.client.head_object(Bucket=self.bucket, Key=remote_id))
        self._call(lambda: self.client.delete_object(Bucket=self.bucket, Key=remote_id))
        with self._lock:
            self._used = None

    def find(self, name: str) -> str | None:
        key = self._key(name)
        try:
            self._call(lambda: self.client.head_object(Bucket=self.bucket, Key=key))
        except NotFoundError:
            return None
        return key

    def list(self, prefix: str = "dm-") -> Iterator[ObjectInfo]:
        paginator = self.client.get_paginator("list_objects_v2")
        pages = self._call(
            lambda: list(paginator.paginate(Bucket=self.bucket, Prefix=self._key(prefix)))
        )
        for page in pages:
            for o in page.get("Contents", []):
                ts = o.get("LastModified")
                yield ObjectInfo(
                    name=o["Key"][len(self.prefix) :],
                    remote_id=o["Key"],
                    size=int(o["Size"]),
                    created_at=ts.replace(tzinfo=None) if ts else None,
                )

    def health(self) -> HealthInfo:
        with self._lock:
            cached = self._used
        if cached is None:
            cached = sum(o.size for o in self.list(""))
            with self._lock:
                self._used = cached
        cap = self.capacity_bytes
        return HealthInfo(cap, cached, None if cap is None else max(cap - cached, 0))
