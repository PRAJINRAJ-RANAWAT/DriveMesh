# Measured Results

Every number here comes from a script that was actually run. Each entry records the method, environment and date.
Raw JSON goes to `bench/out/` (gitignored); the numbers below are copied from it.

**Backend legend**
- `local`: LocalDisk accounts on the laptop SSD. No network. Measures DriveMesh's own CPU/IO cost.
- `sim`: LocalDisk plus `FaultyBackend` with a **simulated** network: fixed per-request latency and a serialized per-account link (`SharedLink`).
  The parameters (100 ms per request, 10 MB/s per account) are **assumptions**, not Google Drive measurements.
- `gdrive`: real Google Drive. **None yet; pending owner runs ([MANUAL_STEPS §4](MANUAL_STEPS.md)).**

**Environment:** Apple Silicon Mac laptop (macOS, Darwin 25.3), Python 3.12.11, SQLite (bundled), Postgres 16-alpine in local Docker.
Date of all runs: 2026-11-08. Defaults: chunk 8 MiB, `inflight_chunks` 4, RF 2.

---

## R-001: Fernet ciphertext overhead on a 5 MiB chunk (legacy format)
- Backend: none (pure CPU).
- Method: `Fernet(Fernet.generate_key()).encrypt(os.urandom(5*1024*1024))`, then compare lengths.
- Result: 5,242,880 B → 6,990,604 B = **+33.34 %** (base64 encoding of the token).

## R-002: Storage overhead per durability scheme (`local`)
- Method: `python -m bench.run overhead --size-mb 64`. Write one 64 MiB file per scheme, sum the bytes of all `dm-*` objects on disk.

| Scheme | Stored bytes | Overhead |
|---|---|---|
| RF=1 | 67,109,120 | 1.0000× |
| RF=2 | 134,218,240 | 2.0000× |
| RF=3 | 201,327,360 | 3.0000× |
| RS(4,2) | 100,663,680 | 1.5000× |
| RS(6,3) | 100,663,704 | 1.5000× |

- Encryption overhead is 32 B per 8 MiB chunk (RF=1: +256 B on 64 MiB = 0.0004 %), vs Fernet's 33.34 % (R-001).

## R-003: Throughput and peak memory vs file size (`local`)
- Method: `python -m bench.run throughput --size-mb N --accounts 3 --rf 2`, one process per size.
  - Data is streamed from a generator (never held in memory) and the download is hashed and compared with the upload's SHA-256.
  - Peak RSS is `ru_maxrss` of the process. The baseline is the RSS after imports, before the transfer.

| File | Upload MB/s | Download MB/s | Peak RSS (MiB) |
|---|---|---|---|
| 100 MB | 563.6 | 1009.3 | 214.1 |
| 1 GB | 582.3 | 1112.4 | 294.6 |
| 2 GB | 513.1 | 1083.3 | 291.3 |
| 3 GB | 313.5 | 1140.3 | 293.9 |
| 5 GB | 224.0 | 1081.9 | 330.9 |

- Baseline (interpreter + libraries) ≈ 68 MiB in every run.
- **Memory is bounded:** it plateaus near 290–330 MiB from 1 GB to 5 GB. The design bound is `inflight × chunk × (plain + cipher + copies)`.
- The upload rate drops for ≥ 3 GB because 2× the file is written with `fsync` to one laptop SSD (RF=2). That's a disk limit, not a DriveMesh one.
- Note: the 100 MB JSON was overwritten by the 1 GB run that finished in the same second. The 100 MB row is copied from that run's console output. `benchkit.save` now never overwrites.

## R-004: Sequential vs parallel, scaling with accounts (`sim`: 100 ms/request, 10 MB/s per account, RF=1, 128 MB file)
- Method: `python -m bench.run scaling --size-mb 128 --latency 0.1 --account-mbps 10 --inflight 8 …`
- Sequential = 1 chunk in flight; parallel = 8.

**Initial single runs, before the optimisations:**

| Accounts | Sequential up/down MB/s | Parallel up/down MB/s |
|---|---|---|
| 1 | 8.47 / 8.62 | 9.88 / 9.89 |
| 2 | 8.49 / 8.57 | 19.49 / 19.07 |
| 4 | 8.47 / 8.58 | 22.27 / 21.73 |
| 8 | 8.49 / 8.58 | 30.57 / 30.78 |

