# AgentAFL Prompt-Design Ablation Study — Implementation Plan

Handoff spec for implementing the prompt-design ablation study discussed with
Claude (chat), covering everything from the minimal-prompt redesign of
`build_context.py` through to the final baseline comparison. Read this whole
file before writing code — later stages depend on decisions made in earlier
sections.

## 1. Research question

Does deliberately engineering AgentAFL's seed-generation prompt (distilled
failure feedback, coverage-aware seed selection, rotating generation
strategies) produce more useful seeds than a no-context baseline, and which
of those three ingredients actually earns its place?

Model: **Claude Haiku 4.5** for every generation call in this study (chosen
for cost/speed; see Section 7 for the generalization caveat this creates and
how to disclose it). Target: libxml2/xmllint only for this study — no second
target has been chosen yet, and there's no reason to block this ablation work
on that decision.

## 2. What's already established (why each stage exists)

- **Minimal-prompt design**: `build_context.py` was already rewritten from a
  verbose, campaign-stats-heavy prompt to a short one modeled on ChatAFL's
  plateau-escape template, Fuzz4All's per-iteration prompt, and CodaMOSA's
  single-example prompt — all short instruction + a handful of real examples,
  no restated mechanics. Measured on identical fixture data, this cut prompt
  size from 3,628 to 818 characters. This version is the "current best"
  configuration every stage below builds on.
- **Stage 1 (distillation)**: Fuzz4All's ablation shows a distilled prompt
  beats a raw one on both coverage and validity. CodaMOSA found a raw
  example seed sometimes gets imitated too literally. FuzzGPT and Reflexion
  both found that having a model articulate *why* something failed, not just
  show the failure, is the more reliable technique. None of these distill a
  *failure* quite the way planned here — this is a genuine adaptation of
  published technique, not a replication, and should be described that way
  in the write-up.
- **Stage 2 (seed selection)**: Truzz/VUzzer validate "more new edges predicts
  deeper code" for classical seed scheduling. AFLFast validates the opposite
  emphasis — rewarding rarely-hit paths. These are genuinely different
  philosophies, not two names for the same thing (see
  `stage2_seed_selection/select_seeds_by_coverage.py`'s docstring for a worked
  example where they disagree), which is exactly why both are worth testing
  rather than assuming one is obviously right.
- **Stage 3 (strategy rotation)**: Fuzz4All's ablation found that rotating
  generate-new / mutate-existing / semantic-equiv instructions across calls
  beat a single fixed instruction, specifically by reducing duplicate output
  over a long run.
- **Stage 0 (baseline)**: added per this conversation as the floor reference
  for Stage 4 — format name only, nothing else.

## 3. The scoring metric (used identically in every stage — do not vary this)

Two numbers per batch, computed the same way regardless of which arm
produced the batch:

- **Primary — total distinct new edges**: the union of every candidate
  seed's new-edge set (edges not already in that trigger's frozen baseline
  coverage). This is the headline number for comparing arms: it doesn't
  care how many seeds contributed, only how much real coverage the batch
  bought.
- **Secondary — distinct contributors**: process candidates in generation
  order; a candidate counts as a contributor only if it adds an edge not
  already claimed by an earlier candidate in the *same* batch. Two seeds
  that both find the same one new edge count as **one** contributor, not
  two — this was an explicit correction from the "just count seeds with any
  new coverage" version, since that inflates a batch that produces several
  redundant near-duplicates.

Implemented in `shared/score_batch.py`, with a self-test that verifies the
dedup logic without needing real coverage data. **Every stage must call this
same module** — do not let an individual stage's runner reimplement scoring,
or a difference in scoring logic becomes an invisible confound.

## 4. Known open dependency — resolve this first

