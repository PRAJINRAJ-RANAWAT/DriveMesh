# Measured Results

Every number here comes from a script that was actually run. Each entry records the method, environment and date.
**Backend column:** `local` = LocalDisk or simulated backends on a laptop; `gdrive` = real Google Drive.
Numbers marked `local` are **not** claims about Google Drive performance.

## Environment (default unless stated)
- Machine: Apple Silicon Mac laptop (darwin), Python 3.12.11.

## R-001 — Fernet ciphertext overhead on a 5 MiB chunk (legacy format)
- Backend: n/a (pure CPU)
- Date: 2026-11-08
- Method: `Fernet(Fernet.generate_key()).encrypt(os.urandom(5*1024*1024))`, then compare lengths.
- Result: plaintext 5,242,880 B → token 6,990,604 B = **+33.34 %**. Cause: Fernet base64-encodes its token.
- Implication: the legacy design spends a quarter of every account's quota on encoding (W-25).