**Finding:** scaling was sub-linear beyond 2 accounts. Two causes were fixed:
1. **Head-of-line blocking:** the writer waited for the *oldest* in-flight chunk. It now waits for any (`FIRST_COMPLETED`).
2. **Short-run placement imbalance:** rendezvous hashing is only balanced over many chunks. A "power of two choices" step now picks the least-loaded
   account among the top 2·RF rendezvous candidates (`placement.load_aware`), and reads prefer the least-loaded replica.

**A/B after fix 1, 3 trials each** (`--trials 3 --parallel-only`, with and without `--no-load-aware`), mean (min–max):

| Accounts | Upload MB/s, HRW only | Upload MB/s, load-aware | Download MB/s, HRW only | Download MB/s, load-aware |
|---|---|---|---|---|
| 2 | 17.49 (15.70–19.45) | 19.50 (19.50–19.51) | 16.09 (15.44–17.37) | 19.26 (19.13–19.52) |
| 4 | 22.52 (19.49–25.85) | **32.73** (29.75–37.64) | 20.34 (17.10–22.26) | **30.59** (25.04–36.81) |
| 8 | 33.82 (25.82–37.83) | **47.16** (46.86–47.52) | 33.24 (25.00–37.78) | **47.07** (46.97–47.18) |

- Load-aware placement: **+39 % mean upload at 8 accounts, +45 % at 4**, with far less variance.
- Parallel at 8 accounts vs sequential: **47.2 vs 8.5 MB/s (5.6×)**.

## R-005: Chunk size sweep (`sim`: 100 ms/request, 10 MB/s per account, 4 accounts, RF=2, 128 MB, inflight 4)
- Method: `python -m bench.run chunksize --size-mb 128 --accounts 4 --latency 0.1 --account-mbps 10 --chunk-sizes 1 4 8 16 32`

| Chunk | Chunks | Upload MB/s | Download MB/s | Create requests per GiB (RF=2) |
|---|---|---|---|---|
| 1 MiB | 128 | 16.23 | 17.54 | 2048 |
| 4 MiB | 32 | 18.42 | 25.57 | 512 |
| 8 MiB | 16 | 19.11 | 28.15 | 256 |
| 16 MiB | 8 | 19.46 | 35.62 | 128 |
| 32 MiB | 4 | 19.15 | 19.19 | 64 |

- Upload is bandwidth-bound: 4 accounts × 10 MB/s ÷ 2 replicas = 20 MB/s ceiling, reached from 4 MiB up. 1 MiB loses ~15 % to per-request latency.
- Download falls at 32 MiB because a 128 MB file has only 4 chunks for an 8-wide window.
- **ADR-009 keeps 8 MiB** until the real-Drive run; 16 MiB is the candidate.

## R-006: Time to restore RF after losing 1 of 4 accounts (RF=2)
- Method: `python -m bench.run repair …`
  1. Write the files.
  2. Delete one account's directory (all its objects vanish).
  3. One health check.
  4. Time `repair()` until no chunk is under-replicated.
  5. fsck.

| Backend | Data lost on the account | Before fix (serial repair) | After fix (parallel repair, 8 workers) | fsck after |
|---|---|---|---|---|
| `sim` (100 ms, 10 MB/s/account), 256 MB written | 134–143 MB | 31.2 s = **4.30 MB/s** | 11.2 s = **12.74 MB/s** | clean, 0 under-replicated |
| `local`, 1 GB written | 528–545 MB | 1.10 s = 479 MB/s | 0.58 s = 933 MB/s | clean |

- **Finding:** repair handled one chunk at a time (read then write, 1.9 s per 8 MiB chunk on the simulated link). It now repairs chunks concurrently,
  bounded, still fewest-healthy-first. 12.7 MB/s is 85 % of the 15 MB/s ceiling: 3 surviving 10 MB/s links, and each repaired byte crosses two of them.

## R-007: Metadata at scale (repair scan and fsck over N chunks × 2 replicas)
- Method: `python -m bench.run metadata --chunks N` bulk-inserts N chunks (1000 per file) with ~0.1 % under-replicated, runs `ANALYZE`,
  then times `under_replicated()` (the repair queue) and `fsck()`.

**Before the fix, SQLite (quadratic):**

| Chunks | Repair scan | fsck |
|---|---|---|
| 5,000 | 3.21 s | 3.16 s |
| 20,000 | 68.3 s | 68.0 s |

