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
| Phase 1 Correctness foundations | ⏳ next |
| Phase 2–7 | ☐ |

## Phase 0 summary
- Legacy prototype moved to `legacy/` unchanged (ADR-017).
- `pyproject.toml` with pinned dependencies and the extras `dev`, `pg`, `s3`, `ec`.
- `drivemesh.config.Settings` (env vars `DRIVEMESH_*`, secrets as `SecretStr`), `env.example`.
- JSON logging with request ids and secret redaction (`drivemesh/logs.py`).
- Benchmark harness (`drivemesh/benchkit.py`).
- CI on GitHub Actions: ruff lint + format check, then pytest with coverage.

## Open issues
- OAuth client secret and the legacy `.key` rotation are pending (owner action; see MANUAL_STEPS.md).
