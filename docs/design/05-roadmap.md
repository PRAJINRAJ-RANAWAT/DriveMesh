# 05 — Phased Roadmap

Budget: about 15–20 h/week. Start: **2026-11-09**. Every link below was checked for HTTP 200 on 2026-11-08.

> **Execution note.** The implementation was produced in an AI-assisted session following this plan. The code exists; *understanding* it does not
> come for free. Each phase's **"Own it"** block is the work you still have to do yourself so you can defend the design in an interview:
> re-derive the decisions, re-run the tests, break things on purpose. Budget it honestly. It is the part that makes this project yours.

| Phase | Est. hours (build from scratch) | Est. hours ("own it": read, re-run, extend) | Weeks |
|---|---|---|---|
| 0 Repo hygiene | 15 | 4 | 1 |
| 1 Correctness foundations | 45 | 10 | 2.5 |
| 2 Durability | 40 | 10 | 2.5 |
| 3 Scalability | 35 | 6 | 2 |
| 4 Web app | 30 | 4 | 1.5 |
| 5 Security | 20 | 4 | 1 |
| 6 Proof (benchmarks + chaos) | 25 | 6 (plus real-Drive runs) | 1.5 |
| 7 Deployment | 15 | 6 (manual steps) | 1 |
| **Total** | **≈225 h ≈ 12–15 weeks → mid-Feb 2027** | **≈50 h ≈ 3 weeks → end of Nov 2026** | |

---

## Phase 0 — Repo hygiene (week 1)
- **Goal:** A professional skeleton where every later change is tested and linted automatically.
- **Deliverables:** the `drivemesh/` package with `pyproject.toml` (pinned deps, extras `dev`, `ec`, `s3`, `pg`); legacy code moved to `legacy/`;
  `drivemesh.config.Settings` (env vars, `DRIVEMESH_*`); `.env.example`; JSON logging; `pytest` + `pytest-cov`; `ruff` (lint + format); type hints;
  GitHub Actions CI; a `bench/` harness that writes JSON results.
