"""
shared/score_batch.py — score one batch of candidate seeds against the
current corpus, using the metric definition agreed for this study:

  PRIMARY metric   — total distinct new edges covered by the whole batch
                     (union of each candidate's new-edge set). This is the
                     number that goes in the results table for comparing
                     arms: it doesn't care how many seeds contributed, only
                     how much real coverage the batch bought you.

  SECONDARY metric — count of "non-redundant contributing seeds": process
                     candidates in generation order, and count a candidate
                     as a distinct contributor only if it adds at least one
                     edge not already claimed by an earlier candidate in the
                     SAME batch. Two seeds that both find only the same one
                     new edge count as ONE contributor, not two — this is
                     the "seed A and B produce the same new coverage, that's
                     only 'one' good seed" rule from the conversation. Use
                     this as a diagnostic for redundancy WITHIN a batch (e.g.
                     "does strategy rotation reduce redundancy"), not as the
                     headline comparison number.

TODO before this module is usable: get_seed_edges() below is a stub. Wire it
to whatever you already have (mentioned as "somewhere in other files...
through some afl function") that returns the set of edge IDs a single seed
hits when run against the target binary — almost certainly a thin wrapper
around `afl-showmap -o - -- <target> <seed>`, or your existing
evaluate_seeds.py already does most of this internally and may just need its
per-seed edge-ID computation exposed as its own function rather than
returning only an aggregate. Check there first before writing a new one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


def get_seed_edges(seed_path: Path, target_binary: str, target_args: list[str]) -> set[int]:
    """TODO: return the set of edge IDs this seed hits when run against
    target_binary. Wire this to your existing per-seed edge-coverage tool
    (see module docstring) rather than reimplementing afl-showmap parsing
    from scratch — you've already solved this problem once."""
    raise NotImplementedError(
        "Wire get_seed_edges to your existing afl-showmap-based tool "
        "(see score_batch.py module docstring) before running any stage."
    )


@dataclass
class BatchScore:
    arm_name: str
    trigger_id: str
    n_candidates: int
    n_parsed: int              # how many fenced blocks parse_seeds actually recovered
    new_edges_total: int       # PRIMARY metric — union of new edges across the batch
    n_distinct_contributors: int  # SECONDARY metric — greedy non-redundant count
    per_seed_new_edge_counts: list[int] = field(default_factory=list)  # raw, for inspection


def score_batch(
    seed_paths: list[Path],
    baseline_edges: set[int],
    target_binary: str,
    target_args: list[str],
    arm_name: str,
    trigger_id: str,
) -> BatchScore:
    """Score one generated batch against a fixed baseline edge set (the
    corpus's cumulative coverage BEFORE this batch was generated — capture
    this once per trigger event, before calling any arm, so every arm in
    that trigger is compared against the identical baseline).
    """
    claimed: set[int] = set()
    per_seed_counts = []
    n_distinct_contributors = 0

    for seed_path in seed_paths:
        edges = get_seed_edges(seed_path, target_binary, target_args)
        new_edges = edges - baseline_edges
        per_seed_counts.append(len(new_edges))

        # SECONDARY metric: does this seed add anything not already claimed
        # by an earlier seed in this same batch?
        genuinely_new = new_edges - claimed
        if genuinely_new:
            n_distinct_contributors += 1
        claimed |= new_edges

    return BatchScore(
        arm_name=arm_name,
        trigger_id=trigger_id,
        n_candidates=len(seed_paths),
        n_parsed=len(seed_paths),
        new_edges_total=len(claimed),          # PRIMARY metric
        n_distinct_contributors=n_distinct_contributors,
        per_seed_new_edge_counts=per_seed_counts,
    )


if __name__ == "__main__":
    # Self-test with a fake get_seed_edges, so this file's own logic can be
    # verified without your real afl-showmap tool wired in yet.
    import unittest.mock as mock

    fake_edges = {
        "seed_a": {1, 2, 3},        # 2 new edges (2, 3) vs baseline {1}
        "seed_b": {1, 2, 3},        # SAME 2 new edges — should NOT count as a 2nd contributor
        "seed_c": {1, 4},           # 1 new edge (4) — should count as a distinct contributor
    }
    with mock.patch(
        "__main__.get_seed_edges",
        side_effect=lambda path, *_: fake_edges[path.stem],
    ):
        paths = [Path(f"/tmp/{k}") for k in fake_edges]
        score = score_batch(paths, baseline_edges={1}, target_binary="x",
                             target_args=[], arm_name="test", trigger_id="t0")
        print(score)
        assert score.new_edges_total == 3          # {2, 3, 4}
        assert score.n_distinct_contributors == 2  # seed_a (or b) + seed_c, not all three
        print("SELF-TEST PASSED")
