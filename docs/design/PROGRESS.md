# Progress

## How to resume
1. `python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev,pg]"`
2. `.venv/bin/ruff check . && .venv/bin/pytest`
3. Read the "Next" section below and continue from the first unchecked item.

## Status
| Step/Phase | State |
|---|---|
| Step 1–6 design docs | ✅ done |
| Phase 0 Repo hygiene | ✅ done |
| Phase 1 Correctness foundations | ✅ done |
| Phase 2 Durability | ✅ done |
| Phase 3 Scalability | ✅ done |
| Phase 4 Web app | ⏳ next |
| Phase 5–7 | ☐ |

## Phase 0 summary
- Legacy prototype moved to `legacy/` unchanged (ADR-017).
- `pyproject.toml` with pinned dependencies and the extras `dev`, `pg`, `s3`, `ec`.
- `drivemesh.config.Settings` (env vars `DRIVEMESH_*`, secrets as `SecretStr`), `env.example`.
- JSON logging with request ids and secret redaction (`drivemesh/logs.py`).
- Benchmark harness (`drivemesh/benchkit.py`).
- CI on GitHub Actions: ruff lint + format check, then pytest with coverage.

## Phase 1 summary
- SQLAlchemy schema (`drivemesh/db.py`), plus the Alembic migration `0001` (checked against the models by a test).
- Envelope crypto: AES-256-GCM with STREAM-style AAD; per-file DEKs wrapped by a KEK keyring (`drivemesh/crypto.py`).
- Backends:
  - Local, Memory, Faulty (chaos).
  - Google Drive (`drive.file`, resumable uploads), tested with the real `googleapiclient` against an in-memory fake Drive HTTP server (`tests/fakes/fake_drive.py`).
- Resilience: full-jitter retries, a token bucket and a concurrency cap per account.
- Two-phase streaming writer; verified reader with fallback and Range support; durable job queue with leases; tombstone delete + purge jobs; GC (abandoned uploads, crash-between-put-and-update cleanup by name, orphan sweep by remote id); fsck.
- CLI (`drivemesh …`) and `migrate-legacy`.
- Tests pinned to the audit: w02, w03, w04, w05, w07, w10, w13, w18, w25.

## Phase 2 summary
- Health checker (heartbeat poll). States: active, full, unreachable (after N consecutive failures) and revoked (auth). `unhealthy_since` drives the `dead_after` grace period.
- Repair: fewest-healthy-first; dead-account replicas stop counting but are never marked bad; excess replicas are trimmed when an account returns.
- Scrubber: oldest-verified-first, byte budget per pass.
- Evacuation (unlink): refused if it would break RF.
- Encrypted logical metadata snapshots on every account, restore into an empty DB, orphan sweep paused after a restore.
- Safe mode: at startup, and whenever no account is reachable.
- Master-key rewrap.
- CI runs the full suite on SQLite **and** Postgres 16.

## Phase 3 summary
- Reed–Solomon EC via zfec (`--ec 4,2`): write, read from any k of k+m, and shard repair.
- S3-compatible backend (boto3; tested with moto).
- Balancer: copy, then delete; never below the target.
- One backend contract suite runs against Local, Memory, S3 and the fake Drive.
- Metadata at 10k chunks in the tests; the 1M-chunk timing is a Phase 6 benchmark.
- Resumable Drive uploads were already done in Phase 1.

## Open issues
- OAuth client secret and the legacy `.key` rotation are pending (owner action; see MANUAL_STEPS.md).
