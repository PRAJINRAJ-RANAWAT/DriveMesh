"""Optional Reed-Solomon erasure coding (zfec, GF(2^8)).

An encrypted chunk object is split into k data shards plus m parity shards, each stored on a
distinct account. Any k shards reconstruct the object; the object's SHA-256 and AEAD tag are
then verified as usual. Overhead is (k+m)/k instead of RF; tolerates m account losses.
"""

from __future__ import annotations

from drivemesh.errors import InvalidRequest

DEFAULT_K = 4
DEFAULT_M = 2


def available() -> bool:
    try:
        import zfec  # noqa: F401
    except ImportError:
        return False
    return True


def require_available() -> None:
    if not available():
        raise InvalidRequest("erasure coding requires the 'ec' extra: pip install drivemesh[ec]")


def validate(k: int, m: int) -> None:
    if not (1 <= k <= 32 and 1 <= m <= 16 and k + m <= 48):
        raise InvalidRequest("invalid erasure parameters (1<=k<=32, 1<=m<=16)")


def encode(obj: bytes, k: int, m: int) -> list[bytes]:
    import zfec

    validate(k, m)
    shard_len = max(1, -(-len(obj) // k))
    padded = obj.ljust(shard_len * k, b"\0")
    blocks = [padded[i * shard_len : (i + 1) * shard_len] for i in range(k)]
    return [bytes(b) for b in zfec.Encoder(k, k + m).encode(blocks)]


def decode(shards: dict[int, bytes], k: int, m: int, size: int) -> bytes:
    import zfec

    validate(k, m)
    if len(shards) < k:
        raise InvalidRequest(f"need {k} shards, have {len(shards)}")
    idx = tuple(sorted(shards)[:k])
    blocks = tuple(shards[i] for i in idx)
    primary = zfec.Decoder(k, k + m).decode(blocks, idx)
    return b"".join(bytes(b) for b in primary)[:size]
