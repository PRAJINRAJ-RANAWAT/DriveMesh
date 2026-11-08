# DriveMesh

A fault-tolerant, encrypted storage layer that pools multiple storage accounts you own (Google Drive first; local disk and S3-compatible
backends behind the same interface) into one replicated namespace, in the spirit of HDFS/GFS.

> **Status:** being rebuilt from a team prototype. The original Flask prototype is preserved unchanged in [`legacy/`](legacy/).
> The design docs are in [`docs/design/`](docs/design/), starting with the [audit of the original code](docs/design/01-current-state.md).

## Development

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
ruff check . && ruff format --check .
pytest
```

Configuration comes from `DRIVEMESH_*` environment variables; see [`env.example`](env.example).
