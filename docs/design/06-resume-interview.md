# 06 — Resume and Interview Prep

Rule: a number goes on the resume only if it appears in `results.md` with its method. Until then use `[placeholder]`.
Prefer the **real Google Drive** numbers once they exist. If you quote local-backend numbers, say "on local/simulated backends".

## Resume bullets (Google XYZ: accomplished X, measured by Y, by doing Z)

**DriveMesh: fault-tolerant encrypted storage across multiple cloud accounts** · Python, FastAPI, PostgreSQL, SQLAlchemy, Docker, GitHub Actions

Numbers below are from results.md. Where a bullet says *local/simulated*, keep that qualifier until the real-Drive runs (MANUAL_STEPS §4) replace it.

1. *Plain:* Rebuilt a team prototype that lost files whenever one cloud account failed into a system that keeps every file readable when accounts are lost.
   *Technical:* Achieved **0 files lost and 0 corrupted reads across 70 chaos trials** (account loss, chunk deletion and bit-flip corruption,
   `SIGKILL` mid-upload and mid-repair, metadata DB wipe + restore; R-008) by designing HDFS-style **replication (RF=2/3) and Reed–Solomon
   erasure coding** with weighted rendezvous placement, heartbeat health checks, parallel priority repair and a checksum scrubber on a **PostgreSQL** metadata service.
2. *Plain:* Made large transfers fast across many accounts while using a small, fixed amount of memory.
   *Technical:* Streamed 1–5 GB files with **peak memory flat at ~0.3 GB regardless of file size** (R-003). Raised 8-account throughput
   **5.6× over sequential** and **39 % over plain rendezvous-hash placement** on a simulated 10 MB/s-per-account network (R-004) by removing head-of-line
   blocking and adding power-of-two-choices load-aware placement on a bounded, backpressured **Python** thread pipeline (**FastAPI** streaming,
   per-account token buckets, jittered exponential backoff).
3. *Plain:* Made the system recover faster after a failure and stay fast as metadata grows.
   *Technical:* Cut post-failure re-replication time **2.8×** (31.2 s → 11.2 s, 4.3 → 12.7 MB/s, simulated; R-006) by parallelising repair. Cut **fsck over 1M chunks from
   291 s to 7.9 s on PostgreSQL** and removed a quadratic SQLite query plan (68 s → 0.03 s at 20k chunks; R-007) with query rewrites, a composite index and query-plan regression tests.
4. *Plain:* Made stored data private and tamper-evident, and took the project from zero tests to continuously deployed.
   *Technical:* Replaced Fernet (**+33.34 % storage overhead**, R-001) with **AES-256-GCM envelope encryption** (32 B per chunk, position-bound AAD,
   key rotation without re-upload). Shipped **199 tests at 91 % coverage** with **CI/CD** (GitHub Actions: lint, SQLite + PostgreSQL test matrix,
   dependency audit, **Docker** build smoke-tested against Postgres).

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

## STAR stories (from real bugs found in the audit and during benchmarking)

**S4: A query that was fine at 10k rows took 68 s at 20k (R-007)**
- *Situation:* The repair scan and fsck passed every test, but the 1M-chunk benchmark never finished.
- *Action:*
  - Measured stage by stage. 4× more rows gave 21× more time.
  - Raw SQL on one database was fast. The same query on another database with an identical schema was slow.
  - `EXPLAIN QUERY PLAN` showed SQLite, lacking statistics, sometimes chose the `(state, verified_at)` index for the replica join.
  - Fixed with a composite `(chunk_id, state)` index (migration 0002), a single-pass grouped fsck, and a test that asserts the query plan.
- *Result:* 0.03 s at 20k chunks. On Postgres at 1M chunks, fsck went from 291 s to 7.9 s.

**S5: Parallel uploads didn't scale (R-004)**
- *Situation:* 8 accounts gave only about 3× the throughput of 1.
- *Action:* Found two causes: head-of-line blocking in the writer's backpressure, and short-run hash imbalance. Fixed them with `FIRST_COMPLETED`
  backpressure and power-of-two-choices placement, and A/B tested with 3 trials each.
- *Result:* +39 % mean throughput, variance cut from 12 to 0.7 MB/s.

**S1: Removing an account silently corrupted every file (W-02)**
- *Situation:* In the forked prototype, chunks referenced accounts by **list index**.
- *Task:* Find out why downloads would fail after an account was removed.
- *Action:* Traced `app.py:229-231` (`pop(index)`) against `storage_manager.py:201-202`. Every account after the removed one shifted, so chunks pointed to
  the wrong Drive account. Redesigned identity as stable UUIDs with a uniqueness constraint on (user, provider, external id), and made unlink an evacuation job.
- *Result:* `test_w02_removing_middle_account_does_not_remap_others` and `test_unlink_with_evacuation` pass. Unlink refuses if it would violate RF. The account-loss chaos experiment lost 0 files in 20 trials (R-008).

**S2: Delete reported success while leaving data behind (W-04/W-03)**
- *Situation:* Delete swallowed errors (`storage_manager.py:247-249`) and still dropped the registry entry, so leaked chunks became unfindable.
  Failed uploads leaked chunks too.
- *Action:* Tombstone + a durable delete job enqueued in the same transaction (transactional outbox); NotFound treated as success (idempotent); retries with backoff; a `dead` state shown in the UI.
  Deterministic object names (`dm-<replica id>`) let GC clean up even when the crash happened between the remote write and the DB update.
- *Result:* 10/10 `SIGKILL`-mid-upload trials and 10/10 `SIGKILL`-mid-repair trials end with 0 orphaned objects and a clean fsck (R-008). `test_w04_delete_retries_and_never_silently_drops` proves a failed delete is retried, never forgotten.

**S3: The encryption key was in the public git history (W-00)**
- *Situation:* `.key` and the OAuth client secret had been committed and later only `git rm --cached`'d.
- *Action:* Rewrote history, force-pushed once, rotated the credentials, and redesigned to envelope encryption with keys only from env/secret store and key rotation by rewrap.
- *Result:* There is no key material in the repo, and `test_rewrap_rotates_master_key` shows rotation without re-uploading any data. Lesson: un-committing is not un-leaking; rotate first.
