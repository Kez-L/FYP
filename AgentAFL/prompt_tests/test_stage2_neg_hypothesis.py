#!/usr/bin/env python3
"""Tests for stage2_neg_hypothesis.py's grouping logic and
build_context.py's history_examples_override hook.

Run: python3 test_stage2_neg_hypothesis.py   (plain asserts, no deps, no
API key / afl-showmap needed — mirrors test_build_context_history.py's
synthetic-cycles.jsonl pattern).
"""

import hashlib
import json
import shutil
import tempfile
from pathlib import Path

import build_context as bc
import stage2_neg_hypothesis as sh


def _sig(edge_ids):
    joined = ",".join(str(e) for e in sorted(edge_ids))
    return hashlib.sha1(joined.encode()).hexdigest() if edge_ids else ""


def _cand(idx, tag, status, new_edges, edge_ids):
    return {
        "candidate_idx": idx,
        "raw_path": f"candidates/{tag}/cand_{idx:02d}.raw",
        "seed_path": f"candidates/{tag}/cand_{idx:02d}.seed",
        "status": status,
        "new_edges": new_edges,
        "total_edges": len(edge_ids),
        "injected": True,
        "edge_sig": _sig(edge_ids),
        "edge_ids": sorted(edge_ids),
    }


def _make_run(tmp, cycles):
    """cycles: list of (cycle_id, [(status, new_edges, edge_ids, body), ...])."""
    run_dir = Path(tmp)
    lines = []
    for cycle_id, cands in cycles:
        tag = f"cycle_{cycle_id:04d}"
        cdir = run_dir / "candidates" / tag
        cdir.mkdir(parents=True, exist_ok=True)
        recs = []
        for idx, (status, new_edges, edge_ids, body) in enumerate(cands):
            (cdir / f"cand_{idx:02d}.seed").write_text(body)
            (cdir / f"cand_{idx:02d}.raw").write_text(body)
            recs.append(_cand(idx, tag, status, new_edges, edge_ids))
        lines.append(json.dumps({"record_type": "cycle", "cycle_id": cycle_id,
                                 "candidates": recs}))
    (run_dir / "cycles.jsonl").write_text("\n".join(lines) + "\n")
    return run_dir


def test_returns_empty_when_no_cluster_qualifies():
    tmp = tempfile.mkdtemp()
    try:
        # A single bad seed can never meet min_group_size=2 on its own --
        # nothing qualifies yet. This is the "early calls have no response"
        # fallback.
        run_dir = _make_run(tmp, [
            (1, [("BAD (redundant)", 0, set(range(8)), "a" * 100)]),
        ])
        failures = bc.scan_cycle_history(run_dir)
        groups = sh.select_negative_groups(failures, jaccard_min=0.3)
        assert groups == []
    finally:
        shutil.rmtree(tmp)


def test_returns_one_group_when_only_one_qualifies():
    tmp = tempfile.mkdtemp()
    try:
        # Real-world shape: a run's bad seeds are dominated by ONE big
        # recurring pattern (everything shares an overlapping edge
        # neighborhood -> single-link clustering chains it all into one
        # cluster) with nothing else big enough to form a second group.
        # Must still surface the one real group instead of going silent.
        run_dir = _make_run(tmp, [
            (1, [("BAD (redundant)", 0, set(range(0, 8)), "a" * 100)]),
            (2, [("BAD (redundant)", 0, set(range(0, 7)) | {50}, "b" * 100)]),
            (3, [("BAD (redundant)", 0, set(range(1, 8)) | {60}, "c" * 100)]),
        ])
        failures = bc.scan_cycle_history(run_dir)
        groups = sh.select_negative_groups(failures, jaccard_min=0.3)
        assert len(groups) == 1, groups
        assert len(groups[0]["members"]) == 3
    finally:
        shutil.rmtree(tmp)


