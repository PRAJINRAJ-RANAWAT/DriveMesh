"""Metadata schema (the NameNode's state) and engine setup.

Invariants enforced by the database itself:
  * one committed version per path        -> ux_files_live_path (partial unique index)
  * no two replicas of a chunk per account -> uq_replica_chunk_account
  * an external account (e.g. one Gmail) is linked by at most one DriveMesh user at a time
                                           -> ux_accounts_external_live (partial unique index)
  * one DriveMesh user per Google identity -> users.google_sub UNIQUE
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    """Naive UTC timestamp (portable across SQLite and Postgres)."""
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


NO_PASSWORD = "!"  # noqa: S105 - not a password: the password_hash of accounts that sign in with Google only (never verifies)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("google_sub", name="uq_users_google_sub"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(255), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    # Google identity used to sign in ("sub" claim of the verified ID token).
    google_sub: Mapped[str | None] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(320))
    # Operators (CLI-created / bootstrap owner) may link server-local folders and read
    # global metrics; self-service Google sign-ups may not.
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    # "Sign out everywhere": sessions and JWTs issued before this instant are invalid.
    tokens_valid_after: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class UserSession(Base):
    """Server-side record of a browser session. The cookie carries a random id; only its
    SHA-256 is stored, so a DB leak does not reveal live session ids."""

    __tablename__ = "sessions"
    id_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)
    user_agent: Mapped[str | None] = mapped_column(String(255))
    ip: Mapped[str | None] = mapped_column(String(64))


class RefreshToken(Base):
    """Opaque, rotating refresh tokens for API clients (only the SHA-256 is stored).

    Each refresh consumes the token and issues a new one in the same family; presenting a
    consumed token again (reuse = likely theft) revokes the whole family."""

    __tablename__ = "refresh_tokens"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    family_id: Mapped[str] = mapped_column(String(32), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)


ACCOUNT_STATES = ("active", "full", "unreachable", "revoked", "draining", "removed")


class LinkedAccount(Base):
    __tablename__ = "linked_accounts"
    __table_args__ = (
        # Global, not per user: two DriveMesh users sharing one Gmail would share its app
        # folder, and one unlinking would revoke Google's grant for the other.
        Index(
            "ux_accounts_external_live",
            "provider",
            "external_id",
            unique=True,
            sqlite_where=text("state != 'removed'"),
            postgresql_where=text("state != 'removed'"),
        ),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    provider: Mapped[str] = mapped_column(String(16))
    external_id: Mapped[str] = mapped_column(String(512))
    display_name: Mapped[str] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(320))
    config_json: Mapped[str] = mapped_column(Text, default="{}")
    secret_blob: Mapped[bytes | None] = mapped_column(LargeBinary)
    state: Mapped[str] = mapped_column(String(16), default="active")
    capacity_bytes: Mapped[int | None] = mapped_column(BigInteger)
    used_bytes: Mapped[int | None] = mapped_column(BigInteger)
    free_bytes: Mapped[int | None] = mapped_column(BigInteger)
    last_health_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime)
    unhealthy_since: Mapped[datetime | None] = mapped_column(DateTime)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Folder(Base):
    __tablename__ = "folders"
    __table_args__ = (UniqueConstraint("user_id", "parent_id", "name", name="uq_folder_name"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("folders.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


FILE_STATES = ("pending", "committed", "deleting", "deleted", "aborted")


class File(Base):
    __tablename__ = "files"
    __table_args__ = (
        Index(
            "ux_files_live_path",
            "user_id",
            "folder_id",
            "name",
            unique=True,
            sqlite_where=text("state = 'committed'"),
            postgresql_where=text("state = 'committed'"),
        ),
        Index("ix_files_state_lease", "state", "lease_expires_at"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    folder_id: Mapped[str] = mapped_column(ForeignKey("folders.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    version: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(16))
    size: Mapped[int | None] = mapped_column(BigInteger)
    sha256: Mapped[str | None] = mapped_column(String(64))
    chunk_size: Mapped[int] = mapped_column(Integer)
    chunk_count: Mapped[int | None] = mapped_column(Integer)
    scheme: Mapped[str] = mapped_column(String(16), default="replica")
    rf: Mapped[int] = mapped_column(Integer)
    ec_k: Mapped[int | None] = mapped_column(Integer)
    ec_m: Mapped[int | None] = mapped_column(Integer)
    wrapped_dek: Mapped[bytes] = mapped_column(LargeBinary)
    kek_id: Mapped[str] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    committed_at: Mapped[datetime | None] = mapped_column(DateTime)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime)


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (UniqueConstraint("file_id", "idx", name="uq_chunk_file_idx"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    file_id: Mapped[str] = mapped_column(ForeignKey("files.id", ondelete="CASCADE"), index=True)
    idx: Mapped[int] = mapped_column(Integer)
    plain_size: Mapped[int] = mapped_column(Integer)
    # Size and SHA-256 of the full encrypted chunk object (before any erasure coding).
    object_size: Mapped[int | None] = mapped_column(Integer)
    object_sha256: Mapped[str | None] = mapped_column(String(64))


REPLICA_STATES = ("pending", "ok", "missing", "corrupt", "deleting")


class Replica(Base):
    __tablename__ = "replicas"
    __table_args__ = (
        UniqueConstraint("chunk_id", "account_id", name="uq_replica_chunk_account"),
        # Composite (chunk_id, state): matches both equality predicates of the repair scan and
        # fsck, so the planner can never pick the (state, verified_at) index for the join,
        # which made those queries quadratic (see results.md R-007).
        Index("ix_replicas_chunk_state", "chunk_id", "state"),
        Index("ix_replicas_account_state", "account_id", "state"),
        Index("ix_replicas_state_verified", "state", "verified_at"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    chunk_id: Mapped[str] = mapped_column(ForeignKey("chunks.id", ondelete="CASCADE"))
    account_id: Mapped[str] = mapped_column(ForeignKey("linked_accounts.id"))
    shard_idx: Mapped[int] = mapped_column(Integer, default=0)
    remote_id: Mapped[str | None] = mapped_column(String(512))
    size: Mapped[int | None] = mapped_column(Integer)
    sha256: Mapped[str | None] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime)

    @property
    def object_name(self) -> str:
        return object_name(self.id)


def object_name(replica_id: str) -> str:
    """Deterministic remote name: lets GC find objects whose upload outran the DB update."""
    return f"dm-{replica_id}"


class RemoteDelete(Base):
    """A stored object that must be deleted from an account that can't be reached right now.

    When a file is deleted while one of its accounts is disconnected (unreachable or access
    revoked), the replica row moves here so the user's delete completes immediately; GC
    deletes the object as soon as the account is reachable again (gc.drain_remote_deletes).
    """

    __tablename__ = "remote_deletes"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(ForeignKey("linked_accounts.id"), index=True)
    remote_id: Mapped[str | None] = mapped_column(String(512))  # NULL: upload outcome unknown
    object_name: Mapped[str] = mapped_column(String(64))  # dm-<replica id>, to find it by name
    reason: Mapped[str] = mapped_column(String(64))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


JOB_STATES = ("queued", "running", "done", "dead")


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_claim", "state", "priority", "run_after"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(32))
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    dedupe_key: Mapped[str | None] = mapped_column(String(255), unique=True)
    state: Mapped[str] = mapped_column(String(16), default="queued")
    priority: Mapped[int] = mapped_column(Integer, default=100)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=8)
    run_after: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    lease_owner: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    user_id: Mapped[str | None] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(64))
    level: Mapped[str] = mapped_column(String(16), default="info")
    file_id: Mapped[str | None] = mapped_column(String(32))
    account_id: Mapped[str | None] = mapped_column(String(32))
    detail_json: Mapped[str] = mapped_column(Text, default="{}")


class KV(Base):
    __tablename__ = "kv"
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


def normalize_url(url: str) -> str:
    """Accept provider-style URLs (postgres://, postgresql://) and use the psycopg 3 driver."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


def make_engine(url: str) -> Engine:
    url = normalize_url(url)
    if url.startswith("sqlite"):
        if url.startswith("sqlite:///") and not url.startswith("sqlite:///:memory:"):
            from pathlib import Path

            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()

        return engine
    return create_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=10)


def make_sessionmaker(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, expire_on_commit=False)


def create_all(engine: Engine) -> None:
    Base.metadata.create_all(engine)
