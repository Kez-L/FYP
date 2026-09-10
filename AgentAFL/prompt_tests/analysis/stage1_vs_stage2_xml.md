# Why Stage 2 (coverage-selected seeds) underperforms Stage 1 for XML

*Analysis of the `prompt_tests/` ablation results, with a literature review on prompt /
few-shot-example sizing and fuzzing seed selection. Written 2026-09-10.*

---

## 1. Summary / verdict

For the XML target (libxml2 `xmllint --noout`, Haiku 4.5), **Stage 2 `stage2_coverage`
scored 57 total distinct new edges vs Stage 1 `stage1_raw`'s 83** — a 31 % drop on
`PLAN.md` §3's primary metric — and 6 distinct in-batch contributors vs 9 on the
secondary metric.

The cause is a **selection → imitation → mode-collapse** chain:

1. `make_coverage_selector` ranks few-shot candidates by **absolute `afl-showmap` edge
   count**, with byte size only a third-order tiebreak and the size band still topping out
   at 3000 B. Within any size band, bigger files hit more edges, so this reliably promotes
   AFL's **block-extension havoc mutants** — multi-KB files that are mostly repeated bytes,
   `&#160;` spam and U+FFFD walls, which "cover" a lot only because they brute-force parser
   loops.
2. Those two junk mutants (2 758 and 2 022 chars, shown truncated at 2 000) became the
   prompt's worked examples. Haiku imitated them.
3. The batch collapsed onto one shallow `DTD`+`ATTLIST` pattern: **13 of 50 Stage 2 seeds
   emit the *identical* 2-edge set**, and ~8 more groups are permutations of a single
   ~19-edge path. More seeds "hit" something (27 vs 18) but the *union* of what they reach
   is smaller.

Stage 1's selector (`select_diverse_seeds`) ranks by closeness to a 400 B target and
filters those mutants out; its examples were ~450–500 B and taught legible generative
patterns (`ENTITY` / parameter-entity tricks), so its batch spread across genuinely
different parser regions.

Two forces the user already suspected are both real and both measurable here: the
coverage-selected **example seeds are too big** (mean example ≈ 2.4 KB vs ≈ 0.5 KB) and
the resulting **prompt is too big** (mean 7.4 KB / ~2 380 tok per call vs 2.4 KB /
~835 tok). The literature (§4) supports a third: the selector optimises the example for a
quantity (*absolute* coverage) that is not what the batch is scored on (*new* coverage).

**Caveats, stated up front.** This is one 50-seed batch per arm, Haiku-only for the
Stage 2 XML point, no repeats, no significance test. `PLAN.md` §7 itself calls for
Mann-Whitney U over ≥3 repeats × 8 triggers before treating an arm difference as real.
Treat this as a **mechanism diagnosis**, not a significant result — §6 recommends the
re-run.

---

## 2. The measured result

Source: `prompt_tests/results/*/summary_*.json` and `contributors_*.md`. Reproduction
script in Appendix A.

### 2.1 Headline numbers (XML / libxml2 / Haiku 4.5, corpus baseline 5 595 edges)

| metric | Stage 0 baseline | **Stage 1 `raw`/diverse** | **Stage 2 `coverage`** | Stage 3 `rotation` |
|---|---:|---:|---:|---:|
| **Total distinct new edges** — primary | 0 | **83** | **57** | 55 |
| **Distinct in-batch contributors** — secondary | 0 | **9** | **6** | 6 |
| Seeds with ≥1 new edge | 0 | 18 | 27 | 9 |
| `new_coverage_rate` | 0.00 | 0.36 | 0.54 | 0.18 |
| Input tokens (10 calls) | 540 | 8 350 | **23 806** | 7 839 |
| Output tokens (10 calls) | 4 848 | 6 377 | **14 427** | 6 406 |
| Mean generated-seed size | 249 B | **298 B** | **799 B** | 273 B |
| Max generated-seed size | 464 B | 485 B | **2 151 B** | 425 B |
| Mean assembled-prompt size | 477 B | **2 410 B** | **7 411 B** | 2 333 B |

