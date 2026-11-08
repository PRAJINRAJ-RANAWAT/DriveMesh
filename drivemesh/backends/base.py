"""Storage backend interface (the DataNode analog) and error taxonomy.

Backends are deliberately dumb object stores: put/get/delete opaque, bounded-size objects.
All intelligence (placement, replication, verification) lives in the metadata service.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable


class BackendError(Exception):
    """Base class. `retryable` tells the resilience layer whether to retry."""

    retryable = False


class TransientError(BackendError):
    """Timeouts, connection resets, 5xx, 429 and rate-limit 403s."""

    retryable = True


class NotFoundError(BackendError):
    """Object (or the account's root) does not exist."""


class AuthError(BackendError):
    """Credentials invalid, expired or revoked (401 / invalid_grant)."""


class QuotaFullError(BackendError):
    """Account storage quota exhausted (403 storageQuotaExceeded)."""


class PermanentError(BackendError):
    """Any other non-retryable failure."""


@dataclass(frozen=True)
class PutResult:
    remote_id: str
    size: int


@dataclass(frozen=True)
class ObjectInfo:
    name: str
    remote_id: str
    size: int
    created_at: datetime | None = None


@dataclass(frozen=True)
class HealthInfo:
    capacity_bytes: int | None
    used_bytes: int | None
    free_bytes: int | None


@runtime_checkable
class StorageBackend(Protocol):
    provider: str

    def put(self, name: str, data: bytes) -> PutResult: ...

    def get(self, remote_id: str) -> bytes: ...

    def delete(self, remote_id: str) -> None: ...

    def find(self, name: str) -> str | None: ...

    def list(self, prefix: str = "dm-") -> Iterator[ObjectInfo]: ...

    def health(self) -> HealthInfo: ...
