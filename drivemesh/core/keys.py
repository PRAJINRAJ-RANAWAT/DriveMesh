"""Master-key rotation by re-wrapping (no data is re-uploaded).

1. Add a new key to DRIVEMESH_MASTER_KEYS and make it DRIVEMESH_ACTIVE_KEY_ID.
2. Run `drivemesh rewrap`: every file DEK and every account secret is re-encrypted with it.
3. When `remaining_old` is 0, the old key can be removed from the environment.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from drivemesh import crypto
from drivemesh.db import File, LinkedAccount

if TYPE_CHECKING:
    from drivemesh.core.system import DriveMesh


def rewrap_all(dm: DriveMesh, batch: int = 500) -> dict[str, int]:
    ring = dm.keyring
    files = accounts = 0
    with dm.session() as s:
        for f in s.scalars(select(File).where(File.kek_id != ring.active_id).limit(batch * 100)):
            dek = crypto.unwrap_dek(ring, f.wrapped_dek, f.kek_id, f.id)
            f.wrapped_dek, f.kek_id = crypto.wrap_dek(ring, dek, f.id)
            files += 1
        for a in s.scalars(select(LinkedAccount).where(LinkedAccount.secret_blob.is_not(None))):
            if crypto.sealed_key_id(a.secret_blob) != ring.active_id:
                plain = crypto.unseal(ring, a.secret_blob, f"account|{a.id}")
                a.secret_blob = crypto.seal(ring, plain, f"account|{a.id}")
                accounts += 1
    with dm.session() as s:
        remaining = len(list(s.scalars(select(File.id).where(File.kek_id != ring.active_id))))
    dm.event("keys_rewrapped", files=files, accounts=accounts, remaining_old=remaining)
    return {"files": files, "accounts": accounts, "remaining_old": remaining}