Stage 2 is worse on **both** scored metrics. Its two better-looking numbers —
seeds-with-new-coverage (27 vs 18) and `new_coverage_rate` (0.54 vs 0.36) — are precisely
the inflation `PLAN.md` §3 designed the contributor metric to discount: *"Two seeds that
both find the same one new edge count as one contributor, not two — since that inflates a
batch that produces several redundant near-duplicates."*

For reference, `stage1_raw` on gpt-5-4-mini scored 37 / 7 — the same ordering (diverse
Stage 1 above the Stage 0 floor of 0). There is **no `stage2_coverage` run for
gpt-5-4-mini on XML**, so the Stage 1 vs Stage 2 contrast is Haiku-only.

### 2.2 The contributor collapse (the actual "why")

From `contributors_*.md`, seeds grouped by the exact new-edge set they produce:

| | Stage 1 `raw` | Stage 2 `coverage` |
|---|---|---|
| number of distinct new-edge groups | 13 | 14 |
| seeds in the largest group | **3** | **13** |
| group-size distribution | `[3,2,2,2,1,1,1,1,1,1,1,1,1]` | `[13,2,1,1,1,1,1,1,1,1,1,1,1,1]` |

- **Stage 2, Group 1: 13 of 50 seeds** all emit the identical 2-edge set `{15658, 50094}`
  — a shallow "DTD with `ELEMENT` decls + `ATTLIST` with an enumerated type + a couple of
  `ENTITY`s + a short matching body". Inspected: `seed_002/003/010/012/013/014/017/020/
  021/040/042/045/049` are surface-varied paraphrases of one another.
- Groups 3–13 are near-permutations of one ~19-edge `ATTLIST`/entity path
  (`5409, 9722, 15515, 15520, 15521, 15527, 15528, 15530, 15541, 15542, 15545 …`).
- **Stage 1** has no group larger than 3 and reaches regions Stage 2 never touches:
  - `seed_026` (485 B): `NOTATION` + `NDATA` unparsed-entity + `ID`/`IDREF` → **50 new
    edges**.
  - `seed_036` (229 B): entity-as-markup `<!ENTITY start "<item id='test1'>">` → 15 new
    edges in the `10107 / 13005 / 30066 …` region — **no other seed in either batch
    reached it**.
  - `seed_013` (32 edges), plus groups touching different `ATTLIST` / entity combinations.

So Stage 2's "27 seeds with new coverage" is illusory breadth: half of that is one
repeated idea, and most of the rest is a second repeated idea. Its true reach (57) is
below Stage 1's (83) *because* Stage 1's 18 hits went to more different places.

### 2.3 The examples that were fed in

`results/stage2_coverage_haiku/prompts/call_00.json`, the two "Reached new coverage"
examples:

- **example 1** — `[truncated, 2758 chars total]`. A `<!ATTLIST e a000 (0) '0' a001 (0)
  '0' …>` repeated ~130 times, interrupted by `a02222222…` (hundreds of literal `2`s — an
  AFL block-extension mutant), `���…` replacement-character walls 200+ chars
  long, and garbage-spliced fragments.
- **example 2** — `[truncated, 2022 chars total]`. A `&#160;&#160;&#160;…`
  character-reference wall, 80-space indentation slabs for many lines, and the same
  `<!ATTLIST doc defatt (0|1) "0" …>` fragment copy-pasted 4+ times.

`results/stage1_raw_haiku/prompts/call_00.json`, for contrast: two ~450–500 B files, one a
recognisable `<!DOCTYPE>` with `<!ENTITY a …>` / `<!ENTITY b "&a;">` expansion, the other
a `<!ENTITY % z '…%z; %z;…'>` parameter-entity recursion. Fuzz-mangled but legible, and
each teaches a *pattern with room to vary*.

### 2.4 Why the coverage selector produced those

`select_seeds_by_coverage.make_coverage_selector` (see `prompt_tests/select_seeds_by_coverage.py`):

```python
banded = [c for c in candidates if FEWSHOT_MIN_SIZE <= c[2] <= FEWSHOT_MAX_SIZE]  # 32..3000
pool = banded or candidates
...
scored.sort(key=lambda t: (-len(t[1]), -t[2], t[3]))   # edges DESC, printable DESC, size ASC
```

Both selectors share the **same 32–3000 B band** and the same `banded or candidates`
fallback. The difference is purely the ranking *inside* the band:

