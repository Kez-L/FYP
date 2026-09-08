#!/usr/bin/env python3
"""
evaluate_seeds.py — offline check: does a candidate file hit any edge not already
in the existing queue? Mirrors AFL++'s own favour/cull check, run up front so a
report can quote a real new-coverage rate without waiting through a live campaign.

Usage:
  python3 evaluate_seeds.py \
      --target /path/to/xmllint-afl --target-args "--noout @@" \
      --queue-dir /home/user/Documents/afl-output-libxml2/main/queue \
      --candidates-dir ./llm_candidates --out results.csv

  needs afl-showmap on PATH (built alongside your afl-cc target).

Notes:
  - format-agnostic: only runs the target + reads afl-showmap coverage, never inspects
    file structure — behaves the same for XML or TIFF. No well-formedness check lives here.
  - --target-args MUST match the live campaign's cmdline file, or "new edges" is meaningless
    (libxml2 here needs "--noout @@", not just "@@")
  - used by agentafl_orchestrator.py for logging only — the classification
    (CRASH/TIMEOUT/NO_COVERAGE/GOOD/BAD) never gates afl-addseeds
"""

import argparse
import csv
import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path


def _edge_fields(edges: set) -> dict:
    """Stable, JSON-friendly representation of a seed's afl-showmap edge set.
    edge_ids: sorted ints (used for Jaccard similarity between seeds).
    edge_sig: sha1 of the joined ids — cheap exact-match key for clustering.
    Logged per candidate so build_context can later group failed seeds by
    "drove the same path through the parser"."""
    ids = sorted(int(e) for e in edges)
    joined = ",".join(map(str, ids))
    sig = hashlib.sha1(joined.encode()).hexdigest() if ids else ""
    return {"edge_ids": ids, "edge_sig": sig}


def run_showmap(afl_showmap_bin, target, target_args, input_path, timeout=30):
    """Run afl-showmap on one input. Returns (status, edges):
    status = "ok" | "crash" | "timeout" | "no_coverage", from afl-showmap's exit
    code (0/1/2) not stdout, so crash vs timeout stay distinct.

    input_path is resolved to absolute (AFL substitutes an absolute path for @@).
    Passing it relative diverges the target's path handling — cost us 3 phantom
    "new" edges on libxml2 once (relative candidate vs absolute baseline run).
    """
    cmd = [afl_showmap_bin, "-e", "-t", str(timeout * 1000), "-o", "-", "--", target]
    for a in target_args:
        cmd.append(str(Path(input_path).resolve()) if a == "@@" else a)

    try:
        result = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
    except subprocess.TimeoutExpired:
        return "timeout", set()

    edges = set()
    for line in result.stdout.decode(errors="replace").splitlines():
        # afl-showmap text format: "<edge_id>:<hit_count>", edge_id zero-padded
        # ("000005:1"). Store as int so the id space is canonical regardless of
        # padding — a str set would make "000005" != "5" and break every diff.
        if ":" in line:
            edge_id = line.split(":", 1)[0].strip()
            if edge_id.isdigit():
                edges.add(int(edge_id))

    if result.returncode == 2:
        return "crash", edges
    if result.returncode == 1:
        return "timeout", edges
    if not edges:
        return "no_coverage", edges
    return "ok", edges


def build_baseline(afl_showmap_bin, target, target_args, queue_dir, timeout=30):
    """Union edge set over the whole queue, via afl-showmap -C batch mode
    (one forkserver pass, not a subprocess per file). ~1 min on a ~12k-file
    libxml2 queue.

    Full corpus, not a sample: sampling undercounts coverage and inflates
    false "new edge" claims — a 500-file sample once reported 2 and 9 new
    edges for candidates that the full baseline showed at 0 and 3.
    """
    print(f"[baseline] scanning full queue at {queue_dir} (single batched pass)...",
          file=sys.stderr)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".showmap", delete=False) as tmp:
        out_path = Path(tmp.name)
    try:
        cmd = [
            afl_showmap_bin, "-e", "-q", "-C",
            "-i", str(Path(queue_dir).resolve()),
            "-o", str(out_path),
            "--", target, *target_args,
        ]
        subprocess.run(cmd, capture_output=True, timeout=max(600, timeout * 20))

        baseline = set()
        for line in out_path.read_text(errors="replace").splitlines():
            if ":" in line:
                edge_id = line.split(":", 1)[0].strip()
                if edge_id.isdigit():
                    baseline.add(int(edge_id))  # int, matching run_showmap
        return baseline
    finally:
        out_path.unlink(missing_ok=True)