def test_no_coverage_seeds_form_their_own_group():
    tmp = tempfile.mkdtemp()
    try:
        # NO_COVERAGE seeds have empty edge sets, so they can never
        # edge-cluster with each other, and these 3 bodies are deliberately
        # unrelated (no raw-byte similarity either) -- each would land as
        # its own singleton and never qualify on its own. "Produced no
        # coverage at all" is itself a real shared trait, so they should
        # still form one group together, alongside the one real edge
        # cluster.
        run_dir = _make_run(tmp, [
            (1, [("BAD (redundant)", 0, set(range(0, 8)), "a" * 100)]),
            (2, [("BAD (redundant)", 0, set(range(0, 7)) | {50}, "b" * 100)]),
            (3, [("NO_COVERAGE", 0, set(), "<<<totally-unrelated-garbage>>>")]),
            (4, [("NO_COVERAGE", 0, set(), "9384710293847abcxyz!!!")]),
            (5, [("NO_COVERAGE", 0, set(), "\x00\x01\x02binary-junk\xff\xfe")]),
        ])
        failures = bc.scan_cycle_history(run_dir)
        groups = sh.select_negative_groups(
            failures, max_groups=2, min_group_size=2, jaccard_min=0.3)
        assert len(groups) == 2, groups
        no_cov_group = next(g for g in groups if
                            all(m["status"] == "NO_COVERAGE" for m in g["members"]))
        assert len(no_cov_group["members"]) == 3
        assert "did not parse" in no_cov_group["label"]
    finally:
        shutil.rmtree(tmp)


def test_returns_two_largest_groups_largest_first():
    tmp = tempfile.mkdtemp()
    try:
        # Cluster A: 3 seeds loosely sharing an ~8-edge neighborhood.
        # Cluster B: 2 seeds loosely sharing a disjoint ~6-edge neighborhood.
        # Cycle 6: a lone NO_COVERAGE seed with unrelated content -> singleton,
        # filtered out by min_group_size.
        run_dir = _make_run(tmp, [
            (1, [("BAD (redundant)", 0, set(range(0, 8)), "a" * 100)]),
            (2, [("BAD (redundant)", 0, set(range(0, 7)) | {50}, "b" * 100)]),
            (3, [("BAD (redundant)", 0, set(range(1, 8)) | {60}, "c" * 100)]),
            (4, [("BAD (redundant)", 0, set(range(100, 106)), "d" * 100)]),
            (5, [("BAD (redundant)", 0, set(range(100, 105)) | {200}, "e" * 100)]),
            (6, [("NO_COVERAGE", 0, set(), "lonely-junk")]),
        ])
        failures = bc.scan_cycle_history(run_dir)
        groups = sh.select_negative_groups(
            failures, max_groups=2, min_group_size=2, jaccard_min=0.3)
        assert len(groups) == 2, groups
        assert len(groups[0]["members"]) == 3   # cluster A is bigger -> first
        assert len(groups[1]["members"]) == 2   # cluster B
        assert "3 of your past seeds" in groups[0]["label"]
        assert "2 of your past seeds" in groups[1]["label"]
    finally:
        shutil.rmtree(tmp)


def test_oversized_cluster_capped_to_most_representative():
    tmp = tempfile.mkdtemp()
    try:
        # 4 seeds share an identical 10-edge set (mutual jaccard 1.0); a 5th
        # ("outlier") only half-overlaps them (jaccard ~0.33 to the rest) —
        # clears jaccard_min so it joins the cluster, but is clearly the
        # least representative member and should be the one capped out.
        core = set(range(10))
        outlier_edges = set(range(5, 10)) | set(range(20, 25))
        run_dir = _make_run(tmp, [
            (1, [("BAD (redundant)", 0, core, "a" * 100)]),
            (2, [("BAD (redundant)", 0, core, "b" * 100)]),
            (3, [("BAD (redundant)", 0, core, "c" * 100)]),
            (4, [("BAD (redundant)", 0, core, "d" * 100)]),
            (5, [("BAD (redundant)", 0, outlier_edges, "outlier-body")]),
            # second, unrelated qualifying group so max_groups=2 is satisfiable
            (6, [("BAD (redundant)", 0, set(range(300, 310)), "x" * 100)]),
            (7, [("BAD (redundant)", 0, set(range(300, 310)), "y" * 100)]),
        ])
        failures = bc.scan_cycle_history(run_dir)
        groups = sh.select_negative_groups(
            failures, max_groups=2, max_per_group=4, min_group_size=2, jaccard_min=0.3)
        assert len(groups) == 2
        big = groups[0]
        assert len(big["members"]) == 4
        bodies = {m["path"].read_text() for m in big["members"]}
        assert "outlier-body" not in bodies
    finally:
        shutil.rmtree(tmp)


