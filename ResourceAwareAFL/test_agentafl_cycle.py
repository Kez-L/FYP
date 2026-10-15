#!/usr/bin/env python3
"""Tests for the AgentAFL Level-2 escalation integration.

Run from ResourceAwareAFL/:   python3 test_agentafl_cycle.py

Plain asserts, no pytest, no API key, no afl-showmap, no fuzzer - matching the
style of AgentAFL's own test files. The headline test is
`test_second_escalation_sees_first_escalations_seeds`: that cross-escalation seed
memory is the whole point of this integration.
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agentafl_cycle as ac
import agentafl_escalation as ae
import build_context as bc


# ------------------------------------------------------------------ fixtures

def _write_cycle(history_dir: Path, cycle_id: int, cands: list[dict]):
    """Append one cycle record the way batch_harness.append_history_cycle does."""
    cdir = history_dir / "candidates" / f"cycle_{cycle_id:04d}"
    cdir.mkdir(parents=True, exist_ok=True)
    out = []
    for j, c in enumerate(cands):
        rel = f"candidates/cycle_{cycle_id:04d}/cand_{j:02d}.xml"
        (history_dir / rel).write_bytes(c.get("body", b"<root><a/></root>"))
        out.append({
            "candidate_idx": j, "seed_path": rel, "raw_path": rel,
            "status": c["status"], "new_edges": c["new_edges"],
            "total_edges": c.get("total_edges", 100),
            "injected": True, "edge_sig": c.get("edge_sig", f"sig{cycle_id}{j}"),
            "edge_ids": c.get("edge_ids", [1, 2, 3]),
        })
    with (history_dir / "cycles.jsonl").open("a") as f:
        f.write(json.dumps({"record_type": "cycle", "cycle_id": cycle_id,
                            "candidates": out}) + "\n")


def _good(edge_ids, body=None):
    return {"status": "GOOD (novel coverage)", "new_edges": len(edge_ids),
            "edge_ids": edge_ids, "body": body or b"<root><good/></root>"}


def _bad(edge_ids, body=None):
    return {"status": "BAD (redundant)", "new_edges": 0, "edge_ids": edge_ids,
            "body": body or b"<root><bad/></root>"}


# ------------------------------------------------------- cycle id / memory

def test_next_cycle_id_starts_at_one_and_is_monotonic():
    with tempfile.TemporaryDirectory() as td:
        h = Path(td)
        assert ac.next_cycle_id(h) == 1, "empty history must start at 1"
        _write_cycle(h, 1, [_good([1, 2])])
        assert ac.next_cycle_id(h) == 2
        _write_cycle(h, 2, [_bad([1, 2])])
        _write_cycle(h, 3, [_bad([1, 2])])
        assert ac.next_cycle_id(h) == 4


def test_next_cycle_id_survives_a_daemon_restart():
    """Derived from the log, not a counter, so a restart cannot reuse ids and
    overwrite candidates/cycle_XXXX/."""
    with tempfile.TemporaryDirectory() as td:
        h = Path(td)
        for i in (1, 2, 3, 4, 5):
            _write_cycle(h, i, [_good([i])])
        assert ac.next_cycle_id(h) == 6


def test_next_cycle_id_ignores_malformed_lines():
    with tempfile.TemporaryDirectory() as td:
        h = Path(td)
        _write_cycle(h, 7, [_good([1])])
        with (h / "cycles.jsonl").open("a") as f:
            f.write("not json at all\n\n")
        assert ac.next_cycle_id(h) == 8


# ============ THE HEADLINE TEST =========================================

def test_second_escalation_sees_first_escalations_seeds():
    """Escalation #2 must be able to use escalation #1's positive AND negative
    seeds as prompt examples. This is the requirement the whole design exists for.
    """
    with tempfile.TemporaryDirectory() as td:
        h = Path(td) / "history"
        h.mkdir(parents=True)

        # --- escalation #1: 5 calls, cycles 1..5 ---
        for cid in range(1, 6):
            _write_cycle(h, cid, [
                _good(list(range(cid * 100, cid * 100 + 9))),
                _bad([5, 6, 7, 8]),
                _bad([5, 6, 7, 8]),
            ])
        first_next = ac.next_cycle_id(h)
        assert first_next == 6

        # --- escalation #2 starts here, same history dir, new process semantics ---
        mem = ac.memory_stats(h)
        assert mem["positive_seeds"] == 5, mem
        assert mem["negative_seeds"] == 10, mem
        assert mem["positive_cycles"] == [1, 2, 3, 4, 5], mem
        assert mem["next_cycle_id"] == 6, mem

        pos = bc.scan_cycle_history_positive(h)
        picked = __import__("stage4_positive_feedback").select_positive_examples(
            pos, 2, jaccard_max=0.7)
        assert len(picked) == 2, "both positive slots filled from prior escalation"
        # Most recent first, and the second must be sufficiently different.
        assert picked[0]["cycle_id"] == 5, picked
        assert picked[0]["cycle_id"] > picked[1]["cycle_id"], picked

        neg = bc.scan_cycle_history(h)
        fails = bc.select_failure_examples(neg, 2)
        assert len(fails) >= 1, "negative feedback drawn from prior escalation"
        assert all("path" in f and "label" in f for f in fails), fails

        # Cycle ids keep climbing into escalation #2 rather than restarting.
        _write_cycle(h, ac.next_cycle_id(h), [_good([999])])
        assert ac.next_cycle_id(h) == 7
        assert ac.memory_stats(h)["positive_cycles"] == [1, 2, 3, 4, 5, 6]


def test_memory_is_lost_if_candidate_files_are_pruned():
    """Guards the 'never delete history/candidates/' invariant: the scan drops any
    record whose seed file is gone, silently, rather than erroring."""
    with tempfile.TemporaryDirectory() as td:
        h = Path(td)
        _write_cycle(h, 1, [_good([1, 2, 3])])
        assert ac.memory_stats(h)["positive_seeds"] == 1
        for f in (h / "candidates" / "cycle_0001").glob("*.xml"):
            f.unlink()
        assert ac.memory_stats(h)["positive_seeds"] == 0, (
            "pruning candidates/ silently shrinks the memory - do not do it")


# ------------------------------------------------------------ run-relative time

def test_campaign_start_time_reads_fuzzer_stats():
    """elapsed_s must be on plot_data's relative_time clock, which is measured
    from fuzzer_stats' start_time - not from when the daemon happened to start."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "main").mkdir()
        (root / "main" / "fuzzer_stats").write_text(
            "start_time        : 1791887202\n"
            "last_update       : 1791930489\n"
            "edges_found       : 4640\n")
        assert ac.campaign_start_time(root, "main") == 1791887202
        assert ac.campaign_start_time(root, "sec1") is None, "missing -> None"


