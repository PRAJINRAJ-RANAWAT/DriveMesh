# 04 — Architecture Decision Records

Format: **Context · Options · Decision · Consequences · Where a managed service wins · AWS/GCP mapping · Interview angle.**
Status is *Accepted* unless noted. An ADR is amended (not rewritten) when measurements change a decision.

---

## ADR-001 Web framework: FastAPI (not Flask)
- **Context:** The new API streams multi-GB uploads and downloads, needs typed request validation and OpenAPI docs, and runs blocking SDK calls (Google client is sync).
  The owner already knows FastAPI.
- **Options:**
  - Flask 3 + Werkzeug. Familiar to the original team; synchronous; streaming possible via generators; no built-in validation.
  - **FastAPI + Uvicorn.** ASGI; `request.stream()` for backpressured uploads; `StreamingResponse`; Pydantic models; automatic OpenAPI; `TestClient`.
- **Decision:** FastAPI. Blocking work (backend I/O, crypto) runs in worker threads (`anyio.to_thread`), so the event loop only shuffles bytes.
- **Consequences:** Every route file is rewritten (the Flask routes had no tests anyway). There are two concurrency worlds (async handlers, threaded core), bridged at one seam (ADR-006).
- **Managed service wins:** API Gateway + Lambda would handle auth and throttling, but cannot stream multi-GB bodies (payload limits).
- **AWS/GCP:** ALB/API Gateway in front of ECS/Cloud Run.
- **Interview angle:** "I picked the framework for its streaming model, not popularity. Backpressure from the socket to the chunk pipeline is what bounds memory."

## ADR-002 Metadata store: SQLite locally, Postgres when deployed, one SQLAlchemy model
- **Context:** The metadata needs transactions, unique/partial indexes for invariants, and concurrent readers. Free-tier hosting has ephemeral disks.
- **Options:**
  - SQLite only. Zero ops and very fast, but a file on an ephemeral container disk is lost on redeploy.
  - Postgres only. Real concurrency, but tests and the laptop need a server.
  - **Both, behind SQLAlchemy 2.0 + Alembic.** Write portable SQL: no `SKIP LOCKED`, no JSONB operators, and partial indexes on both.
- **Decision:** Both. SQLite runs with `journal_mode=WAL`, `foreign_keys=ON`, `busy_timeout=5000`. CI runs the full suite on SQLite and a smoke suite against a Postgres service container.
- **Consequences:** We avoid DB-specific features and keep the job-claim query portable. SQLite serialises writers, which is fine for one owner.
- **Managed service wins:** Neon/RDS give backups, PITR and failover that our snapshot job only approximates.
- **AWS/GCP:** RDS/Aurora Postgres or Cloud SQL. DynamoDB would also work: single-table design, file#chunk keys, conditional writes for commit. We give up multi-row transactions, which is why SQL was picked.
- **Interview angle:** "The NameNode's job is transactional metadata. I enforce 'one committed version per path' and 'no two replicas on one account' as DB constraints, not as hopes."

## ADR-003 JSON registry → relational DB; no separate edit log
- **Context:** `registry.json` is rewritten non-atomically on every change (W-06), keyed by bare filename (W-05), with no locking.
- **Options:**
  - Fix JSON with atomic rename + a file lock.
  - Append-only journal + snapshots (an HDFS edit-log clone).
  - **SQL DB.**
- **Decision:** SQL DB. Its WAL *is* the edit log, and its checkpointing *is* fsimage. Building our own journal on top would duplicate the database and be a classic over-engineering trap.
  Off-site durability of the metadata comes from encrypted logical snapshots (ADR-015).
- **Consequences:** A migration tool imports legacy `registry.json` (`drivemesh migrate-legacy`).
- **Managed service wins:** Same as ADR-002.
- **AWS/GCP:** DynamoDB Streams / Aurora redo log.
- **Interview angle:** "I could explain fsimage + edits, and that's exactly why I didn't rebuild them: the database already gives me atomic, durable mutations."

## ADR-004 Durability scheme: replication by default (RF=2), optional Reed–Solomon
- **Context:** A personal pool has roughly 2–10 accounts of about 12 GB usable each. Failure domain = account.
- **Options:** RF=2, RF=3, RS(4,2), RS(6,3) (see §02-5 for overhead and amplification).
- **Decision:** Default **RF=2**: it tolerates one account loss at 2× overhead with 2 accounts minimum, and reads stay simple. RF is per file (1–5).
  **RS(k,m)** is optional per file when N ≥ k+m, chosen for capacity.
