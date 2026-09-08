"""
run_ablation.py — the top-level driver. Runs one or more "arms" (prompt
configurations) against one or more "trigger events" (saved campaign
snapshots — see PLAN.md on why triggers should be captured ONCE and reused
across arms, not re-derived live per arm), repeated N times each for
Haiku's own stochasticity, and writes one JSONL row per (arm, trigger,
repeat) with that batch's BatchScore — the file all of Stage 1-4's analysis
reads from.

This file composes real, tested logic (the loop, the logging, the
aggregation) with pieces that are still stubs elsewhere in this project
(get_seed_edges / get_edge_ids) — it will raise NotImplementedError until
those are wired up, which is intentional: better to fail loudly at the
first real run than silently score a batch as "0 new edges everywhere."

Usage sketch (fill in real paths once the TODOs elsewhere are resolved):

    python3 run_ablation.py \\
        --triggers triggers.json \\
        --arms stage0,stage1_raw,stage1_distilled \\
        --repeats 5 \\
        --out results.jsonl

triggers.json: a list of {"trigger_id": str, "campaign_root": str,
"instance": str, "run_dir": str, "baseline_edges_file":
str}. baseline_edges_file should point at a saved set of edge IDs
representing the corpus's coverage at the moment this trigger was captured
— capture and freeze this ONCE per trigger, so every arm tested against
that trigger is compared to the identical baseline (this is what makes the
different arms' scores comparable at all).
"""

from __future__ import annotations

import argparse
import json
import statistics
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from build_context import assemble_prompt
from shared.llm_client import call_llm
from shared.parse_seeds import parse_seeds, write_seeds
from shared.score_batch import score_batch, BatchScore

from stage0_baseline.build_context_baseline import assemble_baseline_prompt
from stage1_distillation.distill_seed import bad_seed_renderer as distilled_bad_seed_renderer
from stage2_seed_selection.select_seeds_by_coverage import (
    high_coverage_selector, rare_coverage_selector, no_seed_selector,
)
from stage3_strategy_rotation.strategies import rotate


# ---------------------------------------------------------------------
# Arm registry — add a new arm here once its stage's file is ready.
# Each arm is a function: (trigger, n_generate) -> dict (a prompt, OR a
# list of prompts for multi-call arms like strategy rotation).
# ---------------------------------------------------------------------

def _arm_stage0_baseline(trigger: dict, n_generate: int):
    return assemble_baseline_prompt(trigger["fmt"], n_generate, trigger.get("seed_kind", "text"))


def _arm_default(trigger: dict, n_generate: int, **hooks):
    """The current build_context.py design (2 good + 2 bad seeds, minimal
    prompt) with optional hook overrides — this is the base every Stage
    1/2 arm is a small variation of."""
    return assemble_prompt(
        Path(trigger["campaign_root"]), trigger["instance"], trigger["fmt"],
        trigger.get("n_seeds", 2), n_generate,
        seed_kind=trigger.get("seed_kind", "text"),
        run_dir=Path(trigger["run_dir"]) if trigger.get("run_dir") else None,
        **hooks,
    )


ARMS: dict[str, Callable] = {
    "stage0_baseline": _arm_stage0_baseline,
    "stage1_raw": lambda t, n: _arm_default(t, n),  # today's design: raw bad seeds
    "stage1_distilled": lambda t, n: _arm_default(t, n, bad_seed_renderer=distilled_bad_seed_renderer),
    "stage2_high_coverage": lambda t, n: _arm_default(t, n, seed_selector=high_coverage_selector),
    "stage2_rare_coverage": lambda t, n: _arm_default(t, n, seed_selector=rare_coverage_selector),
    "stage2_no_seed": lambda t, n: _arm_default(t, n, seed_selector=no_seed_selector),
    # stage3 is multi-call, not single-prompt — see run_arm_on_trigger's
    # strategy_rotation branch below rather than the ARMS dict.
}