def test_render_group_no_truncation_at_default_for_realistic_seed_size():
    tmp = tempfile.mkdtemp()
    try:
        # Real AI-generated seeds run up to ~1200 bytes (measured across
        # two live runs) -- must NOT be truncated at the new default
        # (DEFAULT_GROUP_MEMBER_MAX_CHARS=2000). Regression guard for the
        # bug where a 400-char cap was silently hiding most of most seeds.
        body = "<seed>" + ("x" * 900) + "</seed>"
        path = Path(tmp) / "seed.xml"
        path.write_text(body)
        member = {"path": path, "cycle_id": 1}
        rendered = sh.render_group_for_hypothesis([member], "text")
        assert "[truncated" not in rendered
        assert body in rendered
    finally:
        shutil.rmtree(tmp)


def test_render_group_still_truncates_a_pathological_outlier():
    tmp = tempfile.mkdtemp()
    try:
        # The cap is a safety ceiling, not removed entirely -- a truly huge
        # seed should still get truncated, with the existing honest marker.
        body = "y" * 5000
        path = Path(tmp) / "seed.xml"
        path.write_text(body)
        member = {"path": path, "cycle_id": 1}
        rendered = sh.render_group_for_hypothesis([member], "text")
        assert "[truncated, 5000 chars total]" in rendered
    finally:
        shutil.rmtree(tmp)


class _FakeCaller:
    """Stands in for batch_harness.ProviderCaller: (system, user) -> object
    with a .text attribute."""

    def __init__(self, text):
        self.text = text
        self.last_system = None
        self.last_user = None

    def __call__(self, system, user):
        self.last_system = system
        self.last_user = user
        return type("FakeResult", (), {"text": self.text})()


def test_request_group_hypothesis_calls_caller_with_rendered_group():
    tmp = tempfile.mkdtemp()
    try:
        run_dir = _make_run(tmp, [
            (1, [("BAD (redundant)", 0, {1, 2, 3}, "<seed-one/>")]),
            (2, [("BAD (redundant)", 0, {1, 2, 3}, "<seed-two/>")]),
        ])
        failures = bc.scan_cycle_history(run_dir)
        caller = _FakeCaller("  because they all hit the same dead branch.  ")
        res = sh.request_group_hypothesis(failures, "text", "XML document", caller)
        assert res["text"] == "because they all hit the same dead branch."
        assert res["model"] is None  # _FakeCaller's result has no .model etc.
        assert "<seed-one/>" in caller.last_user
        assert "<seed-two/>" in caller.last_user
        assert caller.last_system == sh.build_neg_hypothesis_system("XML document")
        assert "XML document" in caller.last_system
    finally:
        shutil.rmtree(tmp)


def _base_campaign(tmp):
    root = Path(tmp) / "camp"
    (root / "main" / "queue").mkdir(parents=True)
    (root / "main" / "cmdline").write_text("/bin/xmllint\n--noout\n@@\n")
    return root


def test_history_examples_override_shows_only_supplied_pairs():
    tmp = tempfile.mkdtemp()
    try:
        root = _base_campaign(tmp)
        p = bc.assemble_prompt(
            root, "main", "XML document", 0, 5,
            history_examples_override=[("group A", "hypothesis one"),
                                       ("group B", "hypothesis two")],
        )
        assert "hypothesis one" in p["user"]
        assert "hypothesis two" in p["user"]
        assert "group A" in p["user"]
        assert "group B" in p["user"]

        p_empty = bc.assemble_prompt(
            root, "main", "XML document", 0, 5, history_examples_override=[],
        )
        assert "Tried before" not in p_empty["user"]
    finally:
        shutil.rmtree(tmp)


def test_default_behavior_unaffected_by_new_parameter():
    tmp = tempfile.mkdtemp()
    try:
        root = _base_campaign(tmp)
        a = bc.assemble_prompt(root, "main", "XML document", 0, 5)
        b = bc.assemble_prompt(root, "main", "XML document", 0, 5, history_examples_override=None)
        assert a == b
    finally:
        shutil.rmtree(tmp)


def test_cluster_failures_threshold_is_configurable():
    # jaccard(a, b) ~ 3/17 ~ 0.18 — under the default 0.9 (no merge), over a
    # loosened 0.15 (merges). Confirms Stage 1/2/3's call site (which never
    # passes jaccard_min) is unaffected by this new parameter.
    a = {"edge_sig": "sigA", "edge_ids": set(range(0, 10)), "status": "BAD (redundant)"}
    b = {"edge_sig": "sigB", "edge_ids": set(range(7, 17)), "status": "BAD (redundant)"}
    assert len(bc._cluster_failures([a, b])) == 2
    assert len(bc._cluster_failures([a, b], jaccard_min=0.15)) == 1


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
