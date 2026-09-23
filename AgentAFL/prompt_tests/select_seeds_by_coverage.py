"""
stage2_seed_selection/select_seeds_by_coverage.py — three "seed_selector"
hooks for build_context.assemble_prompt(seed_selector=...), each with the
signature (candidates, n_seeds) -> [(path, reason), ...] that
build_context.py already expects (matches select_diverse_seeds's own
contract, so these are drop-in replacements).

  Arm A — high_coverage_selector : seeds with the MOST total edge coverage
  Arm B — rare_coverage_selector : seeds that hit the RARE edges (edges few
                                    other corpus seeds also hit) — this is
                                    the AFLFast-style framing, deliberately
                                    the opposite emphasis from Arm A; see
                                    PLAN.md for why both are worth testing
                                    rather than assuming one is obviously
                                    right
  Arm C — no_seed_selector       : returns [] — the "does providing ANY
                                    seed even help" control

  Arm A' — make_coverage_selector(...) : the WORKING version of Arm A.
        Same idea as high_coverage_selector (rank by total edges hit,
        descending) but (1) restricted to the same readable size band as
        build_context.select_diverse_seeds, (2) de-duped by edge-set overlap
        (Jaccard) instead of byte similarity, and (3) actually wired to a
        real edge-extraction tool — evaluate_seeds.run_showmap — rather than
        the get_edge_ids stub. It is a FACTORY (needs the target binary,
        which the bare seed_selector signature has no slot for): call it once
        per trigger to get a plain (candidates, n_seeds) closure.

  Arm A'' — make_coverage_per_byte_selector(...) : "Stage 2.1". Same
        machinery as Arm A', but ranks by COVERAGE DENSITY — total edges hit
        / file size in bytes — instead of absolute edge count, inside the
        shared size band (COV_MIN_SIZE..COV_MAX_SIZE, widening to
        COV_FALLBACK_MAX_SIZE if nothing lands in the primary band — see
        _band_with_fallback). Arm A' ranking on absolute edges in a wide
        band reliably promotes AFL's multi-KB block-extension mutants (they
        "cover" only by brute-forcing parser loops); the model imitates them
        into mode collapse. Dividing by size makes a compact, structurally
        dense seed win; the hard floor stops the opposite degeneracy (a
        40-byte fragment scoring high edge/B while teaching nothing). See
        analysis/stage1_vs_stage2_xml.md Sec 6.1-6.2 for the measured
        failure this fixes.

  Arm B' — make_rare_coverage_selector(...) : "Stage 2.2", the WORKING
        version of Arm B. Ranks by edge RARITY — sum over the seed's edges
        of 1 / freq(edge), freq counted across the in-band candidates — so a
        seed reaching edges few other seeds touch wins, AFLFast's "reward the
        rarely-hit path" (AFL++ `-p rare`), the deliberate opposite of Arm
        A''. Same shared size band, fallback and Jaccard de-dup as Arm A''.

  make_stage1_jaccard_selector(...) : a Stage-1 sibling, not a Stage-2 arm —
        keeps select_diverse_seeds' own ranking key (closeness to
        build_context.FEWSHOT_TARGET_SIZE, printable-ratio tiebreak) inside
        the same shared size band as the coverage arms, but de-dupes a
        second+ pick by edge-set Jaccard overlap instead of
        select_diverse_seeds' byte-content is_similar() check. Exists so a
        Stage 1 vs Stage 2 (coverage) A/B comparison isolates exactly the
        ranking-criterion variable — same band, same de-dup mechanism, same
        jaccard_max — rather than conflating that with a de-dup-mechanism
        difference too. select_diverse_seeds itself is untouched; this is a
        separate, opt-in selector.

All four coverage-aware selectors share one band+fallback ladder
(_band_with_fallback: primary COV_MIN_SIZE..COV_MAX_SIZE, widen to
COV_FALLBACK_MAX_SIZE if empty, bail to select_diverse_seeds if still
empty) so none of them can silently drift apart on band width or on what
"nothing in band" means — see _band_with_fallback's own docstring.

The stub selectors (high_coverage_selector / rare_coverage_selector /
no_seed_selector) below still use the get_edge_ids() NotImplementedError
stub — kept for their self-tests / documentation of the idea. The runnable
arms are the make_*_selector factories, all wired to
evaluate_seeds.run_showmap via one shared _make_edges_for helper so they
can never disagree on what edges a seed hits.
"""

from __future__ import annotations

import hashlib
import sys
from datetime import datetime, timezone
from pathlib import Path

# evaluate_seeds.py lives at the repo root, one level up from prompt_tests/.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# FEWSHOT_TARGET_SIZE (the ~400B "structurally complete seed" target) is
# imported, not re-declared, so make_stage1_jaccard_selector's ranking key
# stays in lockstep with select_diverse_seeds' own if it's ever retuned.
# select_diverse_seeds is reused as the final fallback for every selector in
# this file (see _band_with_fallback).
from build_context import FEWSHOT_TARGET_SIZE, select_diverse_seeds
from evaluate_seeds import run_showmap

