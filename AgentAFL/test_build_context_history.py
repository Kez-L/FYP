#!/usr/bin/env python3
"""Tests for the prior-failed-seed feedback added to build_context.py:
scan_cycle_history / select_failure_examples / assemble_prompt(run_dir=...).

Run: python3 test_build_context_history.py   (plain asserts, no deps)
"""

import hashlib
import json
import shutil
import tempfile
from pathlib import Path

import build_context as bc


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


def test_scan_filters_and_parses():
    tmp = tempfile.mkdtemp()
    try:
        run_dir = _make_run(tmp, [
            (1, [
                ("GOOD (novel coverage)", 4, {1, 2, 3}, "<good/>"),
                ("BAD (redundant)", 0, {10, 11, 12}, "<bad1/>"),
            ]),
            (2, [
                ("NO_COVERAGE", 0, set(), "not-xml-at-all"),
                ("DRY_RUN_SKIPPED", 0, set(), "skip-me"),
                ("CRASH", 0, {99}, "<crash/>"),
            ]),
        ])
        failures = bc.scan_cycle_history(run_dir)
        statuses = sorted(f["status"] for f in failures)
        assert statuses == ["BAD (redundant)", "NO_COVERAGE"], statuses
        bad = next(f for f in failures if f["status"] == "BAD (redundant)")
        assert bad["edge_ids"] == {10, 11, 12}
        assert bad["cycle_id"] == 1
        # missing seed file -> dropped
        (run_dir / "candidates/cycle_0001/cand_01.seed").unlink()
        assert all(f["status"] != "BAD (redundant)" for f in bc.scan_cycle_history(run_dir))
    finally:
        shutil.rmtree(tmp)


def test_missing_log_is_empty():
    tmp = tempfile.mkdtemp()
    try:
        assert bc.scan_cycle_history(Path(tmp)) == []
    finally:
        shutil.rmtree(tmp)


def test_cluster_picks_smallest_of_biggest_and_a_no_coverage_outlier():
    tmp = tempfile.mkdtemp()
    try:
        # 3 seeds share an edge set (cluster A); the smallest is cand in cycle 3.
        run_dir = _make_run(tmp, [
            (1, [("BAD (redundant)", 0, {1, 2, 3, 4, 5}, "A" * 400)]),
            (2, [("BAD (redundant)", 0, {1, 2, 3, 4, 5}, "A" * 300)]),
            (3, [("BAD (redundant)", 0, {1, 2, 3, 4, 5}, "A" * 100),
                 ("NO_COVERAGE", 0, set(), "Z" * 50)]),
        ])
        failures = bc.scan_cycle_history(run_dir)
        picks = bc.select_failure_examples(failures, 2, exclude_samples=[])
        assert len(picks) == 2, picks
        assert "3 of your past seeds" in picks[0]["label"]
        assert "cycles 1,2,3" in picks[0]["label"]
        assert picks[0]["path"].read_text() == "A" * 100  # smallest
        assert "did not parse" in picks[1]["label"]
        assert picks[1]["path"].read_text() == "Z" * 50
    finally:
        shutil.rmtree(tmp)


def test_jaccard_merges_near_identical_edge_sets():
    tmp = tempfile.mkdtemp()
    try:
        # 20/21 shared -> Jaccard ~0.95 >= 0.9 -> one cluster of 2.
        big = set(range(20))
        run_dir = _make_run(tmp, [
            (1, [("BAD (redundant)", 0, big, "x" * 200)]),
            (2, [("BAD (redundant)", 0, big | {999}, "y" * 200)]),
        ])
        picks = bc.select_failure_examples(bc.scan_cycle_history(run_dir), 2, [])
        assert len(picks) == 1
        assert "2 of your past seeds" in picks[0]["label"]
    finally:
        shutil.rmtree(tmp)


def test_dominant_cluster_is_not_parsing():
    tmp = tempfile.mkdtemp()
    try:
        run_dir = _make_run(tmp, [
            (1, [("NO_COVERAGE", 0, set(), "junk-1")]),
            (2, [("NO_COVERAGE", 0, set(), "junk-2")]),
            (3, [("NO_COVERAGE", 0, set(), "junk-3"),
                 ("BAD (redundant)", 0, {7, 8, 9}, "<well-formed-but-stale/>")]),
        ])
        picks = bc.select_failure_examples(bc.scan_cycle_history(run_dir), 2, [])
        assert "did not parse" in picks[0]["label"]
        assert "3 of your past seeds" in picks[0]["label"]
        # slot 2 falls through to the other (redundant-path) cluster
        assert len(picks) == 2
        assert "0 new edges" in picks[1]["label"]
    finally:
        shutil.rmtree(tmp)