- **Consequences:** Two code paths for read and repair, kept behind one `ChunkCodec` seam. EC costs k× read amplification on repair and k requests per read.
- **Managed service wins:** S3 Standard is designed for 11 nines of durability using EC across ≥ 3 AZs. We cannot approach that, and we do not claim to.
- **AWS/GCP:** S3 / GCS internal EC; HDFS 3 RS-6-3.
- **Interview angle:** "Replication versus erasure coding is a trade between storage overhead and repair/read cost. For 5 accounts I chose replication, and here's the measured overhead."

## ADR-005 Placement: weighted rendezvous hashing with provider diversity
- **Context:** Round-robin ignores free space (W-09). We need RF distinct accounts per chunk, deterministic and testable, with minimal data movement when accounts change.
- **Options:** Round-robin; random weighted; consistent hashing ring with vnodes; **weighted HRW**; a CRUSH-like hierarchy.
- **Decision:** Weighted HRW. Score = `−w/ln(u)`, `w = free_bytes`. Pick the best account of each provider first, then fill by score. Full comparison in §03-5.
- **Consequences:** O(N) per chunk, which doesn't matter at N ≤ 100. Free space changes over time, so placement of *new* chunks shifts and the balancer corrects old skew.
- **Managed service wins:** Nobody exposes placement. S3 hides it entirely.
- **AWS/GCP:** DynamoDB partitioning (consistent hashing); Ceph CRUSH.
- **Interview angle:** Explain why HRW gives minimal disruption: removing an account only moves the keys whose top pick was that account.

## ADR-006 Concurrency: threads for I/O, async only at the HTTP edge
- **Context:** The Google API client (`googleapiclient` + `httplib2`) is synchronous and not thread-safe per `Http` object. Crypto is CPU-bound but releases the GIL in OpenSSL.
- **Options:** Full asyncio with an async HTTP client and a re-implemented Drive REST client; **threads with bounded pools**; multiprocessing.
- **Decision:** Threads. A bounded `ThreadPoolExecutor` per transfer (`inflight_chunks`) plus per-account semaphores and token buckets.
  Each thread uses its own Drive service (thread-local), carrying over the legacy code's good instinct.
- **Consequences:** Simple code. Concurrency is capped by explicit limits, not by the number of coroutines. The GIL is not the bottleneck because work is network- and OpenSSL-bound (verify with B-1).
- **Managed service wins:** n/a.
- **AWS/GCP:** boto3 is also sync, and the S3 Transfer Manager uses threads the same way.
- **Interview angle:** "I measured where time went before choosing; I/O-bound work in Python is fine with threads when you bound them."

## ADR-007 Job queue: DB-backed table with leases (no Redis/Celery)
- **Context:** Deletes, GC, evacuation and backups must survive crashes and must not run twice concurrently. Free tier: one container, one DB.
- **Options:** Celery + Redis; RQ + Redis; **a jobs table with lease/visibility timeout**; APScheduler alone (no durability).
- **Decision:** A jobs table. Claim = conditional `UPDATE` (§03-6). At-least-once semantics, idempotent handlers, `dedupe_key` uniqueness, exponential backoff, a `dead` state.
- **Consequences:** No extra infrastructure, and job state lives in the same transaction as the metadata it relates to (enqueue-delete in the same tx as tombstoning, so there is no dual-write problem).
  Throughput is limited by DB polling, which is irrelevant at our scale.
- **Managed service wins:** SQS gives visibility timeouts, DLQs and scale for free; Cloud Tasks gives rate-limited dispatch.
- **AWS/GCP:** SQS (visibility timeout ≙ lease, DLQ ≙ `dead`), Cloud Tasks.
- **Interview angle:** Transactional outbox. "Enqueuing the delete job in the same transaction as the tombstone means I can never tombstone without scheduling the purge."

## ADR-008 Encryption: envelope encryption with AES-256-GCM and AAD binding
- **Context:** The legacy design uses one Fernet key on disk (W-10, leaked W-00), +33 % overhead (R-001), and no position binding.
- **Options:** Keep Fernet; AES-GCM with a single key; **per-file DEK wrapped by a KEK, AES-GCM with AAD**; age/libsodium secretstream.
- **Decision:** Per-file-version random DEK; AES-256-GCM per chunk with AAD `dm1|file_id|version|idx|chunk_count`; DEK wrapped by a KEK from `DRIVEMESH_MASTER_KEYS`
  (keyring with ids). Tokens and snapshots are encrypted with the KEK under their own AAD.