def evaluate_candidate(afl_showmap_bin, target, target_args, candidate_path: Path,
                        baseline: set, timeout: int = 30) -> dict:
    """Classify one candidate against a precomputed full-queue baseline.
    Returns {seed, status, new_edges, total_edges, edge_ids, edge_sig}; status is
    one of "CRASH", "TIMEOUT", "NO_COVERAGE", "GOOD (novel coverage)",
    "BAD (redundant)". edge_ids/edge_sig describe the seed's own edge set (see
    _edge_fields) — logging only, same as the rest of this dict."""
    status, edges = run_showmap(afl_showmap_bin, target, target_args, candidate_path,
                                 timeout=timeout)

    # Canonicalise the baseline to int ids too. build_baseline already returns
    # ints, but a caller may hand us a set reloaded from JSON or built elsewhere
    # (e.g. as zero-padded / plain strings); without this, edges - baseline can
    # silently subtract nothing and mark every candidate GOOD.
    baseline = {int(e) for e in baseline}

    if status in ("crash", "timeout"):
        return {
            "seed": candidate_path.name, "status": status.upper(),
            "new_edges": 0, "total_edges": len(edges), **_edge_fields(edges),
        }
    if status == "no_coverage":
        return {
            "seed": candidate_path.name, "status": "NO_COVERAGE",
            "new_edges": 0, "total_edges": 0, **_edge_fields(edges),
        }

    new_edges = edges - baseline
    good = len(new_edges) > 0
    return {
        "seed": candidate_path.name,
        "status": "GOOD (novel coverage)" if good else "BAD (redundant)",
        "new_edges": len(new_edges),
        "total_edges": len(edges),
        **_edge_fields(edges),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", required=True)
    ap.add_argument("--target-args", default="@@",
                     help="space-separated, @@ = input placeholder. MUST match the live "
                     "campaign's cmdline file (libxml2 here needs \"--noout @@\", not \"@@\").")
    ap.add_argument("--queue-dir", required=True, type=Path)
    ap.add_argument("--candidates-dir", required=True, type=Path)
    ap.add_argument("--afl-showmap-bin", default="afl-showmap")
    ap.add_argument("--out", type=Path, default=Path("results.csv"))
    args = ap.parse_args()

    target_args = args.target_args.split()

    baseline = build_baseline(
        args.afl_showmap_bin, args.target, target_args, args.queue_dir
    )
    print(f"Baseline: {len(baseline)} unique edges across the FULL queue.",
          file=sys.stderr)

    candidates = sorted(
        f for f in args.candidates_dir.iterdir()
        if f.is_file()
    )
    if not candidates:
        print("No candidate seed files found.", file=sys.stderr)
        sys.exit(1)

    rows = [
        evaluate_candidate(args.afl_showmap_bin, args.target, target_args, f, baseline)
        for f in candidates
    ]
    n_good = sum(1 for r in rows if r["status"] == "GOOD (novel coverage)")

    with args.out.open("w", newline="") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=["seed", "status", "new_edges", "total_edges"],
            extrasaction="ignore",  # evaluate_candidate also returns edge_ids/edge_sig
        )
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nNew-coverage rate: {n_good}/{len(candidates)} "
          f"({n_good/len(candidates)*100:.1f}%)")
    print(f"Full results written to {args.out}")


if __name__ == "__main__":
    main()