"""Runtime configuration. Everything comes from environment variables (prefix DRIVEMESH_)
or an optional .env file. Secrets are SecretStr so they never appear in repr/logs."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

MiB = 1024 * 1024
GiB = 1024 * MiB


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DRIVEMESH_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    env: Literal["dev", "test", "prod"] = "dev"
    database_url: str = "sqlite:///./data/drivemesh.db"

    # Key management: "id1:base64key,id2:base64key"; active_key_id selects the wrapping key.
    master_keys: SecretStr | None = None
    active_key_id: str | None = None

    # Data path
    chunk_size: int = Field(8 * MiB, ge=64 * 1024, le=256 * MiB)
    default_rf: int = Field(2, ge=1, le=5)
    min_write_replicas: int | None = Field(None, ge=1, le=5)
    inflight_chunks: int = Field(4, ge=1, le=64)
    max_upload_bytes: int = 10 * GiB
    free_space_reserve: int = 256 * MiB

    # Per-account limits and retries
    account_concurrency: int = Field(4, ge=1, le=64)
    account_rate: float = Field(8.0, gt=0)
    account_burst: int = Field(16, ge=1)
    retry_max_attempts: int = Field(6, ge=1, le=20)
    retry_base_seconds: float = 0.5
    retry_cap_seconds: float = 32.0

    # Background workers
    workers_enabled: bool = True
    health_interval: float = 60.0
    dead_after_seconds: float = 600.0
    unreachable_after_failures: int = 3
    repair_interval: float = 30.0
    repair_batch: int = 100
    repair_parallelism: int = Field(8, ge=1, le=64)
    scrub_interval: float = 60.0
    scrub_bytes_per_second: int = 1 * MiB
    gc_interval: float = 300.0
    pending_lease_seconds: float = 300.0
    pending_replica_grace_seconds: float = 3600.0
    orphan_grace_seconds: float = 86400.0
    restore_quiet_seconds: float = 7 * 86400.0
    backup_interval: float = 900.0
    backup_keep: int = 5
    balancer_interval: float = 600.0
    balancer_threshold: float = 0.10
    balancer_batch: int = 50
    job_poll_interval: float = 1.0
    job_visibility_seconds: float = 300.0
    safe_mode_timeout: float = 120.0

    # Web
    session_secret: SecretStr | None = None
    secure_cookies: bool = False
    allow_local_backends: bool = True
    metrics_token: SecretStr | None = None
    public_base_url: str = "http://localhost:8000"
    login_rate_per_minute: int = 10
    # Optional first-start bootstrap of the owner account (for hosts without shell access).
    owner_username: str | None = None
    owner_password: SecretStr | None = None

    # Google OAuth (web application client)
    google_client_id: str | None = None
    google_client_secret: SecretStr | None = None

    # Logging
    log_level: str = "INFO"
    log_json: bool = True

    def production_problems(self) -> list[str]:
        """Settings that are unsafe for an internet-facing deployment."""
        problems = []
        if self.session_secret is None or len(self.session_secret.get_secret_value()) < 32:
            problems.append("DRIVEMESH_SESSION_SECRET must be set (>= 32 chars)")
        if self.master_keys is None:
            problems.append("DRIVEMESH_MASTER_KEYS must be set")
        if not self.secure_cookies:
            problems.append("DRIVEMESH_SECURE_COOKIES must be true (HTTPS only)")
        if self.allow_local_backends:
            problems.append(
                "DRIVEMESH_ALLOW_LOCAL_BACKENDS must be false (server filesystem access)"
            )
        if not self.public_base_url.startswith("https://"):
            problems.append("DRIVEMESH_PUBLIC_BASE_URL must be https://")
        if self.database_url.startswith("sqlite"):
            problems.append("use Postgres in production (container disks are ephemeral)")
        return problems

    def write_quorum(self, rf: int) -> int:
        """Replicas that must succeed before a chunk counts as written (default: all)."""
        return min(rf, self.min_write_replicas or rf)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
