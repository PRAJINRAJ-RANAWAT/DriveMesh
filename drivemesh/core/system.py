"""DriveMesh facade: wires settings, metadata DB, key ring, accounts, jobs and workers.

Every entry point (CLI, web API, tests, benchmarks) goes through this object.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime
from typing import IO, Any

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from drivemesh import crypto, metrics
from drivemesh.config import Settings
from drivemesh.core import gc as _gc  # noqa: F401  (registers job handlers)
from drivemesh.core import namespace as ns
from drivemesh.core.accounts import AccountRegistry
from drivemesh.core.jobs import JobQueue, enqueue
from drivemesh.core.reader import FileReader
from drivemesh.core.writer import FileInfo, FileWriter
from drivemesh.db import Event, File, User, create_all, make_engine, make_sessionmaker, utcnow
from drivemesh.errors import AlreadyExists, Conflict, NotFound, Unauthorized

log = logging.getLogger("drivemesh")

_LEVELS = {"debug": 10, "info": 20, "warning": 30, "error": 40, "critical": 50}


class SafeModeState:
    """HDFS-style safe mode: no writes/repairs/GC until account health is known."""

    def __init__(self, active: bool = False):
        self.active = active
        self.reason = "startup" if active else ""
        self.entered_at = time.monotonic()

    def enter(self, reason: str) -> None:
        self.active, self.reason, self.entered_at = True, reason, time.monotonic()
        metrics.SAFE_MODE.set(1)

    def leave(self) -> None:
        self.active, self.reason = False, ""
        metrics.SAFE_MODE.set(0)


class DriveMesh:
    def __init__(
        self,
        settings: Settings,
        *,
        keyring: crypto.KeyRing | None = None,
        engine: Engine | None = None,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        safe_mode: bool = False,
        io_threads: int = 32,
    ):
        self.settings = settings
        self.engine = engine or make_engine(settings.database_url)
        self.Session = make_sessionmaker(self.engine)
        if keyring is None:
            if settings.master_keys is None:
                raise crypto.KeyRingError(
                    "DRIVEMESH_MASTER_KEYS is not set (generate one with `drivemesh gen-key`)"
                )
            keyring = crypto.KeyRing.parse(
                settings.master_keys.get_secret_value(), settings.active_key_id
            )
        self.keyring = keyring
        self._clock = clock or utcnow
        self.sleep = sleep
        self.safe_mode = SafeModeState(safe_mode)
        self.io_pool = ThreadPoolExecutor(io_threads, thread_name_prefix="dm-io")
        self.accounts = AccountRegistry(self)
        self.jobs = JobQueue(self)

    # ---- infrastructure -------------------------------------------------------------------------
    def now(self) -> datetime:
        return self._clock()

    @contextmanager
    def session(self) -> Iterator[Session]:
        s = self.Session()
        try:
            yield s
            s.commit()
        except BaseException:
            s.rollback()
            raise
        finally:
            s.close()

    def create_schema(self) -> None:
        """Create tables directly (tests/dev). Production uses Alembic: `drivemesh init`."""
        create_all(self.engine)

    def close(self) -> None:
        self.io_pool.shutdown(wait=False, cancel_futures=True)
        self.engine.dispose()

    def event(
        self,
        kind: str,
        level: str = "info",
        *,
        user_id: str | None = None,
        file_id: str | None = None,
        account_id: str | None = None,
        **detail: Any,
    ) -> None:
        log.log(
            _LEVELS.get(level, 20),
            kind,
            extra={"event": kind, "file_id": file_id, "account_id": account_id, **detail},
        )
        try:
            with self.session() as s:
                s.add(
                    Event(
                        ts=self.now(),
                        user_id=user_id,
                        kind=kind,
                        level=level,
                        file_id=file_id,
                        account_id=account_id,
                        detail_json=json.dumps(detail, default=str),
                    )
                )
        except Exception:
            log.exception("failed to record event")

    # ---- users ------------------------------------------------------------
    def create_user(self, username: str, password: str) -> User:
        with self.session() as s:
            if s.scalars(select(User).where(User.username == username)).first():
                raise AlreadyExists(f"user {username!r} exists")
            u = User(
                username=username,
                password_hash=crypto.hash_password(password),
                created_at=self.now(),
            )
            s.add(u)
            s.flush()
            ns.root_folder(s, u.id)
            return u

    def get_user(self, username: str) -> User:
        with self.session() as s:
            u = s.scalars(select(User).where(User.username == username)).first()
            if u is None:
                raise NotFound(f"user {username!r} not found")
            return u

    def authenticate(self, username: str, password: str) -> User:
        with self.session() as s:
            u = s.scalars(select(User).where(User.username == username)).first()
        # Hash even when the user is unknown so timing does not reveal valid usernames.
        stored = u.password_hash if u else crypto.hash_password("dummy-password")
        if not crypto.verify_password(password, stored) or u is None:
            raise Unauthorized("invalid credentials")
        return u

    # ---- files ------------------------------------------------------------
    def open_writer(self, user_id: str, path: str, **kwargs: Any) -> FileWriter:
        return FileWriter(self, user_id, path, **kwargs)

    def put(
        self,
        user_id: str,
        path: str,
        data: bytes | IO[bytes] | Iterable[bytes],
        *,
        block: int = 1 << 20,
        **kwargs: Any,
    ) -> FileInfo:
        w = self.open_writer(user_id, path, **kwargs)
        try:
            if isinstance(data, (bytes, bytearray, memoryview)):
                mv = memoryview(data)
                for i in range(0, len(mv), block):
                    w.write(bytes(mv[i : i + block]))
            elif hasattr(data, "read"):
                while piece := data.read(block):
                    w.write(piece)
            else:
                for piece in data:
                    w.write(piece)
        except BaseException as exc:
            w.abort(f"{type(exc).__name__}: {exc}")
            raise
        return w.close()

    def stat(self, user_id: str, path: str) -> File:
        with self.session() as s:
            return ns.find_file(s, user_id, path)

    def open_reader(self, user_id: str, path: str) -> FileReader:
        return FileReader(self, self.stat(user_id, path))

    def get_bytes(self, user_id: str, path: str) -> bytes:
        return self.open_reader(user_id, path).read_all()

    def delete_file(self, user_id: str, path: str) -> str:
        now = self.now()
        with self.session() as s:
            f = ns.find_file(s, user_id, path)
            f.state, f.deleted_at = "deleting", now
            enqueue(s, "delete_file", {"file_id": f.id}, now=now, dedupe_key=f"delete_file:{f.id}")
            fid = f.id
        self.event("file_deleted", user_id=user_id, file_id=fid, path=path)
        return fid

    def mkdir(self, user_id: str, path: str, *, parents: bool = False) -> None:
        with self.session() as s:
            ns.mkdir(s, user_id, path, parents=parents)

    def listdir(self, user_id: str, path: str = "/") -> tuple[list, list[File]]:
        with self.session() as s:
            return ns.listdir(s, user_id, path)

    def move(self, user_id: str, src: str, dst: str) -> None:
        with self.session() as s:
            ns.move(s, user_id, src, dst)

    def delete_folder(self, user_id: str, path: str, *, recursive: bool = False) -> int:
        now = self.now()
        with self.session() as s:
            if not ns.parts(path):
                raise Conflict("cannot delete the root folder")
            folder = ns.resolve_folder(s, user_id, path)
            ids = ns.subtree_folder_ids(s, user_id, folder)
            files = list(
                s.scalars(select(File).where(File.folder_id.in_(ids), File.state == "committed"))
            )
            if (files or len(ids) > 1) and not recursive:
                raise Conflict("folder not empty (use recursive)")
            for f in files:
                f.state, f.deleted_at = "deleting", now
                enqueue(
                    s, "delete_file", {"file_id": f.id}, now=now, dedupe_key=f"delete_file:{f.id}"
                )
            # Detach tombstoned/old file versions from the folders being removed.
            others = list(s.scalars(select(File).where(File.folder_id.in_(ids))))
            root = ns.root_folder(s, user_id)
            for f in others:
                f.folder_id = root.id if f.state != "committed" else f.folder_id
            from drivemesh.db import Folder

            for fid in reversed(ids):  # children first
                s.delete(s.get(Folder, fid))
                s.flush()
        self.event("folder_deleted", user_id=user_id, path=path, files=len(files))
        return len(files)