def run_arm_on_trigger(
    arm_name: str,
    trigger: dict,
    baseline_edges: set,
    n_generate: int,
    seed_kind: str = "text",
    out_dir: Path = Path("./_ablation_seeds"),
) -> BatchScore:
    """Run one arm once against one trigger: build prompt(s), call the LLM,
    parse the response(s) into seed files, score the resulting batch.
    Special-cases stage3_rotation, which is several small calls with a
    rotating strategy instruction rather than one call for n_generate seeds
    (see stage3_strategy_rotation/strategies.py's module docstring for why)."""
    seed_paths: list[Path] = []
    batch_dir = out_dir / arm_name / trigger["trigger_id"]

    if arm_name == "stage3_rotation":
        # n_generate small calls, 1 seed each, rotating strategy.
        for i in range(n_generate):
            prompt = _arm_default(trigger, 1, closing_instruction=rotate(i))
            resp = call_llm(prompt["system"], prompt["user"])
            seeds = parse_seeds(resp.text, seed_kind)
            seed_paths += write_seeds(seeds, batch_dir, prefix=f"call{i}")
    else:
        build = ARMS[arm_name]
        prompt = build(trigger, n_generate)
        resp = call_llm(prompt["system"], prompt["user"])
        seeds = parse_seeds(resp.text, seed_kind)
        seed_paths = write_seeds(seeds, batch_dir)

    return score_batch(
        seed_paths, baseline_edges,
        target_binary=trigger["target_binary"], target_args=trigger.get("target_args", []),
        arm_name=arm_name, trigger_id=trigger["trigger_id"],
    )


def run_study(arms: list[str], triggers: list[dict], repeats: int, out_path: Path):
    """The main loop: every arm x every trigger x `repeats` times. Appends
    one JSON line per run to out_path as it goes, so a crash partway
    through (a flaky API call, a malformed trigger) doesn't lose everything
    already completed."""
    with out_path.open("a") as f:
        for trigger in triggers:
            baseline_edges = set(json.loads(Path(trigger["baseline_edges_file"]).read_text()))
            for arm_name in arms:
                for rep in range(repeats):
                    score = run_arm_on_trigger(arm_name, trigger, baseline_edges,
                                                n_generate=trigger.get("n_generate", 5),
                                                seed_kind=trigger.get("seed_kind", "text"))
                    row = {**asdict(score), "repeat": rep}
                    f.write(json.dumps(row) + "\n")
                    f.flush()
                    print(f"[{trigger['trigger_id']}] {arm_name} rep{rep}: "
                          f"new_edges={score.new_edges_total} "
                          f"distinct_contributors={score.n_distinct_contributors}")


def summarize(results_path: Path):
    """Quick mean/median per arm from a results JSONL — enough for a first
    look; PLAN.md's analysis step still calls for a proper significance
    test (Mann-Whitney U, as CodaMOSA/Fuzz4All/FuzzGPT all used) before
    treating any difference as real."""
    by_arm: dict[str, list[int]] = {}
    for line in results_path.read_text().splitlines():
        row = json.loads(line)
        by_arm.setdefault(row["arm_name"], []).append(row["new_edges_total"])
    for arm, values in sorted(by_arm.items()):
        print(f"{arm:24s} n={len(values):3d}  mean={statistics.mean(values):.2f}  "
              f"median={statistics.median(values):.1f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run")
    run_p.add_argument("--triggers", type=Path, required=True, help="JSON file — see module docstring")
    run_p.add_argument("--arms", required=True, help="comma-separated arm names")
    run_p.add_argument("--repeats", type=int, default=5)
    run_p.add_argument("--out", type=Path, required=True)

    sum_p = sub.add_parser("summarize")
    sum_p.add_argument("--results", type=Path, required=True)

    args = ap.parse_args()
    if args.cmd == "run":
        triggers = json.loads(args.triggers.read_text())
        run_study(args.arms.split(","), triggers, args.repeats, args.out)
    elif args.cmd == "summarize":
        summarize(args.results)
