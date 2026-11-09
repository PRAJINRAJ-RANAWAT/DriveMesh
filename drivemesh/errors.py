"""Service-level errors. Each maps to an HTTP status and a stable error code."""

from __future__ import annotations


class DriveMeshError(Exception):
    code = "error"
    http_status = 500


class NotFound(DriveMeshError):
    code, http_status = "not_found", 404


class AlreadyExists(DriveMeshError):
    code, http_status = "already_exists", 409


class Conflict(DriveMeshError):
    code, http_status = "conflict", 409


class InvalidPath(DriveMeshError):
    code, http_status = "invalid_path", 400


class InvalidRequest(DriveMeshError):
    code, http_status = "invalid_request", 400


class TooLarge(DriveMeshError):
    code, http_status = "too_large", 413


class InsufficientAccounts(DriveMeshError):
    code, http_status = "insufficient_accounts", 503


class SafeMode(DriveMeshError):
    code, http_status = "safe_mode", 503


class ChunkUnavailable(DriveMeshError):
    code, http_status = "chunk_unavailable", 503


class IntegrityFailure(DriveMeshError):
    code, http_status = "integrity_failure", 500


class Unauthorized(DriveMeshError):
    code, http_status = "unauthorized", 401


class Forbidden(DriveMeshError):
    code, http_status = "forbidden", 403


class RateLimited(DriveMeshError):
    code, http_status = "rate_limited", 429
