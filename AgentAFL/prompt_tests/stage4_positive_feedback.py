"""
stage4_positive_feedback.py — positive feedback: replace the good-seed
examples with up to n_seeds of THIS batch's own past seeds that already
found new coverage, instead of AFL-queue-sourced good seeds. The mirror
image of build_context.py's existing negative feedback (scan_cycle_history /
select_failure_examples, which echoes the LLM's own past FAILED seeds back
as "avoid" examples) — this echoes its own past SUCCESSFUL seeds back as
"here's what's been working" examples.

Selection rule (build_context.scan_cycle_history_positive's output, most
recent cycle last): most-recent-first; each next pick must be edge-Jaccard
diverse (<=jaccard_max) from EVERY already-chosen pick, not just the
previous one — generalizes past exactly 2 picks to any --n-seeds.

Top-up: early in a run (or if the diversity requirement can't be met), fewer
than n_seeds qualifying past-AI seeds may exist. The shortfall is filled
from `topup_selector` — whichever --seed-selector is configured for the run
(run_build_context.py resolves this; positive feedback's own implicit
default is "coverage" when --seed-selector isn't explicitly passed) — with
each candidate additionally required to be edge-Jaccard diverse from the
already-chosen past-AI seed(s) where possible. If nothing in the requested
buffer qualifies, settles for the topup selector's own best-ranked pick
anyway rather than leaving a good-seed slot empty.

make_positive_feedback_selector(...) is built FRESH INSIDE run_build_context.
py's prompt_fn, once per call — unlike the make_*_selector factories in
select_seeds_by_coverage.py, which are built once in main() and closed over
by every call. The difference: this selector needs history_dir, itself a
per-call value (None on call 0 — no history exists yet; batch_harness's real
_history/ dir from call 1 on, per BatchConfig.uses_history's own semantics).
Baking a fixed path at construction time the way the other factories bake
target_binary would silently diverge from that per-call None-on-call-0
behavior. Building the outer closure is free (no I/O), so rebuilding it
every call costs nothing.

Reuses build_context._jaccard/scan_cycle_history_positive and
select_seeds_by_coverage's private edge-extraction/logging helpers directly
(_make_edges_for, _edge_jaccard, _log_fallback) — the same
leading-underscore cross-module reuse already established by
stage2_neg_hypothesis.py (bc._cluster_failures, bc._jaccard, etc.).

Logging: _log_fallback (imported) records only the degraded case (settled
for an overlapping top-up). Every call, degraded or not, also gets one JSON
line appended to <out_dir>/positive_feedback_log.jsonl via
_log_positive_tier — the audit trail for which calls were "pure AI seed"
vs. "topped up", since assemble_prompt discards each pick's `reason` string
before it ever reaches the prompt or any other persisted file (a run's
positive-feedback arm is a moving blend especially early on: call 0 is
always 100% top-up, call 1 is top-up-heavy unless call 0 alone produced >=2
GOOD seeds — this log is what makes a later "pure-AI-calls-only" analysis
cut possible instead of only ever seeing the whole run's blended average).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import build_context as bc
from select_seeds_by_coverage import _make_edges_for, _edge_jaccard, _log_fallback


def select_positive_examples(positives: list, n: int, jaccard_max: float = 0.7) -> list:
    """positives: build_context.scan_cycle_history_positive's output (newest
    cycle last). Picks up to n, most-recent-first; each next pick must be
    edge-Jaccard diverse (<=jaccard_max) from EVERY already-chosen pick, not
    just the immediately preceding one — e.g. 3 candidates where the most
    recent two are too similar: picks the most recent, skips the second,
    picks the third. Returns fewer than n if the pool doesn't have enough
    diversity; the caller tops up the shortfall from elsewhere."""
    if n <= 0 or not positives:
        return []
    ranked = sorted(positives, key=lambda p: p["cycle_id"], reverse=True)
    chosen = [ranked[0]]
    for cand in ranked[1:]:
        if len(chosen) >= n:
            break
        if all(bc._jaccard(cand["edge_ids"], c["edge_ids"]) <= jaccard_max for c in chosen):
            chosen.append(cand)
    return chosen


def _log_positive_tier(log_path, call_ctx, *, n_ai_available: int, n_ai_used: int,
                       n_topup_used: int, topup_overlapped: bool) -> None:
    """One JSON line per call to <out_dir>/positive_feedback_log.jsonl,
    unconditionally (not just on a degraded top-up) — see module docstring
    for why this, not the discarded `reason` strings, is the audit trail.
    log_path=None skips the write entirely (no stderr print here; that's
    _log_fallback's job for the degraded case only)."""
    if log_path is None:
        return
    call = (call_ctx or {}).get("call")
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "call": call,
        "n_ai_available": n_ai_available,
        "n_ai_used": n_ai_used,
        "n_topup_used": n_topup_used,
        "topup_overlapped": topup_overlapped,
    }
    log_path = Path(log_path).with_name("positive_feedback_log.jsonl")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a") as f:
        f.write(json.dumps(record) + "\n")


