#!/usr/bin/env python3
"""
bulk_generate.py — a fast, unattended BULK RUN of agentafl_orchestrator.

The orchestrator (agentafl_orchestrator.py) waits for a live AFL++ campaign to
plateau, then runs one generate -> evaluate -> inject "cycle" at a time. This
script runs the *same cycle* (agentafl_orchestrator.run_cycle) back-to-back, N
times per provider, with no plateau wait and no injection — so you get a batch of
results quickly and can compare models.

Because each cycle's record is appended to <provider>/cycles.jsonl immediately,
the prompt EVOLVES exactly as it would in a real orchestrator run:
build_context.assemble_prompt feeds the model its own past no-coverage seeds
("=== YOUR OWN PRIOR SEEDS THAT DID NOT HELP ... ===") pulled from that file.
Pass --n-history-failure 0 to freeze the prompt instead.

Nothing here re-implements the pipeline: fenced-block parsing, candidate
materialization, per-seed afl-showmap classification, prompt assembly and the
prompt/response/candidate file layout all come from run_cycle. This script only
adds the provider loop, the no-gate cadence, a single shared baseline, and a
CSV/txt digest.

Output (under --runs-root, default ./agentafl_runs):

  <label>-<timestamp>/
    bulk_config.json
    SUMMARY.csv                     <- one row per generated seed, both providers
    COVERAGE.txt                    <- per-provider new-coverage tally
    bulk_summary.json
    <provider>/                     <- a real orchestrator-style run dir
      orchestrator.log
      cycles.jsonl                  <- run_start, one 'cycle' per cycle, run_end
      prompts/cycle_XXXX.json       (written by run_cycle)
      responses/cycle_XXXX.json     (written by run_cycle)
      candidates/cycle_XXXX/cand_NN{ext}
      SUMMARY.csv
      new_coverage_seeds/           <- copies of just the GOOD seeds

Example:
  python3 bulk_generate.py --campaign-root afl-output-libxml2 \
      --providers gemini,claude --cycles 10 --n-generate 3 \
      --claude-model claude-sonnet-5 --llm-max-output-tokens 32000
"""

import argparse
import csv
import json
import shutil
import sys
import time
from pathlib import Path

import agentafl_orchestrator as orch
import build_context
import evaluate_seeds

SCRIPT_DIR = Path(__file__).resolve().parent

SUMMARY_FIELDS = [
    "provider", "model", "cycle_id", "candidate_idx", "seed_path", "status",
    "new_edges", "total_edges", "edge_sig", "injected", "llm_finish_reason",
    "response_truncated", "prompt_has_history", "prompt_path",
]

HISTORY_MARKER = "YOUR OWN PRIOR SEEDS THAT DID NOT HELP"

GOOD = "GOOD (novel coverage)"
REDUNDANT = "BAD (redundant)"


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    # Campaign / prompt
    ap.add_argument("--campaign-root", required=True, type=Path)
    ap.add_argument("--instance", default="main")
    ap.add_argument("--format", dest="fmt", default="XML document",
                    help="Human-readable format name, substituted into the prompt.")
    ap.add_argument("--seed-kind", choices=["text", "binary"], default="text")
    ap.add_argument("--seed-ext", default=".xml",
                    help='Candidate file extension (default ".xml"; orchestrator uses '
                         '".seed"/".bin"). Passed straight to run_cycle.')
    ap.add_argument("--format-hint", default="")
    ap.add_argument("--asan-hint", action="store_true")
    ap.add_argument("--plateau-log", type=Path, default=None,
                    help="Default: <campaign-root's parent>/plateau_log.csv")
    ap.add_argument("--n-seeds", type=int, default=3,
                    help="Queue few-shot example seeds embedded in the prompt.")

    # Bulk loop
    ap.add_argument("--providers", default="gemini,claude",
                    help="Comma-separated subset of: gemini, claude (order = run order).")
    ap.add_argument("--cycles", type=int, default=10,
                    help="run_cycle calls per provider.")
    ap.add_argument("--n-generate", type=int, default=3,
                    help="Seeds requested per cycle.")
    ap.add_argument("--n-history-failure", type=int, default=2,
                    help="Past no-coverage seeds echoed back into the prompt each cycle "
                         "(cap 2 in build_context). 0 freezes the prompt.")
    ap.add_argument("--sleep-between-cycles-s", type=float, default=1.0)

    # LLM
    ap.add_argument("--gemini-model", default="gemini-3.5-flash-lite")
    ap.add_argument("--claude-model", default="claude-opus-5")
    ap.add_argument("--gemini-temperature", type=float, default=0.9,
                    help="Applied to gemini only; forced None for claude (current "
                         "Claude models reject a temperature).")
    ap.add_argument("--llm-max-output-tokens", type=int, default=32000)
    ap.add_argument("--llm-timeout-s", type=int, default=300)
    ap.add_argument("--llm-max-retries", type=int, default=3)

    # Coverage
    ap.add_argument("--afl-showmap-bin", default="afl-showmap")
    ap.add_argument("--afl-showmap-timeout-s", type=int, default=30)
    ap.add_argument("--target", default=None,
                    help="Default: binary from <instance>/cmdline (resolved against "
                         "this script's dir if the path is relative and exists there).")
    ap.add_argument("--target-args", default=None,
                    help='Space-separated, @@ = input. Default: from <instance>/cmdline.')

    # Run
    ap.add_argument("--runs-root", type=Path, default=SCRIPT_DIR / "agentafl_runs")
    ap.add_argument("--run-label", default="bulk")
    ap.add_argument("--env-file", type=Path, default=SCRIPT_DIR / ".env")
    ap.add_argument("--log-level", default="INFO")

    ap.add_argument("--inject", action="store_true",
                    help="Pass inject=True to run_cycle (afl-addseeds into the live "
                         "campaign). Off by default. Running two providers with this "
                         "MIXES their seeds into one campaign. Forces a per-cycle "
                         "baseline rebuild since the queue then changes.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Passthrough to run_cycle: canned fixture, no API, no "
                         "afl-showmap. Plumbing test only — does NOT populate history.")
    return ap


