"""Linked accounts: stable ids, encrypted credentials, backend construction and health state."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from drivemesh.backends.base import StorageBackend
from drivemesh.crypto import seal, unseal
from drivemesh.db import LinkedAccount
from drivemesh.errors import InvalidRequest, NotFound
from drivemesh.placement import Candidate
from drivemesh.resilience import ResilientBackend, RetryPolicy

if TYPE_CHECKING:
    from drivemesh.core.system import DriveMesh

Wrapper = Callable[[StorageBackend], StorageBackend]
BackendFactory = Callable[[LinkedAccount, dict[str, Any] | None], StorageBackend]

# Accounts whose replicas can still be read.
READABLE_STATES = ("active", "full", "draining")


def _default_factory(dm: DriveMesh) -> BackendFactory:
    def factory(acc: LinkedAccount, secret: dict[str, Any] | None) -> StorageBackend:
        cfg = json.loads(acc.config_json or "{}")
        if acc.provider == "local":
            from drivemesh.backends.local import LocalDiskBackend

            return LocalDiskBackend(
                cfg["path"], capacity_bytes=cfg.get("capacity_bytes"), create=False
            )
        if acc.provider == "memory":
            from drivemesh.backends.local import MemoryBackend

            return MemoryBackend.named(acc.external_id, cfg.get("capacity_bytes"))
        if acc.provider == "gdrive":
            from drivemesh.backends.gdrive import GoogleDriveBackend

            return GoogleDriveBackend.from_secret(dm.settings, secret or {}, cfg)
        if acc.provider == "s3":
            from drivemesh.backends.s3 import S3Backend

            return S3Backend.from_config(cfg, secret or {})
        raise InvalidRequest(f"unknown provider {acc.provider!r}")

    return factory


class AccountRegistry:
    def __init__(self, dm: DriveMesh):
        self.dm = dm
        self._cache: dict[str, ResilientBackend] = {}
        self._wrappers: dict[str, Wrapper] = {}
        self._lock = threading.Lock()
        self.factory: BackendFactory = _default_factory(dm)

    # --- backends ---------------------------------------------------------------------------
    def backend(self, account_id: str) -> ResilientBackend:
        with self._lock:
            cached = self._cache.get(account_id)
        if cached is not None:
            return cached
        with self.dm.session() as s:
            acc = s.get(LinkedAccount, account_id)
            if acc is None:
                raise NotFound(f"account {account_id} not found")
            secret = self.read_secret(acc)
            raw = self.factory(acc, secret)
        wrapper = self._wrappers.get(account_id)
        if wrapper is not None:
            raw = wrapper(raw)
        st = self.dm.settings
        rb = ResilientBackend(
            raw,
            account_id=account_id,
            policy=RetryPolicy(st.retry_max_attempts, st.retry_base_seconds, st.retry_cap_seconds),
            concurrency=st.account_concurrency,
            rate=st.account_rate,
            burst=st.account_burst,
            sleep=self.dm.sleep,
        )
        with self._lock:
            self._cache.setdefault(account_id, rb)
            return self._cache[account_id]

    def wrap(self, account_id: str, wrapper: Wrapper) -> None:
        """Install a wrapper (e.g. FaultyBackend) for an account. Used by chaos tests/benchmarks."""
        with self._lock:
            self._wrappers[account_id] = wrapper
            self._cache.pop(account_id, None)

    def invalidate(self, account_id: str) -> None:
        with self._lock:
            self._cache.pop(account_id, None)

    # --- secrets ----------------------------------------------------------------------------
    def read_secret(self, acc: LinkedAccount) -> dict[str, Any] | None:
        if not acc.secret_blob:
            return None
        return json.loads(unseal(self.dm.keyring, acc.secret_blob, f"account|{acc.id}"))

    def write_secret(self, acc: LinkedAccount, secret: dict[str, Any] | None) -> None:
        acc.secret_blob = (
            None
            if secret is None
            else seal(self.dm.keyring, json.dumps(secret).encode(), f"account|{acc.id}")
        )

    # --- linking ----------------------------------------------------------------------------
    def link(
        self,
        user_id: str,
        provider: str,
        external_id: str,
        display_name: str,
        *,
        config: dict[str, Any] | None = None,
        secret: dict[str, Any] | None = None,
        email: str | None = None,
    ) -> LinkedAccount:
        """Link an account, or re-link (refresh credentials) if this external account exists."""
        with self.dm.session() as s:
            acc = s.scalars(
                select(LinkedAccount).where(
                    LinkedAccount.user_id == user_id,
                    LinkedAccount.provider == provider,
                    LinkedAccount.external_id == external_id,
                )
            ).first()
            relink = acc is not None
            if acc is None:
                acc = LinkedAccount(
                    user_id=user_id,
                    provider=provider,
                    external_id=external_id,
                    display_name=display_name,
                    created_at=self.dm.now(),
                )
                s.add(acc)
                s.flush()
            if config is not None:
                merged = json.loads(acc.config_json or "{}") if relink else {}
                merged.update(config)
                acc.config_json = json.dumps(merged)
            if secret is not None:
                self.write_secret(acc, secret)
            acc.email = email or acc.email
            if acc.state in ("revoked", "unreachable", "removed") or not relink:
                acc.state = "active"
            acc.consecutive_failures, acc.unhealthy_since, acc.last_error = 0, None, None
            s.flush()
            acc_id = acc.id
        self.invalidate(acc_id)
        self.dm.event(
            "account_relinked" if relink else "account_linked",
            user_id=user_id,
            account_id=acc_id,
            provider=provider,
        )
        return self.get(acc_id)

    def link_local(
        self, user_id: str, path: str, name: str | None = None, capacity_bytes: int | None = None
    ) -> LinkedAccount:
        from pathlib import Path

        p = str(Path(path).expanduser().resolve())
        Path(p).mkdir(parents=True, exist_ok=True)  # created once, at link time
        cfg: dict[str, Any] = {"path": p}
        if capacity_bytes is not None:
            cfg["capacity_bytes"] = capacity_bytes
        return self.link(user_id, "local", p, name or Path(p).name, config=cfg)

    def link_memory(
        self, user_id: str, name: str, capacity_bytes: int | None = None
    ) -> LinkedAccount:
        cfg: dict[str, Any] = {}
        if capacity_bytes is not None:
            cfg["capacity_bytes"] = capacity_bytes
        return self.link(user_id, "memory", name, name, config=cfg)

    # --- queries / state --------------------------------------------------------------------
    def get(self, account_id: str) -> LinkedAccount:
        with self.dm.session() as s:
            acc = s.get(LinkedAccount, account_id)
            if acc is None:
                raise NotFound(f"account {account_id} not found")
            return acc

    def list(
        self, user_id: str | None = None, include_removed: bool = False
    ) -> list[LinkedAccount]:
        with self.dm.session() as s:
            q = select(LinkedAccount).order_by(LinkedAccount.created_at, LinkedAccount.id)
            if user_id is not None:
                q = q.where(LinkedAccount.user_id == user_id)
            if not include_removed:
                q = q.where(LinkedAccount.state != "removed")
            return list(s.scalars(q))

    @staticmethod
    def candidates(s: Session, user_id: str) -> list[Candidate]:
        rows = s.scalars(
            select(LinkedAccount).where(
                LinkedAccount.user_id == user_id, LinkedAccount.state != "removed"
            )
        )
        return [Candidate(a.id, a.provider, a.free_bytes, a.state) for a in rows]

    def mark_state(self, account_id: str, state: str, error: str | None = None) -> None:
        with self.dm.session() as s:
            acc = s.get(LinkedAccount, account_id)
            if acc is None or acc.state in ("removed", "draining") or acc.state == state:
                return
            old = acc.state
            acc.state = state
            if error:
                acc.last_error = error[:2000]
            if state in ("unreachable", "revoked") and acc.unhealthy_since is None:
                acc.unhealthy_since = self.dm.now()
            if state in ("active", "full"):
                acc.unhealthy_since = None
            user_id = acc.user_id
        self.dm.event(
            "account_state",
            level="warning" if state not in ("active",) else "info",
            user_id=user_id,
            account_id=account_id,
            old=old,
            new=state,
            error=error,
        )
