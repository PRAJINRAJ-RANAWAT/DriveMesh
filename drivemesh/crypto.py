"""Envelope encryption.

KEK (master key, from env) --wraps--> DEK (random per file version) --encrypts--> chunks.

Chunk object layout: b"DMC1" || nonce(12) || AES-256-GCM(ciphertext || tag(16)).
The associated data binds a chunk to (file id, version, index, is-last) so that a storage
provider swapping, reordering, replaying or truncating objects is detected (STREAM-style).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

CHUNK_MAGIC = b"DMC1"
SEALED_MAGIC = b"DMS1"
NONCE_LEN = 12
TAG_LEN = 16
CHUNK_OVERHEAD = len(CHUNK_MAGIC) + NONCE_LEN + TAG_LEN  # 32 bytes


class CryptoError(Exception):
    """Authentication failed: wrong key, tampered bytes or wrong associated data."""


class KeyRingError(Exception):
    pass


@dataclass(frozen=True)
class KeyRing:
    keys: dict[str, bytes]
    active_id: str

    @classmethod
    def parse(cls, spec: str, active_id: str | None = None) -> KeyRing:
        """Parse "id1:base64key,id2:base64key". The active key defaults to the last one."""
        keys: dict[str, bytes] = {}
        for part in (p.strip() for p in spec.split(",") if p.strip()):
            if ":" not in part:
                raise KeyRingError("master key entries must look like id:base64key")
            kid, b64 = part.split(":", 1)
            try:
                raw = base64.urlsafe_b64decode(b64.encode())
            except Exception as exc:
                raise KeyRingError(f"key {kid!r} is not valid base64") from exc
            if len(raw) != 32:
                raise KeyRingError(f"key {kid!r} must be 32 bytes, got {len(raw)}")
            keys[kid] = raw
        if not keys:
            raise KeyRingError("no master keys configured")
        active = active_id or list(keys)[-1]
        if active not in keys:
            raise KeyRingError(f"active key id {active!r} not in key ring")
        return cls(keys=keys, active_id=active)

    def get(self, kid: str) -> bytes:
        try:
            return self.keys[kid]
        except KeyError as exc:
            raise KeyRingError(f"key id {kid!r} not available; cannot decrypt") from exc


def generate_key_spec(kid: str = "k1") -> str:
    return f"{kid}:{base64.urlsafe_b64encode(os.urandom(32)).decode()}"


def new_dek() -> bytes:
    return AESGCM.generate_key(bit_length=256)


def _seal(key: bytes, plaintext: bytes, aad: bytes) -> bytes:
    nonce = os.urandom(NONCE_LEN)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, aad)


def _open(key: bytes, blob: bytes, aad: bytes) -> bytes:
    if len(blob) < NONCE_LEN + TAG_LEN:
        raise CryptoError("ciphertext too short")
    try:
        return AESGCM(key).decrypt(blob[:NONCE_LEN], blob[NONCE_LEN:], aad)
    except InvalidTag as exc:
        raise CryptoError("authentication failed") from exc


def wrap_dek(ring: KeyRing, dek: bytes, file_id: str) -> tuple[bytes, str]:
    return _seal(ring.get(ring.active_id), dek, b"dek|" + file_id.encode()), ring.active_id


def unwrap_dek(ring: KeyRing, blob: bytes, kek_id: str, file_id: str) -> bytes:
    return _open(ring.get(kek_id), blob, b"dek|" + file_id.encode())


def seal(ring: KeyRing, plaintext: bytes, context: str) -> bytes:
    """Self-describing KEK encryption (tokens, metadata snapshots). Records the key id."""
    kid = ring.active_id.encode()
    if len(kid) > 255:
        raise KeyRingError("key id too long")
    body = _seal(ring.get(ring.active_id), plaintext, b"seal|" + context.encode())
    return SEALED_MAGIC + bytes([len(kid)]) + kid + body


def sealed_key_id(blob: bytes) -> str:
    if not blob.startswith(SEALED_MAGIC):
        raise CryptoError("not a sealed blob")
    n = blob[4]
    return blob[5 : 5 + n].decode()


def unseal(ring: KeyRing, blob: bytes, context: str) -> bytes:
    kid = sealed_key_id(blob)
    n = blob[4]
    return _open(ring.get(kid), blob[5 + n :], b"seal|" + context.encode())


def chunk_aad(file_id: str, version: int, idx: int, last: bool) -> bytes:
    return f"dm1|{file_id}|{version}|{idx}|{int(last)}".encode()


def encrypt_chunk(dek: bytes, plaintext: bytes, aad: bytes) -> bytes:
    return CHUNK_MAGIC + _seal(dek, plaintext, aad)


def decrypt_chunk(dek: bytes, obj: bytes, aad: bytes) -> bytes:
    if not obj.startswith(CHUNK_MAGIC):
        raise CryptoError("bad chunk magic")
    return _open(dek, obj[len(CHUNK_MAGIC) :], aad)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- passwords (scrypt, stdlib) -------------------------------------------------------------

_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **_SCRYPT)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_b64, dk_b64 = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    salt, expected = base64.b64decode(salt_b64), base64.b64decode(dk_b64)
    dk = hashlib.scrypt(password.encode(), salt=salt, dklen=len(expected), **_SCRYPT)
    return hmac.compare_digest(dk, expected)
