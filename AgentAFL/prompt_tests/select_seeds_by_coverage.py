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
        / file size in bytes — instead of absolute edge count, inside a
        deliberately TIGHTER size band (COVPB_MIN_SIZE..COVPB_MAX_SIZE). Arm
        A' ranking on absolute edges in a 3 KB band reliably promotes AFL's
        multi-KB block-extension mutants (they "cover" only by brute-forcing
        parser loops); the model imitates them into mode collapse. Dividing
        by size makes a compact, structurally dense seed win; the hard floor
        stops the opposite degeneracy (a 40-byte fragment scoring high
        edge/B while teaching nothing). See analysis/stage1_vs_stage2_xml.md
        Sec 6.1-6.2 for the measured failure this fixes.

  Arm B' — make_rare_coverage_selector(...) : "Stage 2.2", the WORKING
        version of Arm B. Ranks by edge RARITY — sum over the seed's edges
        of 1 / freq(edge), freq counted across the in-band candidates — so a
        seed reaching edges few other seeds touch wins, AFLFast's "reward the
        rarely-hit path" (AFL++ `-p rare`), the deliberate opposite of Arm
        A''. Same COVPB size band, fallback and Jaccard de-dup as Arm A''.

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
from pathlib import Path

# evaluate_seeds.py lives at the repo root, one level up from prompt_tests/.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Same readable-size band build_context.select_diverse_seeds ranks within —
# imported (not re-declared) so the two selectors stay in lockstep if it is
# ever retuned.
from build_context import FEWSHOT_MIN_SIZE, FEWSHOT_MAX_SIZE, select_diverse_seeds
from evaluate_seeds import run_showmap

# --- coverage-per-byte (Arm A'') own size band ---------------------------------
# Deliberately tighter than select_diverse_seeds' 32..3000. Dividing edges by
# size already rewards compactness, so the HARD FLOOR is what stops the ranking
# collapsing onto degenerate minimized fragments, and the low CEILING keeps the
# promoted example out of the multi-KB block-extension-mutant regime that made
# Arm A' regress (analysis/stage1_vs_stage2_xml.md Sec 6.2).
COVPB_MIN_SIZE = 200   # floor: below this an XML seed can't carry a real DTD internal subset
COVPB_MAX_SIZE = 800   # ceiling: ~2x the corpus median for a structurally complete seed


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


def make_coverage_selector(
    target_binary: str,
    target_args=None,
    *,
    afl_showmap_bin: str = "afl-showmap",
    showmap_timeout: int = 30,
    jaccard_max: float = 0.7,
    edge_cache: dict | None = None,
):
    """Build a seed_selector that ranks AFL-queue candidates by TOTAL edges
    hit (afl-showmap), within the same size band as select_diverse_seeds,
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

        banded = [c for c in candidates
                  if FEWSHOT_MIN_SIZE <= c[2] <= FEWSHOT_MAX_SIZE]
        pool = banded or candidates  # same fallback as select_diverse_seeds

        scored = []
        for path, ratio, size in pool:
            edges = _edges_for(path)
            if not edges:  # crash-with-no-map / timeout / genuinely no coverage
                continue
            scored.append((path, edges, ratio, size))

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
    min_size: int = COVPB_MIN_SIZE,
    max_size: int = COVPB_MAX_SIZE,
    jaccard_max: float = 0.7,
    edge_cache: dict | None = None,
):
    """Like make_coverage_selector, but ranks by COVERAGE DENSITY —
    total afl-showmap edges hit / file size in bytes — descending, inside a
    tight [min_size, max_size] band, de-duped by edge-set overlap.

    Why density and not absolute count: ranking on raw edge count in a 3 KB
    band reliably promotes AFL's multi-KB block-extension mutants (repeated
    attributes, character-reference walls) that "cover" a lot only by
    brute-forcing parser loops. The model imitates them and the batch
    mode-collapses (measured: analysis/stage1_vs_stage2_xml.md Sec 2). Edges
    per byte makes a compact, structurally dense seed win instead.

    Why a HARD size band (not `banded or candidates`): dividing by size
    rewards small, so without a floor the ranking drifts to sub-DTD
    minimized fragments that score high edge/B while teaching nothing; the
    ceiling keeps the promoted example out of the mutant regime. If NOTHING
    is in band, defer to build_context.select_diverse_seeds rather than
    silently re-admit the oversized files (analysis Sec 6.2).

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

        banded = [c for c in candidates if min_size <= c[2] <= max_size]
        if not banded:
            # Nothing compact-and-in-band — fall back to the size/diversity
            # selector, never rank the oversized mutants this band excludes.
            return select_diverse_seeds(candidates, n_seeds)

        scored = []
        for path, ratio, size in banded:
            edges = _edges_for(path)
            if not edges or size <= 0:  # crash-no-map / timeout / no coverage
                continue
            density = len(edges) / size
            scored.append((path, edges, density, ratio, size))

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
    min_size: int = COVPB_MIN_SIZE,
    max_size: int = COVPB_MAX_SIZE,
    jaccard_max: float = 0.7,
    edge_cache: dict | None = None,
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

    Same [min_size, max_size] hard band, select_diverse_seeds fallback, and
    Jaccard de-dup as make_coverage_per_byte_selector. Factory / shared
    edge_cache / target_args rules identical to make_coverage_selector.
    """
    args = list(target_args) if target_args is not None else ["@@"]
    cache = edge_cache if edge_cache is not None else {}
    _edges_for = _make_edges_for(target_binary, args, afl_showmap_bin, showmap_timeout, cache)

    def rare_coverage_selector_impl(candidates, n_seeds):
        """candidates: scan_queue's output, [(path, printable_ratio, size), ...]."""
        if not candidates:
            return []

        banded = [c for c in candidates if min_size <= c[2] <= max_size]
        if not banded:
            return select_diverse_seeds(candidates, n_seeds)

        # First pass: every in-band seed's edge set (needed before any edge's
        # frequency across the band is known).
        seen = []
        for path, ratio, size in banded:
            edges = _edges_for(path)
            if not edges:  # crash-no-map / timeout / no coverage
                continue
            seen.append((path, edges, ratio, size))
        if not seen:
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