def make_positive_feedback_selector(
    history_dir, topup_selector, *,
    target_binary: str, target_args=None,
    afl_showmap_bin: str = "afl-showmap", showmap_timeout: int = 30,
    jaccard_max: float = 0.7, topup_buffer: int = 3,
    edge_cache: dict | None = None, log_path=None, call_ctx=None,
):
    """Build a seed_selector(candidates, n_seeds) -> [(path, reason), ...]
    closure for assemble_prompt — see module docstring for the full design
    and why this must be built fresh per call, not once like
    select_seeds_by_coverage's make_*_selector factories.

      history_dir   : this call's _history dir, or None (call 0 — no
                      history yet). Matches run_build_context.py's prompt_fn
                      parameter of the same name exactly.
      topup_selector: an already-built (candidates, n_seeds) -> [(path,
                      reason), ...] closure — whichever --seed-selector is
                      configured for the run. Used as a black box: this
                      module never inspects its internals, only re-derives
                      edge sets for whatever it returns.
      edge_cache    : MUST be the SAME dict object passed to topup_selector's
                      own factory call, or every top-up candidate gets
                      afl-showmap'd twice per call (once inside
                      topup_selector's own ranking, once here for the
                      Jaccard-vs-AI-seed check) — _make_edges_for's cache key
                      is content-hash-based, not selector-identity-based, so
                      a shared dict gives a real cross-path hit.
    """
    args = list(target_args) if target_args is not None else ["@@"]
    cache = edge_cache if edge_cache is not None else {}
    _edges_for = _make_edges_for(target_binary, args, afl_showmap_bin, showmap_timeout, cache)

    def positive_feedback_selector(candidates, n_seeds):
        positives = bc.scan_cycle_history_positive(history_dir) if history_dir is not None else []
        picked = select_positive_examples(positives, n_seeds, jaccard_max)
        chosen = [(p["path"], f"positive-ai (cycle {p['cycle_id']}, {len(p['edge_ids'])} edges)")
                  for p in picked]
        chosen_edges = [p["edge_ids"] for p in picked]

        shortfall = n_seeds - len(chosen)
        topup_used = 0
        topup_overlapped = False
        if shortfall > 0:
            topup_candidates = topup_selector(candidates, shortfall + topup_buffer)

            # First pass: only accept top-up candidates diverse enough from
            # every already-chosen past-AI seed.
            for path, reason in topup_candidates:
                if topup_used >= shortfall:
                    break
                edges = _edges_for(path)
                if chosen_edges and any(_edge_jaccard(edges, e) > jaccard_max for e in chosen_edges):
                    continue
                chosen.append((path, f"positive-topup ({reason})"))
                chosen_edges.append(edges)
                topup_used += 1

            # Second pass: still short — settle for the topup selector's own
            # best-ranked leftovers rather than leave a slot empty.
            if topup_used < shortfall:
                topup_overlapped = True
                chosen_paths = {p for p, _ in chosen}
                for path, reason in topup_candidates:
                    if topup_used >= shortfall:
                        break
                    if path in chosen_paths:
                        continue
                    chosen.append((path, f"positive-topup-overlap ({reason})"))
                    chosen_paths.add(path)
                    topup_used += 1
                _log_fallback(
                    log_path, "positive-feedback",
                    f"only {len(picked)} AI seed(s) available and no sufficiently diverse "
                    f"top-up found — settled for overlap",
                    call_ctx=call_ctx,
                )

        _log_positive_tier(
            log_path, call_ctx,
            n_ai_available=len(positives), n_ai_used=len(picked),
            n_topup_used=topup_used, topup_overlapped=topup_overlapped,
        )
        return chosen[:n_seeds]

    return positive_feedback_selector