# --- shared size band for every coverage-aware selector in this file -----------
# Deliberately tighter than build_context's own 32..3000 band. Dividing edges by
# size already rewards compactness, so the HARD FLOOR is what stops the ranking
# collapsing onto degenerate minimized fragments, and the low CEILING keeps the
# promoted example out of the multi-KB block-extension-mutant regime that made
# Arm A' regress (analysis/stage1_vs_stage2_xml.md Sec 6.2). One shared band
# (not one per selector) so Stage 1 (Jaccard) vs Coverage vs Coverage-per-byte
# vs Rare-coverage never silently disagree on what "in band" means.
COV_MIN_SIZE = 200            # floor: below this an XML seed can't carry a real DTD internal subset
COV_MAX_SIZE = 800            # primary ceiling: ~2x the corpus median for a structurally complete seed
COV_FALLBACK_MAX_SIZE = 1500  # widened ceiling, tried before bailing to select_diverse_seeds entirely


def get_edge_ids(seed_path: Path) -> set[int]:
    """TODO: return the set of edge IDs this seed hits. Same job as
    shared/score_batch.py's get_seed_edges — wire both to your existing
    tool, ideally by having one of them import the other rather than
    duplicating the afl-showmap call."""
    raise NotImplementedError(
        "Wire get_edge_ids to your existing per-seed edge-coverage tool "
        "(see module docstring) before running Stage 2."
    )


def high_coverage_selector(candidates, n_seeds):
    """Arm A: rank candidates by total edge count, descending. candidates is
    scan_queue's output: [(path, printable_ratio, size), ...]."""
    scored = [(path, len(get_edge_ids(path))) for path, _ratio, _size in candidates]
    scored.sort(key=lambda t: t[1], reverse=True)
    return [(path, f"high-coverage ({n} edges)") for path, n in scored[:n_seeds]]


def rare_coverage_selector(candidates, n_seeds):
    """Arm B: rank candidates by how many CORPUS-PRIVATE edges they hit —
    edges that few or no other seeds in this candidate set also hit.

    IMPORTANT CAVEAT, worth reading before trusting this: this is one
    reasonable proxy for "rare coverage," not the only one, and it is NOT
    quite AFLFast's actual definition. AFLFast up-weights paths by how
    rarely they've been EXECUTED during fuzzing (a runtime frequency
    count) — this function instead uses how few OTHER SEEDS IN THIS STATIC
    CANDIDATE SET also happen to hit the same edge, which is a presence
    count, not an execution count. If your existing edge-coverage tool (see
    get_edge_ids TODO) can expose real execution-frequency data instead,
    prefer that — it's the closer match to what the cited literature means
    by "rare." Scoring below counts each seed's PRIVATE edges (edges hit by
    at most one seed in the candidate set), rather than averaging inverse
    frequency across all its edges — a seed with three fairly-uncommon
    edges and a seed with one truly-unique edge are treated as genuinely
    different quantities here, deliberately, rather than collapsed into one
    ambiguous average (see this file's own history in PLAN.md for why the
    averaged version was tried first and dropped)."""
    paths = [path for path, _ratio, _size in candidates]
    edge_sets = {path: get_edge_ids(path) for path in paths}

    freq = {}
    for edges in edge_sets.values():
        for e in edges:
            freq[e] = freq.get(e, 0) + 1

    def private_edge_count(edges: set[int]) -> int:
        return sum(1 for e in edges if freq[e] == 1)

    scored = [(path, private_edge_count(edge_sets[path])) for path in paths]
    scored.sort(key=lambda t: t[1], reverse=True)
    return [(path, f"rare-coverage ({n} private edges)") for path, n in scored[:n_seeds]]


def no_seed_selector(candidates, n_seeds):
    """Arm C: the control — never show a good-seed example at all,
    regardless of what's available. n_seeds is accepted (to match the
    seed_selector signature) but ignored."""
    return []


# ---------------------------------------------------------------------------
# Arm A' — the working "total coverage" selector
# ---------------------------------------------------------------------------