`shared/score_batch.py`'s `get_seed_edges()` and
`stage2_seed_selection/select_seeds_by_coverage.py`'s `get_edge_ids()` are
both stubs that raise `NotImplementedError`. You already have something that
computes per-seed edge coverage ("somewhere in other files, through some afl
function") — locate it and wire **both** stubs to the **same** underlying
call (ideally one imports the other, so they can't silently drift apart).
Almost certainly a thin wrapper around `afl-showmap`; your existing
`evaluate_seeds.py` may already do most of this internally and just need its
per-seed edge-set computation exposed as its own callable rather than
returning only an aggregate usefulness number. Check there before writing
anything new.

Nothing else in this plan is blocked by this — Stage 0, Stage 1, and the
harness plumbing (LLM calls, response parsing) are already real, tested code
(see Section 8) and can be exercised today. Only real scoring and Stage 2's
selection logic wait on this.

## 5. Timeline (stages build on each other — lock in a winner before moving on)

This sequencing matches how CodaMOSA, FuzzGPT, and Fuzz4All each ran their
own ablations: one axis at a time, carrying the winner forward, rather than
testing every combination at once.

### Stage 0 — Baseline
Generate batches with **only** the file format named (`stage0_baseline/`).
No dependencies beyond Section 4 being resolved for scoring. Run this early
— it's cheap and gives a floor number to refer back to throughout, even
though the *comparison* against it only happens in Stage 4.

### Stage 0.5 — just good and bad seeds (2 of each) 
Bad seeds are 2 selected from previous AI outputs

### Stage 1 — Distillation (raw bad seed vs. AI-distilled explanation)
Two arms, everything else held at the current default (2 good seeds via
`select_diverse_seeds`, current strategy line):
- `stage1_raw` — today's behavior, unchanged.
- `stage1_distilled` — `stage1_distillation/distill_seed.py`'s renderer:
  one extra Haiku call per bad seed shown, asking for a one-sentence guess
  at why it failed, shown instead of the raw bytes.

Adds +2 Haiku calls per prompt at the default `n_history_failure=2` — cheap
with Haiku, but real; factor into the cost estimate in Section 7. **Lock in
the winner before Stage 2.**

### Stage 2 — Seed selection (A/B/C)
Three arms, Stage 1's winning bad-seed treatment carried forward, only the
good-seed selection varies (`stage2_seed_selection/select_seeds_by_coverage.py`):
- `stage2_high_coverage` — most total edges.
- `stage2_rare_coverage` — most corpus-private edges (see Section 2's note on
  why this isn't the same test as high-coverage).
- `stage2_no_seed` — no good-seed example at all, the control.

**Blocked on Section 4.** If `stage2_no_seed` wins outright, that's a real
and important result — it would mean the good-seed-example idea (idea 1 from
the very start of this plan) doesn't earn its place, not a failure of the
test. Flag this explicitly rather than burying it if it happens.

### Stage 3 — Strategy rotation
Stage 1 + Stage 2 winners carried forward. One arm generates the full batch
in a single call (current behavior); the other (`stage3_rotation`) makes
`n_generate` separate single-seed calls, rotating generate-new /
mutate-existing / semantic-equiv per call
(`stage3_strategy_rotation/strategies.py`). This arm is already wired into
`run_ablation.py`'s special-cased branch, since it's structurally different
(many small calls, not one big one) from every other arm.

### Stage 4 — Final comparison
Assemble the winners of Stages 1–3 into one final configuration and run it
against Stage 0's baseline on the same triggers, same metric. This is the
number that answers the original research question — everything before this
is which ingredients earned their way into it.

## 6. Trigger events (capture once, reuse across every arm)

A "trigger" is one frozen snapshot: a campaign state (queue, cycle history)
plus a frozen baseline edge set captured at that moment. **Capture triggers
once, before running any arm**, and reuse the identical trigger (identical
baseline edge set) across every arm being compared on it — this is what
makes the arms' scores comparable at all. Re-deriving "current corpus
coverage" live and separately per arm risks two arms being scored against
subtly different baselines if anything about the corpus changed between
calls.

Proposed default, adjust freely: **8 trigger events × 3 repeats per arm**
per stage. More repeats cost only Haiku calls (cheap); the real constraint
is having 8 genuinely different plateau moments to draw from, not compute
budget.

## 7. Parameters to lock before running (proposed defaults)

- **Model**: `claude-haiku-4-5-20251001`, pinned in `shared/llm_client.py`.
  Disclose this as a scope decision in the write-up — results are "best
  prompt design for Haiku on this task," not a general claim, per the
  Haiku-vs-Sonnet discussion earlier in this project.
- **Temperature**: 0.8 — CodaMOSA's single most robust lever, more impactful
  than most of the content choices they tested.
- **Batch size / call structure**: keep individual calls small (1–2 seeds)
  rather than one big batch in one continuous response — within-response
  batches risk quality/diversity drift over the response the way Fuzz4All's
  own multi-sample design avoids by drawing independent samples instead.
  Stage 3 already does this structurally (see above); consider applying the
  same to Stages 0–2 rather than treating it as unique to Stage 3.
- **Significance testing**: don't compare arms on mean/median alone — use
  Mann-Whitney U (as CodaMOSA, Fuzz4All, and FuzzGPT all did) before calling
  a difference real. `run_ablation.py summarize` currently only prints
  mean/median as a first look; add the U-test before drawing conclusions.

## 8. File manifest — status of what's built vs. what's left

| File | Status | Notes |
|---|---|---|
| `build_context.py` | **Done, tested** | Extended with 3 optional hooks (`seed_selector`, `bad_seed_renderer`, `closing_instruction`) — defaults reproduce today's exact behavior; regression-tested identical output. |
| `shared/llm_client.py` | **Done, tested** | Generic Haiku wrapper, retries, imports cleanly against the real `anthropic` SDK. Needs `ANTHROPIC_API_KEY` in the environment to actually call. |
| `shared/parse_seeds.py` | **Done, tested** | Fenced-block parser, text and binary modes, tested against synthetic responses including malformed input. |
| `shared/score_batch.py` | **Logic done, one stub** | Dedup/scoring logic self-tests correctly with mocked edge data. `get_seed_edges()` needs wiring — see Section 4. |
| `stage0_baseline/build_context_baseline.py` | **Done, tested** | No unknowns — standalone, doesn't import `build_context.py` on purpose. |
| `stage1_distillation/distill_seed.py` | **Done, needs API key to run live** | Distillation prompt written and grounded (see Section 2); imports cleanly; untested against a real model response (needs your key to verify tone/length in practice). |
| `stage2_seed_selection/select_seeds_by_coverage.py` | **Selection logic done, one stub** | All three selectors self-test correctly with mocked edge data, including a worked example where high- and rare-coverage genuinely disagree. `get_edge_ids()` needs wiring — see Section 4 (same underlying call as `score_batch.py`'s stub — wire both together). |
| `stage3_strategy_rotation/strategies.py` | **Done, tested** | Three instruction strings + rotation helper. |
| `run_ablation.py` | **Orchestration done, tested end-to-end with mocks** | Full loop (build prompt → call LLM → parse → score → log) verified with the LLM call and edge lookup mocked out — real run is blocked only on Section 4 and a real API key, not on any remaining logic bug. |

## 9. Suggested build order for Claude Code

1. Resolve Section 4 (locate and wire the existing edge-coverage tool into
   both stubs).
2. Set `ANTHROPIC_API_KEY`; run `python3 shared/llm_client.py` and
   `python3 stage1_distillation/distill_seed.py` once each as smoke tests.
3. Build the trigger-capture step (freeze `baseline_edges_file` per trigger —
   not included here, since it depends on exactly how your campaign directory
   is laid out day-to-day).
4. Run Stage 0 + Stage 1 (`python3 run_ablation.py run --arms
   stage0_baseline,stage1_raw,stage1_distilled --triggers triggers.json
   --repeats 3 --out results.jsonl`), then `summarize`.
5. Lock Stage 1's winner, proceed to Stage 2, then Stage 3, then Stage 4 —
   each time carrying the previous stage's winner forward per Section 5.
