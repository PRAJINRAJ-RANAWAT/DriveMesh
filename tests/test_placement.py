from collections import Counter

from drivemesh.placement import Candidate, choose, rank

GiB = 1 << 30


def accts(n, provider="gdrive", free=10 * GiB):
    return [Candidate(f"a{i}", provider, free) for i in range(n)]


def test_deterministic_and_distinct():
    cands = accts(6)
    a = choose("chunk-1", cands, 3)
    assert a == choose("chunk-1", cands, 3)
    assert len({c.id for c in a}) == 3


def test_excludes_ineligible():
    cands = accts(3) + [
        Candidate("dead", "gdrive", 10 * GiB, "unreachable"),
        Candidate("full", "gdrive", 0),
    ]
    ids = {c.id for c in rank("k", cands, exclude={"a0"}, min_free=1)}
    assert ids == {"a1", "a2"}


def test_provider_diversity_first():
    cands = accts(5, "gdrive") + [Candidate("s3-1", "s3", 1 * GiB)]
    for i in range(50):
        picked = choose(f"chunk-{i}", cands, 2)
        assert {c.provider for c in picked} == {"gdrive", "s3"}


def test_weighting_is_proportional_to_free_space():
    cands = [Candidate("big", "x", 30 * GiB), Candidate("small", "x", 10 * GiB)]
    wins = Counter(rank(f"k{i}", cands, diversify=False)[0].id for i in range(20000))
    share = wins["big"] / 20000
    assert 0.72 < share < 0.78  # expected 0.75


def test_minimal_disruption_on_removal():
    cands = accts(10)
    before = {f"k{i}": rank(f"k{i}", cands)[0].id for i in range(2000)}
    remaining = [c for c in cands if c.id != "a3"]
    moved = sum(1 for k, top in before.items() if rank(k, remaining)[0].id != top)
    owned_by_a3 = sum(1 for top in before.values() if top == "a3")
    assert moved == owned_by_a3  # only keys owned by the removed account move


def test_unknown_free_space_is_still_placeable():
    cands = [Candidate("new", "x", None), Candidate("old", "x", GiB)]
    assert {c.id for c in rank("k", cands)} == {"new", "old"}
