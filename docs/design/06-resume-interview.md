# 06 — Resume and Interview Prep

Rule: a number goes on the resume only if it appears in `results.md` with its method. Until then use `[placeholder]`.
Prefer the **real Google Drive** numbers once they exist. If you quote local-backend numbers, say "on local/simulated backends".

## Resume bullets (Google XYZ: accomplished X, measured by Y, by doing Z)

**DriveMesh: fault-tolerant encrypted storage across multiple cloud accounts** · Python, FastAPI, PostgreSQL, SQLAlchemy, Docker, GitHub Actions

1. *Plain:* Rebuilt a team prototype that lost files whenever one cloud account failed into a system that keeps every file readable after losing any account.
   *Technical:* Achieved **0 files lost across [N] chaos runs** (account loss, chunk deletion and corruption, crashes mid-upload and mid-repair) by designing
   HDFS-style **replication (RF=2/3)** with weighted rendezvous placement, heartbeat health checks, a priority repair worker and a checksum scrubber over a **PostgreSQL** metadata service.
2. *Plain:* Made uploads and downloads of very large files use a small, fixed amount of memory and run faster in parallel.
   *Technical:* Streamed **[X] GB** files at **[Y] MB/s** with peak memory **≤ [Z] MB regardless of file size** by building a backpressured,
   bounded-concurrency chunk pipeline (**Python** threads, **FastAPI** streaming, per-account token-bucket rate limiting with jittered exponential backoff).
3. *Plain:* Made stored data both private and tamper-evident while cutting storage overhead.
   *Technical:* Reduced encryption storage overhead from **33.34 % to < 0.001 %** per chunk and made chunk swaps and truncation detectable by replacing Fernet with
   **AES-256-GCM envelope encryption** (per-file data keys, key rotation without re-upload, associated data binding each chunk to its file and position).
   *(33.34 % is measured, R-001. The new figure is from the format definition (32 B per 8 MiB) and is confirmed by test.)*
4. *Plain:* Took the project from no tests to a continuously tested, deployable service.
   *Technical:* Shipped **[T] tests at [C] % coverage** with **CI/CD** (GitHub Actions: lint, tests on SQLite and PostgreSQL, Docker build), structured logs and
   Prometheus metrics, and recovered the full metadata store from encrypted off-site snapshots in **[R] s** (chaos test) to eliminate the metadata single point of failure.

ATS keywords covered: distributed systems, fault tolerance, replication, durability, scalability, encryption, Python, PostgreSQL, REST API, concurrency, Docker, CI/CD.

**Attribution line** (put it under the project title or say it out loud):
> "Extended a 3-person course prototype (chunked, encrypted uploads to Google Drive). I audited it, found the data-loss and security bugs, and redesigned and rebuilt the
> storage engine, metadata service, durability layer, security model, tests and deployment."

If asked whether you used AI tools: say yes and say how. You used an AI pair-programmer to implement a design you specified and reviewed,
and you verified it with tests and chaos experiments you can rerun live. Dishonesty here is far worse than the answer.

## The 10 most likely interview questions

1. **Walk me through a write.**
   - Reserve a `pending` version row with a wrapped DEK.
   - Stream chunks with a bounded window: encrypt each with AAD.
   - Place it with rendezvous hashing: insert chunk + replica rows (`pending`), whose ids double as remote names, then upload in parallel with retries and fall back to the next candidate.
   - Mark the replicas ok.
   - Commit in one transaction: check that every chunk has enough ok replicas, supersede the old version, enqueue its deletion.
   - Failure at any step leaves a `pending` file that GC cleans by id or by name.
2. **What happens when an account disappears?**
   - The health checker sees AUTH or transient failures and marks it `revoked`/`unreachable`. Reads skip it immediately.
   - After `dead_after`, its replicas count as lost and repair re-replicates them, fewest healthy replicas first.
   - Window of vulnerability = repair time (B-5).
