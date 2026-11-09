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


def test_upgrade_to_head_keeps_real_data_on_sqlite(tmp_path):
    """Regression: migration 0003 rebuilds `users` and `linked_accounts` (SQLite batch mode).

    With rows in replicas/files/folders pointing at them and foreign keys enforced, the old
    env.py failed with 'FOREIGN KEY constraint failed' on a real dev database (an empty test
    DB hid it). Upgrade a populated 0002 database and check every row survives.
    """
    from drivemesh.config import Settings
    from drivemesh.core.system import DriveMesh
    from drivemesh.db import make_engine

    url = f"sqlite:///{tmp_path}/m.db"
    migrate.upgrade(url, "d5a37403a62e")  # schema as of 0002
    engine = make_engine(url)  # foreign_keys=ON, like the app
    with engine.begin() as c:
        c.exec_driver_sql(
            "INSERT INTO users (id, username, password_hash, created_at) "
            "VALUES ('u1', 'owner', 'x', '2026-01-01')"
        )
        c.exec_driver_sql(
            "INSERT INTO folders (id, user_id, parent_id, name, created_at) "
            "VALUES ('f0', 'u1', NULL, '', '2026-01-01')"
        )
        c.exec_driver_sql(
            "INSERT INTO linked_accounts (id, user_id, provider, external_id, display_name, "
            "config_json, state, consecutive_failures, created_at) "
            "VALUES ('a1', 'u1', 'local', '/x', 'x', '{}', 'active', 0, '2026-01-01')"
        )
        c.exec_driver_sql(
            "INSERT INTO files (id, user_id, folder_id, name, version, state, chunk_size, "
            "scheme, rf, wrapped_dek, kek_id, created_at) VALUES ('file1', 'u1', 'f0', 'a', 1, "
            "'committed', 8, 'replica', 1, x'00', 'k1', '2026-01-01')"
        )
        c.exec_driver_sql(
            "INSERT INTO chunks (id, file_id, idx, plain_size) VALUES ('c1', 'file1', 0, 1)"
        )
        c.exec_driver_sql(
            "INSERT INTO replicas (id, chunk_id, account_id, shard_idx, state, created_at) "
            "VALUES ('r1', 'c1', 'a1', 0, 'ok', '2026-01-01')"
        )
    engine.dispose()

    migrate.upgrade(url)  # must not raise

    engine = make_engine(url)
    with engine.connect() as c:
        counts = {
            t: c.exec_driver_sql(f"SELECT count(*) FROM {t}").scalar()
            for t in ("users", "folders", "linked_accounts", "files", "chunks", "replicas")
        }
        assert counts == dict.fromkeys(counts, 1), counts
        assert c.exec_driver_sql("SELECT is_admin FROM users").scalar() == 1
        assert c.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
        assert c.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        leftovers = c.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE name LIKE '_alembic_tmp%'"
        ).fetchall()
        assert leftovers == []
    engine.dispose()
    DriveMesh(  # the app opens it normally
        Settings(_env_file=None, database_url=url),
        keyring=crypto.KeyRing.parse(crypto.generate_key_spec()),
    ).close()