- `select_diverse_seeds`: `key=(abs(size - 400), -ratio)` → picks the **middle** of the
  band (~400 B), explicitly to dodge "the fuzzer's giant block-extension mutants
  (repeated-character walls, all +cov)" (its own docstring).
- `make_coverage_selector`: `key=(-edges, -ratio, size)` → because edge count rises with
  size within a band, this picks the **top** of the band (~3 000 B). Size only breaks
  ties between candidates with *equal* edge counts, which essentially never happens.

The `jaccard_max = 0.7` edge-set dedup is a weak diversity gate: two `ATTLIST`-heavy
mutants sharing 65 % of their edges both pass, so the model can see one theme twice.

---

## 3. Hypothesis (ranked by confidence)

1. **Selection surfaced degenerate maximal-coverage mutants.** (Strong — shown directly
   in §2.3–2.4.) Ranking by absolute edge count in a 3 KB band makes semi-garbage
   block-extension seeds win. They carry near-zero generative signal and are shown
   truncated, so the model is being taught "high-coverage XML looks like a wall of
   repeated attributes and character references."

2. **The model imitated them → semantic mode collapse.** (Strong — §2.2.) A dense,
   elaborate concrete exemplar anchors the output distribution harder than a compact one.
   13/50 outputs converge on one idea; the batch's coverage *union* falls even though more
   individual seeds hit something. Matches CodaMOSA's "raw example seed sometimes gets
   imitated too literally" and the ICL demonstration-bias literature (§4B).

3. **Objective mismatch between the selector and the score.** (Strong — structural.) The
   selector maximises the example's *absolute* edge count; the batch is scored on *new*
   edges over a frozen 5 595-edge baseline. A maximal-coverage example is mostly made of
   *already-covered* edges, so faithful imitation reproduces already-covered behaviour —
   visible as Group 1's 13 seeds contributing only 2 *new* edges between them.

4. **Prompt bloat pushed each call into the input-length degradation band.** (Plausible,
   supported by literature not by an ablation here.) 7.4 KB / ~2 380 tok per call vs
   2.4 KB / ~835 tok (2.85×). The "Generate N new files, structurally different from all of
   the above" instruction now sits *after* ~2 KB of dense noise. Levy et al. 2024 measure
   reasoning degradation from ~500 tokens; Liu et al. 2023 show instructions/material in
   the middle of a long context get less weight (§4A).

5. **Output bloat, downstream cost.** (Certain but secondary.) Stage 2 seeds average
   799 B vs 298 B (2.7×); max 2 151 B vs 485 B. Even ignoring coverage, AFL/AFL++ guidance
   is to keep seeds < 1 KB for execution throughput (§4C), and the large seeds here were
   *not* the distinct contributors anyway — they were the redundant ones.

Contributing but not sufficient alone: the **weak Jaccard-0.7 dedup** let the two
examples be near-identical themes, compounding (2).

---

## 4. Literature review

Grouped by theme. Each entry: what it says, then how it bears on this result. Where only
an abstract/summary was available that is noted; venue / arXiv id is given so every claim
can be checked.

### 4A. Prompt / input-length effects on LLM output quality

- **Liu, Lin, Hewitt, Paranjape, Bevilacqua, Petroni, Liang — "Lost in the Middle: How
  Language Models Use Long Contexts." TACL 2024 (arXiv:2307.03172).**
  Performance is highest when relevant information is at the very start or very end of the
  input and sags in the middle — a U-shaped curve, with the middle-vs-end gap exceeding
  ~30 % on multi-document QA. Replicated across GPT-3.5/4, Claude-1.3 and open models.
  *Bearing:* Stage 2's closing instruction ("structurally different") trails ~2 KB of
  dense example text, the position this paper shows is weighted least.

- **Levy, Jacoby, Goldberg — "Same Task, More Tokens: the Impact of Input Length on the
  Reasoning Performance of Large Language Models." ACL 2024 (arXiv:2402.14848).**
  Holding the task fixed and padding the input, accuracy falls from ~0.92 to ~0.68 by
  ~3 000 tokens, with degradation visible from ~500 tokens — far below any context limit.
  Crucially, *irrelevant* padding hurts even though the task is unchanged, and unrelated
  padding hurts more than task-similar padding. Tested on GPT-4, GPT-3.5, Gemini Pro,
  Mistral Medium, Mixtral 8x7B.
  *Bearing:* directly models Stage 2 — the extra ~1 550 tokens per call is mostly
  low-information repeated-byte padding, the exact case this paper shows is most harmful.