3. **Replication vs erasure coding?**
   - Overhead: 2–3× vs 1.5×.
   - Failures tolerated per account count.
   - Repair read amplification (k×) and per-read request fan-out.
   - The minimum number of accounts.
   - Why the default is RF=2 for 5 accounts; when RS(4,2) wins (≥ 6 accounts, capacity-bound, cold data).
4. **How do you detect corruption?**
   - SHA-256 of each stored object (checked without the key, on every read and by the scrubber).
   - The GCM tag with AAD (detects tampering and swaps).
   - A whole-file hash.
   - The scrubber walks oldest-verified first under a byte-rate limit.
5. **What's your NameNode SPOF story?**
   - The metadata DB is the SPOF. Mitigations: managed Postgres backups, plus encrypted logical snapshots on every storage account, plus a `restore` command (chaos test C-6).
   - RPO = snapshot interval. No hot standby, on purpose.
   - What you'd do next: WAL shipping / a Postgres replica.
6. **How would you scale to 1M files / 100 accounts?**
   - Metadata: indexed queries, batch repair scans; Postgres; partition replicas by account.
   - Placement: still O(N), trivial at N = 100.
   - Quotas: per-account buckets mean aggregate throughput scales with N until host bandwidth binds, so you shard workers across hosts.
   - The job table moves to SQS.
   - Listing-based reconciliation becomes the bottleneck. Do it incrementally.
7. **How do concurrent writers to the same path behave?** Immutable versions, last-commit-wins, a partial unique index; `overwrite=false` gives create-only. Compare HDFS leases and S3 conditional writes.
8. **Why a DB-backed job queue instead of Celery/SQS?** Transactional outbox: the enqueue is in the same transaction as the tombstone. Leases give at-least-once delivery, so handlers are idempotent.
9. **How do you stay within Google's rate limits?**
   - Per-account token bucket and semaphore.
   - Error classification: only retry transient errors.
   - Full-jitter backoff capped at 32 s.
   - Metrics on retries.
   - Quota math: 50 units per create.
10. **How is the key managed, and what if it's lost?**
    - KEK in env/secret store → per-file DEKs; rotation = rewrap.
    - A lost KEK means total loss, with no escrow (by design). Here's how KMS would change that.
    - Tell the leaked-key incident story.

## STAR stories (from real bugs found in the audit)

**S1: Removing an account silently corrupted every file (W-02)**
- *Situation:* In the forked prototype, chunks referenced accounts by **list index**.
- *Task:* Find out why downloads would fail after an account was removed.
- *Action:* Traced `app.py:229-231` (`pop(index)`) against `storage_manager.py:201-202`. Every account after the removed one shifted, so chunks pointed to
  the wrong Drive account. Redesigned identity as stable UUIDs with a uniqueness constraint on (user, provider, external id), and made unlink an evacuation job.
- *Result:* A test now removes the middle account of 3 and every file stays readable ([test name]). Unlink refuses if it would violate RF.

**S2: Delete reported success while leaving data behind (W-04/W-03)**
- *Situation:* Delete swallowed errors (`storage_manager.py:247-249`) and still dropped the registry entry, so leaked chunks became unfindable.
  Failed uploads leaked chunks too.
- *Action:* Tombstone + a durable delete job enqueued in the same transaction (transactional outbox); NotFound treated as success (idempotent); retries with backoff; a `dead` state shown in the UI.
  Deterministic object names (`dm-<replica id>`) let GC clean up even when the crash happened between the remote write and the DB update.
- *Result:* The crash-injection chaos test finds 0 orphaned objects after GC ([results.md C-5]).

**S3: The encryption key was in the public git history (W-00)**
- *Situation:* `.key` and the OAuth client secret had been committed and later only `git rm --cached`'d.
- *Action:* Rewrote history, force-pushed once, rotated the credentials, and redesigned to envelope encryption with keys only from env/secret store and key rotation by rewrap.
- *Result:* There is no key material in the repo, and rotation runs without re-uploading data ([test name]). Lesson: un-committing is not un-leaking; rotate first.