def resolve_target(args, inst_dir: Path):
    info = build_context.parse_cmdline(inst_dir / "cmdline")
    target = args.target or info["binary"]
    tp = Path(target)
    if not tp.is_absolute():
        cand = SCRIPT_DIR / tp
        if cand.exists():
            target = str(cand.resolve())
    if args.target_args is not None:
        target_args = args.target_args.split()
    else:
        target_args = info["args"].split() or ["@@"]
    return target, target_args


def make_run_root(runs_root: Path, label: str) -> Path:
    run_id = orch.make_run_id(label)
    run_root = runs_root / run_id
    suffix = 2
    while True:
        try:
            run_root.mkdir(parents=True, exist_ok=False)
            return run_root
        except FileExistsError:
            run_root = runs_root / f"{run_id}-{suffix}"
            suffix += 1


def prompt_has_history(provider_dir: Path, prompt_path) -> bool:
    if not prompt_path:
        return False
    try:
        data = json.loads((provider_dir / prompt_path).read_text())
        return HISTORY_MARKER in (data.get("user") or "")
    except (OSError, ValueError):
        return False


def run_provider(provider, args, ctx, run_root: Path):
    """Drive --cycles calls of orch.run_cycle for one provider. Returns
    (model, [cycle_record, ...])."""
    model = args.claude_model if provider == "claude" else args.gemini_model
    key_var = "CLAUDE_API_KEY" if provider == "claude" else "GEMINI_API_KEY"
    temperature = None if provider == "claude" else args.gemini_temperature

    provider_dir = run_root / provider
    provider_dir.mkdir(parents=True, exist_ok=True)
    logger = orch._setup_logging(provider_dir, args.log_level)

    api_key = None
    if not args.dry_run:
        try:
            api_key = orch.load_api_key(args.env_file, key_var)
        except SystemExit as e:
            print(f"[{provider}] SKIPPED: {e}")
            return model, []

    cycles_jsonl = provider_dir / "cycles.jsonl"
    orch.append_jsonl_record(cycles_jsonl, {
        "record_type": "run_start", "provider": provider, "model": model,
        "started_at": orch._now_iso(), "cycles_planned": args.cycles,
        "n_generate": args.n_generate, "n_history_failure": args.n_history_failure,
    })

    baseline = None if args.inject else ctx["baseline"]
    records = []
    print(f"[{provider}] model={model}  {args.cycles} cycles x {args.n_generate} seeds")

    for cycle_id in range(1, args.cycles + 1):
        plateau_row = build_context.parse_plateau_row(ctx["plateau_log"])
        t0 = time.time()
        record = orch.run_cycle(
            cycle_id=cycle_id,
            campaign_root=ctx["campaign_root"],
            instance=args.instance,
            plateau_log=ctx["plateau_log"],
            run_dir=provider_dir,
            api_key=api_key,
            model=model,
            fmt=args.fmt,
            n_seeds=args.n_seeds,
            n_generate=args.n_generate,
            afl_showmap_bin=args.afl_showmap_bin,
            target=ctx["target"],
            target_args=ctx["target_args"],
            afl_addseeds_bin="afl-addseeds",
            main_queue_dir=ctx["main_queue_dir"],
            plateau_row=plateau_row,
            seed_kind=args.seed_kind,
            seed_ext=args.seed_ext,
            format_hint=args.format_hint,
            temperature=temperature,
            max_output_tokens=args.llm_max_output_tokens,
            timeout_s=args.llm_timeout_s,
            max_retries=args.llm_max_retries,
            asan_hint=args.asan_hint,
            afl_showmap_timeout_s=args.afl_showmap_timeout_s,
            dry_run=args.dry_run,
            inject=args.inject,
            baseline=baseline,
            provider=provider,
            logger=logger,
            n_history_failure=args.n_history_failure,
        )
        # Persist immediately so the NEXT cycle's assemble_prompt sees this
        # cycle's no-coverage seeds.
        orch.append_jsonl_record(cycles_jsonl, record)
        record["_prompt_has_history"] = prompt_has_history(
            provider_dir, record.get("prompt_path"))
        records.append(record)

        print(f"[{provider}] cycle {cycle_id:02d}: "
              f"parsed {record['n_candidates_parsed']}/{args.n_generate}  "
              f"new_cov {record['n_new_coverage']}  "
              f"trunc {bool(record['response_truncated'])}  "
              f"hist {record['_prompt_has_history']}  "
              f"errs {len(record['errors'])}  {time.time() - t0:.1f}s")

        time.sleep(args.sleep_between_cycles_s)

    good = sum(1 for r in records for c in r["candidates"] if c["status"] == GOOD)
    orch.append_jsonl_record(cycles_jsonl, {
        "record_type": "run_end", "provider": provider, "model": model,
        "ended_at": orch._now_iso(), "cycles_run": len(records),
        "seeds_materialized": sum(r["n_materialized"] for r in records),
        "new_coverage_seeds": good,
    })
    print(f"[{provider}] done: {good} new-coverage seed(s) over {len(records)} cycles "
          f"-> {provider_dir}")
    return model, records


