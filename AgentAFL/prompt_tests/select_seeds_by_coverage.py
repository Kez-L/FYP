"""
stage2_seed_selection/select_seeds_by_coverage.py — three "seed_selector"
hooks for build_context.assemble_prompt(seed_selector=...), each with the
signature (candidates, n_seeds) -> [(path, reason), ...] that
build_context.py already expects (matches select_diverse_seeds's own
contract, so these are drop-in replacements).

  Arm A — high_coverage_selector : seeds with the MOST total edge coverage
  Arm B — rare_coverage_selector : seeds that hit the RARE edges (edges few
                                    other corpus seeds also hit) — this is
                                    the AFLFast-style framing, deliberately
                                    the opposite emphasis from Arm A; see
                                    PLAN.md for why both are worth testing
                                    rather than assuming one is obviously
                                    right
  Arm C — no_seed_selector       : returns [] — the "does providing ANY
                                    seed even help" control

TODO before Arms A/B are usable: get_edge_ids() below is a stub with the
exact same shape as shared/score_batch.py's get_seed_edges — wire both to
the SAME underlying tool (you mentioned already having one "somewhere in
other files, through some afl function"). Don't write two different
edge-extraction implementations; if they ever disagree, every downstream
number in this study becomes suspect.
"""

from __future__ import annotations

from pathlib import Path


def get_edge_ids(seed_path: Path) -> set[int]:
    """TODO: return the set of edge IDs this seed hits. Same job as
    shared/score_batch.py's get_seed_edges — wire both to your existing
    tool, ideally by having one of them import the other rather than
    duplicating the afl-showmap call."""
    raise NotImplementedError(
        "Wire get_edge_ids to your existing per-seed edge-coverage tool "
        "(see module docstring) before running Stage 2."
    )


def high_coverage_selector(candidates, n_seeds):
    """Arm A: rank candidates by total edge count, descending. candidates is
    scan_queue's output: [(path, printable_ratio, size), ...]."""
    scored = [(path, len(get_edge_ids(path))) for path, _ratio, _size in candidates]
    scored.sort(key=lambda t: t[1], reverse=True)
    return [(path, f"high-coverage ({n} edges)") for path, n in scored[:n_seeds]]


def rare_coverage_selector(candidates, n_seeds):
    """Arm B: rank candidates by how many CORPUS-PRIVATE edges they hit —
    edges that few or no other seeds in this candidate set also hit.

    IMPORTANT CAVEAT, worth reading before trusting this: this is one
    reasonable proxy for "rare coverage," not the only one, and it is NOT
    quite AFLFast's actual definition. AFLFast up-weights paths by how
    rarely they've been EXECUTED during fuzzing (a runtime frequency
    count) — this function instead uses how few OTHER SEEDS IN THIS STATIC
    CANDIDATE SET also happen to hit the same edge, which is a presence
    count, not an execution count. If your existing edge-coverage tool (see
    get_edge_ids TODO) can expose real execution-frequency data instead,
    prefer that — it's the closer match to what the cited literature means
    by "rare." Scoring below counts each seed's PRIVATE edges (edges hit by
    at most one seed in the candidate set), rather than averaging inverse
    frequency across all its edges — a seed with three fairly-uncommon
    edges and a seed with one truly-unique edge are treated as genuinely
    different quantities here, deliberately, rather than collapsed into one
    ambiguous average (see this file's own history in PLAN.md for why the
    averaged version was tried first and dropped)."""
    paths = [path for path, _ratio, _size in candidates]
    edge_sets = {path: get_edge_ids(path) for path in paths}

    freq = {}
    for edges in edge_sets.values():
        for e in edges:
            freq[e] = freq.get(e, 0) + 1

    def private_edge_count(edges: set[int]) -> int:
        return sum(1 for e in edges if freq[e] == 1)

    scored = [(path, private_edge_count(edge_sets[path])) for path in paths]
    scored.sort(key=lambda t: t[1], reverse=True)
    return [(path, f"rare-coverage ({n} private edges)") for path, n in scored[:n_seeds]]


def no_seed_selector(candidates, n_seeds):
    """Arm C: the control — never show a good-seed example at all,
    regardless of what's available. n_seeds is accepted (to match the
    seed_selector signature) but ignored."""
    return []


if __name__ == "__main__":
    # Self-test with a fake get_edge_ids, verifying this file's own selection
    # logic (not the real edge-extraction, which still needs wiring — see
    # the TODO above). Deliberately constructed so high- and rare-coverage
    # pick DIFFERENT seeds, to confirm they're actually measuring different
    # things rather than reinventing each other under a different name.
    import unittest.mock as mock

    fake_edges = {
        Path("/a"): set(range(1, 11)),        # 10 edges, only #10 is private
        Path("/b"): set(range(1, 10)),        # 9 edges, all shared with /a
        Path("/c"): {1, 2, 11, 12, 13},       # 5 edges, but 3 are private
    }
    candidates = [(p, 1.0, 100) for p in fake_edges]

    with mock.patch(__name__ + ".get_edge_ids", side_effect=lambda p: fake_edges[p]):
        hi = high_coverage_selector(candidates, 1)
        rare = rare_coverage_selector(candidates, 1)
        none = no_seed_selector(candidates, 2)
        print("high_coverage picks:", hi)
        print("rare_coverage picks:", rare)
        print("no_seed picks:", none)
        assert hi[0][0] == Path("/a"), "expected /a (10 edges) to win high-coverage"
        assert rare[0][0] == Path("/c"), "expected /c (3 private edges) to win rare-coverage"
        assert none == []
        print("ALL SELECTION TESTS PASSED — high- and rare-coverage genuinely disagree, as intended")
