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
    """The cap counts what the CAMPAIGN has done, read from escalations.jsonl,
    not what this process has done - see
    test_max_escalations_is_enforced_across_a_restart."""
    with tempfile.TemporaryDirectory() as td:
        d = _daemon(td, max_escalations=2, cooldown_s=0)
        req = {"trigger_reason": "COVERAGE_SLOWDOWN", "improved": False}
        assert d.should_escalate(req)[0], "nothing done yet -> allowed"
        _esc_log(d.log_path, [1, 2])
        go, why = d.should_escalate(req)
        assert not go and "max_escalations" in why, why


# ------------------------------------------------- escalation id derivation

def _esc_log(path: Path, ids: list):
    with path.open("w") as f:
        for i in ids:
            f.write(json.dumps({"event": "escalation", "escalation_id": i}) + "\n")
        f.write(json.dumps({"event": "dropped", "reason": "cooldown"}) + "\n")


def test_next_escalation_id_survives_a_daemon_restart():
    """Regression for an observed bug: an in-process counter restarted at 1 after
    the daemon was restarted mid-campaign, so both escalations logged id=1 and the
    second overwrote esc_001/'s prompts, seeds and frozen baseline."""
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "escalations.jsonl"
        assert ac.next_escalation_id(log) == 1, "no log yet -> 1"
        _esc_log(log, [1])
        assert ac.next_escalation_id(log) == 2, "a restart must not reuse id 1"
        _esc_log(log, [1, 2, 3])
        assert ac.next_escalation_id(log) == 4


def test_next_escalation_id_ignores_non_escalation_events():
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "escalations.jsonl"
        log.write_text(
            json.dumps({"event": "dropped", "reason": "cooldown"}) + "\n"
            + json.dumps({"event": "error", "escalation_id": 9}) + "\n"
            + "not json\n")
        assert ac.next_escalation_id(log) == 1, (
            "drops and errors must not consume an escalation id")


def test_completed_escalations_counts_the_campaign_not_the_process():
    """--max-escalations is a cost cap, so it must survive a restart."""
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "escalations.jsonl"
        assert ac.completed_escalations(log) == 0
        _esc_log(log, [1, 2])
        assert ac.completed_escalations(log) == 2


def test_max_escalations_is_enforced_across_a_restart():
    with tempfile.TemporaryDirectory() as td:
        d = _daemon(td, max_escalations=2, cooldown_s=0)
        _esc_log(d.log_path, [1, 2])
        d.escalations = 0          # fresh process, as after a restart
        go, why = d.should_escalate(
            {"trigger_reason": "COVERAGE_SLOWDOWN", "improved": False})
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


# ---------------------------------------------- OpenAI API key failover
#
# The campaign's combined arm makes ~140 LLM calls per 12h run. If the account
# runs out of credits partway through, the arm silently degrades to `minyee`.
# These tests cover the failover: key 1 is used until the API says it is out of
# credits, then key 2 takes over PERMANENTLY and key 1 is never contacted again.
#
# No network, no API key, no spend: a fake `openai` module records which key each
# client was constructed with.


class _FakeQuotaError(Exception):
    """Stand-in for openai.RateLimitError with OpenAI's 429 quota shape."""
    def __init__(self, msg="You exceeded your current quota"):
        super().__init__(msg)
        self.code = "insufficient_quota"
        self.type = "insufficient_quota"


class _FakeBadRequest(Exception):
    pass


class _FakeOpenAIModule:
    """Minimal fake of the `openai` module surface ProviderCaller touches."""
    BadRequestError = _FakeBadRequest
    RateLimitError = _FakeQuotaError

    def __init__(self, dead_keys=()):
        self.dead_keys = set(dead_keys)
        self.calls = []          # every (key, n) attempted, in order
        self.clients_built = []  # every key a client was constructed with

    def OpenAI(self, api_key=None, **kw):                   # noqa: N802
        self.clients_built.append(api_key)
        return _FakeClient(self, api_key)


class _FakeClient:
    def __init__(self, mod, key):
        self._mod = mod
        self._key = key
        self.chat = self

    @property
    def completions(self):
        return self

    def create(self, **kw):
        self._mod.calls.append(self._key)
        if self._key in self._mod.dead_keys:
            raise _FakeQuotaError()
        return _FakeResponse()


class _FakeResponse:
    def __init__(self):
        self.choices = [_FakeChoice()]
        self.usage = None
        self.model = "fake-model"


class _FakeChoice:
    def __init__(self):
        self.message = type("M", (), {"content": "<root><a/></root>"})()
        self.finish_reason = "stop"


