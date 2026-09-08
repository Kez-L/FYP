#!/usr/bin/env python3
"""
run_baseline.py — Stage 0 baseline (PLAN.md Sec 5): the "no prompt engineering
at all" floor. Each call gets only the format name + a count; no campaign state,
no example seeds, no plateau framing.

Thin wrapper over batch_harness.run_batch — it owns the generate -> parse ->
afl-showmap -> contribution-report loop and the output layout. This file only
supplies the prompt (build_context_baseline.assemble_baseline_prompt) and the
CLI. See run_build_context.py for the engineered-prompt arm.

    python3 run_baseline.py                          # 10 x 5 -> 50 seeds, claude/haiku, then evaluate
    python3 run_baseline.py --provider openai --model gpt-4o-mini
    python3 run_baseline.py --calls 50 --seeds-per-call 1   # true per-seed token counts
    python3 run_baseline.py --skip-eval
    python3 run_baseline.py --reuse-baseline results/stage0_baseline/baseline_edges.json
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

from build_context_baseline import assemble_baseline_prompt          # noqa: E402
from batch_harness import (                                          # noqa: E402
    BatchConfig, ProviderCaller, run_batch, DEFAULT_MODEL, model_tag,
)

DEFAULT_CAMPAIGN = REPO_ROOT / "afl-output-libxml2-20260907"          # the 12-hour run
DEFAULT_INSTANCES = "main,sec1,sec2,sec3,sec4,sec5"
DEFAULT_TARGET = "/home/user/Documents/AFLPlus/libxml2-build/xmllint-afl"
DEFAULT_TARGET_ARGS = "--noout @@"
# Shared frozen corpus baseline — every arm scores against the identical edge
# set (PLAN.md Sec 6). Used automatically when present; pass a path to override,
# or a non-existent path to force a fresh afl-showmap queue scan.
DEFAULT_BASELINE = HERE / "results" / "baseline_edges.json"


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--provider", choices=("claude", "openai"), default="claude")
    ap.add_argument("--model", default=None, help="default: haiku for claude, gpt-4o-mini for openai")
    ap.add_argument("--temperature", type=float, default=0.8)
    ap.add_argument("--no-temperature", action="store_true",
                    help="never send a top-level temperature (for models that reject it, e.g. gpt-5.x)")
    ap.add_argument("--calls", type=int, default=10)
    ap.add_argument("--seeds-per-call", type=int, default=5)
    ap.add_argument("--fmt", default="XML document")
    ap.add_argument("--seed-kind", choices=("text", "binary"), default="text")
    ap.add_argument("--fence-lang", default="")
    ap.add_argument("--tag", default=None,
                    help="folder suffix for the AI used (default: auto — haiku / gpt-4o-mini / ...)")
    ap.add_argument("--out", type=Path, default=None,
                    help="output dir (default: results/stage0_baseline_<tag>)")
    ap.add_argument("--env-file", type=Path, default=None)
    # evaluation
    ap.add_argument("--skip-eval", action="store_true")
    ap.add_argument("--reuse-baseline", type=Path,
                    default=DEFAULT_BASELINE if DEFAULT_BASELINE.is_file() else None,
                    help="baseline_edges.json to score against (skips the queue scan). "
                         f"Default: {DEFAULT_BASELINE} when it exists. Pass a non-existent "
                         "path to force a fresh afl-showmap scan.")
    ap.add_argument("--campaign-root", type=Path, default=DEFAULT_CAMPAIGN)
    ap.add_argument("--instances", default=DEFAULT_INSTANCES)
    ap.add_argument("--target", default=DEFAULT_TARGET)
    ap.add_argument("--target-args", default=DEFAULT_TARGET_ARGS)
    ap.add_argument("--afl-showmap-bin", default="afl-showmap")
    args = ap.parse_args()

    model = args.model or DEFAULT_MODEL[args.provider]
    tag = args.tag or model_tag(args.provider, model)
    out_dir = args.out or (HERE / "results" / f"stage0_baseline_{tag}")
    temperature = None if args.no_temperature else args.temperature
    caller = ProviderCaller(args.provider, model, temperature, args.env_file)

    def prompt_fn(call_index, history_dir):
        return assemble_baseline_prompt(args.fmt, args.seeds_per_call, args.seed_kind, args.fence_lang)

    run_batch(BatchConfig(
        out_dir=out_dir, title=f"Stage 0 baseline ({tag})", arm=f"stage0_baseline_{tag}",
        calls=args.calls, seeds_per_call=args.seeds_per_call, seed_kind=args.seed_kind,
        fmt=args.fmt, caller=caller, prompt_fn=prompt_fn, uses_history=False,
        do_eval=not args.skip_eval, campaign_root=args.campaign_root,
        instances=[s.strip() for s in args.instances.split(",") if s.strip()],
        target=args.target, target_args=args.target_args.split(),
        showmap_bin=args.afl_showmap_bin, reuse_baseline=args.reuse_baseline,
        extra_summary={"model_tag": tag},
    ))


if __name__ == "__main__":
    main()
