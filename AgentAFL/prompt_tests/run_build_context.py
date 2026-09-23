#!/usr/bin/env python3
"""
run_build_context.py — the engineered-prompt arm: run the real
build_context.assemble_prompt in a 15 x 5 batch (75 seeds), same storage +
coverage evaluation + contribution report as run_baseline.py (all via
batch_harness).

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
fresh --label. Stage 3 (--rotate) keeps the same 15x5 batch but cycles
Fuzz4All's generate-new / mutate-existing / semantic-equiv closing instruction
per call (call 0 -> generate-new x5, call 1 -> mutate-existing x5, ...) instead
of the one fixed instruction — a flag here rather than its own file since only
that one line changes. Everything else (good/bad seed selection, history
feedback, scoring, report) is identical, so dropping --rotate reproduces the
Stage 1 arm exactly.

    python3 run_build_context.py --label stage1_raw                   # Stage 1 (raw bad seeds)
    python3 run_build_context.py --seed-selector coverage            # Stage 2 (-> stage2_coverage)
    python3 run_build_context.py --seed-selector coverage-per-byte   # Stage 2.1 (-> stage2_coverage_per_byte)
    python3 run_build_context.py --seed-selector rare-coverage       # Stage 2.2 (-> stage2_rare_coverage)
    python3 run_build_context.py --seed-selector stage1-jaccard      # Stage 1, edge-Jaccard dedup (-> stage1_jaccard)
    python3 run_build_context.py --rotate                            # Stage 3 (-> stage3_rotation)
    python3 run_build_context.py --provider openai --no-temperature --rotate
    python3 run_build_context.py --no-feedback                       # good seeds only (-> stage1_raw_nofeedback)
    python3 run_build_context.py --seed-selector coverage --no-feedback  # Stage 2, good seeds only
    python3 run_build_context.py --positive-feedback                 # Stage 4, both feedbacks (-> stage4_both)
    python3 run_build_context.py --positive-feedback --no-feedback   # Stage 4, positive only (-> stage4_positive_only)
    python3 run_build_context.py --good-seeds-first-round-only       # negative-only, AFL seeds on call 0 only (-> stage1_raw_bootstrap1)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
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
import stage2_neg_hypothesis                                         # noqa: E402
import stage4_positive_feedback                                      # noqa: E402
from batch_harness import (                                          # noqa: E402
    BatchConfig, ProviderCaller, run_batch, DEFAULT_MODEL, model_tag,
)
from select_seeds_by_coverage import (                              # noqa: E402
    make_coverage_selector, make_coverage_per_byte_selector,
    make_rare_coverage_selector, make_stage1_jaccard_selector,
    high_coverage_selector, no_seed_selector,
)

DEFAULT_CAMPAIGN = REPO_ROOT / "afl-output-libxml2-20260907"          # the 12-hour run
DEFAULT_INSTANCES = "main,sec1,sec2,sec3,sec4,sec5"                   # unioned for the eval baseline
DEFAULT_TARGET = "/home/user/Documents/AFLPlus/libxml2-build/xmllint-afl"
DEFAULT_TARGET_ARGS = "--noout @@"
# Shared frozen corpus baseline — every arm scores against the identical edge
# set (PLAN.md Sec 6). Used automatically when present; pass a path to override,
# or a non-existent path to force a fresh afl-showmap queue scan. Deliberately
# still points at the old results/ location (not results3/xml/) — the frozen
# corpus itself hasn't changed, so there's no reason to force an expensive
# ~1-min-per-instance rescan just because new run output moved. Each run still
# writes its own copy of baseline_edges.json into its own out_dir regardless.
DEFAULT_BASELINE = HERE / "results" / "baseline_edges.json"


def _next_run_number(group_dir: Path, label: str) -> int:
    """Next unused "{label}_run_N..." number under group_dir, scanning by
    label alone (not label+tag) — a rerun with a different --model still
    gets a fresh number, so nothing overlays a previous run regardless of
    which model it used. 1 if group_dir doesn't exist yet or has no run
    dirs for this label."""
    if not group_dir.is_dir():
        return 1
    pat = re.compile(rf"^{re.escape(label)}_run_(\d+)")
    nums = [int(m.group(1)) for d in group_dir.iterdir() if d.is_dir()
            for m in [pat.match(d.name)] if m]
    return max(nums, default=0) + 1


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
    ap.add_argument("--calls", type=int, default=15)
    ap.add_argument("--seeds-per-call", type=int, default=5, help="= build_context n_generate")
    ap.add_argument("--rotate", action="store_true",
                    help="Stage 3: same 10x5 batch, but cycle Fuzz4All's generate-new / "
                         "mutate-existing / semantic-equiv closing instruction per call "
                         "(call 0 -> generate-new x5, call 1 -> mutate-existing x5, ...). "
                         "Drop this flag to get the Stage 1 arm.")
    ap.add_argument("--n-seeds", type=int, default=2, help="good-seed examples in the prompt (PLAN.md default 2)")
    ap.add_argument(
        "--seed-selector",
        choices=("default", "coverage", "coverage-per-byte",
                 "high-coverage", "rare-coverage", "stage1-jaccard", "no-seed"),
        default=None,
        help="how the good-seed examples are picked from the +cov queue entries "
             "(also the TOP-UP source for --positive-feedback, when fewer than "
             "--n-seeds past-AI good seeds exist yet). Default: 'default' normally, "
             "'coverage' when --positive-feedback is set (unset -> resolved right "
             "after arg parsing, see below). "
             "'default' = build_context.select_diverse_seeds — ranks by closeness to a "
             "400 B target within a 32-3000 B band, de-dupes a later pick by byte-content "
             "similarity (is_similar). "
             "'coverage' = rank by total edges hit via afl-showmap, in the shared "
             "200-800 B band (widening to 1500 B if nothing lands in it, then falling "
             "back to 'default' if still nothing), edge-set-Jaccard de-dup, jaccard_max=0.7 "
             "(make_coverage_selector). "
             "'coverage-per-byte' = Stage 2.1: rank by edges-per-byte (coverage density), "
             "same shared band/fallback/de-dup as 'coverage' (make_coverage_per_byte_selector). "
             "'rare-coverage' = Stage 2.2: rank by edge rarity (sum of 1/frequency over "
             "the seed's edges, frequency counted across the in-band candidates) — "
             "AFLFast's rarely-hit-path emphasis, same shared band/fallback/de-dup as "
             "'coverage' (make_rare_coverage_selector). "
             "'stage1-jaccard' = a Stage 1 sibling, not a Stage 2 arm: same ranking key as "
             "'default' (closeness to 400 B) and the same shared 200-800->1500 B band as "
             "the coverage arms, but de-dupes a later pick by edge-Jaccard (jaccard_max=0.7) "
             "instead of byte-content similarity — isolates the ranking-criterion variable "
             "when A/B-testing against 'coverage' (make_stage1_jaccard_selector). "
             "'high-coverage'/'no-seed' = the remaining select_seeds_by_coverage stubs.",
    )
    ap.add_argument("--n-history-failure", type=int, default=2,
                    help="avoid-examples drawn from this batch's own prior seeds (0 disables; PLAN.md default 2)")
    ap.add_argument("--no-feedback", action="store_true",
                    help="good seeds only — drop the 'tried before, avoid' block entirely "
                         "(forces n_history_failure=0, builds no _history/). The auto-derived "
                         "label gets a '_nofeedback' suffix so it doesn't clash with the "
                         "feedback-on run.")
    ap.add_argument("--stage2-neg-hypothesis", action="store_true",
                    help="alternate 'avoid' block: instead of raw bad-seed bytes, find up to "
                         "the 2 largest groups of this batch's own past bad seeds that share "
                         "overlapping edge coverage (or share 'produced no coverage at all'), "
                         "ask the LLM (1-2 extra calls) to hypothesize why each group failed "
                         "to find new coverage, and show only those hypotheses. Shows fewer "
                         "than 2 if only one pattern has emerged yet, none until at least one "
                         "qualifying group exists. Overridden by --no-feedback.")
    ap.add_argument("--neg-hypothesis-group-size", type=int, default=5,
                    help="max bad seeds shown per group to the hypothesis call (default 5)")
    ap.add_argument("--neg-hypothesis-min-group-size", type=int, default=2,
                    help="min bad seeds a cluster needs to count as a group (default 2)")
    ap.add_argument("--neg-hypothesis-jaccard-min", type=float, default=0.3,
                    help="edge-Jaccard threshold for grouping bad seeds together (default "
                         "0.3 — looser than the raw-feedback path's 0.9, since groups here "
                         "are meant to be large/coarse, not tight duplicates)")
    ap.add_argument("--neg-hypothesis-seed-max-chars", type=int, default=2000,
                    help="per-seed render budget shown to the hypothesis call (default 2000 "
                         "— a safety ceiling for a pathological outlier, not a real limit: "
                         "real AI-generated seeds run up to ~1200 bytes, so a too-low cap "
                         "silently hides most of each seed's content from the model)")
    ap.add_argument("--positive-feedback", action="store_true",
                    help="Stage 4: replace the good-seed examples with up to --n-seeds of "
                         "this batch's OWN past seeds that already found new coverage "
                         "(most-recent-first, each next pick required to be edge-Jaccard "
                         "diverse from every already-chosen pick — see "
                         "stage4_positive_feedback.py), instead of AFL-queue-sourced good "
                         "seeds. Tops up any shortfall (not enough past-AI seeds yet, or not "
                         "enough of them are diverse enough) from --seed-selector, which also "
                         "defaults to 'coverage' instead of 'default' when this is set. "
                         "Independent of --no-feedback/--n-history-failure, which keep "
                         "controlling the SEPARATE negative 'avoid' block exactly as before — "
                         "combine the two flags freely: --positive-feedback alone = both "
                         "positive+negative feedback (-> stage4_both); with --no-feedback = "
                         "positive-only (-> stage4_positive_only).")
    ap.add_argument("--positive-feedback-jaccard-max", type=float, default=0.7,
                    help="edge-Jaccard ceiling for two positive-feedback picks (or a pick vs. "
                         "a top-up candidate) to count as 'diverse enough' (default 0.7, "
                         "matching the Stage 2 coverage-family convention)")
    ap.add_argument("--positive-feedback-topup-buffer", type=int, default=3,
                    help="extra candidates requested from --seed-selector when topping up a "
                         "positive-feedback shortfall, so there's room to skip ones too "
                         "similar to the already-chosen past-AI seed(s) (default 3)")
    ap.add_argument("--good-seeds-first-round-only", action="store_true",
                    help="negative-only mode ONLY (mutually exclusive with "
                         "--positive-feedback): show the usual --n-seeds good-seed examples "
                         "on call 0 only, to jump-start the run, then zero good-seed examples "
                         "for every later call — just the negative 'avoid' block from then on. "
                         "Label gets a '_bootstrap1' suffix.")
    ap.add_argument("--fmt", default="XML document")
    ap.add_argument("--seed-kind", choices=("text", "binary"), default="text")
    ap.add_argument("--fence-lang", default="")
    ap.add_argument("--no-asan-hint", action="store_true", help="drop the 'built with ASan' system line")
    ap.add_argument("--label", default=None,
                    help="base name -> results3/<format-dir>/<label>/<label>_run_N_<tag>/ "
                         "(default: stage3_rotation with --rotate, else stage1_raw)")
    ap.add_argument("--tag", default=None,
                    help="folder suffix for the AI used (default: auto — haiku / gpt-4o-mini / ...)")
    ap.add_argument("--format-dir", default="xml",
                    help="results3/<format-dir>/ — which target this run belongs under "
                         "(default 'xml' for the libxml2 campaign; pass e.g. 'ical' when "
                         "using --campaign-root/--target for a different target, so its runs "
                         "don't land in the xml/ folder).")
    ap.add_argument("--out", type=Path, default=None, help="output dir (overrides --label/--tag/--format-dir)")
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

    # --seed-selector's argparse default is the sentinel None (not "default"), so
    # positive-feedback's own implicit default ("coverage") only applies when the
    # user hasn't explicitly passed --seed-selector. MUST reassign into
    # args.seed_selector itself, not a new local — it's read in 3 more places below
    # (label derivation, selector construction, meta/extra_summary) that all assume
    # a resolved string, never None.
    args.seed_selector = args.seed_selector or ("coverage" if args.positive_feedback else "default")

    # --good-seeds-first-round-only is a negative-only-mode toggle; not a real use
    # case combined with --positive-feedback (which already replaces the good-seed
    # source every call, first round included) — same guard style as the
    # --no-feedback/--stage2-neg-hypothesis pair below.
    if args.positive_feedback and args.good_seeds_first_round_only:
        print("NOTE: --positive-feedback overrides --good-seeds-first-round-only "
              "(that flag is for negative-only mode).")
        args.good_seeds_first_round_only = False

    # --no-feedback wins over an explicit --n-history-failure; everything below
    # reads this local, never args.n_history_failure directly.
    n_history_failure = 0 if args.no_feedback else args.n_history_failure
    if args.no_feedback and args.n_history_failure > 0:
        print("NOTE: --no-feedback overrides --n-history-failure "
              f"{args.n_history_failure} -> 0 (good seeds only).")

    if args.skip_eval and n_history_failure > 0:
        print("NOTE: --skip-eval means no seed is classified, so the 'tried before, avoid' "
              "block will always be empty (scan_cycle_history needs BAD/NO_COVERAGE statuses).")

    if args.no_feedback and args.stage2_neg_hypothesis:
        print("NOTE: --no-feedback overrides --stage2-neg-hypothesis (good seeds only).")
        args.stage2_neg_hypothesis = False
    if args.skip_eval and args.stage2_neg_hypothesis:
        print("NOTE: --skip-eval means no seed is classified, so --stage2-neg-hypothesis "
              "will never find a qualifying group (falls back to good seeds only).")
    if args.skip_eval and args.positive_feedback:
        print("NOTE: --skip-eval means no seed is classified, so --positive-feedback will "
              "never find a qualifying past-AI seed (falls back to top-up every call).")

    if args.label:
        label = args.label
    else:
        # label_encodes_feedback: True for branches whose name already states
        # the negative-feedback status (e.g. stage4_positive_only already says
        # "no negative feedback") — guards the "_nofeedback" suffix below from
        # redundantly doubling up on those.
        label_encodes_feedback = False
        if args.stage2_neg_hypothesis:
            label = "stage2_neg_hypothesis"
        elif args.positive_feedback and args.no_feedback:
            label = "stage4_positive_only"
            label_encodes_feedback = True
        elif args.positive_feedback:
            label = "stage4_both"
        elif args.rotate:
            label = "stage3_rotation"
        elif args.seed_selector == "stage1-jaccard":
            # A Stage 1 sibling (same ranking key, different de-dup
            # mechanism), not a Stage 2 arm — must not fall into the generic
            # stage2_<selector> branch below, same reasoning as
            # stage2_neg_hypothesis's own hand-naming above.
            label = "stage1_jaccard"
        elif args.seed_selector != "default":
            label = f"stage2_{args.seed_selector.replace('-', '_')}"
        else:
            label = "stage1_raw"
        if args.no_feedback and not label_encodes_feedback:
            label += "_nofeedback"
        if args.positive_feedback and args.seed_selector != "coverage":
            # Without this, --positive-feedback alone (topup="coverage") and
            # --positive-feedback --seed-selector rare-coverage would both
            # auto-label "stage4_both" and share _next_run_number's sequence
            # and folder — silently mixing non-comparable runs.
            label += f"_topup-{args.seed_selector.replace('-', '_')}"
        if args.good_seeds_first_round_only:
            label += "_bootstrap1"
    model = args.model or DEFAULT_MODEL[args.provider]
    tag = args.tag or model_tag(args.provider, model)
    if args.out:
        out_dir = args.out
    else:
        # One folder per arm (label), then a fresh numbered run subfolder
        # inside it each invocation — so repeat runs of the same arm (e.g.
        # to get multiple 75-seed batches for later averaging/significance
        # testing) never overlay a previous run's output. Grouped under
        # --format-dir first so different targets (xml/ical/...) never share
        # a label's run-numbering or results tree.
        group_dir = HERE / "results3" / args.format_dir / label
        run_n = _next_run_number(group_dir, label)
        out_dir = group_dir / f"{label}_run_{run_n}_{tag}"
    temperature = None if args.no_temperature else args.temperature
    caller = ProviderCaller(args.provider, model, temperature, args.env_file)
    _memoise_scan_queue()

    # Good-seed selection strategy (Stage 2). Built ONCE here, not inside
    # prompt_fn, so make_coverage_selector's afl-showmap edge cache is shared
    # across all calls (the frozen queue is showmap'd once, then reused). Also
    # doubles as --positive-feedback's TOP-UP selector (see stage4_positive_
    # feedback.py) — its own wrapper is built fresh per call, inside prompt_fn,
    # but the underlying topup_selector built here is reused unchanged.
    # fallback_log: every time one of the 4 coverage-aware selectors (or
    # positive-feedback) gives up on its own logic and defers/settles, that
    # gets appended here (and printed live to stderr) — see
    # select_seeds_by_coverage._log_fallback. Lives in out_dir so it's found
    # alongside this run's own results, not lost in a shared/rotating location.
    fallback_log = out_dir / "selector_fallback.log"
    # call_ctx: the seed_selector(candidates, n_seeds) contract assemble_prompt
    # calls has no call-index slot, so this mutable dict is how a fallback log
    # line finds out which call (prompts/call_NN.json) it happened during —
    # prompt_fn below updates call_ctx["call"] right before each
    # assemble_prompt call, and _log_fallback reads it at fallback time.
    call_ctx = {"call": None}
    # edge_cache: shared across whichever coverage-family factory fires below
    # AND positive-feedback's own edge computation (see prompt_fn) — content-
    # hash-keyed, so sharing it means a top-up candidate never gets
    # afl-showmap'd twice (once for topup_selector's own ranking, once for
    # positive-feedback's Jaccard-vs-AI-seed check).
    edge_cache: dict = {}
    if args.seed_selector == "default":
        seed_selector = bc.select_diverse_seeds
    elif args.seed_selector == "coverage":
        seed_selector = make_coverage_selector(
            args.target, args.target_args.split(), afl_showmap_bin=args.afl_showmap_bin,
            edge_cache=edge_cache, log_path=fallback_log, call_ctx=call_ctx)
    elif args.seed_selector == "coverage-per-byte":
        seed_selector = make_coverage_per_byte_selector(
            args.target, args.target_args.split(), afl_showmap_bin=args.afl_showmap_bin,
            edge_cache=edge_cache, log_path=fallback_log, call_ctx=call_ctx)
    elif args.seed_selector == "rare-coverage":
        seed_selector = make_rare_coverage_selector(
            args.target, args.target_args.split(), afl_showmap_bin=args.afl_showmap_bin,
            edge_cache=edge_cache, log_path=fallback_log, call_ctx=call_ctx)
    elif args.seed_selector == "stage1-jaccard":
        seed_selector = make_stage1_jaccard_selector(
            args.target, args.target_args.split(), afl_showmap_bin=args.afl_showmap_bin,
            edge_cache=edge_cache, log_path=fallback_log, call_ctx=call_ctx)
    elif args.seed_selector == "high-coverage":
        seed_selector = high_coverage_selector
    else:
        seed_selector = no_seed_selector
    topup_selector = seed_selector  # the name prompt_fn's positive-feedback wrapping uses

    # Stage 3 (--rotate): same 15x5 batch shape as Stage 1 — only the closing
    # instruction changes, cycling per call (call 0 -> generate-new x5, call 1 ->
    # mutate-existing x5, call 2 -> semantic-equiv x5, call 3 -> generate-new x5,
    # ...). Without --rotate this is byte-for-byte the Stage 1 arm.
    def prompt_fn(call_index, history_dir):
        call_ctx["call"] = call_index  # see call_ctx's own comment above
        closing = strategies.rotate(call_index) if args.rotate else None

        # Stage 4 (--positive-feedback): wrap topup_selector fresh EVERY call —
        # not built once above like topup_selector itself — because it needs
        # history_dir, itself a per-call value (None on call 0; batch_harness's
        # real _history/ dir from call 1 on). See
        # stage4_positive_feedback.make_positive_feedback_selector's own
        # docstring for why this can't be hoisted out of prompt_fn.
        call_seed_selector = (
            stage4_positive_feedback.make_positive_feedback_selector(
                history_dir, topup_selector,
                target_binary=args.target, target_args=args.target_args.split(),
                afl_showmap_bin=args.afl_showmap_bin,
                jaccard_max=args.positive_feedback_jaccard_max,
                topup_buffer=args.positive_feedback_topup_buffer,
                edge_cache=edge_cache, log_path=fallback_log, call_ctx=call_ctx,
            )
            if args.positive_feedback else topup_selector
        )

        # --good-seeds-first-round-only: the usual --n-seeds good-seed examples
        # on call 0 only, zero for every later call (just the negative "avoid"
        # block from then on). Every seed_selector already returns [] for
        # n_seeds=0 (its very first loop check), and assemble_prompt only
        # emits the "Reached new coverage:" section when chosen_seeds is
        # non-empty — no build_context.py changes needed for this.
        n_seeds_this_call = (
            args.n_seeds if (call_index == 0 or not args.good_seeds_first_round_only) else 0
        )

        history_override = None
        if args.stage2_neg_hypothesis:
            # [] (not None) means "deliberately empty" — no history yet (call 0)
            # or the pool has no qualifying group yet.
            history_override = []
            if history_dir is not None:
                failures = bc.scan_cycle_history(Path(history_dir))
                groups = stage2_neg_hypothesis.select_negative_groups(
                    failures,
                    max_per_group=args.neg_hypothesis_group_size,
                    min_group_size=args.neg_hypothesis_min_group_size,
                    jaccard_min=args.neg_hypothesis_jaccard_min,
                )
                if groups:
                    groups_with_hyps = []
                    for gi, group in enumerate(groups, 1):
                        res = stage2_neg_hypothesis.request_group_hypothesis(
                            group["members"], args.seed_kind, args.fmt, caller,
                            max_chars=args.neg_hypothesis_seed_max_chars)
                        hyp = res["text"]
                        history_override.append((group["label"], hyp))
                        groups_with_hyps.append((group, hyp))

                        # Mirror the main call's prompts/<->responses/ split
                        # (see prompts_to_text.py: it only renders "system"/
                        # "user" out of a prompts/*.json, so the response
                        # belongs in its own responses/*.txt, not bundled in).
                        stem = f"call_{call_index:02d}_hyp{gi}"
                        prompt_record = {
                            "call": call_index, "group": gi, "label": group["label"],
                            "provider": caller.provider, "model": res["model"] or caller.model,
                            "temperature": caller.temperature,
                            "n_members": len(group["members"]),
                            "system": stage2_neg_hypothesis.build_neg_hypothesis_system(args.fmt),
                            "user": stage2_neg_hypothesis.render_group_for_hypothesis(
                                group["members"], args.seed_kind,
                                max_chars=args.neg_hypothesis_seed_max_chars),
                            "input_tokens": res["input_tokens"], "output_tokens": res["output_tokens"],
                            "finish_reason": res["finish_reason"], "attempt_count": res["attempt_count"],
                            "latency_s": res["latency_s"],
                            "ts": datetime.now(timezone.utc).isoformat(),
                        }
                        (out_dir / "prompts" / f"{stem}.json").write_text(
                            json.dumps(prompt_record, indent=2))
                        (out_dir / "responses" / f"{stem}.txt").write_text(hyp)

                        with (out_dir / "neg_hypothesis_calls.jsonl").open("a") as f:
                            f.write(json.dumps({
                                **{k: v for k, v in prompt_record.items() if k not in ("system", "user")},
                                "prompt_file": f"prompts/{stem}.json",
                                "response_file": f"responses/{stem}.txt",
                            }) + "\n")
                    stage2_neg_hypothesis.append_neg_hypothesis_log(
                        history_dir, call_index, groups_with_hyps)

        p = bc.assemble_prompt(
            args.campaign_root, args.instance, args.fmt,
            n_seeds_this_call, args.seeds_per_call,
            asan_hint=not args.no_asan_hint, fence_lang=args.fence_lang,
            seed_kind=args.seed_kind,
            run_dir=history_dir, n_history_failure=n_history_failure,
            closing_instruction=closing,
            seed_selector=call_seed_selector,
            history_examples_override=history_override,
        )
        return {"system": p["system"], "user": p["user"], "meta": {
            "n_seeds": n_seeds_this_call, "n_history_failure": n_history_failure,
            "feedback": not args.no_feedback,
            "seed_selector": args.seed_selector,
            "positive_feedback": args.positive_feedback,
            "positive_feedback_jaccard_max": args.positive_feedback_jaccard_max,
            "good_seeds_first_round_only": args.good_seeds_first_round_only,
            "used_history": history_dir is not None,
            "campaign_root": str(args.campaign_root), "instance": args.instance,
            "asan_hint": not args.no_asan_hint,
            "strategy": strategies.name_for(call_index) if args.rotate else "single-batch",
            "neg_hypothesis": args.stage2_neg_hypothesis,
        }}

    run_batch(BatchConfig(
        out_dir=out_dir, title=f"build_context {label} ({tag})", arm=f"{label}_{tag}",
        calls=args.calls, seeds_per_call=args.seeds_per_call, seed_kind=args.seed_kind,
        fmt=args.fmt, caller=caller, prompt_fn=prompt_fn,
        uses_history=(n_history_failure > 0) or args.stage2_neg_hypothesis or args.positive_feedback,
        do_eval=not args.skip_eval, campaign_root=args.campaign_root,
        instances=[s.strip() for s in args.instances.split(",") if s.strip()],
        target=args.target, target_args=args.target_args.split(),
        showmap_bin=args.afl_showmap_bin, reuse_baseline=args.reuse_baseline,
        extra_summary={
            "model_tag": tag,
            "n_seeds": args.n_seeds, "n_history_failure": n_history_failure,
            "feedback": not args.no_feedback,
            "seed_selector": args.seed_selector,
            "positive_feedback": args.positive_feedback,
            "positive_feedback_jaccard_max": args.positive_feedback_jaccard_max,
            "positive_feedback_topup_buffer": args.positive_feedback_topup_buffer,
            "good_seeds_first_round_only": args.good_seeds_first_round_only,
            "instance": args.instance,
            "asan_hint": not args.no_asan_hint,
            "strategy": "rotation" if args.rotate else "single-batch",
            "neg_hypothesis": args.stage2_neg_hypothesis,
            "neg_hypothesis_group_size": args.neg_hypothesis_group_size,
            "neg_hypothesis_min_group_size": args.neg_hypothesis_min_group_size,
            "neg_hypothesis_jaccard_min": args.neg_hypothesis_jaccard_min,
            "neg_hypothesis_seed_max_chars": args.neg_hypothesis_seed_max_chars,
        },
    ))


if __name__ == "__main__":
    main()