def test_single_cluster_gives_one_example():
    tmp = tempfile.mkdtemp()
    try:
        run_dir = _make_run(tmp, [
            (1, [("BAD (redundant)", 0, {1, 2, 3}, "a" * 200)]),
            (2, [("BAD (redundant)", 0, {1, 2, 3}, "a" * 150)]),
        ])
        picks = bc.select_failure_examples(bc.scan_cycle_history(run_dir), 2, [])
        assert len(picks) == 1
    finally:
        shutil.rmtree(tmp)


def test_n_zero_and_empty():
    assert bc.select_failure_examples([], 2, []) == []
    tmp = tempfile.mkdtemp()
    try:
        run_dir = _make_run(tmp, [(1, [("BAD (redundant)", 0, {1}, "a")])])
        assert bc.select_failure_examples(bc.scan_cycle_history(run_dir), 0, []) == []
    finally:
        shutil.rmtree(tmp)


def test_exclude_samples_suppresses_a_pick():
    tmp = tempfile.mkdtemp()
    try:
        body = "<duplicate-of-a-queue-example/>" + "p" * 300
        run_dir = _make_run(tmp, [
            (1, [("BAD (redundant)", 0, {1, 2, 3}, body)]),
        ])
        failures = bc.scan_cycle_history(run_dir)
        # exclude fingerprint == the seed's own fingerprint -> dropped
        excl = [bc.content_sample(failures[0]["path"])]
        assert bc.select_failure_examples(failures, 2, excl) == []
    finally:
        shutil.rmtree(tmp)


def _base_campaign(tmp):
    """Minimal on-disk campaign so assemble_prompt runs."""
    root = Path(tmp) / "camp"
    inst = root / "main"
    (inst / "queue").mkdir(parents=True)
    (inst / "fuzzer_stats").write_text(
        "edges_found : 100\nbitmap_cvg : 5%\ncorpus_count : 10\n"
        "saved_crashes : 0\nsaved_hangs : 0\n")
    (inst / "cmdline").write_text("/bin/xmllint\n--noout\n@@\n")
    return root


def test_assemble_prompt_unchanged_without_run_dir():
    tmp = tempfile.mkdtemp()
    try:
        root = _base_campaign(tmp)
        plog = Path(tmp) / "plateau_log.csv"
        plog.write_text("timestamp,best_edges,instances,total_crashes,total_hangs,plateau_secs\n"
                        "1000,100,1,0,0,7200\n")
        a = bc.assemble_prompt(root, "main", "XML document", 3, 5, plog)
        b = bc.assemble_prompt(root, "main", "XML document", 3, 5, plog,
                               run_dir=None, n_history_failure=2)
        assert a == b
        assert "YOUR OWN PRIOR SEEDS" not in a["user"]
    finally:
        shutil.rmtree(tmp)


def test_assemble_prompt_includes_section_with_history():
    tmp = tempfile.mkdtemp()
    try:
        root = _base_campaign(tmp)
        plog = Path(tmp) / "plateau_log.csv"
        plog.write_text("timestamp,best_edges,instances,total_crashes,total_hangs,plateau_secs\n"
                        "1000,100,1,0,0,7200\n")
        run_dir = Path(tmp) / "run"
        _make_run(run_dir, [
            (1, [("BAD (redundant)", 0, {1, 2, 3}, "<stale>" + "s" * 300 + "</stale>")]),
            (2, [("BAD (redundant)", 0, {1, 2, 3}, "<stale>" + "s" * 200 + "</stale>")]),
        ])
        p = bc.assemble_prompt(root, "main", "XML document", 3, 5, plog,
                               run_dir=run_dir, n_history_failure=2)
        assert "YOUR OWN PRIOR SEEDS THAT DID NOT HELP" in p["user"]
        assert "AVOID — 2 of your past seeds" in p["user"]

        p0 = bc.assemble_prompt(root, "main", "XML document", 3, 5, plog,
                                run_dir=run_dir, n_history_failure=0)
        assert "YOUR OWN PRIOR SEEDS" not in p0["user"]

        # budget: the added block stays small
        added = len(p["user"]) - len(p0["user"])
        assert added < 2000, added
    finally:
        shutil.rmtree(tmp)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
