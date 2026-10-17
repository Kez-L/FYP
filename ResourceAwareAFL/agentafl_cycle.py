"""One AgentAFL Level-2 escalation, with seed memory that survives across escalations.

This is the integration layer between ResourceAwareAFL (Min Yee's resource-aware
Level-1 loop) and AgentAFL's Stage 4 "positive and negative feedback" seed
generation. Nothing here modifies either component; it calls AgentAFL's existing
functions through their current signatures.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENTAFL = REPO_ROOT / "AgentAFL"
PROMPT_TESTS = AGENTAFL / "prompt_tests"


def _ensure_agentafl_on_path():
    """Put AgentAFL's prompt_tests first, then AgentAFL itself.

    `batch_harness` performs the same remove-then-reinsert on import so its
    study-local `build_context` wins over the repo-root module of the same name;
    we only need the directories present for the first import to resolve.
    """
    if not PROMPT_TESTS.is_dir():
        raise RuntimeError(f"AgentAFL prompt_tests not found at {PROMPT_TESTS}")
    for p in (str(AGENTAFL), str(PROMPT_TESTS)):
        if p in sys.path:
            sys.path.remove(p)
        sys.path.insert(0, p)


_ensure_agentafl_on_path()

import batch_harness                      # noqa: E402
import build_context as bc                # noqa: E402
import select_seeds_by_coverage as ssc     # noqa: E402
import stage4_positive_feedback as s4      # noqa: E402
from parse_seeds import parse_seeds        # noqa: E402

# Stage 4 "both" defaults, matching results3/xml/stage4_both's recorded summary.
DEFAULT_CALLS = 5
DEFAULT_SEEDS_PER_CALL = 5
DEFAULT_N_SEEDS = 2
DEFAULT_N_HISTORY_FAILURE = 2
DEFAULT_JACCARD_MAX = 0.7
DEFAULT_TOPUP_BUFFER = 3


# --------------------------------------------------------------- seed memory

def next_cycle_id(history_dir: Path) -> int:
    """One past the highest cycle_id in cycles.jsonl (1 if there is none).

    Derived from the log rather than a counter so it is correct after a daemon
    restart mid-campaign, which would otherwise reuse cycle ids and overwrite
    `candidates/cycle_XXXX/`.
    """
    log = Path(history_dir) / "cycles.jsonl"
    if not log.is_file():
        return 1
    top = 0
    for line in log.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("record_type") == "cycle":
            top = max(top, int(rec.get("cycle_id") or 0))
    return top + 1


def next_escalation_id(log_path: Path) -> int:
    """One past the highest escalation_id in escalations.jsonl (1 if none).

    Derived from the log for the same reason as `next_cycle_id`: an in-process
    counter restarts at 1 when the daemon restarts mid-campaign, and
    `escalations/esc_%03d/` is keyed on it, so the earlier escalation's prompts,
    responses, seeds and frozen baseline are silently overwritten. Observed in a
    real local run: two escalations both logged as id=1, with the second run's
    baseline replacing the first's in esc_001/.
    """
    p = Path(log_path)
    if not p.is_file():
        return 1
    top = 0
    for line in p.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("event") == "escalation":
            top = max(top, int(rec.get("escalation_id") or 0))
    return top + 1


def completed_escalations(log_path: Path) -> int:
    """How many escalations this CAMPAIGN has already run, from the log.

    Used for --max-escalations so the cost cap is per campaign rather than per
    daemon process; otherwise a restart would silently double the LLM budget.
    """
    return next_escalation_id(log_path) - 1


def memory_stats(history_dir: Path) -> dict:
    """Size of the accumulated seed memory, for logging and assertions."""
    hd = Path(history_dir)
    pos = bc.scan_cycle_history_positive(hd)
    neg = bc.scan_cycle_history(hd)
    return {
        "positive_seeds": len(pos),
        "negative_seeds": len(neg),
        "positive_cycles": sorted({p["cycle_id"] for p in pos}),
        "negative_cycles": sorted({n["cycle_id"] for n in neg}),
        "next_cycle_id": next_cycle_id(hd),
    }


# ----------------------------------------------------------------- injection

def inject_seeds(campaign_root: Path, seed_paths: list[Path],
                 addseeds_bin: str = "afl-addseeds", timeout: int = 120) -> dict:
    """Hand seeds to a LIVE campaign via afl-addseeds.

    afl-addseeds copies each file to `<campaign_root>/addseeds/queue/` as
    `id:%06d,time:0,execs:0,orig:NAME` with gap-free ids. That naming is
    load-bearing: sync_fuzzers() locates the next entry with
    `strncmp(d_name, "id:%06u", 9)` and imports NOTHING if no file matches the
    exact expected id, so hand-writing the queue dir is not a safe substitute.

    The main node (`-M main`) syncs from every sibling dir, so it picks these up;
    secondaries then inherit them from main. A single instance started without
    -M/-S is a SECONDARY (afl-fuzz.c:1776-1781) and skips any sync dir lacking an
    `is_main_node` marker, so it would import nothing - see `ensure_syncable`.
    """
    if not seed_paths:
        return {"injected": 0, "ok": True, "detail": "no seeds"}
    cmd = [addseeds_bin, "-o", str(campaign_root)] + [str(p) for p in seed_paths]
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=timeout)
        ok = res.returncode == 0
        detail = (res.stderr or res.stdout or b"").decode(errors="replace")[-400:]
    except (OSError, subprocess.SubprocessError) as exc:
        ok, detail = False, f"{type(exc).__name__}: {exc}"
    return {"injected": len(seed_paths) if ok else 0, "ok": ok, "detail": detail}


def ensure_syncable(campaign_root: Path) -> bool:
    """Make `addseeds/` importable by a single-instance (secondary) campaign.

    Only needed when no `-M main` instance exists: secondaries require an
    `is_main_node` marker in a sync source. Writing one where a real main node is
    present only costs a cosmetic WARNF ("it is wasteful to run more than one main
    node", afl-fuzz.c:2277), never an abort - but we skip it in that case anyway.

    Returns True if a marker was created.
    """
    root = Path(campaign_root)
    queue = root / "addseeds" / "queue"
    if not queue.is_dir():
        return False
    if any((d / "is_main_node").exists() for d in root.iterdir() if d.is_dir()):
        return False
    marker = root / "addseeds" / "is_main_node"
    if marker.exists():
        return False
    marker.touch()
    return True


#: AFL++ env vars that configure a *custom mutator*. They are meant for
#: afl-fuzz only, but afl-showmap and afl-addseeds honour them too, and
#: afl-showmap PROGRAM ABORTs if it cannot import the module
#: ("Failed to load python mutator", afl-fuzz-python.c:453).
MUTATOR_ENV_VARS = (
    "AFL_PYTHON_MODULE",
    "AFL_CUSTOM_MUTATOR_LIBRARY",
    "AFL_CUSTOM_MUTATOR_ONLY",
    "AFL_NO_PYTHON",
)


def sanitize_afl_env(env=None) -> list:
    """Strip custom-mutator vars so child afl-* tools cannot inherit them.

    This is not hypothetical tidying. `run_campaign.sh` exports
    `AFL_PYTHON_MODULE=adaptive_mutator` (so it lands in afl_env.txt) *before*
    launching this daemon, and PYTHONPATH is scoped to the fuzzers only. Every
    afl-showmap call from here would therefore abort with
    "ModuleNotFoundError: No module named 'adaptive_mutator'".

    The failure is silent in the worst way: `evaluate_seeds.build_baseline`
    does not check afl-showmap's return code, so it reads an empty output file
    and returns an empty edge set; `freeze_baseline` then raises SystemExit and
    the daemon disables escalation for the rest of the run. The campaign keeps
    fuzzing and the arm quietly degenerates to `minyee`.

    Returns the names actually removed, so startup can log them.
    """
    env = os.environ if env is None else env
    return [v for v in MUTATOR_ENV_VARS if env.pop(v, None) is not None]


def _hms(seconds) -> str:
    """Seconds -> "1h23m45s", for log lines a human reads against a 12h run."""
    if seconds is None:
        return "?"
    s = int(seconds)
    sign = "-" if s < 0 else ""
    s = abs(s)
    return f"{sign}{s // 3600}h{(s % 3600) // 60:02d}m{s % 60:02d}s"


def campaign_start_time(campaign_root: Path, instance: str = "main") -> int | None:
    """AFL's own campaign start epoch, from `<instance>/fuzzer_stats`, or None.

    This is deliberately AFL's `start_time` rather than the daemon's own start or
    meta.txt's `start_unix`: `plot_data`'s first column, `relative_time`, is
    measured from exactly this value, so an escalation logged at `elapsed_s` can
    be dropped straight onto the coverage-over-time curve with no offset
    guesswork. Returns None before afl-fuzz has written fuzzer_stats.
    """
    p = Path(campaign_root) / instance / "fuzzer_stats"
    try:
        for line in p.read_text(errors="replace").splitlines():
            key, _, val = line.partition(":")
            if key.strip() == "start_time":
                return int(val.strip())
    except (OSError, ValueError):
        return None
    return None


def synced_count(campaign_root: Path, instance: str = "main",
                 source: str = "addseeds") -> int | None:
    """How many addseeds entries `instance` has consumed, or None if unknown.

    `<campaign_root>/<instance>/.synced/<source>` is a 4-byte little-endian u32
    written by sync_fuzzers(). Watching it advance is the one cheap check that
    proves injected seeds actually reached the fuzzer.
    """
    p = Path(campaign_root) / instance / ".synced" / source
    try:
        raw = p.read_bytes()[:4]
    except OSError:
        return None
    return int.from_bytes(raw, "little") if len(raw) == 4 else None


# ------------------------------------------------------------- one escalation

def make_selectors(*, target: str, target_args: list[str], showmap_bin: str,
                   edge_cache: dict, jaccard_max: float = DEFAULT_JACCARD_MAX,
                   log_path: Path | None = None, call_ctx: dict | None = None):
    """Build the coverage top-up selector ONCE per campaign.

    It is the bootstrap/fallback source of "good" seeds, drawn from the AFL++
    queue, used on escalation #1 call 0 before any LLM seed has earned its place
    and thereafter only to fill a slot Stage 4 cannot. Sharing `edge_cache` across
    the whole campaign avoids re-running afl-showmap on queue entries we have
    already measured (keyed by (sha1(bytes), target, args), so it stays valid as
    the queue grows).
    """
    return ssc.make_coverage_selector(
        target,
        target_args,
        afl_showmap_bin=showmap_bin,
        jaccard_max=jaccard_max,
        edge_cache=edge_cache,
        log_path=log_path,
        call_ctx=call_ctx,
    )


def run_escalation(*, campaign_root: Path, history_dir: Path, out_dir: Path,
                   escalation_id: int, caller, baseline_ints: set[int],
                   target: str, target_args: list[str],
                   topup_selector, edge_cache: dict,
                   instance: str = "main",
                   calls: int = DEFAULT_CALLS,
                   seeds_per_call: int = DEFAULT_SEEDS_PER_CALL,
                   n_seeds: int = DEFAULT_N_SEEDS,
                   n_history_failure: int = DEFAULT_N_HISTORY_FAILURE,
                   fmt: str = "XML document", seed_kind: str = "text",
                   showmap_bin: str = "afl-showmap",
                   jaccard_max: float = DEFAULT_JACCARD_MAX,
                   topup_buffer: int = DEFAULT_TOPUP_BUFFER,
                   asan_hint: bool = True,
                   addseeds_bin: str = "afl-addseeds",
                   early_stop_patience: int = 0,
                   max_calls: int | None = None,
                   call_ctx: dict | None = None,
                   campaign_start: int | None = None,
                   log=print) -> dict:
    """Run one escalation: `calls` x `seeds_per_call` Stage 4 generations, evaluate,
    remember, inject. Returns a summary dict (also appended to escalations.jsonl
    by the caller).

    Budget: 5 x 5 is the default because, replayed over every stage4_both run in
    results3/, the first 5 calls captured 90% (xml) / 81% (ical) of a 15-call run's
    new edges at the best edges-per-call of any option retaining ~90% yield, and
    the single biggest-yield call fell within the first 5 in every xml and ical run.
    `early_stop_patience` > 0 enables the measured-but-off-by-default extension
    (stop after N consecutive zero-yield calls, up to `max_calls`).
    """
    history_dir = Path(history_dir)
    out_dir = Path(out_dir)
    seeds_dir = out_dir / "seeds"
    for d in (history_dir / "candidates", seeds_dir,
              out_dir / "prompts", out_dir / "responses"):
        d.mkdir(parents=True, exist_ok=True)

    ext = batch_harness.EXT.get(seed_kind, "xml")
    call_ctx = call_ctx if call_ctx is not None else {"call": None}

    # Seconds since afl-fuzz started, i.e. the same clock as plot_data's
    # relative_time, so escalations can be plotted against the coverage curve.
    if campaign_start is None:
        campaign_start = campaign_start_time(campaign_root, instance)

    def elapsed(at=None):
        if campaign_start is None:
            return None
        return round((at if at is not None else time.time()) - campaign_start, 1)

    started_at = time.time()
    before = memory_stats(history_dir)
    log(f"[AgentAFL] escalation {escalation_id} at T+{_hms(elapsed(started_at))}: "
        f"memory has {before['positive_seeds']} positive / "
        f"{before['negative_seeds']} negative prior seeds, "
        f"next cycle_id {before['next_cycle_id']}")

    # The AFL++ queue grows during a campaign, so the memoised scan from a previous
    # escalation is stale. Re-memoise per escalation, but locally -- NOT via
    # run_build_context._memoise_scan_queue(), which patches bc.scan_queue globally
    # and never undoes it, so repeated use layers closures.
    _queue_cache: dict = {}
    _real_scan_queue = getattr(bc, "_agentafl_real_scan_queue", bc.scan_queue)
    bc._agentafl_real_scan_queue = _real_scan_queue

    def _scan_queue_cached(queue_dir):
        key = str(queue_dir)
        if key not in _queue_cache:
            _queue_cache[key] = _real_scan_queue(queue_dir)
        return _queue_cache[key]

    bc.scan_queue = _scan_queue_cached

    call_rows: list[dict] = []
    seed_rows: list[dict] = []
    all_new_edges: set[int] = set()
    marginal: list[int] = []
    zero_streak = 0
    budget = calls if not early_stop_patience else (max_calls or calls)

    try:
        for i in range(budget):
            call_ctx["call"] = i
            cycle_id = next_cycle_id(history_dir)

            # Stage 4 selector is rebuilt per call: it closes over the history dir
            # contents, which just changed, and must see the seeds the previous
            # call produced.
            seed_selector = s4.make_positive_feedback_selector(
                history_dir,
                topup_selector,
                target_binary=target,
                target_args=target_args,
                afl_showmap_bin=showmap_bin,
                jaccard_max=jaccard_max,
                topup_buffer=topup_buffer,
                edge_cache=edge_cache,
                log_path=out_dir / "selector_fallback.log",
                call_ctx=call_ctx,
            )

            prompt = bc.assemble_prompt(
                Path(campaign_root), instance, fmt, n_seeds, seeds_per_call,
                asan_hint=asan_hint,
                seed_kind=seed_kind,
                run_dir=history_dir,
                n_history_failure=n_history_failure,
                seed_selector=seed_selector,
            )
            (out_dir / "prompts" / f"call_{i:02d}.json").write_text(
                json.dumps({"call": i, "cycle_id": cycle_id, "ts": time.time(),
                            **prompt}, indent=2))

            t0 = time.time()
            try:
                res = caller(prompt["system"], prompt["user"])
            except SystemExit:
                # ProviderCaller / run_batch exit the process on a missing key or
                # exhausted credits. A 12h campaign must not die with it.
                raise
            latency = round(time.time() - t0, 2)
            (out_dir / "responses" / f"call_{i:02d}.txt").write_text(res.text or "")

            seeds = parse_seeds(res.text or "", seed_kind)
            rows = []
            for j, blob in enumerate(seeds):
                idx = len(seed_rows) + j
                name = f"seed_{idx:03d}.{ext}"
                (seeds_dir / name).write_bytes(blob)
                rows.append({"seed_index": idx, "file": f"seeds/{name}",
                             "call": i, "cycle_id": cycle_id,
                             "index_in_call": j, "bytes": len(blob)})

            for row in rows:
                batch_harness.evaluate_row(
                    row, seeds_dir / Path(row["file"]).name,
                    baseline_ints, target, target_args, showmap_bin)

            # Persist this call into the campaign-wide memory BEFORE the next call,
            # so the next prompt can use it. This is the mechanism that makes
            # escalation N able to cite escalation N-1's seeds.
            if rows:
                batch_harness.append_history_cycle(
                    history_dir, cycle_id, rows, seeds_dir, ext)

            fresh = set()
            for row in rows:
                fresh |= set(row.get("new_edge_ids") or [])
            gained = len(fresh - all_new_edges)
            all_new_edges |= fresh
            marginal.append(gained)
            zero_streak = zero_streak + 1 if gained == 0 else 0

            seed_rows.extend(rows)
            call_rows.append({
                "call": i, "cycle_id": cycle_id, "seeds_requested": seeds_per_call,
                "seeds_parsed": len(seeds), "new_edges_marginal": gained,
                "ts": round(t0, 3), "elapsed_s": elapsed(t0),
                "latency_s": latency,
                "input_tokens": getattr(res, "input_tokens", None),
                "output_tokens": getattr(res, "output_tokens", None),
                "finish_reason": getattr(res, "finish_reason", None),
            })
            with (out_dir / "calls.jsonl").open("a") as f:
                f.write(json.dumps(call_rows[-1]) + "\n")

            short = "  <-- short" if len(seeds) < seeds_per_call else ""
            log(f"[AgentAFL]   call {i} at T+{_hms(elapsed(t0))}: {len(seeds)} seeds, "
                f"+{gained} new edges, {latency}s{short}")

            if early_stop_patience and i + 1 >= calls and zero_streak >= early_stop_patience:
                log(f"[AgentAFL]   early stop after {i + 1} calls "
                    f"({zero_streak} zero-yield)")
                break
    finally:
        bc.scan_queue = _real_scan_queue

    seed_paths = [seeds_dir / Path(r["file"]).name for r in seed_rows]
    synced_before = synced_count(campaign_root, instance)
    inj = inject_seeds(campaign_root, seed_paths, addseeds_bin=addseeds_bin)
    created_marker = ensure_syncable(campaign_root)
    if created_marker:
        log("[AgentAFL]   wrote addseeds/is_main_node (no -M main instance found)")

    after = memory_stats(history_dir)
    finished_at = time.time()
    summary = {
        "escalation_id": escalation_id,
        "ts": round(started_at, 3),
        # Seconds since afl-fuzz started. Same clock as plot_data's relative_time,
        # so `elapsed_s` can be plotted directly against the coverage curve.
        "campaign_start_unix": campaign_start,
        "elapsed_s": elapsed(started_at),
        "elapsed_hms": _hms(elapsed(started_at)),
        "elapsed_end_s": elapsed(finished_at),
        "wall_s": round(finished_at - started_at, 1),
        "calls_made": len(call_rows),
        "seeds_generated": len(seed_rows),
        "seeds_with_new_coverage": sum(
            1 for r in seed_rows if r.get("produced_new_edge")),
        "new_edges_total": len(all_new_edges),
        "marginal_per_call": marginal,
        "baseline_edges": len(baseline_ints),
        "injected": inj["injected"],
        "inject_ok": inj["ok"],
        "inject_detail": inj["detail"] if not inj["ok"] else "",
        "synced_before": synced_before,
        "addseeds_marker_created": created_marker,
        "memory_before": before,
        "memory_after": after,
        "out_dir": str(out_dir),
        "llm_latency_s": round(sum(c["latency_s"] for c in call_rows), 1),
    }
    summary["call_log"] = call_rows
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    log(f"[AgentAFL] escalation {escalation_id} done at "
        f"T+{_hms(elapsed(finished_at))} ({summary['wall_s']}s): "
        f"{len(seed_rows)} seeds, {len(all_new_edges)} new edges, "
        f"injected={inj['injected']}, "
        f"memory now {after['positive_seeds']}+/{after['negative_seeds']}-")
    return summary
