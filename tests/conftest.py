from __future__ import annotations

import os
from datetime import datetime, timedelta

import pytest

from drivemesh import crypto
from drivemesh.backends.local import MemoryBackend
from drivemesh.config import Settings
from drivemesh.core.system import DriveMesh
from drivemesh.db import Base, make_engine, utcnow

CHUNK = 64 * 1024


class FakeClock:
    def __init__(self, start: datetime | None = None):
        self.t = start or utcnow()

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


def make_settings(**overrides) -> Settings:
    base = dict(
        env="test",
        chunk_size=CHUNK,
        default_rf=2,
        inflight_chunks=4,
        retry_max_attempts=3,
        retry_base_seconds=0.0,
        retry_cap_seconds=0.0,
        account_rate=10_000,
        account_burst=10_000,
        free_space_reserve=0,
        workers_enabled=False,
        log_json=False,
    )
    base.update(overrides)
    return Settings(_env_file=None, **base)


@pytest.fixture(autouse=True)
def _reset_memory_backends():
    MemoryBackend.reset_all()
    yield
    MemoryBackend.reset_all()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def keyring() -> crypto.KeyRing:
    return crypto.KeyRing.parse(crypto.generate_key_spec("k1"))


@pytest.fixture
def db_url(tmp_path) -> str:
    return os.environ.get("DRIVEMESH_TEST_DATABASE_URL") or f"sqlite:///{tmp_path}/meta.db"


@pytest.fixture
def make_dm(db_url, keyring, clock):
    created: list[DriveMesh] = []

    def factory(**overrides) -> DriveMesh:
        settings = make_settings(database_url=db_url, **overrides)
        engine = make_engine(db_url)
        if not created:
            Base.metadata.drop_all(engine)
        dm = DriveMesh(settings, keyring=keyring, engine=engine, clock=clock, sleep=lambda _s: None)
        dm.create_schema()
        created.append(dm)
        return dm

    yield factory
    for dm in created:
        dm.close()


@pytest.fixture
def dm(make_dm) -> DriveMesh:
    return make_dm()


@pytest.fixture
def user(dm):
    return dm.create_user("owner", "correct horse battery staple")


@pytest.fixture
def accounts(dm, user):
    """Three in-memory accounts: a, b, c."""
    return [dm.accounts.link_memory(user.id, name) for name in ("acct-a", "acct-b", "acct-c")]


def payload(n: int, seed: int = 1) -> bytes:
    import random

    return random.Random(seed).randbytes(n)
