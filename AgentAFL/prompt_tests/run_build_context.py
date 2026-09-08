#!/usr/bin/env python3
"""
run_build_context.py — the engineered-prompt arm: run the real
build_context.assemble_prompt in a 10 x 5 batch, same storage + coverage
evaluation + contribution report as run_baseline.py (all via batch_harness).

The "tried before, avoid" seeds come from THIS batch's own output, not a prior
orchestrator run: call 1 has no history; for calls 2..N the harness has already
run each earlier seed through afl-showmap and written them into <out>/_history/
(cycles.jsonl + candidates/), so build_context.scan_cycle_history /
select_failure_examples pick the 2 most-representative failures (clustered by
shared edge set / "did not parse") to echo back as avoid-examples.

Good-seed examples come from the frozen 12-hour campaign queue
(afl-output-libxml2-20260907/<instance>/queue) via build_context's default
select_diverse_seeds. The queue is frozen for the run, so it's scanned once and
cached (scan_queue is memoised for this process).

This is the base the Stage 1/2/3 arms vary. Stage 1/2 differ only in a
build_context hook (edit build_context.py / pass a selector) and re-run with a
fresh --label. Stage 3 (--rotate) keeps the same 10x5 batch but cycles
Fuzz4All's generate-new / mutate-existing / semantic-equiv closing instruction
per call (call 0 -> generate-new x5, call 1 -> mutate-existing x5, ...) instead
of the one fixed instruction — a flag here rather than its own file since only
that one line changes. Everything else (good/bad seed selection, history
feedback, scoring, report) is identical, so dropping --rotate reproduces the
Stage 1 arm exactly.

    python3 run_build_context.py --label stage1_raw            # Stage 1 (raw bad seeds)
    python3 run_build_context.py --rotate                      # Stage 3 (-> --label stage3_rotation)
    python3 run_build_context.py --provider openai --no-temperature --rotate
    python3 run_build_context.py --n-history-failure 0         # no avoid block (good seeds only)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
# Study-local copies must win over repo-root originals of the same name (see
# batch_harness.py for the full note); REPO_ROOT stays after HERE for evaluate_seeds.
for _p in (str(HERE), str(REPO_ROOT)):
    while _p in sys.path:
        sys.path.remove(_p)
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(HERE))

import build_context as bc                                           # noqa: E402
import strategies                                                    # noqa: E402
from batch_harness import (                                          # noqa: E402
    BatchConfig, ProviderCaller, run_batch, DEFAULT_MODEL, model_tag,
)

DEFAULT_CAMPAIGN = REPO_ROOT / "afl-output-libxml2-20260907"          # the 12-hour run
DEFAULT_INSTANCES = "main,sec1,sec2,sec3,sec4,sec5"                   # unioned for the eval baseline
DEFAULT_TARGET = "/home/user/Documents/AFLPlus/libxml2-build/xmllint-afl"
DEFAULT_TARGET_ARGS = "--noout @@"
# Shared frozen corpus baseline — every arm scores against the identical edge
# set (PLAN.md Sec 6). Used automatically when present; pass a path to override,
# or a non-existent path to force a fresh afl-showmap queue scan.
DEFAULT_BASELINE = HERE / "results" / "baseline_edges.json"


def _memoise_scan_queue():
    """The campaign queue is frozen for this batch, so scan_queue's result is
    constant across the 10 calls — compute it once instead of re-reading ~9k
    files per call."""
    orig = bc.scan_queue
    cache: dict[str, object] = {}

    def cached(queue_dir):
        key = str(queue_dir)
        if key not in cache:
            cache[key] = orig(queue_dir)
        return cache[key]

    bc.scan_queue = cached


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--provider", choices=("claude", "openai"), default="claude")
    ap.add_argument("--model", default=None, help="default: haiku for claude, gpt-4o-mini for openai")
    ap.add_argument("--temperature", type=float, default=0.8)
    ap.add_argument("--no-temperature", action="store_true",
                    help="never send a top-level temperature (for models that reject it, e.g. gpt-5.x)")
    ap.add_argument("--calls", type=int, default=10)
    ap.add_argument("--seeds-per-call", type=int, default=5, help="= build_context n_generate")
    ap.add_argument("--rotate", action="store_true",
                    help="Stage 3: same 10x5 batch, but cycle Fuzz4All's generate-new / "
                         "mutate-existing / semantic-equiv closing instruction per call "
                         "(call 0 -> generate-new x5, call 1 -> mutate-existing x5, ...). "
                         "Drop this flag to get the Stage 1 arm.")
    ap.add_argument("--n-seeds", type=int, default=2, help="good-seed examples in the prompt (PLAN.md default 2)")
    ap.add_argument("--n-history-failure", type=int, default=2,
                    help="avoid-examples drawn from this batch's own prior seeds (0 disables; PLAN.md default 2)")
    ap.add_argument("--fmt", default="XML document")
    ap.add_argument("--seed-kind", choices=("text", "binary"), default="text")
    ap.add_argument("--fence-lang", default="")
    ap.add_argument("--no-asan-hint", action="store_true", help="drop the 'built with ASan' system line")
    ap.add_argument("--label", default=None,
                    help="base name -> results/<label>_<tag>/ "
                         "(default: stage3_rotation with --rotate, else stage1_raw)")
    ap.add_argument("--tag", default=None,
                    help="folder suffix for the AI used (default: auto — haiku / gpt-4o-mini / ...)")
    ap.add_argument("--out", type=Path, default=None, help="output dir (overrides --label/--tag)")
    ap.add_argument("--env-file", type=Path, default=None)
    # campaign state feeding the prompt
    ap.add_argument("--campaign-root", type=Path, default=DEFAULT_CAMPAIGN)
    ap.add_argument("--instance", default="main", help="which instance's queue supplies good seeds")
    # evaluation
    ap.add_argument("--skip-eval", action="store_true")
    ap.add_argument("--reuse-baseline", type=Path,
                    default=DEFAULT_BASELINE if DEFAULT_BASELINE.is_file() else None,
                    help="baseline_edges.json to score against (skips the ~50k-file queue "
                         f"scan). Default: {DEFAULT_BASELINE} when it exists. Pass a "
                         "non-existent path to force a fresh afl-showmap scan.")
    ap.add_argument("--instances", default=DEFAULT_INSTANCES, help="unioned for the eval baseline")
    ap.add_argument("--target", default=DEFAULT_TARGET)
    ap.add_argument("--target-args", default=DEFAULT_TARGET_ARGS)
    ap.add_argument("--afl-showmap-bin", default="afl-showmap")
    args = ap.parse_args()

    if args.skip_eval and args.n_history_failure > 0:
        print("NOTE: --skip-eval means no seed is classified, so the 'tried before, avoid' "
              "block will always be empty (scan_cycle_history needs BAD/NO_COVERAGE statuses).")

    label = args.label or ("stage3_rotation" if args.rotate else "stage1_raw")
    model = args.model or DEFAULT_MODEL[args.provider]
    tag = args.tag or model_tag(args.provider, model)
    out_dir = args.out or (HERE / "results" / f"{label}_{tag}")
    temperature = None if args.no_temperature else args.temperature
    caller = ProviderCaller(args.provider, model, temperature, args.env_file)
    _memoise_scan_queue()

    # Stage 3 (--rotate): same 10x5 batch shape as Stage 1 — only the closing
    # instruction changes, cycling per call (call 0 -> generate-new x5, call 1 ->
    # mutate-existing x5, call 2 -> semantic-equiv x5, call 3 -> generate-new x5,
    # ...). Without --rotate this is byte-for-byte the Stage 1 arm.
    def prompt_fn(call_index, history_dir):
        closing = strategies.rotate(call_index) if args.rotate else None
        p = bc.assemble_prompt(
            args.campaign_root, args.instance, args.fmt,
            args.n_seeds, args.seeds_per_call,
            asan_hint=not args.no_asan_hint, fence_lang=args.fence_lang,
            seed_kind=args.seed_kind,
            run_dir=history_dir, n_history_failure=args.n_history_failure,
            closing_instruction=closing,
        )
        return {"system": p["system"], "user": p["user"], "meta": {
            "n_seeds": args.n_seeds, "n_history_failure": args.n_history_failure,
            "used_history": history_dir is not None,
            "campaign_root": str(args.campaign_root), "instance": args.instance,
            "asan_hint": not args.no_asan_hint,
            "strategy": strategies.name_for(call_index) if args.rotate else "single-batch",
        }}

    run_batch(BatchConfig(
        out_dir=out_dir, title=f"build_context {label} ({tag})", arm=f"{label}_{tag}",
        calls=args.calls, seeds_per_call=args.seeds_per_call, seed_kind=args.seed_kind,
        fmt=args.fmt, caller=caller, prompt_fn=prompt_fn,
        uses_history=(args.n_history_failure > 0),
        do_eval=not args.skip_eval, campaign_root=args.campaign_root,
        instances=[s.strip() for s in args.instances.split(",") if s.strip()],
        target=args.target, target_args=args.target_args.split(),
        showmap_bin=args.afl_showmap_bin, reuse_baseline=args.reuse_baseline,
        extra_summary={
            "model_tag": tag,
            "n_seeds": args.n_seeds, "n_history_failure": args.n_history_failure,
            "instance": args.instance,
            "asan_hint": not args.no_asan_hint,
            "strategy": "rotation" if args.rotate else "single-batch",
        },
    ))


if __name__ == "__main__":
    main()