### 4B. In-context learning: how many, and how large, should the examples be

- **Min, Lyu, Holtzman, Artetxe, Lewis, Hajishirzi, Zettlemoyer — "Rethinking the Role of
  Demonstrations: What Makes In-Context Learning Work?" EMNLP 2022 (arXiv:2202.12837).**
  Across 12 models, label *correctness* in demonstrations barely matters; what drives ICL
  is the demonstrations' *format*, *label space*, and *input distribution* — i.e. the
  examples define the region of output space the model operates in.
  *Bearing:* if the two examples define "repetitive `ATTLIST` walls" as the region, that
  is what the batch explores — the §2.2 collapse.

- **Zhao, Wallace, Feng, Klein, Singh — "Calibrate Before Use: Improving Few-Shot
  Performance of Language Models." ICML 2021 (arXiv:2102.09690).**
  Few-shot predictions are biased by surface properties of the prompt: a majority-label
  bias, a recency bias (toward the last example), and a common-token bias (toward strings
  frequent in the examples).
  *Bearing:* a mechanism for over-literal imitation — the model over-reproduces frequent
  surface features of the shown examples (here: long `ATTLIST` enumerations, entity
  boilerplate).

- **Agarwal, Singh, Zhang et al. — "Many-Shot In-Context Learning." NeurIPS 2024
  (arXiv:2404.11018).**
  More demonstrations help but with clearly diminishing returns, and the benefit is
  sensitive to demonstration quality; more context is not monotonically better.
  *Bearing:* adding example *bytes* (Stage 2) is not free; past a point it trades against
  quality/diversity of generation.

- **"When Does Few-Shot Prompting Help? A Systematic Empirical Study of Shot-Count
  Effects…" 2026 (arXiv:2607.22969). (Abstract/summary only.)**
  Reports an "over-prompting" effect: performance is non-monotonic in shot count and the
  optimal number of shots is often well below the maximum the model can hold; longer
  prompts can hurt before the context-window limit because attention spreads too thin.
  *Bearing:* corroborates 4A/4B for the specific "few-shot examples got bigger" change.

- **Chen, Wang, Lin et al. — "Can Few-shot Work in Long-Context? Recycling the Context to
  Generate Demonstrations." 2024 (arXiv:2406.13632). (Abstract/summary only.)**
  Treats demonstration length as competing for the same budget as demonstration count and
  diversity: long individual examples crowd out having more, and more varied, examples.
  *Bearing:* frames the Stage 1 → Stage 2 change as spending the example budget on two big
  homogeneous seeds instead of small varied ones.

*Practical synthesis of 4A–4B:* published LLM-seed/fuzzing systems deliberately keep the
in-prompt example **small and single**: Fuzz4All feeds *one* concise example per iteration
(§4D); CodaMOSA uses a single-example prompt (§4D). AgentAFL's own `build_context.py`
docstring cites exactly these as the design it copied — Stage 2's selector quietly broke
that property.

### 4C. Fuzzing: seed selection, corpus size, and individual seed size

- **Rebert, Cha, Avgerinos, Foote, Warren, Grieco, Brumley — "Optimizing Seed Selection
  for Fuzzing." USENIX Security 2014.**
  Formalises seed selection as choosing a minimal "minset" that covers the same
  instrumentation as a large collection (a set-cover problem). Over ~650 CPU-days / 8
  programs, the choice of selection algorithm significantly changed the number of bugs
  found (240 total). Establishes: a smaller, coverage-preserving seed set is preferable to
  a large raw one.
  *Bearing:* the coverage-per-*seed* idea Stage 2 is reaching for is sound; ranking by
  *absolute* per-file coverage without a size/parsimony term is the classic failure mode
  this line of work exists to avoid.