- **Consequences:** Rotation = rewrap (cheap). Chunk swap, reorder and truncation are detected. Overhead is 32 B/object. Losing the KEK loses everything (stated plainly).
- **Managed service wins:** KMS keeps the KEK in an HSM, audits every unwrap, and enables per-request IAM. That is strictly better if budget allows.
- **AWS/GCP:** AWS KMS `GenerateDataKey` / S3 SSE-KMS (exactly this envelope pattern); GCP Cloud KMS envelope encryption.
- **Interview angle:** "Encryption without associated data authenticates bytes, not meaning. I bound each chunk to its position, so reassembly can't be fooled."

## ADR-009 Chunk size: 8 MiB default (provisional until measured on Drive)
- **Context:** Larger chunks mean fewer API calls and less metadata, but more memory per in-flight chunk, coarser placement, and more re-sent bytes on retry.
- **Options:** 5 MiB (legacy), 8 MiB, 16 MiB, 64 MiB.
- **Decision:** **8 MiB** default, configurable per deployment (`DRIVEMESH_CHUNK_SIZE`).
  - 128 objects/GiB keeps the request count well under quota (§02-5).
  - 4 in-flight chunks ≈ 100 MiB peak memory.
  - Drive's resumable upload granularity (256 KiB multiples) divides it evenly.
  - **Benchmark B-4 measures throughput vs chunk size on a simulated-latency backend. The real-Drive run is pending** (results.md).
- **Consequences:** The decision may move to 16 MiB after real measurements. Changing it affects new files only (`files.chunk_size` is stored per file).
- **Managed service wins:** S3 multipart parts are 5 MiB–5 GiB; the same trade-off.
- **AWS/GCP:** S3 multipart part size; GCS resumable chunk size.
- **Interview angle:** "I treated chunk size as an experiment, not a constant. Here's the curve."

## ADR-010 OAuth scope: `drive.file` (+ `openid email`)
- **Context:** The legacy app requests full `drive`, a *restricted* scope (W-11).
- **Options:** `drive` (restricted; read/write everything); `drive.appdata` (hidden app folder, non-sensitive, but the space is invisible to users and harder to inspect);
  **`drive.file`** (non-sensitive; only files this app created).
- **Decision:** `drive.file`. Objects live in a visible `DriveMesh` folder, so users can see where their quota went.
- **Consequences:** Can't read objects created by other Cloud projects, including the legacy app's chunks if it used a different project.
  Legacy migration therefore runs locally with the legacy token, once. No restricted-scope security assessment is needed.
- **Managed service wins:** n/a.
- **AWS/GCP:** IAM least privilege.
- **Interview angle:** "Least privilege isn't just security; it changed my product's compliance path from 'restricted scope assessment' to 'non-sensitive'."

## ADR-011 Deployment target: Docker container on Render (free web service) + Neon Postgres (free)
- **Context:** Long-running streamed transfers, background worker threads, and near-zero spend.
- **Options:**
  - **Vercel/Netlify functions:** request time and body-size limits, no long-lived threads for repair/scrub. A poor fit for a storage service.
  - **Cloud Run:** scales to zero and needs a billing account. Background threads are throttled without CPU-always-allocated.
  - **Fly.io:** no free tier for new orgs.
  - **Render free web service:** Docker; spins down after 15 min idle, spin-up about 1 min; 750 instance-hours/month.
  - **Render Postgres free:** expires after 30 days. Rejected.
  - **Neon free:** 1 GB per project, 100 CU-hours/month, no credit card.
- **Decision:** Render (Docker) + Neon. Metadata lives in Neon. The container is stateless (no persistent disk needed). Workers run in-process.
  Because jobs are durable in the DB, a spun-down instance resumes them on wake.
- **Consequences:** No repair or scrub while the instance is asleep. Acceptable for a demo; documented as a limitation.
  The `Dockerfile` is host-agnostic, so moving to Cloud Run/ECS is configuration only.
  Free-tier terms change: re-verify before deploying (links in MANUAL_STEPS).