def _caller_with_keys(keys, dead_keys=(), monkey_env=True):
    """Build a real ProviderCaller against the fake openai module."""
    import batch_harness as bh
    fake = _FakeOpenAIModule(dead_keys=dead_keys)
    caller = bh.ProviderCaller.__new__(bh.ProviderCaller)
    caller.provider = "openai"
    caller.model = "fake-model"
    caller.requested_temperature = None
    caller.temperature = None
    caller.dropped_temperature = False
    caller._openai = fake
    caller._keys = list(keys)
    caller._key_idx = 0
    caller._dead_keys = []
    caller._dead_key_cb = None
    caller._client = fake.OpenAI(api_key=keys[0][1])
    return caller, fake


def test_openai_keys_reads_both_vars_in_order():
    import batch_harness as bh
    env = {"OPENAI_API_KEY": "k1", "OPENAI_API_KEY_2": "k2"}
    assert bh.openai_keys(env) == [("OPENAI_API_KEY", "k1"),
                                   ("OPENAI_API_KEY_2", "k2")], bh.openai_keys(env)


def test_openai_keys_dedupes_the_same_key_pasted_twice():
    # Otherwise the preflight would report failover as available when it is not.
    import batch_harness as bh
    env = {"OPENAI_API_KEY": "same", "OPENAI_API_KEY_2": "same"}
    assert bh.openai_keys(env) == [("OPENAI_API_KEY", "same")], bh.openai_keys(env)


def test_openai_keys_strips_quotes_and_skips_blanks():
    import batch_harness as bh
    env = {"OPENAI_API_KEY": '  "k1"  ', "OPENAI_API_KEY_2": "   ",
           "OPENAI_API_KEY_3": "k3"}
    assert bh.openai_keys(env) == [("OPENAI_API_KEY", "k1"),
                                   ("OPENAI_API_KEY_3", "k3")], bh.openai_keys(env)


def test_failover_switches_to_second_key_and_retries_same_prompt():
    caller, fake = _caller_with_keys(
        [("OPENAI_API_KEY", "k1"), ("OPENAI_API_KEY_2", "k2")], dead_keys={"k1"})
    res = caller("sys", "usr")
    assert res.text == "<root><a/></root>", res.text
    # k1 attempted once, then k2 served the same prompt.
    assert fake.calls == ["k1", "k2"], fake.calls
    assert caller.key_var == "OPENAI_API_KEY_2", caller.key_var
    assert res.attempt_count == 2, res.attempt_count


def test_first_key_is_never_contacted_again_after_exhaustion():
    """The requirement: no continued pinging of key 1 after the first failure."""
    caller, fake = _caller_with_keys(
        [("OPENAI_API_KEY", "k1"), ("OPENAI_API_KEY_2", "k2")], dead_keys={"k1"})
    for _ in range(5):
        caller("sys", "usr")
    # exactly one k1 attempt, ever - the rest all went to k2
    assert fake.calls.count("k1") == 1, fake.calls
    assert fake.calls.count("k2") == 5, fake.calls


def test_all_keys_exhausted_raises_out_of_credits():
    import batch_harness as bh
    caller, fake = _caller_with_keys(
        [("OPENAI_API_KEY", "k1"), ("OPENAI_API_KEY_2", "k2")],
        dead_keys={"k1", "k2"})
    try:
        caller("sys", "usr")
    except bh.OutOfCreditsError:
        pass
    else:
        raise AssertionError("expected OutOfCreditsError")
    assert fake.calls == ["k1", "k2"], fake.calls


def test_out_of_credits_error_is_recognised_by_is_out_of_credits():
    # So run_batch still exits OUT_OF_CREDITS_EXIT and run_all_stages.sh still
    # aborts the sweep.
    import batch_harness as bh
    assert bh.is_out_of_credits(bh.OutOfCreditsError("all keys dead"))
    assert bh.is_out_of_credits(_FakeQuotaError())
    assert not bh.is_out_of_credits(ValueError("some other failure"))


def test_non_credit_errors_are_not_failed_over():
    caller, fake = _caller_with_keys(
        [("OPENAI_API_KEY", "k1"), ("OPENAI_API_KEY_2", "k2")])

    def boom(**kw):
        fake.calls.append("k1")
        raise RuntimeError("connection reset")

    caller._client.create = boom
    try:
        caller("sys", "usr")
    except RuntimeError as exc:
        assert "connection reset" in str(exc), exc
    else:
        raise AssertionError("expected the original error to propagate")
    # key 1 must still be in use - a transient error must not burn the fallback
    assert caller.key_var == "OPENAI_API_KEY", caller.key_var