- **Klees, Ruef, Cooper, Wei, Hicks — "Evaluating Fuzz Testing." CCS 2018.**
  Surveying 32 fuzzing papers: "a fuzzer's performance on the same program can be very
  different depending on what seed is used." Recommends evaluating with more than one seed
  configuration and running enough repeats for a statistical test.
  *Bearing:* justifies both the ablation's existence and this document's central caveat —
  one batch per arm is not enough to call the 83-vs-57 gap significant; re-run with
  repeats + Mann-Whitney U (already in `PLAN.md` §7).

- **Herrera, Gunadi, Magrath, Norrish, Payer, Hosking — "Seed Selection for Successful
  Fuzzing." ISSTA 2021.**
  ~33 CPU-years across six seed-selection strategies. Minimised corpora **beat singleton,
  empty, and large (thousands-of-files) seed sets** for bug finding. Corpus minimisers
  (afl-cmin, MoonLight, OptiMin) that pick the smallest subset preserving coverage vary
  wildly in output size but consistently outperform the raw corpus; e.g. a 62 726-seed
  corpus reduced to 145 seeds while preserving coverage.
  *Bearing:* the strongest external statement that "more raw coverage in the seed material"
  ≠ "better fuzzing" — smaller and non-redundant wins. Stage 2 moved the *examples* in the
  wrong direction on both axes.

- **Zalewski, AFL docs / Fioraldi, Maier, Eißfeldt, Heuse — "AFL++: Combining Incremental
  Steps of Fuzzing Research." USENIX WOOT 2020; AFL `README`/`fuzzing.md`.**
  Explicit guidance: keep seed files small (**"under 1 kB"** is the number in the AFL
  docs) for higher executions/second; use `afl-cmin` to drop functionally redundant files
  from a corpus and `afl-tmin` to shrink each file; "a minimized corpus is always better
  due to the faster iteration rate."
  *Bearing:* Stage 2's 799 B mean / 2 151 B max generated seeds are still under 1 kB on
  average but trending the wrong way, and the *examples* (2–2.8 KB) are well over.

- **"SeedAIchemy: LLM-Driven Seed Corpus Generation for Fuzzing." 2025
  (arXiv:2511.12448).** (HTML full text read.)
  An LLM pipeline that builds seed corpora. States plainly: *"fuzzing performance is best
  when the seed corpus is small because smaller corpora increase execution speed."*
  Filters out files larger than a max size (default 1 MB) "to improve fuzzing efficiency,"
  and when trimming to a file budget it *"preferentially select[s] the … smallest files
  before minimization."* Uses per-feature descriptors to get diversity.
  *Bearing:* a contemporary LLM-seed system encodes "prefer small" and "diversify by
  feature" as hard rules — the opposite of Stage 2's "prefer most edges."

- **"Sow Smarter, Not Harder: Evaluating LLM-Generated Seeds for Fuzzing Critical
  Infrastructure." CRITIS 2025.** (Abstract/summary only.)
  Seven models vs manual seeds across six programs, 20×24 h campaigns each. LLM-generated
  seeds gave +14.8 % code coverage, +56.3 % unique crashes, and reached the first crash
  ~374 % faster than manual seeds — with lower per-seed validity rates.
  *Bearing:* the AgentAFL premise (LLM seeds are worth it) is well supported; the failure
  here is in *which* example steers generation, not in using LLM seeds.

- **Wang, Chen, Wei, Liu — "Skyfire: Data-Driven Seed Generation for Fuzzing." IEEE S&P
  2017.**
  Learns a probabilistic context-sensitive grammar from a sample corpus and samples it to
  generate seeds "with diverse grammar structures while reducing seed redundancy."
  *Bearing:* the objective a good Stage 2 selector should approximate — structural
  diversity with redundancy suppression — not raw coverage maximisation.

### 4D. LLM-based seed / test generation (closest prior art)

- **Xia, Paltenghi, Tian, Pradel, Zhang — "Fuzz4All: Universal Fuzzing with Large Language
  Models." ICSE 2024.**
  Format-agnostic LLM fuzzer. Two design points relevant here: (i) an *autoprompting* step
  picks a *single concise* example input to include, and (ii) it *rotates* generate-new /
  mutate-existing / semantic-equivalent instructions across iterations specifically to
  reduce duplicate output over a long run. Reports LLM inputs with lower validity but
  higher coverage than grammar baselines.
  *Bearing:* AgentAFL's Stage 3 copies the rotation idea; Stage 2 violates the "single
  concise example" idea. Note Stage 3 rotation also scored below Stage 1 here (55 vs 83) —
  see §7.