- **Managed service wins:** For a real product, use ECS/Fargate + RDS + SQS + KMS, or Cloud Run with min-instances + Cloud SQL + Cloud Tasks.
- **AWS/GCP:** Elastic Beanstalk / App Runner + RDS; Cloud Run + Cloud SQL.
- **Interview angle:** "Serverless is wrong for long-lived streams and background repair. I chose a container and designed for being killed."

## ADR-012 Upload transport: raw streamed request body (not multipart)
- **Context:** Multipart parsing in Starlette spools to temp files, and the legacy code staged whole uploads to disk (W-24) with attacker-controlled names (W-13).
- **Decision:** `PUT /files/content?path=` with the raw body. The browser sends `xhr.send(file)` (progress still works). Bytes go socket → chunk buffer → encrypt → backends, with no temp files.
- **Consequences:** One file per request (fine). Name and path come from the query string and are validated.
- **AWS/GCP:** S3 `PutObject` is also a raw body.
- **Interview angle:** Backpressure end to end.

## ADR-013 Namespace in the DB only; no provider-side folders
- **Context:** The legacy code mirrored every virtual folder as a real folder in every account (`storage_manager.py:128-161`), with partial-failure bugs (W-21).
- **Decision:** Folders are rows. Objects live flat in one app folder per account, named `dm-<replica_id>`. Rename/move is a metadata-only update.
- **Consequences:** Provider UIs show opaque names (good for privacy, W-18). Rename is O(1).
- **AWS/GCP:** S3 is flat too; "folders" are prefixes.

## ADR-014 Concurrent writers: immutable versions, last-commit-wins (no writer leases)
- **Context:** HDFS grants a single-writer lease per file. We never modify objects in place.
- **Decision:** Each upload creates a new `pending` version. The commit flips old → `deleting` and new → `committed` in one transaction, guarded by the partial unique index.
  `overwrite=false` gives create-only semantics (409).
  A writer *heartbeat* (`lease_expires_at`) exists only so GC can recognise abandoned uploads, not for mutual exclusion.
- **Consequences:** No lease recovery protocol. Readers always see a complete version.
- **AWS/GCP:** S3 is last-writer-wins with strong read-after-write; conditional writes (`If-None-Match`) map to `overwrite=false`.

## ADR-015 Metadata backup: encrypted logical snapshots stored on the backends
- **Context:** The metadata DB is the single point of truth (the NameNode SPOF).
- **Options:** A hot standby (overkill); `pg_dump` (needs binaries, Postgres-only); **a logical JSON dump via SQLAlchemy** (portable SQLite↔Postgres).
- **Decision:** Every 15 min, if changed: dump all tables → gzip → AES-GCM(KEK) → put on every active account. Keep the last 5. `drivemesh restore` loads one into an empty DB.
  After a restore, the orphan sweep pauses for 7 days so chunks of files newer than the snapshot are not deleted.
- **Consequences:** RPO = snapshot interval; RTO = minutes. Restoring needs the KEK and access to one account.
- **Managed service wins:** Neon/RDS PITR. Use both when deployed.
- **AWS/GCP:** RDS automated backups + snapshots to S3; Cloud SQL backups.
- **Interview angle:** "My NameNode SPOF story: I can wipe the DB and rebuild it from the storage accounts. Here's the chaos test."

## ADR-016 Erasure-coding library: zfec
- **Decision:** `zfec` (mature C RS implementation; a wheel exists for CPython 3.12 arm64, checked with `pip download zfec`). Optional dependency (`pip install .[ec]`).
- **Consequences:** If wheels are missing on a platform, EC is unavailable there and replication still works.

## ADR-017 Keep legacy code in `legacy/`, untouched
- **Decision:** The original Flask app moves to `legacy/` (excluded from lint and tests). It is used by `migrate-legacy` and keeps attribution clear.
- **Interview angle:** A clear line between inherited code and my code.

## ADR-018 Auth for v1: single owner, username + scrypt password, signed session cookie, CSRF header
- **Options:** Google Sign-In for login; **a local owner account**; an external IdP.
- **Decision:** An owner account created by `drivemesh create-user`. Sessions use Starlette's signed cookie (`HttpOnly`, `SameSite=Lax`, `Secure` in prod).
  CSRF uses a synchronizer token stored in the session and sent as `X-CSRF-Token`. Login attempts are rate-limited per IP.
- **Consequences:** The schema is multi-user, so adding Google login later is additive.
- **AWS/GCP:** Cognito / Identity Platform.