def copy_good_seeds(provider, provider_dir: Path, records, seed_ext):
    dest = provider_dir / "new_coverage_seeds"
    n = 0
    for r in records:
        for c in r["candidates"]:
            if c["status"] != GOOD or not c.get("seed_path"):
                continue
            src = provider_dir / c["seed_path"]
            if not src.is_file():
                continue
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(
                src,
                dest / f"{provider}_cyc{r['cycle_id']:02d}_cand{c['candidate_idx']:02d}{seed_ext}",
            )
            n += 1
    return n


def candidate_rows(provider, model, records):
    for r in records:
        for c in r["candidates"]:
            yield {
                "provider": provider, "model": model, "cycle_id": r["cycle_id"],
                "candidate_idx": c["candidate_idx"], "seed_path": c.get("seed_path") or "",
                "status": c["status"], "new_edges": c["new_edges"],
                "total_edges": c["total_edges"], "edge_sig": c.get("edge_sig") or "",
                "injected": c.get("injected", False),
                "llm_finish_reason": r["llm_finish_reason"],
                "response_truncated": bool(r["response_truncated"]),
                "prompt_has_history": r["_prompt_has_history"],
                "prompt_path": r["prompt_path"] or "",
            }


def write_csv(path: Path, rows):
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=SUMMARY_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def provider_tally(provider, model, records):
    counts = {}
    best_new = 0
    seeds = 0
    for r in records:
        for c in r["candidates"]:
            seeds += 1
            counts[c["status"]] = counts.get(c["status"], 0) + 1
            best_new = max(best_new, c["new_edges"] or 0)
    first_hist = next((r["cycle_id"] for r in records if r["_prompt_has_history"]), None)
    return {
        "provider": provider, "model": model,
        "cycles": len(records), "seeds_materialized": seeds,
        "new_coverage": counts.get(GOOD, 0),
        "redundant": counts.get(REDUNDANT, 0),
        "crash": counts.get("CRASH", 0), "timeout": counts.get("TIMEOUT", 0),
        "no_coverage": counts.get("NO_COVERAGE", 0),
        "eval_error": counts.get("EVAL_ERROR", 0),
        "hex_decode_error": counts.get("HEX_DECODE_ERROR", 0),
        "dry_run_skipped": counts.get("DRY_RUN_SKIPPED", 0),
        "best_single_seed_new_edges": best_new,
        "first_history_cycle": first_hist,
        "cycle_errors": sum(1 for r in records if r["errors"]),
    }


