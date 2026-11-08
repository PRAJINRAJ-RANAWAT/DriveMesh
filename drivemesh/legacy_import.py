"""Import files from the legacy prototype (registry.json + Fernet chunks) into DriveMesh.

Each legacy file is streamed chunk by chunk: fetch legacy chunk -> Fernet-decrypt ->
DriveMesh writer (re-encrypted with envelope encryption, replicated). Nothing is deleted from
the legacy accounts; once verified, the owner can remove the old chunks manually.

Legacy facts this relies on (see docs/design/01-current-state.md):
  * registry.json: {name: {file_size, parent_path, chunks: [{account_id, drive_id, order}]}}
  * account_id is a *list index* into accounts_config.json (W-02)
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from drivemesh.core.system import DriveMesh
from drivemesh.errors import AlreadyExists

LegacyFetch = Callable[[int, str], bytes]


@dataclass
class ImportReport:
    imported: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)


def _dest_path(dest_root: str, parent_path: str, name: str) -> str:
    segs = [p for p in (parent_path or "root").split("/") if p]
    if segs and segs[0] == "root":
        segs = segs[1:]
    return "/".join([dest_root.rstrip("/"), *segs, name]) or "/"


def import_registry(
    dm: DriveMesh,
    user_id: str,
    registry: dict[str, Any],
    fernet_key: bytes,
    fetch: LegacyFetch,
    *,
    dest_root: str = "/legacy",
    dry_run: bool = False,
) -> ImportReport:
    cipher = Fernet(fernet_key)
    report = ImportReport()
    for name, info in sorted(registry.items()):
        dest = _dest_path(dest_root, info.get("parent_path", "root"), name)
        chunks = sorted(info.get("chunks", []), key=lambda c: c["order"])
        if [c["order"] for c in chunks] != list(range(len(chunks))):
            report.failed[name] = "registry entry has missing/duplicate chunk orders"
            continue
        if dry_run:
            report.imported.append(dest)
            continue
        parent = dest.rsplit("/", 1)[0] or "/"
        if parent != "/":
            with contextlib.suppress(AlreadyExists):
                dm.mkdir(user_id, parent, parents=True)
        try:
            w = dm.open_writer(user_id, dest)
        except AlreadyExists:
            report.skipped.append(dest)
            continue
        try:
            for c in chunks:
                w.write(cipher.decrypt(fetch(int(c["account_id"]), c["drive_id"])))
            result = w.close()
        except (InvalidToken, Exception) as exc:  # noqa: BLE001 - recorded per file
            if not w._closed:
                w.abort(f"legacy import failed: {exc}")
            report.failed[name] = f"{type(exc).__name__}: {exc}"
            continue
        if result.size != info.get("file_size", result.size):
            report.failed[name] = f"size mismatch {result.size} != {info.get('file_size')}"
            continue
        report.imported.append(dest)
    return report


def drive_fetcher(legacy_dir: Path) -> LegacyFetch:
    """Build a fetcher that reads legacy chunks with the legacy (full-scope) tokens.

    Runs locally and once: the new app never holds the legacy full-Drive scope.
    """
    from google.oauth2.credentials import Credentials

    from drivemesh.backends.gdrive import GoogleDriveBackend

    accounts = json.loads((legacy_dir / "accounts_config.json").read_text())
    backends: dict[int, GoogleDriveBackend] = {}

    def fetch(index: int, drive_id: str) -> bytes:
        if index not in backends:
            token_file = legacy_dir / accounts[index]["token_file"]
            creds = Credentials.from_authorized_user_file(str(token_file))
            backends[index] = GoogleDriveBackend.from_credentials(creds, folder_id="legacy")
        return backends[index].get(drive_id)

    return fetch