def _edge_jaccard(a: set, b: set) -> float:
    """|a & b| / |a | b|. 1.0 == identical edge set, 0.0 == disjoint.
    Two empty sets are treated as identical (1.0) so an empty pick can
    never sneak past the diversity gate."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _band_with_fallback(candidates, min_size=COV_MIN_SIZE, max_size=COV_MAX_SIZE,
                        fallback_max_size=COV_FALLBACK_MAX_SIZE):
    """The one banding rule shared by every coverage-aware selector in this
    file. Primary band first; if nothing lands in it, widen the ceiling
    (same floor) instead of going fully unbounded or switching to a
    different selector outright — a 1000-1500B seed is still a plausible
    XML document, unlike the 5+KB block-extension mutants an unbounded pool
    would readmit. Still empty after widening -> returns [] so the caller
    can do its own final bail (every make_*_selector below falls back to
    select_diverse_seeds at that point)."""
    banded = [c for c in candidates if min_size <= c[2] <= max_size]
    if banded:
        return banded
    return [c for c in candidates if min_size <= c[2] <= fallback_max_size]


def _log_fallback(log_path, arm_name: str, reason: str, call_ctx=None) -> None:
    """Every time a coverage-aware selector gives up and defers to
    select_diverse_seeds instead of its own ranking, this is the ONE place
    that records it — always to stderr (visible live, mid-run), and to
    log_path too when the caller has one (a durable record you can check
    after the fact, so a silent fallback can't masquerade as a normal
    coverage-ranked pick in the results). log_path=None skips the file
    write; the stderr print always happens.

    call_ctx: the same mutable {"call": int|None} dict passed to the
    selector factory (see make_coverage_selector's call_ctx param) — the
    seed_selector(candidates, n_seeds) contract build_context.assemble_prompt
    calls has no call-index slot, so run_build_context.py's prompt_fn instead
    updates this dict's "call" key right before each assemble_prompt call,
    and this function reads it at fallback time. None/missing -> "call=?"."""
    call = (call_ctx or {}).get("call")
    call_str = f"call={call} (prompts/call_{call:02d}.json)" if call is not None else "call=?"
    msg = f"[selector-fallback] {arm_name} {call_str}: {reason} -> deferring to select_diverse_seeds"
    print(f"  ! {msg}", file=sys.stderr)
    if log_path is not None:
        log_path = Path(log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a") as f:
            f.write(f"{datetime.now(timezone.utc).isoformat()} {msg}\n")


def _make_edges_for(target_binary, args, afl_showmap_bin, showmap_timeout, cache):
    """Closure: seed path -> its afl-showmap edge set, memoised in `cache` on
    (content-hash, target, args). The ONE edge-extraction path in this file —
    shared by make_coverage_selector and make_coverage_per_byte_selector so
    they can never silently disagree (see module docstring TODO)."""
    def edges_for(path: Path) -> set:
        try:
            raw = path.read_bytes()
        except OSError:
            return set()
        key = (hashlib.sha1(raw).hexdigest(), target_binary, tuple(args))
        if key not in cache:
            _status, edges = run_showmap(
                afl_showmap_bin, target_binary, args, path, timeout=showmap_timeout
            )
            cache[key] = edges
        return cache[key]
    return edges_for


# ---------------------------------------------------------------------------
# Stage 1 sibling — select_diverse_seeds' own ranking key, edge-Jaccard dedup
# ---------------------------------------------------------------------------

def make_stage1_jaccard_selector(
    target_binary: str,
    target_args=None,
    *,
    afl_showmap_bin: str = "afl-showmap",
    showmap_timeout: int = 30,
    jaccard_max: float = 0.7,
    edge_cache: dict | None = None,
    log_path=None,
    call_ctx=None,
):
    """A Stage-1 sibling, not a Stage-2 arm: same ranking key as
    select_diverse_seeds (closeness to build_context.FEWSHOT_TARGET_SIZE,
    printable-ratio tiebreak), same shared size band as the coverage arms
    (_band_with_fallback), but a later pick is rejected by edge-set Jaccard
    overlap (matching make_coverage_selector's own de-dup mechanism and
    jaccard_max default) instead of select_diverse_seeds' byte-content
    is_similar() check.

    Why this exists: a Stage 1 vs Stage 2 (coverage) A/B comparison is only
    informative about the RANKING criterion (size-closeness vs coverage) if
    everything else — band, de-dup mechanism, de-dup threshold — is held
    identical between the two arms. select_diverse_seeds' is_similar() dedup
    and make_coverage_selector's edge-Jaccard dedup are a second, unintended
    variable riding along with the intended one. This selector holds Stage
    1's ranking fixed and swaps only the de-dup mechanism, so the remaining
    difference from make_coverage_selector is exactly the ranking criterion.

    log_path: passed straight through to _log_fallback — see its docstring.
    Every time this selector gives up and defers to select_diverse_seeds
    (band empty even after widening, or nothing survived the edge/dedup
    pass), that's logged there so a run's results can be trusted to
    actually reflect this selector, not a silent fallback to a different one.

    Factory, shared edge_cache, target_args rules: identical to
    make_coverage_selector — see its docstring.
    """
    args = list(target_args) if target_args is not None else ["@@"]
    cache = edge_cache if edge_cache is not None else {}
    _edges_for = _make_edges_for(target_binary, args, afl_showmap_bin, showmap_timeout, cache)

    def stage1_jaccard_selector(candidates, n_seeds):
        """candidates: scan_queue's output, [(path, printable_ratio, size), ...]."""
        if not candidates:
            return []

        pool = _band_with_fallback(candidates)
        if not pool:
            _log_fallback(log_path, "stage1-jaccard", "no candidate within 200-1500B", call_ctx=call_ctx)
            return select_diverse_seeds(candidates, n_seeds)

        # select_diverse_seeds' own ranking key: closeness to the ~400B
        # target, printable ratio as tiebreak. Unlike the coverage arms,
        # edges are never used for ranking here — only for the dedup gate
        # below — so this candidate never even needs a showmap call unless
        # it's actually being considered as a pick.
        ranked = sorted(pool, key=lambda t: (abs(t[2] - FEWSHOT_TARGET_SIZE), -t[1]))

        chosen: list = []
        chosen_edges: list = []
        for path, _ratio, size in ranked:
            if len(chosen) >= n_seeds:
                break
            edges = _edges_for(path)
            if not edges:  # crash-with-no-map / timeout / genuinely no coverage
                continue
            if any(_edge_jaccard(edges, e) > jaccard_max for e in chosen_edges):
                continue
            chosen.append((path, f"stage1-jaccard (size {size}B, {len(edges)} edges)"))
            chosen_edges.append(edges)

        if not chosen:
            # every ranked candidate was un-showmap-able (crash/timeout) —
            # same "no usable coverage signal" fallback as the coverage arms.
            _log_fallback(log_path, "stage1-jaccard",
                         "no in-band candidate produced a showmap-able edge set", call_ctx=call_ctx)
            return select_diverse_seeds(candidates, n_seeds)

        return chosen

    return stage1_jaccard_selector


def make_coverage_selector(
    target_binary: str,
    target_args=None,
    *,
    afl_showmap_bin: str = "afl-showmap",
    showmap_timeout: int = 30,
    jaccard_max: float = 0.7,
    edge_cache: dict | None = None,
    log_path=None,
    call_ctx=None,
):
    """Build a seed_selector that ranks AFL-queue candidates by TOTAL edges
    hit (afl-showmap), within the shared size band (_band_with_fallback),
    de-duped by edge-set overlap.

    Why a factory: the seed_selector contract build_context expects is just
    (candidates, n_seeds) -> [(path, reason), ...] — no target binary in it.
    afl-showmap needs one, so call this once with the target (per trigger,
    in run_ablation) and hand the returned closure to
    assemble_prompt(seed_selector=...).

      target_binary : the fuzzed binary (from the campaign's cmdline).
      target_args   : the campaign's EXACT arg list, "@@" marking the input
                      slot (libxml2 here: ["--noout", "@@"]). Must match the
                      live cmdline or the edge counts are meaningless — same
                      rule as evaluate_seeds.py. Defaults to ["@@"].
      jaccard_max   : a later pick is rejected when its edge set overlaps an
                      already-chosen pick's by more than this (0.7 -> "70% of
                      the two seeds' combined edges are shared" == too close).
      edge_cache    : optional dict reused across calls so afl-showmap runs
                      once per (seed-content, target), not once per prompt
                      build. The ablation harness rebuilds the prompt for the
                      same trigger many times (repeats x arms) — pass one
                      shared dict per study run to pay the showmap cost once.

    Cost: one afl-showmap process per in-band +cov candidate on the FIRST
    build for a trigger (~tens of ms each for xmllint; a few seconds for a
    few-hundred-file band, up to ~1 min for a very large queue). Every later
    build for that trigger is a dict lookup. select_diverse_seeds by
    contrast touches no subprocess at all — this is the price of ranking on
    real coverage instead of size.
    """
    args = list(target_args) if target_args is not None else ["@@"]
    cache = edge_cache if edge_cache is not None else {}
    _edges_for = _make_edges_for(target_binary, args, afl_showmap_bin, showmap_timeout, cache)

    def coverage_selector(candidates, n_seeds):
        """candidates: scan_queue's output, [(path, printable_ratio, size), ...]
        — the +cov AFL queue entries, exactly the same input
        select_diverse_seeds gets."""
        if not candidates:
            return []

        pool = _band_with_fallback(candidates)
        if not pool:
            # nothing within COV_MIN_SIZE..COV_FALLBACK_MAX_SIZE at all —
            # never silently rank the giants outside it (see module docstring).
            _log_fallback(log_path, "coverage", "no candidate within 200-1500B", call_ctx=call_ctx)
            return select_diverse_seeds(candidates, n_seeds)

        scored = []
        for path, ratio, size in pool:
            edges = _edges_for(path)
            if not edges:  # crash-with-no-map / timeout / genuinely no coverage
                continue
            scored.append((path, edges, ratio, size))

        if not scored:
            # every in-band candidate came back crash/timeout/no-coverage —
            # same "no usable coverage signal" case as an empty band.
            _log_fallback(log_path, "coverage",
                         "no in-band candidate produced a showmap-able edge set", call_ctx=call_ctx)
            return select_diverse_seeds(candidates, n_seeds)

        # most edges first; ties broken by more-printable then smaller, purely
        # so the pick is deterministic across runs.
        scored.sort(key=lambda t: (-len(t[1]), -t[2], t[3]))

        chosen: list = []
        chosen_edges: list = []
        for path, edges, _ratio, _size in scored:
            if len(chosen) >= n_seeds:
                break
            if any(_edge_jaccard(edges, e) > jaccard_max for e in chosen_edges):
                continue
            chosen.append((path, f"coverage-ranked ({len(edges)} edges)"))
            chosen_edges.append(edges)

        return chosen

    return coverage_selector


# ---------------------------------------------------------------------------
# Arm A'' — coverage density: total edges hit PER BYTE ("Stage 2.1")
# ---------------------------------------------------------------------------

def make_coverage_per_byte_selector(
    target_binary: str,
    target_args=None,
    *,
    afl_showmap_bin: str = "afl-showmap",
    showmap_timeout: int = 30,
    min_size: int = COV_MIN_SIZE,
    max_size: int = COV_MAX_SIZE,
    fallback_max_size: int = COV_FALLBACK_MAX_SIZE,
    jaccard_max: float = 0.7,
    edge_cache: dict | None = None,
    log_path=None,
    call_ctx=None,
):
    """Like make_coverage_selector, but ranks by COVERAGE DENSITY —
    total afl-showmap edges hit / file size in bytes — descending, inside
    the shared [min_size, max_size] band (_band_with_fallback), de-duped by
    edge-set overlap.

    Why density and not absolute count: ranking on raw edge count in a wide
    band reliably promotes AFL's multi-KB block-extension mutants (repeated
    attributes, character-reference walls) that "cover" a lot only by
    brute-forcing parser loops. The model imitates them and the batch
    mode-collapses (measured: analysis/stage1_vs_stage2_xml.md Sec 2). Edges
    per byte makes a compact, structurally dense seed win instead.

    Why a HARD size band (not `banded or candidates`): dividing by size
    rewards small, so without a floor the ranking drifts to sub-DTD
    minimized fragments that score high edge/B while teaching nothing; the
    ceiling keeps the promoted example out of the mutant regime. If nothing
    is in the primary band, _band_with_fallback tries the widened
    [min_size, fallback_max_size] band before this selector defers to
    build_context.select_diverse_seeds — never silently re-admit the
    oversized files the band excludes (analysis Sec 6.2).

    Factory, shared edge_cache, target_args rules: all identical to
    make_coverage_selector — see its docstring.
    """
    args = list(target_args) if target_args is not None else ["@@"]
    cache = edge_cache if edge_cache is not None else {}
    _edges_for = _make_edges_for(target_binary, args, afl_showmap_bin, showmap_timeout, cache)

    def coverage_per_byte_selector(candidates, n_seeds):
        """candidates: scan_queue's output, [(path, printable_ratio, size), ...]."""
        if not candidates:
            return []

        pool = _band_with_fallback(candidates, min_size, max_size, fallback_max_size)
        if not pool:
            # Nothing compact-and-in-band, even widened — fall back to the
            # size/diversity selector, never rank the oversized mutants this
            # band excludes.
            _log_fallback(log_path, "coverage-per-byte", "no candidate within 200-1500B", call_ctx=call_ctx)
            return select_diverse_seeds(candidates, n_seeds)

        scored = []
        for path, ratio, size in pool:
            edges = _edges_for(path)
            if not edges or size <= 0:  # crash-no-map / timeout / no coverage
                continue
            density = len(edges) / size
            scored.append((path, edges, density, ratio, size))

        if not scored:
            # every in-band candidate came back crash/timeout/no-coverage —
            # same "no usable coverage signal" case as an empty band.
            _log_fallback(log_path, "coverage-per-byte",
                         "no in-band candidate produced a showmap-able edge set", call_ctx=call_ctx)
            return select_diverse_seeds(candidates, n_seeds)

        # highest edge/byte first; ties -> more printable, then smaller,
        # purely for a deterministic pick across runs.
        scored.sort(key=lambda t: (-t[2], -t[3], t[4]))

        chosen: list = []
        chosen_edges: list = []
        for path, edges, density, _ratio, size in scored:
            if len(chosen) >= n_seeds:
                break
            if any(_edge_jaccard(edges, e) > jaccard_max for e in chosen_edges):
                continue
            chosen.append((
                path,
                f"coverage-per-byte ({len(edges)} edges / {size} B = {density:.3f} edge/B)",
            ))
            chosen_edges.append(edges)

        return chosen

    return coverage_per_byte_selector


# ---------------------------------------------------------------------------
# Arm B' — rare edges: rank by rarity of the edges a seed hits ("Stage 2.2")
# ---------------------------------------------------------------------------

def make_rare_coverage_selector(
    target_binary: str,
    target_args=None,
    *,
    afl_showmap_bin: str = "afl-showmap",
    showmap_timeout: int = 30,
    min_size: int = COV_MIN_SIZE,
    max_size: int = COV_MAX_SIZE,
    fallback_max_size: int = COV_FALLBACK_MAX_SIZE,
    jaccard_max: float = 0.7,
    edge_cache: dict | None = None,
    log_path=None,
    call_ctx=None,
):
    """The WORKING version of rare_coverage_selector. Ranks +cov queue
    candidates by how RARE the edges they hit are — AFLFast's "reward the
    rarely-hit path" emphasis (AFL++ `-p rare`), the deliberate opposite of
    make_coverage_selector's "reward the most-covering seed".

    Rarity score = sum over the seed's edges of 1 / freq(edge), where
    freq(edge) is the number of IN-BAND candidates that also hit that edge.
    An edge only this one seed reaches contributes 1.0; an edge half the
    pool reaches contributes a little. Summed (not averaged): a seed that
    uniquely covers many edges outranks one with a single unique edge —
    matched to the metric, which counts distinct new edges, not seeds.
    (The averaged variant was tried first and dropped — see this file's
    history in PLAN.md.)

    Same shared [min_size, max_size] band (_band_with_fallback),
    select_diverse_seeds fallback, and Jaccard de-dup as
    make_coverage_per_byte_selector. Factory / shared edge_cache /
    target_args rules identical to make_coverage_selector.
    """
    args = list(target_args) if target_args is not None else ["@@"]
    cache = edge_cache if edge_cache is not None else {}
    _edges_for = _make_edges_for(target_binary, args, afl_showmap_bin, showmap_timeout, cache)

    def rare_coverage_selector_impl(candidates, n_seeds):
        """candidates: scan_queue's output, [(path, printable_ratio, size), ...]."""
        if not candidates:
            return []

        pool = _band_with_fallback(candidates, min_size, max_size, fallback_max_size)
        if not pool:
            _log_fallback(log_path, "rare-coverage", "no candidate within 200-1500B", call_ctx=call_ctx)
            return select_diverse_seeds(candidates, n_seeds)

        # First pass: every in-band seed's edge set (needed before any edge's
        # frequency across the band is known).
        seen = []
        for path, ratio, size in pool:
            edges = _edges_for(path)
            if not edges:  # crash-no-map / timeout / no coverage
                continue
            seen.append((path, edges, ratio, size))
        if not seen:
            _log_fallback(log_path, "rare-coverage",
                         "no in-band candidate produced a showmap-able edge set", call_ctx=call_ctx)
            return select_diverse_seeds(candidates, n_seeds)

        freq: dict[int, int] = {}
        for _p, edges, _r, _s in seen:
            for e in edges:
                freq[e] = freq.get(e, 0) + 1

        # rarity = Sum 1/freq(e); ties -> more printable, then smaller.
        scored = [
            (path, edges, sum(1.0 / freq[e] for e in edges), ratio, size)
            for path, edges, ratio, size in seen
        ]
        scored.sort(key=lambda t: (-t[2], -t[3], t[4]))

        chosen: list = []
        chosen_edges: list = []
        for path, edges, rarity, _ratio, _size in scored:
            if len(chosen) >= n_seeds:
                break
            if any(_edge_jaccard(edges, e) > jaccard_max for e in chosen_edges):
                continue
            private = sum(1 for e in edges if freq[e] == 1)
            chosen.append((
                path,
                f"rare-coverage (rarity {rarity:.1f}, {private}/{len(edges)} edges band-private)",
            ))
            chosen_edges.append(edges)

        return chosen

    return rare_coverage_selector_impl


if __name__ == "__main__":
    # Self-test with a fake get_edge_ids, verifying this file's own selection
    # logic (not the real edge-extraction, which still needs wiring — see
    # the TODO above). Deliberately constructed so high- and rare-coverage
    # pick DIFFERENT seeds, to confirm they're actually measuring different
    # things rather than reinventing each other under a different name.
    import unittest.mock as mock

    fake_edges = {
        Path("/a"): set(range(1, 11)),        # 10 edges, only #10 is private
        Path("/b"): set(range(1, 10)),        # 9 edges, all shared with /a
        Path("/c"): {1, 2, 11, 12, 13},       # 5 edges, but 3 are private
    }
    candidates = [(p, 1.0, 100) for p in fake_edges]

    with mock.patch(__name__ + ".get_edge_ids", side_effect=lambda p: fake_edges[p]):
        hi = high_coverage_selector(candidates, 1)
        rare = rare_coverage_selector(candidates, 1)
        none = no_seed_selector(candidates, 2)
        print("high_coverage picks:", hi)
        print("rare_coverage picks:", rare)
        print("no_seed picks:", none)
        assert hi[0][0] == Path("/a"), "expected /a (10 edges) to win high-coverage"
        assert rare[0][0] == Path("/c"), "expected /c (3 private edges) to win rare-coverage"
        assert none == []
        print("ALL SELECTION TESTS PASSED — high- and rare-coverage genuinely disagree, as intended")

    # --- Arm A' (make_coverage_selector): size band + Jaccard de-dup ---------
    # "big" is outside the size band and must be excluded despite having the
    # most edges. "x1" and "x2" hit near-identical edges — only one should be
    # picked; "y" is the diverse runner-up. Real files on disk, because the
    # selector reads each candidate's bytes (to key the afl-showmap cache)
    # before it ever calls showmap.
    import tempfile

    edges_by_name = {
        "big": set(range(1, 40)),           # 39 edges, but size out of band
        "x1": set(range(1, 21)),            # 20 edges
        "x2": set(range(1, 20)),            # 19 edges, ~95% shared with x1
        "y": {50, 51, 52, 53, 54, 55},      # 6 edges, disjoint
    }
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        paths = {}
        for name, sz in (("big", 5000), ("x1", 400), ("x2", 400), ("y", 400)):
            p = tdp / name
            # distinct bytes per file: the selector keys its showmap cache on
            # content hash, so identical contents would collapse to one entry.
            p.write_bytes(name.encode().ljust(sz, b"."))
            paths[name] = p
        cov_candidates = [(paths[n], 1.0, len(paths[n].read_bytes()))
                          for n in ("big", "x1", "x2", "y")]

        with mock.patch(
            __name__ + ".run_showmap",
            side_effect=lambda _bin, _t, _a, path, timeout=30: (
                "ok", edges_by_name[Path(path).name]
            ),
        ):
            sel = make_coverage_selector("dummy-target", ["@@"])
            picks = sel(cov_candidates, 2)
            print("coverage_selector picks:", [(p.name, r) for p, r in picks])
            picked = [p.name for p, _ in picks]
            assert "big" not in picked, "oversized seed should be banded out"
            assert picked[0] == "x1", "expected x1 (20 in-band edges) first"
            assert picked[1] == "y", "expected y (diverse) second, not x2 (dup of x1)"
            print("ARM A' TESTS PASSED — size band + edge-set de-dup both applied")

    # --- Arm A'' (make_coverage_per_byte_selector): density beats both extremes
    # "wall": most absolute edges but a multi-KB mutant -> over the ceiling.
    # "frag": high edge/byte but below the floor -> a teach-nothing fragment.
    # "dense" (400 B) should beat "ok" (600 B) purely on edges-per-byte even
    # though "ok" hits more absolute edges — the whole point of this arm.
    covpb_edges = {
        "wall": set(range(1, 401)),          # 400 edges / 2500 B  = 0.160 edge/B  (out: ceiling)
        "dense": set(range(1000, 1150)),     # 150 edges / 400 B   = 0.375 edge/B  (WIN)
        "ok": set(range(3000, 3180)),        # 180 edges / 600 B   = 0.300 edge/B  (2nd, disjoint)
        "frag": set(range(5000, 5040)),      # 40 edges / 60 B     = 0.667 edge/B  (out: floor)
    }
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        cpaths = {}
        for name, sz in (("wall", 2500), ("dense", 400), ("ok", 600), ("frag", 60)):
            p = tdp / name
            p.write_bytes(name.encode().ljust(sz, b"."))
            cpaths[name] = p
        covpb_candidates = [(cpaths[n], 1.0, len(cpaths[n].read_bytes()))
                            for n in ("wall", "dense", "ok", "frag")]

        with mock.patch(
            __name__ + ".run_showmap",
            side_effect=lambda _bin, _t, _a, path, timeout=30: (
                "ok", covpb_edges[Path(path).name]
            ),
        ):
            sel = make_coverage_per_byte_selector("dummy-target", ["@@"])
            picks = sel(covpb_candidates, 2)
            print("coverage_per_byte_selector picks:", [(p.name, r) for p, r in picks])
            picked = [p.name for p, _ in picks]
            assert "wall" not in picked, "over-ceiling mutant must be banded out"
            assert "frag" not in picked, "sub-floor fragment must be banded out"
            assert picked[0] == "dense", "expected dense (0.375 edge/B) over ok (0.300)"
            assert picked[1:] == ["ok"], "expected ok second (disjoint, in band)"
            print("ARM A'' TESTS PASSED — edges-per-byte ranking + hard floor & ceiling")

    # --- Arm B' (make_rare_coverage_selector): inverse-frequency-sum ---------
    # "wide" hits the MOST edges but they're all common (shared with c1/c2/c3);
    # "rare" hits fewer edges, 30 of them private to it -> highest Sum 1/freq.
    # "huge"/"frag" carry private edges too but sit outside the size band.
    rare_edges = {
        "c1":   set(range(1, 61)),
        "c2":   set(range(1, 41)),
        "c3":   set(range(1, 41)),
        "wide": set(range(1, 61)),                 # 60 common edges, 0 private
        "rare": {1, 2} | set(range(700, 730)),     # 32 edges, 30 band-private
        "huge": set(range(9000, 9200)),            # 200 private, but 5000 B (out)
        "frag": set(range(8000, 8050)),            # 50 private, but 60 B (out)
    }
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        rpaths = {}
        for name, sz in (("c1", 400), ("c2", 400), ("c3", 400), ("wide", 400),
                         ("rare", 400), ("huge", 5000), ("frag", 60)):
            p = tdp / name
            p.write_bytes(name.encode().ljust(sz, b"."))
            rpaths[name] = p
        rare_candidates = [(rpaths[n], 1.0, len(rpaths[n].read_bytes())) for n in rare_edges]

        with mock.patch(
            __name__ + ".run_showmap",
            side_effect=lambda _bin, _t, _a, path, timeout=30: (
                "ok", rare_edges[Path(path).name]
            ),
        ):
            sel = make_rare_coverage_selector("dummy-target", ["@@"])
            picks = [p.name for p, _ in sel(rare_candidates, 2)]
            print("rare_coverage_selector picks:", picks)
            assert picks[0] == "rare", "expected 'rare' (30 band-private edges -> top Sum 1/freq)"
            assert "huge" not in picks and "frag" not in picks, "out-of-band seeds must be dropped"
            print("ARM B' TESTS PASSED — inverse-frequency-sum ranking + size band")

    # --- shared band+fallback ladder: widen tier, then full bail -------------
    # Exercised identically against all four coverage-aware selectors (Arm A',
    # A'', B', and the new stage1-jaccard) so none of them can silently regress
    # the ladder independently.
    all_factories = {
        "coverage": make_coverage_selector,
        "coverage_per_byte": make_coverage_per_byte_selector,
        "rare_coverage": make_rare_coverage_selector,
        "stage1_jaccard": make_stage1_jaccard_selector,
    }

    # Widen tier: nothing in the primary 200-800B band, but "midband" (1000B)
    # is inside the widened 200-1500B ceiling and must be picked; "giant"
    # (5000B) must never be, even though it would carry more edges.
    ladder_edges = {
        "midband": set(range(1, 21)),   # 20 edges
        "giant": set(range(1, 999)),    # 998 edges — most of any pool, must still be excluded
    }
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        lpaths = {}
        for name, sz in (("midband", 1000), ("giant", 5000)):
            p = tdp / name
            p.write_bytes(name.encode().ljust(sz, b"."))
            lpaths[name] = p
        ladder_candidates = [(lpaths[n], 1.0, len(lpaths[n].read_bytes())) for n in ladder_edges]

        with mock.patch(
            __name__ + ".run_showmap",
            side_effect=lambda _bin, _t, _a, path, timeout=30: (
                "ok", ladder_edges[Path(path).name]
            ),
        ):
            for arm_name, factory in all_factories.items():
                sel = factory("dummy-target", ["@@"])
                picks = [p.name for p, _ in sel(ladder_candidates, 1)]
                assert picks == ["midband"], (
                    f"{arm_name}: expected widen-tier to pick 'midband' only, got {picks}"
                )
            print("WIDEN-TIER TESTS PASSED — all four selectors try 200-1500B before bailing")

    # Full bail: nothing between 200-1500B at all ("tiny"=50B, "giant"=5000B —
    # both outside every coverage-arm band). Every selector must defer to
    # select_diverse_seeds, which re-applies ITS OWN 32-3000B band to the
    # original candidate list and picks "tiny" (the only one inside it).
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        bpaths = {}
        for name, sz in (("tiny", 50), ("giant", 5000)):
            p = tdp / name
            p.write_bytes(name.encode().ljust(sz, b"."))
            bpaths[name] = p
        bail_candidates = [(bpaths[n], 1.0, len(bpaths[n].read_bytes())) for n in ("tiny", "giant")]

        with mock.patch(
            __name__ + ".run_showmap",
            side_effect=AssertionError("run_showmap should never be called once every "
                                       "coverage-arm band is empty — the bail must happen "
                                       "before any afl-showmap call"),
        ):
            for arm_name, factory in all_factories.items():
                sel = factory("dummy-target", ["@@"])
                picks = [p.name for p, _ in sel(bail_candidates, 1)]
                assert picks == ["tiny"], (
                    f"{arm_name}: expected full bail to select_diverse_seeds -> 'tiny', got {picks}"
                )
            print("FULL-BAIL TESTS PASSED — all four selectors defer to select_diverse_seeds "
                  "without ever calling afl-showmap")

    # --- fallback logging: a triggered bail is actually recorded, not silent -
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        for name, sz in (("tiny", 50), ("giant", 5000)):
            (tdp / name).write_bytes(name.encode().ljust(sz, b"."))
        bail_candidates = [((tdp / n), 1.0, (tdp / n).stat().st_size) for n in ("tiny", "giant")]
        log_path = tdp / "nested" / "selector_fallback.log"  # nested: proves mkdir(parents=True)

        with mock.patch(__name__ + ".run_showmap", side_effect=AssertionError("unused")):
            for arm_name, factory in all_factories.items():
                sel = factory("dummy-target", ["@@"], log_path=log_path)
                sel(bail_candidates, 1)
        assert log_path.is_file(), "log_path should have been created by the first fallback"
        lines = log_path.read_text().splitlines()
        assert len(lines) == len(all_factories), (
            f"expected one logged line per selector that fell back, got {len(lines)}: {lines}"
        )
        for arm_name in all_factories:
            assert any(arm_name.replace("_", "-") in line or arm_name in line for line in lines), (
                f"no fallback log line mentions {arm_name}: {lines}"
            )
        print("FALLBACK-LOG TESTS PASSED — a triggered bail is written to log_path, not just printed")

    # --- stage1-jaccard: proves the DEDUP MECHANISM actually changed ---------
    # Same ranking key as select_diverse_seeds (closeness to 400B, all these
    # candidates sit exactly at 400B so ranking ties on insertion order), but
    # acceptance is edge-Jaccard, not byte-content is_similar(). Two checks,
    # each the mirror image of what a content-similarity dedup would do:
    stage1j_edges = {
        "near1": set(range(1, 11)),                    # 10 edges — the baseline pick
        "near3_overlap": set(range(1, 10)) | {101},     # 9/10 shared with near1 -> Jaccard 0.818 (REJECT)
        "near2_disjoint": set(range(201, 211)),          # fully disjoint from near1 -> Jaccard 0.0 (ACCEPT)
    }
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        jpaths = {}
        # near1 / near3_overlap: near-IDENTICAL length, deliberately different
        # byte content — a content-similarity dedup would happily accept
        # near3_overlap as "different enough"; edge-Jaccard must reject it.
        (tdp / "near1").write_bytes(b"A" * 400)
        (tdp / "near3_overlap").write_bytes(b"Z" * 400)
        # near2_disjoint: byte-for-byte near1 with ONE byte changed — a
        # content-similarity dedup would almost certainly reject this as "too
        # similar to near1"; edge-Jaccard must accept it, since its edges
        # don't overlap near1's at all.
        (tdp / "near2_disjoint").write_bytes(b"A" * 399 + b"B")
        for name in stage1j_edges:
            jpaths[name] = tdp / name

        with mock.patch(
            __name__ + ".run_showmap",
            side_effect=lambda _bin, _t, _a, path, timeout=30: (
                "ok", stage1j_edges[Path(path).name]
            ),
        ):
            sel = make_stage1_jaccard_selector("dummy-target", ["@@"])

            reject_candidates = [(jpaths["near1"], 1.0, 400), (jpaths["near3_overlap"], 1.0, 400)]
            picks = [p.name for p, _ in sel(reject_candidates, 2)]
            assert picks == ["near1"], (
                f"expected near3_overlap rejected despite different bytes (edge-Jaccard "
                f"0.818 > 0.7), got {picks}"
            )

            accept_candidates = [(jpaths["near1"], 1.0, 400), (jpaths["near2_disjoint"], 1.0, 400)]
            picks = [p.name for p, _ in sel(accept_candidates, 2)]
            assert set(picks) == {"near1", "near2_disjoint"}, (
                f"expected near2_disjoint accepted despite near-identical bytes (edge-Jaccard "
                f"0.0), got {picks}"
            )
            print("STAGE1-JACCARD DEDUP TESTS PASSED — acceptance tracks edge overlap, "
                  "not byte content")
