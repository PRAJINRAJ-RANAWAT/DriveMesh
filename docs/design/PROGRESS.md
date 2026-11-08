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
| Phase 4 Web app | ✅ done |
| Phase 5 Security | ✅ done |
| Phase 6 Proof | ✅ done (local/simulated); real-Drive runs pending |
| Phase 7 Deployment | ✅ artifacts ready; deploy pending (owner, MANUAL_STEPS §5) |

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

## Phase 4 summary
- FastAPI app (`drivemesh serve`):
  - Session login (scrypt), CSRF synchronizer token, login throttle, strict CSP and security headers.
  - Streamed raw-body upload with backpressure; streamed download with Range/416/ETag.
  - Namespace ops; accounts (local link, unlink with evacuation); cluster status; /metrics, /healthz, /readyz.
- Google OAuth web flow (state + PKCE, offline access, `drive.file` scope check, re-link by Google `sub`), tested with a substituted flow and the fake Drive.
- UI: file browser (drag-and-drop upload with progress, rename, delete), accounts sidebar with health and capacity, status dashboard. No inline JS/CSS; DOM built with `textContent`.
- Multi-user isolation tests at the HTTP level.
- Smoke test against a real uvicorn server: a 3 MB round-trip is byte-identical, workers run, a backup is written.

## Phase 5 summary
- Threat model: `07-threat-model.md` (STRIDE, each threat mapped to a mitigation and a test).
- `Settings.production_problems()`: the app refuses to start in prod with an unsafe config.
- HSTS when cookies are secure; cookie flags tested.
- Google tokens revoked on unlink; secrets checked to be ciphertext at rest.
- ruff bandit rules (`S`) enabled; `pip-audit` CI job (0 known vulnerabilities on 2026-11-08).
- Already in place from earlier phases: CSRF, CSP, login throttle, upload limits, envelope encryption and rewrap.

## Phase 6 summary
- Benchmarks (`python -m bench.run …`) and chaos experiments (`python -m bench.chaos …`), all recorded in results.md (R-001…R-009).
- Measurement found and fixed four real problems:
  - writer head-of-line blocking
  - short-run placement imbalance (fixed with load-aware power-of-two-choices)
  - serial repair (now parallel)
  - a quadratic repair scan / fsck (composite index, single-pass fsck, plan regression test)
- One negative result recorded: a larger SQLite page cache didn't help.
- Chaos: 70 trials across 7 experiments, 0 files lost, 0 wrong bytes, fsck clean and 0 orphans every time.

## Phase 7 summary
- Multi-stage `Dockerfile` (non-root, healthcheck, migrations at startup), `.dockerignore`, `render.yaml` (Render free + Neon).
- CI `docker` job builds the image and smoke-tests it in prod mode against Postgres. That test caught a real bug (reserved log key crashing the backup worker).
- README rewritten with architecture, results and limitations. `MANUAL_STEPS.md` covers everything that needs the owner.

## UI redesign (2026-11-09)
- "Paper & Ink" design system (base #F5F3EE, charcoal #1C1C1C, accent #FF5A1F) in `static/css/tokens.css`; reference study in `08-ui-reference.md`.
- New landing page:
  - sticky blur nav that switches to dark glass over charcoal bands
  - animated mesh hero
  - real-number stats
  - steps with hover micro-animations
  - a pinned scroll-driven durability scene
  - security flow and HDFS architecture diagrams
  - staggered punchline cards with original art
  - accent CTA band and footer
- New app pages: Overview `/app`, Files `/files` (folder tree, replica badges, per-chunk upload status), Accounts `/accounts` (quota bars, unlink dialog with an evacuation warning), Status `/status`, and a split login.
- Lighthouse (R-012): landing 99/100/100/100 on mobile and 100 across the board on desktop. 208 tests, 91 % coverage (R-011).

## Next (owner)
1. MANUAL_STEPS §1: rotate the leaked OAuth client and destroy the old Fernet key (import legacy data first if wanted).
2. §2–3: create the OAuth web client and link real Gmail accounts locally.
3. §4: real-Drive benchmarks → fill the `gdrive` rows in results.md and the resume placeholders.
4. §5: deploy to Render + Neon.
5. §6: record the demo video.

## Open issues
- OAuth client secret and the legacy `.key` rotation are pending (owner action; see MANUAL_STEPS.md).
- Not measured on real Google Drive yet: every throughput, repair and chunk-size number is local or simulated.
- SQLite slows down beyond ~100k chunks (R-007); use Postgres for large pools.
- Docker image is 568 MB, mostly google-api-python-client. A slimmer image is possible by dropping unused extras.
- Placement is load-aware, so it isn't a pure function of (chunk, accounts) (ADR-005a).