def main():
    args = build_arg_parser().parse_args()
    providers = [p.strip() for p in args.providers.split(",") if p.strip()]
    for p in providers:
        if p not in ("gemini", "claude"):
            sys.exit(f"unknown provider {p!r} (want gemini and/or claude)")

    campaign_root = args.campaign_root.resolve()
    plateau_log = args.plateau_log or (campaign_root.parent / "plateau_log.csv")
    inst_dir = campaign_root / args.instance
    main_queue_dir = inst_dir / "queue"
    target, target_args = resolve_target(args, inst_dir)

    run_root = make_run_root(args.runs_root, args.run_label)
    queue_at_start = sum(1 for _ in main_queue_dir.iterdir()) if main_queue_dir.is_dir() else 0

    (run_root / "bulk_config.json").write_text(json.dumps({
        **{k: (str(v) if isinstance(v, Path) else v) for k, v in vars(args).items()},
        "campaign_root_resolved": str(campaign_root),
        "plateau_log_resolved": str(plateau_log),
        "target_resolved": target, "target_args_resolved": target_args,
        "main_queue_dir": str(main_queue_dir),
        "queue_files_at_start": queue_at_start,
        "started_at": orch._now_iso(),
    }, indent=2))

    print(f"Run dir: {run_root}")
    print(f"target: {target} {' '.join(target_args)}")
    print(f"queue:  {main_queue_dir}  ({queue_at_start} files)")

    baseline = None
    if not args.dry_run and not args.inject:
        print("Building full-queue afl-showmap baseline (once, ~1 min)...")
        try:
            baseline = evaluate_seeds.build_baseline(
                args.afl_showmap_bin, target, target_args, main_queue_dir,
                timeout=args.afl_showmap_timeout_s,
            )
            print(f"baseline: {len(baseline)} unique edges")
        except Exception as e:
            print(f"baseline build FAILED ({e}) — each cycle will rebuild its own.")
            baseline = None
    elif args.inject:
        print("--inject set: skipping shared baseline (queue changes each cycle).")

    ctx = {
        "campaign_root": campaign_root, "plateau_log": plateau_log,
        "main_queue_dir": main_queue_dir, "target": target,
        "target_args": target_args, "baseline": baseline,
    }

    all_rows = []
    tallies = []
    for provider in providers:
        print()
        model, records = run_provider(provider, args, ctx, run_root)
        if not records:
            continue
        rows = list(candidate_rows(provider, model, records))
        all_rows.extend(rows)
        write_csv(run_root / provider / "SUMMARY.csv", rows)
        copy_good_seeds(provider, run_root / provider, records, args.seed_ext)
        tallies.append(provider_tally(provider, model, records))

    write_csv(run_root / "SUMMARY.csv", all_rows)

    queue_at_end = sum(1 for _ in main_queue_dir.iterdir()) if main_queue_dir.is_dir() else 0
    lines = [
        "Bulk run of agentafl_orchestrator.run_cycle — coverage digest",
        f"run dir : {run_root}",
        f"target  : {target} {' '.join(target_args)}",
        f"queue   : {main_queue_dir}  ({queue_at_start} files at start, {queue_at_end} at end)",
        f"baseline: {len(baseline) if baseline is not None else 'per-cycle (not shared)'}",
        f"ended   : {orch._now_iso()}",
        "",
    ]
    if queue_at_end > queue_at_start:
        lines.append(f"WARNING: queue grew by {queue_at_end - queue_at_start} during the run "
                     "(live campaign still fuzzing?) — new_edges may be slightly over-reported.")
        lines.append("")
    for t in tallies:
        lines.append(f"[{t['provider']}] {t['model']}")
        lines.append(f"    cycles {t['cycles']}   seeds {t['seeds_materialized']}   "
                     f"cycle_errors {t['cycle_errors']}")
        lines.append(f"    NEW-COVERAGE {t['new_coverage']}   redundant {t['redundant']}   "
                     f"no_coverage {t['no_coverage']}   crash {t['crash']}   "
                     f"timeout {t['timeout']}   eval_error {t['eval_error']}   "
                     f"hex_decode_error {t['hex_decode_error']}"
                     + (f"   dry_run_skipped {t['dry_run_skipped']}" if t['dry_run_skipped'] else ""))
        lines.append(f"    best single seed: +{t['best_single_seed_new_edges']} edges")
        lines.append(f"    prompt gained history section at cycle: {t['first_history_cycle']}")
        lines.append("")
    (run_root / "COVERAGE.txt").write_text("\n".join(lines) + "\n")

    (run_root / "bulk_summary.json").write_text(json.dumps({
        "run_dir": str(run_root),
        "target": target, "target_args": target_args,
        "baseline_edges": len(baseline) if baseline is not None else None,
        "queue_files_at_start": queue_at_start, "queue_files_at_end": queue_at_end,
        "providers": tallies,
        "ended_at": orch._now_iso(),
    }, indent=2))

    print("\n" + "\n".join(lines))
    print(f"  combined summary : {run_root / 'SUMMARY.csv'}")
    print(f"  coverage tally   : {run_root / 'COVERAGE.txt'}")
    print(f"  structured       : {run_root / 'bulk_summary.json'}")


if __name__ == "__main__":
    main()
