import io
import json
import logging

from drivemesh import benchkit
from drivemesh.config import Settings
from drivemesh.logs import JsonFormatter, request_id_var


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("DRIVEMESH_DEFAULT_RF", "3")
    monkeypatch.setenv("DRIVEMESH_CHUNK_SIZE", str(4 * 1024 * 1024))
    monkeypatch.setenv("DRIVEMESH_MASTER_KEYS", "k1:abc")
    s = Settings(_env_file=None)
    assert s.default_rf == 3
    assert s.chunk_size == 4 * 1024 * 1024
    assert "abc" not in repr(s)  # secrets never in repr


def test_write_quorum_defaults_to_rf():
    s = Settings(_env_file=None)
    assert s.write_quorum(3) == 3
    s2 = Settings(_env_file=None, min_write_replicas=1)
    assert s2.write_quorum(3) == 1


def test_json_formatter_redacts_and_includes_context():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    log = logging.getLogger("t.redact")
    log.handlers[:] = [handler]
    log.propagate = False
    token = request_id_var.set("req-1")
    try:
        log.warning("hello", extra={"refresh_token": "xyz", "file_id": "f1"})
    finally:
        request_id_var.reset(token)
    data = json.loads(stream.getvalue())
    assert data["msg"] == "hello"
    assert data["request_id"] == "req-1"
    assert data["refresh_token"] == "[REDACTED]"
    assert data["file_id"] == "f1"


def test_benchkit_measure_and_save(tmp_path):
    with benchkit.measure("smoke", size=1) as m:
        sum(range(1000))
        m.metrics["ok"] = True
    assert m.wall_seconds >= 0
    assert m.peak_rss_bytes > 0
    path = benchkit.save(m, tmp_path)
    loaded = json.loads(path.read_text())
    assert loaded["name"] == "smoke"
    assert loaded["params"] == {"size": 1}
    assert loaded["metrics"]["ok"] is True