- **Root cause:** without statistics, SQLite sometimes chose the `(state, verified_at)` index for the replica join (seen with `EXPLAIN QUERY PLAN` on
  one database but not another of identical schema). That scans every `ok` replica per chunk.
- **Fix:** composite index `(chunk_id, state)` (migration 0002) plus a plan regression test, and fsck rewritten from one query per file to a single grouped pass.

**After the fix:**

| DB | Chunks | Repair scan | fsck |
|---|---|---|---|
| SQLite | 20,000 | 0.033 s | 0.052 s |
| SQLite | 100,000 | 0.197 s | 0.301 s |
| SQLite (2 MiB page cache) | 1,000,000 | 26.4 s | 16.5 s |
| Postgres 16 | 1,000,000 | **5.04 s** | **7.87 s** (291 s with the per-file fsck) |

- Postgres is the deployed DB (ADR-002).
- SQLite slows down beyond ~100k chunks: with a 2 MiB page cache, random UUID-ordered index lookups on a ~1 GB file become I/O.

## R-008: Chaos experiments (`local`, real files and real processes)
- Method: `python -m bench.chaos <experiment> --trials 10` (seeded; chunk 256 KiB; 10–12 random files of 0–3 MiB per trial).
- After every trial the oracle checks:
  - every committed file reads back with its original SHA-256
  - `fsck --deep` is clean (every `ok` replica's object exists; every chunk at target RF)
  - after GC, no object on any account is unreferenced by the metadata

| Experiment (10 trials each) | What is done | Files lost | Wrong bytes served | fsck clean | Orphans left |
|---|---|---|---|---|---|
| account-loss | delete 1 of 4 account directories (RF=2), heartbeat, repair | 0 | 0 | 10/10 | 0 |
| account-loss-rf3 | delete 2 of 5 account directories (RF=3) | 0 | 0 | 10/10 | 0 |
| delete-chunks | delete one replica of ~40 % of chunks (21–34 objects), then read, scrub, repair | 0 | 0 | 10/10 | 0 |
| corrupt-chunks | flip one random bit in one replica of ~40 % of chunks (22–40 objects) | 0 | 0 | 10/10 | 0 |
| kill-upload | `SIGKILL` a process mid-upload of a 64 MiB file, after 15–32 of its objects were written | 0 | 0 | 10/10 | 0 |
| kill-repair | delete 1 of 4 accounts, `SIGKILL` the repair process mid-pass (7–34 chunks still under-replicated; 0–7 replicas left `pending`) | 0 | 0 | 10/10 | 0 |
| metadata-restore | back up, delete the SQLite DB, restore from a random account | 0 | 0 | 10/10 | 0 |

- **Total: 70 trials, 0 files lost, 0 wrong bytes served.**
- Account-loss trials: every file was already readable *before* repair (fallback reads).
- Corruption was detected by the read path and the scrubber; nothing corrupt was ever returned.
- Metadata restore took 0.014–0.045 s per trial (small namespaces).
- Two flaws in my own first harness were caught by inspecting per-trial details, and those trials were re-run:
  - kill-upload originally fired before the victim upload had started (its object counter included pre-existing objects).
  - kill-repair could fire before repair began.
  - Both now trigger on objects/replicas *created by the victim process*. The counts above are from the corrected runs
    (`chaos-2026-11-08T224835.json`, `chaos-2026-11-08T224910.json`; others from `chaos-2026-11-08T224611.json`).

## R-009: SQLite page cache experiment (negative result)
- Hypothesis: a larger page cache (`PRAGMA cache_size=-65536`, 64 MiB) would speed up SQLite at 1M chunks.
- Result: repair scan 26.1 s (vs 26.4 s), fsck 17.8 s (vs 16.5 s). **No improvement**, so the pragma was reverted.
- At this scale use Postgres (R-007).

## Pending (`gdrive`)
| Metric | Status |
|---|---|
| Upload/download MB/s on real Drive (1 GB, 3–4 accounts) | [placeholder]: MANUAL_STEPS §4 |
| Sequential vs parallel on real Drive | [placeholder] |
| Chunk-size sweep on real Drive | [placeholder] |
| Repair time after revoking one real account | [placeholder] |
| 403/429 retry counts at the default rate limits | [placeholder] |
