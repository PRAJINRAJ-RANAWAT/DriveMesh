"""Everything DriveMesh puts on a storage account is ciphertext, whatever the file size.

* A file up to one chunk is NOT split: it becomes exactly one encrypted object per replica.
* Larger files become one encrypted object per chunk per replica.
* Object layout: b"DMC1" || 12-byte nonce || AES-256-GCM(ciphertext || 16-byte tag)
  -> stored size = plaintext size + 32 bytes.
* Metadata snapshots on the accounts are sealed too (b"DMS1"...).
"""

from __future__ import annotations

import pytest

from drivemesh import crypto
from drivemesh.backends.local import MemoryBackend
from tests.conftest import CHUNK

SECRET = b"TOP-SECRET-PASSPORT-NUMBER-X1234567 "


def stored_objects() -> list[bytes]:
    return [
        data
        for be in MemoryBackend._instances.values()
        for name, (data, _) in be.objects.items()
        if name.startswith("dm-") and not name.startswith("dm-meta-")
    ]


def assert_ciphertext(obj: bytes) -> None:
    assert obj.startswith(crypto.CHUNK_MAGIC)
    assert SECRET not in obj and SECRET[:16] not in obj


@pytest.mark.parametrize("size", [1, 100, CHUNK - 1, CHUNK])
def test_small_files_are_one_encrypted_object_not_split(dm, user, accounts, size):
    data = (SECRET * (size // len(SECRET) + 1))[:size]
    dm.put(user.id, "/small.txt", data)
    objs = stored_objects()
    assert len(objs) == dm.settings.default_rf  # 1 chunk x RF copies: not split
    for obj in objs:
        assert_ciphertext(obj) if size >= 16 else obj.startswith(crypto.CHUNK_MAGIC)
        assert len(obj) == size + crypto.CHUNK_OVERHEAD  # +32 B: magic, nonce, GCM tag
    assert dm.stat(user.id, "/small.txt").chunk_count == 1
    assert dm.get_bytes(user.id, "/small.txt") == data


def test_large_files_are_split_and_every_piece_is_encrypted(dm, user, accounts):
    data = SECRET * (3 * CHUNK // len(SECRET) + 5)  # a little over 3 chunks
    dm.put(user.id, "/big.bin", data)
    f = dm.stat(user.id, "/big.bin")
    objs = stored_objects()
    assert f.chunk_count == 4 and len(objs) == 4 * dm.settings.default_rf
    for obj in objs:
        assert_ciphertext(obj)
    assert dm.get_bytes(user.id, "/big.bin") == data


def test_same_content_uploaded_twice_gives_different_ciphertext(dm, user, accounts):
    """Fresh key and nonces per file version: the storage provider can't spot duplicates."""
    dm.put(user.id, "/a", SECRET * 10)
    first = set(stored_objects())
    dm.put(user.id, "/b", SECRET * 10)
    second = set(stored_objects()) - first
    # The RF copies of one chunk are the same ciphertext (encrypted once, then copied), so each
    # upload adds one distinct object; across uploads of identical content nothing repeats.
    assert len(first) == 1 and len(second) == 1 and second.isdisjoint(first)


def test_empty_file_stores_nothing_on_the_accounts(dm, user, accounts):
    dm.put(user.id, "/empty", b"")
    assert stored_objects() == []
    assert dm.get_bytes(user.id, "/empty") == b""


def test_metadata_backups_on_the_accounts_are_sealed(dm, user, accounts):
    from drivemesh.core.backup import backup_now

    dm.put(user.id, "/my-tax-return-2026.pdf", SECRET)
    backup_now(dm)
    snaps = [
        data
        for be in MemoryBackend._instances.values()
        for name, (data, _) in be.objects.items()
        if name.startswith("dm-meta-")
    ]
    assert snaps, "no snapshot written"
    for blob in snaps:
        assert blob.startswith(crypto.SEALED_MAGIC)
        assert b"tax-return" not in blob and b"owner" not in blob