- **Lemieux, Priya Inala, Lahiri, Sen — "CodaMOSA: Escaping Coverage Plateaus in Test
  Generation with Pre-trained Large Language Models." ICSE 2023.**
  When search-based test generation plateaus, ask the LLM for example tests for
  under-covered functions. Findings AgentAFL's `PLAN.md` cites: temperature is the single
  most robust lever, and a *raw example is sometimes imitated too literally*, so what you
  show matters as much as that you show something.
  *Bearing:* names the Stage 2 mechanism exactly.

- **(Context) FuzzGPT / Reflexion-style feedback**, cited in `PLAN.md` §2: having the
  model articulate *why* a prior attempt failed beats showing the raw failing input. This
  is the Stage 1 `distilled` idea and is orthogonal to the Stage 2 problem, but reinforces
  that raw multi-KB blobs are a poor teaching signal.

### 4E. LLM structured-output reliability by format (XML vs YAML)

- **"StructEval: Benchmarking LLMs' Capabilities to Generate Structural Outputs." 2025
  (arXiv:2505.20139). (Abstract/summary only.)**
  Benchmarks generation of structured formats; large/newer models produce schema-conforming
  XML fairly reliably but, unlike JSON, with **no guarantee** of well-formedness.

- **Practitioner benchmarks + "Are LLMs Ready for TOON?" (arXiv:2601.12014). (Summary
  only.)**
  Consistent reports that **XML is the least token-efficient common structured format
  (~14 % more tokens than formatted JSON, ~114 % more than compact formats)** and that its
  strict closing-tag matching is comparatively error-prone for token-by-token generation;
  YAML's indentation sensitivity is the analogous hazard but its token cost is far lower.
  *Bearing:* a secondary reason XML shows more arm-to-arm variance than YAML — when there
  is coverage headroom, XML output quality (hence new-coverage yield) is more sensitive to
  prompt conditions. It is *not* the primary reason (see §5).

---

## 5. XML vs YAML: the format-agnostic angle

The generation is meant to be format-agnostic; YAML is tested via libyaml. Every libyaml
arm — both models, Stage 0/1/2/3 — floors at **0–1 total new edges**
(`results/libyaml/*/summary_*.json`). The XML/YAML difference in "how much the arm
matters" is **mostly experimental design, not a model-skill gap**:

1. **Target headroom.** libxml2's frozen baseline is **5 595 edges** with many independent
   optional sub-grammars (DTD, `ATTLIST` enumerations, `ENTITY` / parameter-entity,
   `NOTATION` / `NDATA`, namespaces, multiple encodings, XInclude…). libyaml's baseline is
   **1 554 edges** — a much smaller grammar — and the campaign corpus (~3.5–4 K queue
   files) has it close to saturated. With almost no reachable-but-uncovered edges left, no
   seed-generation strategy can separate from the others: the metric has no dynamic range
   on this YAML target, so YAML's flatness says nothing about the model.

2. **The Stage 2 pathology needs big feature-dense seeds in the queue to fire.** libxml2's
   +cov queue contains multi-KB DTD/`ATTLIST` mutants for the coverage selector to promote;
   libyaml's does not (YAML is compact — a 300 B file already exercises most of the tiny
   grammar). Measured: for YAML, Stage 1 and Stage 2 prompts are ~equal (mean 2 354 B vs
   2 167 B — Stage 2 is actually *smaller*) and generated seeds ~equal (264 B vs 286 B).
   The selector had nothing bloated to grab, so it behaved like `select_diverse_seeds`.

3. **Format effect (secondary, §4E).** XML is the least reliable common structured format
   for LLM generation, so *when* headroom exists, XML yield is more prompt-sensitive than
   YAML — amplifying, not causing, the variance.

**Recommendation (deferred by decision):** before drawing any "the model is better at XML
than YAML" conclusion, re-run YAML against a target with real headroom — an earlier /
less-saturated campaign trigger, or a larger YAML consumer (e.g. libfyaml, or a
YAML-driven application) — so the metric can actually move. As it stands the YAML arms are
measuring a floor.