if __name__ == "__main__":
    # Self-test: no API key or afl-showmap needed (run_showmap mocked, cycles.jsonl
    # synthesized directly) — mirrors select_seeds_by_coverage.py's mocked-run_showmap
    # self-tests plus test_stage2_neg_hypothesis.py's synthetic-cycles.jsonl _make_run
    # helper, since this module needs both patterns at once.
    import hashlib
    import tempfile
    import unittest.mock as mock

    def _sig(edge_ids):
        joined = ",".join(str(e) for e in sorted(edge_ids))
        return hashlib.sha1(joined.encode()).hexdigest() if edge_ids else ""

    def _cand(idx, tag, status, new_edges, edge_ids):
        return {
            "candidate_idx": idx, "raw_path": f"candidates/{tag}/cand_{idx:02d}.raw",
            "seed_path": f"candidates/{tag}/cand_{idx:02d}.seed",
            "status": status, "new_edges": new_edges, "total_edges": len(edge_ids),
            "injected": True, "edge_sig": _sig(edge_ids), "edge_ids": sorted(edge_ids),
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
                (cdir / f"cand_{idx:02d}.seed").write_bytes(body.encode())
                recs.append(_cand(idx, tag, status, new_edges, edge_ids))
            lines.append(json.dumps({"record_type": "cycle", "cycle_id": cycle_id,
                                     "candidates": recs}))
        (run_dir / "cycles.jsonl").write_text("\n".join(lines) + "\n")
        return run_dir

    # Real on-disk files for every fake AFL-queue candidate the top-up selector
    # "returns" — _make_edges_for hashes actual bytes to key its cache, so a
    # nonexistent path silently resolves to an empty edge set (edges_for's own
    # OSError->set() fallback) *before* the mocked run_showmap is ever reached,
    # which would make every top-up candidate look spuriously "diverse". These
    # files' CONTENT never matters (only their existence + distinct bytes for a
    # distinct cache key) — the edges themselves come from FAKE_QUEUE_EDGES via
    # the mocked run_showmap below.
    _queue_dir_obj = tempfile.TemporaryDirectory()
    _queue_dir = _queue_dir_obj.name
    FAKE_QUEUE_EDGES = {
        "q_disjoint": {901, 902, 903},   # disjoint from every AI seed below
        "q_overlap": {1, 2, 3, 4},       # matches ai-seed-1's edges exactly
    }
    for _name in FAKE_QUEUE_EDGES:
        (Path(_queue_dir) / _name).write_bytes(_name.encode())

    def fake_topup_selector(n_returned_names):
        """A stand-in topup_selector — pretends to rank real (but content-
        irrelevant) AFL-queue files; their edges come from FAKE_QUEUE_EDGES via
        the mocked run_showmap below."""
        def selector(candidates, n_seeds):
            return [(Path(_queue_dir) / name, "fake-ranked") for name in n_returned_names[:n_seeds]]
        return selector

    def _mocked_run_showmap(_bin, _t, _a, path, timeout=30):
        name = Path(path).name
        if name in FAKE_QUEUE_EDGES:
            return "ok", FAKE_QUEUE_EDGES[name]
        return "ok", set()  # AI-history seed files: edges come from cycles.jsonl, not showmap

    # --- select_positive_examples: recency + Jaccard-diverse-from-all-chosen -----
    p1 = {"cycle_id": 3, "edge_ids": {1, 2, 3, 4, 5}, "path": Path("/c3")}    # most recent
    p2 = {"cycle_id": 2, "edge_ids": {1, 2, 3, 4}, "path": Path("/c2")}       # too similar to p1: 4/5=0.8 > 0.7
    p3 = {"cycle_id": 1, "edge_ids": {50, 51, 52}, "path": Path("/c1")}      # disjoint from p1

    picks = select_positive_examples([p1, p2, p3], 2, jaccard_max=0.7)
    assert [p["cycle_id"] for p in picks] == [3, 1], (
        f"expected most-recent (3) then the diverse one (1), skipping too-similar (2), got "
        f"{[p['cycle_id'] for p in picks]}"
    )
    print("select_positive_examples: skips too-similar, picks next diverse -> PASSED")

    picks_n1 = select_positive_examples([p1, p2, p3], 1)
    assert [p["cycle_id"] for p in picks_n1] == [3]
    picks_n3 = select_positive_examples([p1, p2, p3], 3, jaccard_max=0.9)  # loose enough to admit p2 too
    assert len(picks_n3) == 3, f"expected all 3 admitted at a loose threshold, got {len(picks_n3)}"
    print("select_positive_examples: generalizes to n=1 and n=3 -> PASSED")

    # --- make_positive_feedback_selector: 0 / 1 / 2+ AI seeds available ----------
    with tempfile.TemporaryDirectory() as td:
        run_dir = _make_run(td, [])  # empty history -> call-0-equivalent via history_dir=None below
        with mock.patch("select_seeds_by_coverage.run_showmap", side_effect=_mocked_run_showmap):
            sel = make_positive_feedback_selector(
                None, fake_topup_selector(["q_disjoint", "q_overlap"]),
                target_binary="dummy",
            )
            picks = sel([], 2)
            assert len(picks) == 2 and all("positive-topup" in r for _, r in picks), (
                f"call-0-equivalent (history_dir=None) should be 100% top-up, got {picks}"
            )
            print("0 AI seeds (history_dir=None): pure top-up -> PASSED")

        # 1 AI seed available: cycle 1 found edges {1,2,3} — top-up must avoid it.
        run_dir = _make_run(td, [(1, [("GOOD (novel coverage)", 3, {1, 2, 3}, "ai-seed-1")])])
        with mock.patch("select_seeds_by_coverage.run_showmap", side_effect=_mocked_run_showmap):
            sel = make_positive_feedback_selector(
                run_dir, fake_topup_selector(["q_disjoint"]), target_binary="dummy",
            )
            picks = sel([], 2)
            assert len(picks) == 2
            assert "positive-ai" in picks[0][1] and "cycle 1" in picks[0][1]
            assert "positive-topup" in picks[1][1] and "positive-topup-overlap" not in picks[1][1]
            print("1 AI seed: keeps it + diverse top-up -> PASSED")

        # 2 AI seeds, sufficiently diverse: no top-up needed at all — fake_topup_selector
        # would raise if called with an empty name list requested for 0 shortfall, so a
        # bug here would surface as a crash, not a silent wrong answer.
        run_dir = _make_run(td, [
            (1, [("GOOD (novel coverage)", 3, {1, 2, 3}, "ai-seed-1")]),
            (2, [("GOOD (novel coverage)", 3, {900, 901, 902}, "ai-seed-2")]),
        ])
        with mock.patch("select_seeds_by_coverage.run_showmap", side_effect=_mocked_run_showmap):
            sel = make_positive_feedback_selector(
                run_dir, fake_topup_selector([]), target_binary="dummy",
            )
            picks = sel([], 2)
            assert len(picks) == 2 and all("positive-ai" in r for _, r in picks)
            assert {"positive-ai (cycle 2, 3 edges)", "positive-ai (cycle 1, 3 edges)"} == {r for _, r in picks}
            print("2 diverse AI seeds: no top-up called -> PASSED")

        # All top-up candidates too similar to the 1 available AI seed -> settle for
        # overlap rather than an empty slot, and log the degraded case.
        run_dir = _make_run(td, [(1, [("GOOD (novel coverage)", 3, {1, 2, 3, 4}, "ai-seed-1")])])
        with tempfile.TemporaryDirectory() as td2:
            log_path = Path(td2) / "selector_fallback.log"
            with mock.patch("select_seeds_by_coverage.run_showmap", side_effect=_mocked_run_showmap):
                sel = make_positive_feedback_selector(
                    run_dir, fake_topup_selector(["q_overlap"]),  # {1,2,3,4} vs AI {1,2,3,4} -> jaccard 1.0
                    target_binary="dummy", log_path=log_path,
                )
                picks = sel([], 2)
                assert len(picks) == 2
                assert "positive-topup-overlap" in picks[1][1]
                assert log_path.is_file() and "settled for overlap" in log_path.read_text()
                tier_log = log_path.with_name("positive_feedback_log.jsonl")
                assert tier_log.is_file()
                rec = json.loads(tier_log.read_text().splitlines()[-1])
                assert rec["topup_overlapped"] is True and rec["n_ai_used"] == 1
                print("all top-up too similar: settles for overlap + logs degraded case -> PASSED")

    _queue_dir_obj.cleanup()
    print("\nALL stage4_positive_feedback TESTS PASSED")
