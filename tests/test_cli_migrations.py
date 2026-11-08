import json

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, inspect

from drivemesh import cli, crypto, migrate
from drivemesh.config import get_settings
from drivemesh.db import Base
from drivemesh.legacy_import import _dest_path, import_registry
from tests.conftest import payload


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DRIVEMESH_DATABASE_URL", f"sqlite:///{tmp_path}/cli.db")
    monkeypatch.setenv("DRIVEMESH_MASTER_KEYS", crypto.generate_key_spec("k1"))
    monkeypatch.setenv("DRIVEMESH_CHUNK_SIZE", str(64 * 1024))
    monkeypatch.setenv("DRIVEMESH_LOG_JSON", "false")
    monkeypatch.setenv("DRIVEMESH_LOG_LEVEL", "WARNING")
    get_settings.cache_clear()
    import logging

    root = logging.getLogger()
    saved = (root.handlers[:], root.level)
    yield tmp_path
    root.handlers[:], _ = saved[0], root.setLevel(saved[1])
    get_settings.cache_clear()


def run(*argv) -> int:
    return cli.main(list(argv))


def test_alembic_schema_matches_models(tmp_path):
    url = f"sqlite:///{tmp_path}/mig.db"
    migrate.upgrade(url)
    migrated = inspect(create_engine(url))
    model_engine = create_engine(f"sqlite:///{tmp_path}/models.db")
    Base.metadata.create_all(model_engine)
    expected = inspect(model_engine)
    tables = set(expected.get_table_names())
    assert tables <= set(migrated.get_table_names())
    for t in tables:
        assert {c["name"] for c in migrated.get_columns(t)} == {
            c["name"] for c in expected.get_columns(t)
        }, t
        assert {i["name"] for i in migrated.get_indexes(t)} == {
            i["name"] for i in expected.get_indexes(t)
        }, t


def test_cli_end_to_end(cli_env, capsys):
    tmp = cli_env
    assert run("init") == 0
    assert run("create-user", "owner", "--password", "pw") == 0
    for name in ("a", "b"):
        assert run("account-add-local", str(tmp / name), "--name", name) == 0
    src = tmp / "in.bin"
    src.write_bytes(payload(200_000))
    assert run("mkdir", "/docs") == 0
    assert run("put", str(src), "/docs/in.bin") == 0
    capsys.readouterr()
    assert run("ls", "/docs") == 0
    assert "in.bin" in capsys.readouterr().out
    assert run("get", "/docs/in.bin", str(tmp / "out.bin")) == 0
    assert (tmp / "out.bin").read_bytes() == src.read_bytes()
    assert run("mv", "/docs/in.bin", "/moved.bin") == 0
    assert run("fsck", "--deep") == 0
    assert run("rm", "/moved.bin") == 0
    assert run("jobs") == 0
    capsys.readouterr()
    assert run("account-list") == 0
    out = capsys.readouterr().out
    assert out.count("local") == 2
    # error path is reported, not a traceback
    assert run("get", "/nope", str(tmp / "x")) == 1


def test_gen_key(capsys):
    assert run("gen-key", "--key-id", "k7") == 0
    spec = capsys.readouterr().out.strip()
    assert crypto.KeyRing.parse(spec).active_id == "k7"


def test_legacy_import(dm, user, accounts):
    key = Fernet.generate_key()
    f = Fernet(key)
    store: dict[tuple[int, str], bytes] = {}
    data_a = payload(12_000_000, seed=3)  # 3 legacy 5 MiB chunks
    data_b = b"small file"
    registry = {}
    for name, data, parent in (("a.bin", data_a, "root/docs"), ("b.txt", data_b, "root")):
        chunks = []
        for i in range(0, max(len(data), 1), 5 * 1024 * 1024):
            idx = len(chunks)
            did = f"drive-{name}-{idx}"
            store[(idx % 2, did)] = f.encrypt(data[i : i + 5 * 1024 * 1024])
            chunks.append({"account_id": idx % 2, "drive_id": did, "order": idx})
        registry[name] = {"file_size": len(data), "parent_path": parent, "chunks": chunks}
    registry["broken"] = {
        "file_size": 1,
        "chunks": [{"account_id": 0, "drive_id": "x", "order": 1}],
    }
    rep = import_registry(dm, user.id, registry, key, lambda i, d: store[(i, d)])
    assert sorted(rep.imported) == ["/legacy/b.txt", "/legacy/docs/a.bin"]
    assert "broken" in rep.failed
    assert dm.get_bytes(user.id, "/legacy/docs/a.bin") == data_a
    again = import_registry(dm, user.id, registry, key, lambda i, d: store[(i, d)])
    assert sorted(again.skipped) == ["/legacy/b.txt", "/legacy/docs/a.bin"]


def test_legacy_import_wrong_key_fails_cleanly(dm, user, accounts):
    store = {(0, "d"): Fernet(Fernet.generate_key()).encrypt(b"x")}
    reg = {"f": {"file_size": 1, "chunks": [{"account_id": 0, "drive_id": "d", "order": 0}]}}
    rep = import_registry(dm, user.id, reg, Fernet.generate_key(), lambda i, d: store[(i, d)])
    assert "f" in rep.failed
    dm.jobs.run_until_idle()


def test_dest_path():
    assert _dest_path("/legacy", "root/a/b", "f") == "/legacy/a/b/f"
    assert _dest_path("/legacy", "root", "f") == "/legacy/f"
    _ = json


def test_cli_operations(cli_env, capsys):
    tmp = cli_env
    run("create-user", "owner", "--password", "pw")
    for name in ("a", "b", "c"):
        run("account-add-local", str(tmp / name), "--name", name)
    src = tmp / "x.bin"
    src.write_bytes(payload(100_000))
    assert run("put", str(src), "/x.bin", "--rf", "2") == 0
    assert run("backup") == 0
    capsys.readouterr()
    assert run("status") == 0
    status = json.loads(capsys.readouterr().out)
    assert status["replicas"]["ok"] == 4 and status["last_backup_at"]
    acc_id = status["accounts"][0]["id"]
    assert run("account-remove", acc_id) == 0
    assert run("jobs") == 0
    capsys.readouterr()
    assert run("account-list", "--all") == 0
    assert "removed" in capsys.readouterr().out
    assert run("fsck", "--deep") == 0
    assert run("rewrap") == 0