---

## 6. Recommended changes to the coverage selector (not implemented here)

Recommendations only — scope for this document is analysis. Validate any change with the
`PLAN.md` §7 protocol (≥3 repeats × multiple triggers, Mann-Whitney U) rather than a
single batch.

### 6.1 Primary: rank by new-edge *density*, not absolute edge count

Rank candidates by **edges-per-byte** (ideally *new*-edges-per-byte against the frozen
baseline the batch is scored on), not `-len(edges)`. This aligns the selector with the
metric (§3.3) and structurally removes the "biggest havoc mutant wins" failure: a 2.7 KB
mutant that hits 400 edges (0.14 edge/B, mostly already-covered) loses to a 400 B seed
that hits 120 (0.30 edge/B). Density is the standard parsimony term this whole line of
work (Rebert 2014, Herrera 2021) is built on.

### 6.2 Also: tighten the size band and re-introduce target-size ranking for this arm

- Give the coverage arm its own `FEWSHOT_MAX_SIZE` well below 3 000 B (the corpus median
  for a structurally complete seed is ~400 B per `build_context.FEWSHOT_TARGET_SIZE`;
  ~800–1000 B is a defensible ceiling).
- Among the top density-ranked candidates, break ties / final-rank by **closeness to
  `FEWSHOT_TARGET_SIZE`**, reusing `select_diverse_seeds`' key, so the two selectors only
  differ in *what they optimise for* (coverage density vs pure diversity), not in the
  size regime they operate in.
- Make the `pool = banded or candidates` fallback for this arm refuse to silently readmit
  multi-KB files — if nothing is in band, that is a signal to fall back to
  `select_diverse_seeds`, not to rank the giants.

### 6.3 Secondary (briefly worth doing)

- **Lower the edge-set Jaccard ceiling** from 0.7 to ~0.4 (and/or add a structural /
  content-similarity check) so the two chosen examples cannot be near-identical themes —
  this directly attacks the mode-collapse compounding in §3.
- **Hard byte cap on rendered example text** in `build_context.format_seed_for_prompt`
  (currently `max_chars=2000` for good seeds) — drop to ~800 and, more importantly, make
  *any* selector unable to triple the prompt. A cap that bites is a symptom that the
  wrong seed was chosen; log it.
- **Re-run `stage2_coverage` for gpt-5-4-mini on XML** so the arm isn't Haiku-only.

---

## 7. Note on Stage 3

Not the question asked, but relevant context: Stage 3 `rotation` (Fuzz4All-style
generate/mutate/semantic-equiv rotation) also scored **below** Stage 1 on XML/Haiku
(55 vs 83 total new edges; 6 vs 9 contributors; and only 9 seeds hit anything). On
gpt-5-4-mini it collapsed to 2. `strategies.py` carries a commented-out note that
"generate-new … was the only one to actually generate seeds unlike the other 2." So on
this evidence the **diverse Stage 1 baseline is the current high-water mark** for XML, and
both changes tested on top of it (coverage selection, strategy rotation) regressed it.
That strengthens the §6 framing: fix the selector to match Stage 1's size/diversity
regime *first*, then re-test.

---

## Appendix A — reproduction

Run from `prompt_tests/results/`:

```python
import os, json, glob, statistics, re

def size_stats(d):
    s=[os.path.getsize(os.path.join(d,f)) for f in os.listdir(d)
       if os.path.isfile(os.path.join(d,f))]
    return dict(n=len(s), min=min(s), mean=round(statistics.mean(s)),
               median=int(statistics.median(s)), max=max(s), total=sum(s)) if s else None

def cov(dirpath):
    f=glob.glob(os.path.join(dirpath,"summary_*.json"))
    if not f: return None
    d=json.load(open(f[0])); c=d["coverage"]
    return (c["new_edges_total_batch"], c["distinct_new_edge_contributors"],
            c["seeds_with_new_coverage"], d["input_tokens_total"], d["baseline"].get("baseline_edges"))

for label,d in [("XML s1","stage1_raw_haiku"), ("XML s2","stage2_coverage_haiku"),
                ("XML s3","stage3_rotation_haiku"), ("XML s0","stage0_baseline_haiku5.6"),
                ("YAML s1","libyaml/stage1_raw_haiku"), ("YAML s2","libyaml/stage2_coverage_haiku")]:
    print(label, "cov(new,contrib,hit,in_tok,baseline)=", cov(d),
          "seeds=", size_stats(d+"/seeds"), "prompts=", size_stats(d+"/prompts"))

for d in ["stage1_raw_haiku","stage2_coverage_haiku"]:
    txt=open(glob.glob(d+"/contributors_*.md")[0]).read()
    g=[int(m[1]) for m in re.findall(r"### Group \d+ — (\d+) new edge\(s\) — (\d+) seed\(s\)", txt)]
    print(d, "group sizes:", g, "max:", max(g))
```