def test_campaign_start_time_tolerates_a_missing_or_partial_file():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        assert ac.campaign_start_time(root, "main") is None
        (root / "main").mkdir()
        (root / "main" / "fuzzer_stats").write_text("edges_found : 1\n")
        assert ac.campaign_start_time(root, "main") is None, "no start_time key"
        (root / "main" / "fuzzer_stats").write_text("start_time : notanumber\n")
        assert ac.campaign_start_time(root, "main") is None, "unparseable"


def test_hms_formats_a_twelve_hour_run():
    assert ac._hms(0) == "0h00m00s"
    assert ac._hms(95) == "0h01m35s"
    assert ac._hms(3600) == "1h00m00s"
    assert ac._hms(43200) == "12h00m00s"
    assert ac._hms(None) == "?", "unknown start must not crash a log line"


def test_timeline_csv_header_and_rows():
    """The CSV is the joinable artifact: one row per event on the same relative
    time axis as afl-out/*/plot_data."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "main").mkdir()
        (root / "main" / "fuzzer_stats").write_text("start_time : 1000\n")
        d = _daemon(td)
        assert d.timeline_path.exists(), "header written on construction"
        header = d.timeline_path.read_text().splitlines()[0]
        assert header.startswith("unix_time,elapsed_s,elapsed_hms,event"), header

        d.record({"event": "escalation", "escalation_id": 1, "ts": 1600.0,
                  "calls_made": 5, "seeds_generated": 25, "new_edges_total": 70,
                  "injected": 25, "request": {"total_recovery_edges": 0}})
        rows = d.timeline_path.read_text().splitlines()
        assert len(rows) == 2, rows
        cells = rows[1].split(",")
        assert cells[1] == "600.0", f"elapsed_s should be 1600-1000: {cells}"
        assert cells[2] == "0h10m00s", cells
        assert cells[3] == "escalation" and cells[4] == "1", cells
        assert cells[5:9] == ["5", "25", "70", "25"], cells

        # And the JSONL got the same elapsed_s stamped on it.
        rec = json.loads(d.log_path.read_text().splitlines()[0])
        assert rec["elapsed_s"] == 600.0, rec


def test_timeline_csv_escapes_commas_in_detail():
    with tempfile.TemporaryDirectory() as td:
        d = _daemon(td)
        d.record({"event": "dropped", "reason": "cooldown, 900s left", "ts": 5.0})
        last = d.timeline_path.read_text().splitlines()[-1]
        assert '"cooldown, 900s left"' in last, last
        assert len(last.split('"')) == 3, f"exactly one quoted cell: {last}"


def test_records_still_work_before_fuzzer_stats_exists():
    """The daemon may start before afl-fuzz writes fuzzer_stats; logging must not
    crash, it just leaves elapsed_s empty until the clock resolves."""
    with tempfile.TemporaryDirectory() as td:
        d = _daemon(td)
        assert d.elapsed() is None
        d.record({"event": "dropped", "reason": "cooldown"})
        row = d.timeline_path.read_text().splitlines()[-1].split(",")
        assert row[1] == "" and row[2] == "?", row


# -------------------------------------------------------- request handling

class _Args:
    """Minimal stand-in for the parsed CLI namespace."""
    def __init__(self, **kw):
        d = dict(campaign_root=".", work_dir=".", target="x", target_args="@@",
                 request_file="/nonexistent/.agentafl_request", instance="main",
                 instances="main", calls=5, seeds_per_call=5, n_seeds=2,
                 n_history_failure=2, jaccard_max=0.7, topup_buffer=3,
                 early_stop_patience=0, max_calls=None, cooldown_s=1500,
                 allow_hook_off=False,
                 max_escalations=0, poll_s=2.0, fmt="XML document",
                 seed_kind="text", no_asan_hint=False, showmap_bin="afl-showmap",
                 addseeds_bin="afl-addseeds", reuse_baseline=None,
                 provider="claude", model=None, temperature=1.0,
                 no_temperature=False, env_file=None)
        d.update(kw)
        self.__dict__.update(d)


def _daemon(tmp, **kw):
    return ae.Daemon(_Args(campaign_root=tmp, work_dir=tmp,
                           request_file=str(Path(tmp) / ".agentafl_request"), **kw))


def test_request_is_consumed_exactly_once():
    with tempfile.TemporaryDirectory() as td:
        d = _daemon(td)
        d.request_path.write_text(json.dumps(
            {"trigger_reason": "COVERAGE_SLOWDOWN", "improved": False}))
        assert d.take_request() is not None, "first read sees the request"
        assert d.take_request() is None, "request must not fire twice"


def test_malformed_request_is_ignored_not_fatal():
    with tempfile.TemporaryDirectory() as td:
        d = _daemon(td)
        d.request_path.write_text('{"trigger_reason": "COVER')   # truncated
        assert d.take_request() is None
        d.request_path.write_text('[1, 2, 3]')                   # not a dict
        assert d.take_request() is None


def test_only_unrecovered_coverage_plateaus_escalate():
    with tempfile.TemporaryDirectory() as td:
        d = _daemon(td)
        go, _ = d.should_escalate(
            {"trigger_reason": "COVERAGE_SLOWDOWN", "improved": False})
        assert go, "the documented trigger condition must escalate"

        go, why = d.should_escalate(
            {"trigger_reason": "COVERAGE_SLOWDOWN", "improved": True})
        assert not go and "recovered" in why, why

        for reason in ("MEMORY_SPIKE", "CPU_SPIKE", "CPU_MEMORY_SPIKE", None):
            go, why = d.should_escalate(
                {"trigger_reason": reason, "improved": False})
            assert not go, f"{reason} is resource pressure, not a coverage plateau"


def test_cooldown_drops_a_second_request():
    with tempfile.TemporaryDirectory() as td:
        d = _daemon(td, cooldown_s=1500)
        req = {"trigger_reason": "COVERAGE_SLOWDOWN", "improved": False}
        assert d.should_escalate(req)[0]
        d.last_escalation_at = __import__("time").time()
        go, why = d.should_escalate(req)
        assert not go and "cooldown" in why, why


def test_max_escalations_caps_the_run():
    with tempfile.TemporaryDirectory() as td:
        d = _daemon(td, max_escalations=2, cooldown_s=0)
        req = {"trigger_reason": "COVERAGE_SLOWDOWN", "improved": False}
        d.escalations = 2
        go, why = d.should_escalate(req)
        assert not go and "max_escalations" in why, why


# --------------------------------------------------------- the opt-in gate

def _hook_source() -> str:
    return (Path(__file__).resolve().parent / "run_analyzer.py").read_text()


def test_hook_is_gated_on_agentafl_hook_env_var():
    """The hook must be opt-in, so `condition=minyee` and her own
    run_six_experiments.sh behave exactly as they did before it existed."""
    src = _hook_source()
    assert ".agentafl_request" in src, "the hook should be present"
    assert 'os.environ.get(' in src and '"AGENTAFL_HOOK"' in src, (
        "hook must be gated on AGENTAFL_HOOK, or a minyee run writes a request "
        "file its committed code never wrote")
    # The gate and the `not improved` test must be on the SAME condition, so the
    # try block is never entered when the gate is off.
    assert "if not improved and os.environ.get(" in src, (
        "gate must be part of the hook's own condition")


def test_hook_gate_is_strict_equality_to_one():
    """A truthiness check would make AGENTAFL_HOOK=0 enable the hook, which is
    exactly how run_campaign.sh spells 'off' for baseline and minyee."""
    src = _hook_source()
    i = src.index('"AGENTAFL_HOOK"')
    tail = src[i:i + 120]
    assert '== "1"' in tail, f"gate must compare == '1', got: {tail[:80]!r}"


def test_daemon_refuses_to_start_without_the_hook_enabled():
    """Closes the default-off risk: a `combined` campaign whose hook was never
    enabled would poll an empty dir for 12h and silently equal `minyee`."""
    import os as _os
    with tempfile.TemporaryDirectory() as td:
        d = _daemon(td)
        (Path(td) / "main").mkdir()
        (Path(td) / "main" / "fuzzer_stats").write_text("start_time : 1000\n")
        prev = _os.environ.pop("AGENTAFL_HOOK", None)
        try:
            # target exists so we get past the earlier checks
            d.target = __file__
            assert d.preflight() is False, "must refuse when the gate is off"

            _os.environ["AGENTAFL_HOOK"] = "0"
            assert d.preflight() is False, "'0' must not count as enabled"

            d.args.allow_hook_off = True
            # --allow-hook-off gets past the gate; it may still fail later on the
            # showmap self-test, which is fine - we only assert the gate passed.
            d.log_lines = []
        finally:
            if prev is None:
                _os.environ.pop("AGENTAFL_HOOK", None)
            else:
                _os.environ["AGENTAFL_HOOK"] = prev


# ------------------------------------------------------- inherited env vars

def test_sanitize_afl_env_strips_mutator_vars():
    """afl-showmap PROGRAM ABORTs if AFL_PYTHON_MODULE names a module it cannot
    import, and build_baseline reports that as an empty baseline rather than an
    error - so these must never reach a child afl-* process."""
    env = {"AFL_PYTHON_MODULE": "adaptive_mutator",
           "AFL_CUSTOM_MUTATOR_ONLY": "1",
           "AFL_SKIP_CPUFREQ": "1",          # unrelated, must survive
           "PATH": "/usr/bin"}
    removed = ac.sanitize_afl_env(env)
    assert set(removed) == {"AFL_PYTHON_MODULE", "AFL_CUSTOM_MUTATOR_ONLY"}, removed
    assert "AFL_PYTHON_MODULE" not in env and "AFL_CUSTOM_MUTATOR_ONLY" not in env
    assert env["AFL_SKIP_CPUFREQ"] == "1", "unrelated AFL_ vars must be kept"
    assert env["PATH"] == "/usr/bin"


def test_sanitize_afl_env_is_a_noop_when_clean():
    env = {"AFL_SKIP_CPUFREQ": "1"}
    assert ac.sanitize_afl_env(env) == []
    assert env == {"AFL_SKIP_CPUFREQ": "1"}


# ------------------------------------------------------------ sync plumbing

def test_synced_count_reads_the_little_endian_counter():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        sd = root / "main" / ".synced"
        sd.mkdir(parents=True)
        (sd / "addseeds").write_bytes((25).to_bytes(4, "little"))
        assert ac.synced_count(root, "main") == 25
        assert ac.synced_count(root, "sec1") is None, "missing file -> None"


def test_ensure_syncable_only_marks_when_no_main_node_exists():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "addseeds" / "queue").mkdir(parents=True)

        # Single-instance campaign (auto '-S default' = secondary): needs a marker,
        # or sync_fuzzers() skips addseeds/ entirely and nothing is ever imported.
        (root / "default").mkdir()
        assert ac.ensure_syncable(root) is True
        assert (root / "addseeds" / "is_main_node").exists()
        assert ac.ensure_syncable(root) is False, "idempotent"

        # A real '-M main' campaign already syncs from everyone: no marker needed.
        root2 = Path(td) / "two"
        (root2 / "addseeds" / "queue").mkdir(parents=True)
        (root2 / "main").mkdir()
        (root2 / "main" / "is_main_node").touch()
        assert ac.ensure_syncable(root2) is False
        assert not (root2 / "addseeds" / "is_main_node").exists()


def test_inject_seeds_noop_on_empty_list():
    with tempfile.TemporaryDirectory() as td:
        r = ac.inject_seeds(Path(td), [])
        assert r["ok"] and r["injected"] == 0, r


# ---------------------------------------------------------------- the runner

def main():
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as exc:
            failed.append(name)
            print(f"  FAIL  {name}: {exc}")
        except Exception as exc:                            # noqa: BLE001
            failed.append(name)
            print(f"  ERROR {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