- **Definition of done:** `ruff check .` and `ruff format --check .` clean; `pytest` green; CI green on `main`; no secret paths are read by code without coming from env/config.
- **Tests:** settings parsing; log formatter (redaction); bench harness smoke test.
- **Risks:** Over-polishing the tooling. Time-box it to one week.
- **Learn first:**
  - Config and 12-factor: [12factor.net/config](https://12factor.net/config)
  - pytest: [Getting started](https://docs.pytest.org/en/stable/getting-started.html)
  - CI: [GitHub Actions for Python](https://docs.github.com/en/actions/tutorials/build-and-test-code/python); [Ruff tutorial](https://docs.astral.sh/ruff/tutorial/)
  - Logging: [Python logging cookbook](https://docs.python.org/3/howto/logging-cookbook.html)
- **Own it:** Break the CI on purpose (a lint error, a failing test) and watch it go red.

## Phase 1 — Correctness foundations (weeks 2–3.5)
- **Goal:** Fix every High-severity correctness bug from 01 without adding replication yet (RF can be 1).
- **Deliverables:** SQLAlchemy models + Alembic migration; stable account IDs; `StorageBackend` protocol + `LocalDiskBackend`/`MemoryBackend`/`FaultyBackend`;
  error taxonomy + retry/backoff + token bucket; envelope crypto (AES-GCM + AAD); two-phase writer with bounded memory; streaming reader with checksum
  verification and fallback; safe delete via the jobs table; GC of abandoned uploads; overwrite = new version; `drivemesh` CLI (`init`, `put`, `get`,
  `ls`, `rm`, `mkdir`, `mv`, `fsck`); `migrate-legacy`.
- **Definition of done:** W-02, W-03, W-04, W-05, W-06, W-07, W-08, W-10, W-13, W-14, W-18, W-25 fixed, each with a test named after it.
  Round-trip of 0 B, 1 B, chunk−1, chunk, chunk+1 and multi-chunk files. Memory is bounded (unit test with a tracking backend).
- **Tests:** unit tests (crypto, placement stub, retry, token bucket, jobs claim race); property-style round-trips; a crash-injection test (kill the writer
  mid-upload → GC cleans up → fsck clean).
- **Risks:** Scope creep into Phase 2. Keep RF logic minimal here.
- **Learn first:**
  - GFS paper §2–3: [gfs-sosp2003.pdf](https://static.googleusercontent.com/media/research.google.com/en//archive/gfs-sosp2003.pdf)
  - SQLAlchemy 2.0: [tutorial](https://docs.sqlalchemy.org/en/20/tutorial/); [Alembic tutorial](https://alembic.sqlalchemy.org/en/latest/tutorial.html); [partial indexes](https://www.postgresql.org/docs/current/indexes-partial.html)
  - Crypto: [AEAD in `cryptography`](https://cryptography.io/en/latest/hazmat/primitives/aead/); [envelope encryption (Cloud KMS)](https://cloud.google.com/kms/docs/envelope-encryption)
  - Retries: [Exponential backoff and jitter (AWS)](https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/); [Drive usage limits](https://developers.google.com/workspace/drive/api/guides/limits)
- **Own it:** Draw the write-path sequence diagram from memory, then explain what happens if the process dies after the remote put and before the DB update.

## Phase 2 — Durability: the HDFS core (weeks 4–6)
- **Goal:** Survive the loss of RF−1 accounts with no data loss.
- **Deliverables:** RF per file with weighted rendezvous placement; health checker; repair worker; scrubber; unlink with evacuation;
  metadata backup and restore; safe mode; status API.
- **Definition of done:** Chaos tests pass on local backends: kill one of 3 accounts → all files readable → repair restores RF;
  delete or corrupt random replicas → detected and repaired; wipe the DB → restore → fsck clean. Every number is recorded in results.md.
- **Tests:** placement distribution and minimal-disruption tests; repair priority; scrub detects a bit flip; restore round-trip; safe-mode gating.
- **Risks:** Repair storms when the network blips. Mitigated by `dead_after` and requiring at least one healthy source.
- **Learn first:**
  - HDFS: [HDFS Architecture](https://hadoop.apache.org/docs/stable/hadoop-project-dist/hadoop-hdfs/HdfsDesign.html) (replication, heartbeats, safe mode)
  - Placement: [Rendezvous hashing](https://en.wikipedia.org/wiki/Rendezvous_hashing)
  - Lectures: [MIT 6.5840 schedule](https://pdos.csail.mit.edu/6.824/schedule.html) (GFS lecture + notes)
  - Durability: [S3 data durability](https://docs.aws.amazon.com/AmazonS3/latest/userguide/DataDurability.html)
- **Own it:** Explain why repair orders chunks by fewest healthy replicas, and what the window of vulnerability is.

## Phase 3 — Scalability (weeks 6.5–8)
- **Goal:** Use capacity well and add providers.
- **Deliverables:** free-space weighting + balancer; per-account concurrency/rate tuning; resumable Drive uploads; `S3Backend`;
  optional RS(k,m) mode via zfec; a synthetic 1M-chunk metadata test.
- **Definition of done:** The balancer evens out utilisation in a test. EC files survive m account losses (chaos test). The S3 backend passes the shared backend contract tests.
- **Tests:** a backend contract test suite run against every backend; EC encode/decode with any k of k+m shards.
- **Risks:** EC complexity. Ship it as optional and keep replication the default.
- **Learn first:**
  - [HDFS Erasure Coding](https://hadoop.apache.org/docs/stable/hadoop-project-dist/hadoop-hdfs/HDFSErasureCoding.html)
  - [Backblaze: Reed–Solomon](https://www.backblaze.com/blog/reed-solomon/)
  - [Drive resumable uploads](https://developers.google.com/workspace/drive/api/guides/manage-uploads)
- **Own it:** Compute the RS(4,2) vs RF=2 overhead by hand, then check the measured number.

## Phase 4 — Web app: one user, many Gmail accounts (weeks 8.5–9.5)
- **Goal:** A usable product UI.
- **Deliverables:** FastAPI app; login; Google OAuth web linking (multiple accounts); file browser with drag-and-drop upload and progress; status page; multi-user isolation tests.
- **Definition of done:** The API test suite covers every endpoint (TestClient + local backends). The OAuth callback is tested with a mocked token exchange. User A can never see user B's data (test).
- **Learn first:**
  - [OAuth 2.0 for web server apps](https://developers.google.com/identity/protocols/oauth2/web-server)
  - [FastAPI custom/streaming responses](https://fastapi.tiangolo.com/advanced/custom-response/)
- **Own it:** Trace a single upload from the browser's `xhr.send` to the replica rows.

## Phase 5 — Security hardening (week 10)
- **Deliverables:** threat model doc; CSRF; session hardening; login rate limit; upload size limit; security headers (CSP); key rotation (`rewrap`); encrypted tokens.
- **Definition of done:** Every item in the threat model has a test or a documented residual risk.
- **Learn first:**
  - [OWASP Threat Modeling](https://owasp.org/www-community/Threat_Modeling)
  - [OWASP CSRF cheat sheet](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html)
  - [OWASP session management cheat sheet](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html)

## Phase 6 — Proof (weeks 11–12)
- **Deliverables:** benchmarks B-1..B-5 and chaos tests C-1..C-7, scripted under `bench/`, with results in `results.md`.
- **Definition of done:** Every resume number traces to a results.md entry with its method. Real-Drive runs are listed as pending in MANUAL_STEPS until done.
- **Learn first:**
  - [Principles of Chaos Engineering](https://principlesofchaos.org/)
  - [USE method](https://brendangregg.com/usemethod.html)
  - [concurrent.futures](https://docs.python.org/3/library/concurrent.futures.html)

## Phase 7 — Deployment (week 13)
- **Deliverables:** Dockerfile (non-root, slim); `render.yaml`; Neon connection; secrets in the host's env; OAuth redirect URIs; README with architecture, results and limitations; demo video script.
- **Definition of done:** `docker build` passes in CI; the container starts against Postgres in CI; MANUAL_STEPS lets the owner deploy without guessing.
- **Learn first:**
  - [Docker get started](https://docs.docker.com/get-started/); [Dockerfile best practices](https://docs.docker.com/build/building/best-practices/)
  - [Render free tier](https://render.com/docs/free); [Neon plans](https://neon.com/docs/introduction/plans)
  - [SQLite WAL](https://www.sqlite.org/wal.html) (to explain why the deployed DB is Postgres)

---

## Cut list
**Drop first, in order:**
1. S3 backend (LocalDisk is enough for the multi-backend story)
2. Balancer (free-space-weighted placement covers most of it)
3. Erasure coding
4. Range requests
5. Prometheus histograms (keep counters)
6. Postgres in CI (keep SQLite)

**Never cut:**
- The two-phase write + GC
- Stable account IDs
- RF ≥ 2 with repair
- AEAD with AAD binding
- Checksum-verified reads
- The chaos tests and results.md: without them there are no numbers
- CI
- The honest attribution line
