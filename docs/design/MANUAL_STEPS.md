# Manual Steps (owner only)

Everything here needs your Google identity, your accounts or your judgment. Do them in order. Each step says how to tell it worked.
Commands assume the repo root with the virtualenv active (`source .venv/bin/activate`).

---

## 0. Before anything: protect the master key
1. Generate a key: `drivemesh gen-key` prints `k1:<base64>`.
2. Store it in a password manager **and** on paper. **If this key is lost, every file is unrecoverable.** There is no escrow (07-threat-model.md).
3. Never paste it into chat, a commit, an issue or a screenshot.

## 1. Rotate the secrets leaked in git history (W-00)
The old `.key` and OAuth client secret were public from 2026-03-16 to 2026-11-08.

1. **Legacy data first (optional).** If you want the files stored by the old prototype, import them *before* discarding the old key:
   ```bash
   # put registry.json, accounts_config.json, token_*.json and .key from the old app into a folder
   drivemesh migrate-legacy /path/to/legacy-folder --dry-run   # shows what would be imported
   drivemesh migrate-legacy /path/to/legacy-folder             # streams, decrypts, re-encrypts, replicates
   ```
   It works when the report lists files under `imported`, `drivemesh ls /legacy` shows them, and `drivemesh fsck --deep` exits 0.
   Then delete the old chunk files from the old Drive accounts by hand (they're named `<file>.chunkN`).
2. **Destroy the old Fernet key.** Delete the legacy `.key` from every machine. Treat anything still encrypted with it as public.
3. **Rotate the OAuth client.**
   1. Google Cloud Console → APIs & Services → Credentials → the old client → **Delete** it. Deleting is simpler than resetting the secret, and the old one was a Desktop client.
   2. Create the new Web client in step 2.
4. **Revoke old grants.** For each Google account used with the prototype: <https://myaccount.google.com/permissions> → remove the old app.
   It worked when the app is no longer listed.

## 2. Google Cloud project and OAuth client (free)
1. <https://console.cloud.google.com/>: create a project named `drivemesh`. No billing account is needed for the Drive API.
2. APIs & Services → Library → **Google Drive API** → Enable.
3. **Google Auth Platform → Branding:**
   - App name: `DriveMesh`
   - Support email: yours
   - Developer contact: yours
4. **Audience:**
   - User type: **External**.
   - Publishing status: **Testing**.
   - Add **every Gmail address you will link** as a test user (limit 100).
   - Testing-mode facts ([Google docs](https://support.google.com/cloud/answer/15549945)): consent and refresh tokens **expire after 7 days**, so accounts show as `revoked` weekly and must be re-linked (the "Link Google Drive" button refreshes them in place).
   - To avoid the weekly expiry, publish the app ("In production"). DriveMesh requests only non-sensitive scopes (`openid`, `email`, `drive.file`), so it needs no restricted-scope security assessment. Google may still ask for **brand verification** (homepage, privacy policy) before showing your app name.
   - An unverified app shows a warning screen and has a lifetime cap of 100 users. That's fine for a single-owner demo.
5. **Data Access → Add scopes:**
   - `openid`
   - `.../auth/userinfo.email`
   - `.../auth/drive.file`
   - **Do not** add `.../auth/drive` (restricted; see ADR-010).
6. **Clients → Create client → Web application**, name `drivemesh-web`. **Authorized redirect URIs:**
   - `http://localhost:8000/accounts/callback/google` (local)
   - `https://<your-render-service>.onrender.com/accounts/callback/google` (add after step 5)
7. Copy the client ID and secret into `.env` (local) or Render (deploy). Never commit them.

Check: `drivemesh serve` → the sign-in page shows **Continue with Google**, and after signing in, Accounts shows **Link a Google Drive account**.

**The same client serves both "Continue with Google" (sign-in / sign-up) and "Link Google Drive"** (storage), through the same
redirect URI, so no extra setup is needed. Sign-in asks only for `openid` + `email`; linking asks for `drive.file`.
While the app is in **Testing** mode, only the Gmail addresses listed as test users can sign up or link. That is your sign-up allowlist until you publish the app.

## 3. Run locally with real Gmail accounts
```bash
cp env.example .env           # then edit .env: DRIVEMESH_MASTER_KEYS, DRIVEMESH_SESSION_SECRET,
                              # DRIVEMESH_GOOGLE_CLIENT_ID / _SECRET
drivemesh init
drivemesh create-user owner   # optional: an operator account with a password (can link local folders, sees /metrics)
drivemesh serve               # http://localhost:8000
```
1. Click **Continue with Google** to create your DriveMesh account, then on **Accounts** click **Link a Google Drive account** once per Gmail account. Use the account picker to choose a different account each time, and tick the Drive checkbox on the consent screen.
2. Accounts appear as `active` with their real quota.
3. Upload a file, open **Cluster status**, and check that replicas are on 2 different accounts (`/api/v1/files/meta?path=…`).
4. In each Google Drive web UI you should see a `DriveMesh` folder with `dm-…` objects. **Don't delete them by hand** unless you're doing a chaos test.

## 4. Real Google Drive benchmarks (fills the `gdrive` rows in results.md)
Use 3–4 linked accounts and a wired network. Quiet the machine first.
```bash
# 1. Throughput (B-1) and memory (B-3): upload/download a 1 GB file through the real accounts
dd if=/dev/urandom of=/tmp/dm-1g.bin bs=1m count=1024
/usr/bin/time -l drivemesh put /tmp/dm-1g.bin /bench/1g.bin     # macOS: "maximum resident set size"
/usr/bin/time -l drivemesh get /bench/1g.bin /tmp/dm-1g.out && cmp /tmp/dm-1g.bin /tmp/dm-1g.out
# 2. Sequential vs parallel (B-2): repeat with DRIVEMESH_INFLIGHT_CHUNKS=1 and =8
# 3. Chunk size (B-4): repeat 1. with DRIVEMESH_CHUNK_SIZE=4194304 / 8388608 / 16777216
# 4. Repair time (B-5): unlink one account in the UI (it evacuates), or revoke it at
#    myaccount.google.com/permissions and time `drivemesh worker` until status shows 0 under-replicated
drivemesh status
```
Record each run in `results.md` under **gdrive**:
- date
- accounts and network (e.g. "home Wi-Fi, ~40 Mbps up")
- command
- the time output

Watch for `dm_backend_retries` (`/metrics`): 403/429 responses show the quota limits in practice.

## 5. Deploy (free tier: Render + Neon)
Free-tier terms change. Re-check [Render free](https://render.com/docs/free) and [Neon plans](https://neon.com/docs/introduction/plans) first.
As of 2026-11-08:
- Render free web services spin down after 15 idle minutes.
- Render's free Postgres expires after 30 days, so it isn't used.
- Neon Free has 1 GB per project and needs no card.

1. **Neon:**
   1. Sign up with GitHub at <https://neon.com>.
   2. Create project `drivemesh` (region near Oregon / us-west).
   3. Copy the **pooled** connection string (`postgresql://…-pooler…/neondb?sslmode=require`).
2. **Render:**
   1. Sign up with GitHub at <https://render.com>.
   2. **New → Blueprint** → select `PRAJINRAJ-RANAWAT/DriveMesh` (it reads `render.yaml`).
   3. When prompted, paste:
      - `DRIVEMESH_DATABASE_URL` = the Neon string
      - `DRIVEMESH_MASTER_KEYS` = your `k1:…` key (step 0)
      - `DRIVEMESH_PUBLIC_BASE_URL` = `https://<service>.onrender.com`
      - `DRIVEMESH_GOOGLE_CLIENT_ID` / `_SECRET`
      - `DRIVEMESH_OWNER_USERNAME` / `DRIVEMESH_OWNER_PASSWORD` (a strong password; the owner is created on first start, so you can remove these two afterwards)
3. Add the Render callback URL to the OAuth client (step 2.6).
4. Deploy. Checks:
   - `https://<service>.onrender.com/healthz` → `{"ok":true}`
   - `/readyz` → `{"ready":true}` (about 1 minute after start, once the first health pass leaves safe mode)
   - If startup fails with "unsafe production configuration", the log says which variable is wrong.
5. Log in and link accounts as in step 3.
6. Optional monitoring: `/metrics` with header `Authorization: Bearer <DRIVEMESH_METRICS_TOKEN>` (shown in Render's env tab).

Known limitations of the free deployment:
- Repair, scrub and backup only run while the instance is awake. Jobs are durable and resume on the next request.
- The first request after idle takes about a minute.
- Large uploads depend on Render's proxy limits.

## 6. Demo video (≤ 3 minutes)
Record a screen capture (QuickTime → New Screen Recording) at 1080p. Script:
1. **0:00–0:20** Problem: "Free storage is scattered across accounts, and one lost account means lost files." Show the status page with 3–4 linked Gmail accounts.
2. **0:20–0:50** Upload a ~200 MB file with drag-and-drop and show the progress bar. Open `/api/v1/files/meta`: each chunk is on 2 different accounts.
3. **0:50–1:30** Chaos: revoke one account at myaccount.google.com/permissions (or delete its `DriveMesh` folder). Show it turning `revoked`/`unreachable`. Download the file anyway; it's intact.
4. **1:30–2:10** Repair: the under-replicated count goes to 0 on the status page. Show the events list (`replica_missing` → `repair_pass`).
5. **2:10–2:40** Engineering:
   - architecture diagram (README)
   - results table: throughput, memory bound, repair time, 0 files lost across N chaos runs
   - CI badge
6. **2:40–3:00** What you'd do next (03-lld / 06 interview doc), plus the honest attribution line.

Upload it unlisted to YouTube and link it in the README.

## 7. Housekeeping
- Delete the local Postgres test container when done: `docker rm -f dm-pg`.
- `bench/work/` and `bench/out/` are gitignored. results.md is the record.