Expected (2026-09-10 data):

```
XML s1  cov=(83, 9, 18, 8350, 5595)  seeds mean=298 max=485   prompts mean=2410
XML s2  cov=(57, 6, 27, 23806, 5595) seeds mean=799 max=2151  prompts mean=7411
XML s3  cov=(55, 6, 9, 7839, 5595)   seeds mean=273 max=425   prompts mean=2333
XML s0  cov=(0, 0, 0, 540, 5595)
YAML s1 cov=(0, 0, 0, 7724, 1554)    seeds mean=264            prompts mean=2354
YAML s2 cov=(0, 0, 0, 6039, 1554)    seeds mean=286            prompts mean=2167
stage1_raw_haiku      group sizes: [3,2,2,2,1,1,1,1,1,1,1,1,1]        max: 3
stage2_coverage_haiku group sizes: [13,2,1,1,1,1,1,1,1,1,1,1,1,1]    max: 13
```

Prompts to inspect by eye:
`results/stage2_coverage_haiku/prompts/call_00.json` (two multi-KB repeated-character
mutants, shown truncated) vs `results/stage1_raw_haiku/prompts/call_00.json` (two < 600 B
legible XML docs).

## Appendix B — full source list

| # | Reference | Venue / id |
|---|---|---|
| 1 | Liu et al., "Lost in the Middle: How Language Models Use Long Contexts" | TACL 2024 / arXiv:2307.03172 |
| 2 | Levy, Jacoby, Goldberg, "Same Task, More Tokens…" | ACL 2024 / arXiv:2402.14848 |
| 3 | Min et al., "Rethinking the Role of Demonstrations…" | EMNLP 2022 / arXiv:2202.12837 |
| 4 | Zhao et al., "Calibrate Before Use…" | ICML 2021 / arXiv:2102.09690 |
| 5 | Agarwal et al., "Many-Shot In-Context Learning" | NeurIPS 2024 / arXiv:2404.11018 |
| 6 | "When Does Few-Shot Prompting Help?…" | arXiv:2607.22969 |
| 7 | Chen et al., "Can Few-shot Work in Long-Context?…" | arXiv:2406.13632 |
| 8 | Rebert et al., "Optimizing Seed Selection for Fuzzing" | USENIX Security 2014 |
| 9 | Klees et al., "Evaluating Fuzz Testing" | CCS 2018 |
| 10 | Herrera et al., "Seed Selection for Successful Fuzzing" | ISSTA 2021 |
| 11 | Fioraldi et al., "AFL++…" + AFL docs (`fuzzing.md`) | USENIX WOOT 2020 |
| 12 | "SeedAIchemy: LLM-Driven Seed Corpus Generation for Fuzzing" | arXiv:2511.12448 (2025) |
| 13 | "Sow Smarter, Not Harder…" | CRITIS 2025 |
| 14 | Wang et al., "Skyfire: Data-Driven Seed Generation for Fuzzing" | IEEE S&P 2017 |
| 15 | Xia et al., "Fuzz4All: Universal Fuzzing with Large Language Models" | ICSE 2024 / arXiv:2308.04748 |
| 16 | Lemieux et al., "CodaMOSA…" | ICSE 2023 |
| 17 | "StructEval: Benchmarking LLMs' Capabilities to Generate Structural Outputs" | arXiv:2505.20139 (2025) |
| 18 | "Are LLMs Ready for TOON?…" (structured-format token/validity comparison) | arXiv:2601.12014 |
