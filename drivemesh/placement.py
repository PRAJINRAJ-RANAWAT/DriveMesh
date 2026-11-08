"""Replica placement: weighted rendezvous (highest-random-weight) hashing.

For chunk key k and account a with weight w (free bytes):
    u = H(k, a) mapped to (0, 1);  score = -w / ln(u)
Highest score wins. This is the weighted HRW of Schindelhauer & Schomaker: an account's
probability of being the top pick is proportional to its weight, and removing an account only
moves keys whose top pick was that account (minimal disruption).

`rank` returns *all* eligible accounts in preference order; callers take the first RF and use
the rest as fallbacks. The order puts the best account of each provider first (provider
diversity, the analog of HDFS rack awareness).
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

PLACEABLE_STATES = ("active",)
DEFAULT_UNKNOWN_WEIGHT = 1 << 30  # weight for accounts whose free space is not yet known


@dataclass(frozen=True)
class Candidate:
    id: str
    provider: str
    free_bytes: int | None
    state: str = "active"


def _unit_hash(key: str, account_id: str) -> float:
    h = hashlib.blake2b(f"{key}|{account_id}".encode(), digest_size=8).digest()
    return (int.from_bytes(h, "big") + 0.5) / 2**64  # strictly inside (0, 1)


def weight(c: Candidate, reserve: int) -> float:
    if c.free_bytes is None:
        return float(DEFAULT_UNKNOWN_WEIGHT)
    return float(max(c.free_bytes - reserve, 0))


def score(key: str, c: Candidate, reserve: int = 0) -> float:
    w = weight(c, reserve)
    if w <= 0:
        return float("-inf")
    return -w / math.log(_unit_hash(key, c.id))


def rank(
    key: str,
    candidates: Iterable[Candidate],
    *,
    exclude: Iterable[str] = (),
    reserve: int = 0,
    min_free: int = 0,
    diversify: bool = True,
) -> list[Candidate]:
    excluded = set(exclude)
    eligible = [
        c
        for c in candidates
        if c.state in PLACEABLE_STATES
        and c.id not in excluded
        and (c.free_bytes is None or c.free_bytes - reserve >= min_free)
        and weight(c, reserve) > 0
    ]
    ordered = sorted(eligible, key=lambda c: (score(key, c, reserve), c.id), reverse=True)
    if not diversify:
        return ordered
    first_per_provider: list[Candidate] = []
    seen: set[str] = set()
    for c in ordered:
        if c.provider not in seen:
            seen.add(c.provider)
            first_per_provider.append(c)
    rest = [c for c in ordered if c not in first_per_provider]
    return first_per_provider + rest


def load_aware(
    ranked: Sequence[Candidate], need: int, load: Callable[[str], int], window: int = 2
) -> list[Candidate]:
    """'Power of two choices' on top of HRW: among the best `window*need` candidates, prefer
    the least-loaded ones. Long-run balance still follows HRW weights (only top candidates
    are eligible), while short-run hot spots are avoided. Never reduces provider diversity.
    """
    pool = list(ranked[: window * need])
    if len(pool) <= need:
        return list(ranked)
    order = {c.id: i for i, c in enumerate(pool)}
    by_load = sorted(pool, key=lambda c: (load(c.id), order[c.id]))
    if len({c.provider for c in by_load[:need]}) < len({c.provider for c in pool[:need]}):
        return list(ranked)
    return by_load + list(ranked[window * need :])


def choose(key: str, candidates: Sequence[Candidate], n: int, **kwargs) -> list[Candidate]:
    return rank(key, candidates, **kwargs)[:n]