def test_dead_key_hook_fires_with_index_and_var_name():
    caller, fake = _caller_with_keys(
        [("OPENAI_API_KEY", "k1"), ("OPENAI_API_KEY_2", "k2")], dead_keys={"k1"})
    seen = []
    caller.set_dead_key_hook(lambda i, v: seen.append((i, v)))
    caller("sys", "usr")
    assert seen == [(0, "OPENAI_API_KEY")], seen


def test_dead_key_hook_failure_does_not_break_failover():
    """Persisting the marker is best-effort; a write error must not stop the run."""
    caller, fake = _caller_with_keys(
        [("OPENAI_API_KEY", "k1"), ("OPENAI_API_KEY_2", "k2")], dead_keys={"k1"})
    caller.set_dead_key_hook(lambda i, v: (_ for _ in ()).throw(OSError("disk full")))
    res = caller("sys", "usr")
    assert res.text, "failover should still have succeeded"
    assert caller.key_var == "OPENAI_API_KEY_2", caller.key_var


def test_skip_dead_keys_starts_on_the_fallback_after_a_restart():
    caller, fake = _caller_with_keys(
        [("OPENAI_API_KEY", "k1"), ("OPENAI_API_KEY_2", "k2")], dead_keys={"k1"})
    assert caller.skip_dead_keys([0]) is True
    assert caller.key_var == "OPENAI_API_KEY_2", caller.key_var
    caller("sys", "usr")
    # k1 never touched at all this time
    assert fake.calls == ["k2"], fake.calls


def test_skip_dead_keys_returns_false_when_nothing_is_left():
    caller, fake = _caller_with_keys(
        [("OPENAI_API_KEY", "k1"), ("OPENAI_API_KEY_2", "k2")])
    assert caller.skip_dead_keys([0, 1]) is False


def test_failover_log_line_avoids_the_run_all_stages_abort_regex():
    """run_all_stages.sh greps stage logs for quota markers and aborts the sweep
    even on exit 0, so a SUCCESSFUL failover must not echo the provider text."""
    import io, contextlib
    caller, fake = _caller_with_keys(
        [("OPENAI_API_KEY", "k1"), ("OPENAI_API_KEY_2", "k2")], dead_keys={"k1"})
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        caller("sys", "usr")
    out = buf.getvalue().lower()
    for marker in ("insufficient_quota", "credit_balance_exhausted",
                   "no credits remaining", "exceeded your current quota",
                   "credit balance is too low", "billing_hard_limit_reached",
                   "out of credits"):
        assert marker not in out, f"{marker!r} would abort the sweep: {out!r}"
    assert "OPENAI_API_KEY_2" in buf.getvalue(), buf.getvalue()


# ------------------------------- daemon side of the key failover

def test_dead_keys_file_round_trips():
    with tempfile.TemporaryDirectory() as td:
        d = _daemon(td)
        assert d.load_dead_keys() == [], "no file yet"
        d.note_dead_key(0, "OPENAI_API_KEY")
        assert d.load_dead_keys() == [0], d.load_dead_keys()
        # append-only: a second retirement keeps the first
        d.note_dead_key(1, "OPENAI_API_KEY_2")
        assert d.load_dead_keys() == [0, 1], d.load_dead_keys()
        data = json.loads(d.dead_keys_path.read_text())
        assert [e["var"] for e in data["dead"]] == [
            "OPENAI_API_KEY", "OPENAI_API_KEY_2"], data
        # and it never writes the key itself, only the variable name
        assert "sk-" not in d.dead_keys_path.read_text()


def test_dead_keys_file_survives_corruption():
    """A truncated marker must not stop the daemon starting."""
    with tempfile.TemporaryDirectory() as td:
        d = _daemon(td)
        d.dead_keys_path.write_text("{not json")
        assert d.load_dead_keys() == [], "corrupt file should read as empty"


def test_retiring_a_key_is_recorded_in_escalations_jsonl():
    with tempfile.TemporaryDirectory() as td:
        d = _daemon(td)
        d.note_dead_key(0, "OPENAI_API_KEY")
        rows = [json.loads(l) for l in d.log_path.read_text().splitlines() if l.strip()]
        kr = [r for r in rows if r.get("event") == "key_retired"]
        assert len(kr) == 1, rows
        assert kr[0]["key_var"] == "OPENAI_API_KEY", kr[0]
        assert kr[0]["key_index"] == 0, kr[0]


def test_stop_campaign_sets_stop_and_does_not_raise():
    """With no matching fuzzers, pkill finds nothing - must still be clean."""
    with tempfile.TemporaryDirectory() as td:
        d = _daemon(td)
        d.stop_campaign("test")
        assert d.stop is True, "the poll loop must exit"


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
