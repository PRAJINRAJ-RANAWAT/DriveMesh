"""Local-disk and in-memory backends: used for tests, benchmarks, chaos and as a real
'provider' (e.g. an external drive). Both can simulate a capacity limit."""

from __future__ import annotations

import os
import re
import shutil
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from drivemesh.backends.base import (
    HealthInfo,
    NotFoundError,
    ObjectInfo,
    PermanentError,
    PutResult,
    QuotaFullError,
    TransientError,
)

_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,200}$")


def _check_name(name: str) -> None:
    if not _SAFE_NAME.match(name) or name.startswith("."):
        raise PermanentError(f"invalid object name: {name!r}")


class LocalDiskBackend:
    provider = "local"

    def __init__(self, root: str | os.PathLike[str], capacity_bytes: int | None = None):
        self.root = Path(root)
        self.capacity_bytes = capacity_bytes
        self._lock = threading.Lock()
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise TransientError(f"cannot create root {self.root}: {exc}") from exc

    def _path(self, name: str) -> Path:
        _check_name(name)
        return self.root / name

    def _used(self) -> int:
        total = 0
        with os.scandir(self.root) as it:
            for entry in it:
                if entry.is_file() and not entry.name.startswith("."):
                    total += entry.stat().st_size
        return total

    def put(self, name: str, data: bytes) -> PutResult:
        path = self._path(name)
        if self.capacity_bytes is not None:
            with self._lock:
                if self._used() + len(data) > self.capacity_bytes:
                    raise QuotaFullError(f"{self.root} full")
        tmp = self.root / f".tmp-{name}-{threading.get_ident()}"
        try:
            with open(tmp, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)  # atomic: readers never see a partial object
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            raise TransientError(str(exc)) from exc
        return PutResult(remote_id=name, size=len(data))

    def get(self, remote_id: str) -> bytes:
        try:
            return self._path(remote_id).read_bytes()
        except FileNotFoundError as exc:
            raise NotFoundError(remote_id) from exc
        except OSError as exc:
            raise TransientError(str(exc)) from exc

    def delete(self, remote_id: str) -> None:
        try:
            self._path(remote_id).unlink()
        except FileNotFoundError as exc:
            raise NotFoundError(remote_id) from exc

    def find(self, name: str) -> str | None:
        return name if self._path(name).exists() else None

    def list(self, prefix: str = "dm-") -> Iterator[ObjectInfo]:
        with os.scandir(self.root) as it:
            entries = [e for e in it if e.is_file() and e.name.startswith(prefix)]
        for e in entries:
            st = e.stat()
            yield ObjectInfo(
                name=e.name,
                remote_id=e.name,
                size=st.st_size,
                created_at=datetime.fromtimestamp(st.st_mtime, UTC).replace(tzinfo=None),
            )

    def health(self) -> HealthInfo:
        if not self.root.is_dir():
            raise NotFoundError(f"root {self.root} missing")
        used = self._used()
        if self.capacity_bytes is not None:
            return HealthInfo(self.capacity_bytes, used, max(self.capacity_bytes - used, 0))
        du = shutil.disk_usage(self.root)
        return HealthInfo(du.total, used, du.free)


class MemoryBackend:
    """Thread-safe dict-backed store. Instances can be shared by name (see `named`)."""

    provider = "memory"
    _instances: dict[str, MemoryBackend] = {}
    _instances_lock = threading.Lock()

    def __init__(self, capacity_bytes: int | None = None):
        self.capacity_bytes = capacity_bytes
        self.objects: dict[str, tuple[bytes, datetime]] = {}
        self._lock = threading.Lock()

    @classmethod
    def named(cls, name: str, capacity_bytes: int | None = None) -> MemoryBackend:
        with cls._instances_lock:
            if name not in cls._instances:
                cls._instances[name] = cls(capacity_bytes)
            return cls._instances[name]

    @classmethod
    def reset_all(cls) -> None:
        with cls._instances_lock:
            cls._instances.clear()

    def used(self) -> int:
        return sum(len(v[0]) for v in self.objects.values())

    def put(self, name: str, data: bytes) -> PutResult:
        _check_name(name)
        with self._lock:
            if self.capacity_bytes is not None and self.used() + len(data) > self.capacity_bytes:
                raise QuotaFullError("memory backend full")
            self.objects[name] = (bytes(data), datetime.now(UTC).replace(tzinfo=None))
        return PutResult(remote_id=name, size=len(data))

    def get(self, remote_id: str) -> bytes:
        with self._lock:
            try:
                return self.objects[remote_id][0]
            except KeyError as exc:
                raise NotFoundError(remote_id) from exc

    def delete(self, remote_id: str) -> None:
        with self._lock:
            if self.objects.pop(remote_id, None) is None:
                raise NotFoundError(remote_id)

    def find(self, name: str) -> str | None:
        with self._lock:
            return name if name in self.objects else None

    def list(self, prefix: str = "dm-") -> Iterator[ObjectInfo]:
        with self._lock:
            snapshot = [(k, v) for k, v in self.objects.items() if k.startswith(prefix)]
        for k, (data, ts) in snapshot:
            yield ObjectInfo(name=k, remote_id=k, size=len(data), created_at=ts)

    def health(self) -> HealthInfo:
        used = self.used()
        cap = self.capacity_bytes
        return HealthInfo(cap, used, None if cap is None else max(cap - used, 0))
