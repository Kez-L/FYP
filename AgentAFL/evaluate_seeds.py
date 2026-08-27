#!/usr/bin/env python3
"""
evaluate_seeds.py — Offline, format-agnostic check for candidate seeds: does
a given file exercise any edge tuple not already present anywhere in the
existing queue? This mirrors what AFL++ itself checks internally for
favouring/culling, just done up front so a report can quote a real new-
coverage rate without waiting through a live campaign.

Format-agnostic: this only ever runs the real target binary against a file
and reads back afl-showmap's coverage output — it never looks at the file's
own structure or content, so it behaves identically for a text-format (XML)
or binary-format (TIFF) seed. There is no well-formedness check in this
module any more (that was XML-specific and only lived here because this
file started out XML-only). If you want a format-specific validity signal,
it belongs next to whatever format-specific tooling produced the seed, not
in a shared, format-agnostic evaluator.

Note on how this is used by agentafl_orchestrator.py: the classification
here (CRASH / TIMEOUT / NO_COVERAGE / GOOD / BAD) is informational only —
the orchestrator injects every candidate that materialized into a real seed
file regardless of this result, and uses this purely to populate its
per-cycle and per-run logs (how many showed new coverage, etc.). It doesn't
gate afl-addseeds.

Usage:
  python3 evaluate_seeds.py \
      --target /path/to/xmllint-afl \
      --target-args "--noout @@" \
      --queue-dir /home/user/Documents/afl-output-libxml2/main/queue \
      --candidates-dir ./llm_candidates \
      --out results.csv

--target-args MUST match the real campaign's invocation (check the instance's
`cmdline` file) — using different args than the live campaign exercises a
different code path and makes "new edges" numbers meaningless. For libxml2
in this project that's "--noout @@", not just "@@" (the default below).

Requires afl-showmap on PATH (built alongside your afl-cc target).
"""

import argparse
import csv
import subprocess
import sys
import tempfile
from pathlib import Path


def run_showmap(afl_showmap_bin, target, target_args, input_path, timeout=30):
    """
    Returns (status, edges): status is "ok", "crash", "timeout", or
    "no_coverage" — read from afl-showmap's own exit code (0=ok, 1=timeout/
    exec problem, 2=crash) rather than guessed from stdout, so crash and
    timeout are no longer indistinguishable.

    IMPORTANT: input_path must be passed the same way (absolute vs. relative)
    that the live campaign passes it, or you'll get different target code
    paths for the identical file — this bit us once already on libxml2 (a
    relative-path candidate run and an absolute-path baseline run disagreed
    on 3 edges that turned out to be pure path-handling artifacts, not real
    new coverage). AFL always substitutes an absolute path for @@, so
    input_path should be absolute here too.
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
        # afl-showmap text format: "<edge_id>:<hit_count>"
        if ":" in line:
            edge_id = line.split(":", 1)[0].strip()
            if edge_id.isdigit():
                edges.add(edge_id)

    if result.returncode == 2:
        return "crash", edges
    if result.returncode == 1:
        return "timeout", edges
    if not edges:
        return "no_coverage", edges
    return "ok", edges


def build_baseline(afl_showmap_bin, target, target_args, queue_dir, timeout=30):
    """
    Full-corpus baseline via afl-showmap's own -C (combined/union) batch
    mode — one forkserver-backed pass over the whole queue, not one fresh
    subprocess per file. This is both faster AND more accurate than
    sampling: on a real ~12k-file libxml2 queue it takes under a minute, and
    a sampled subset systematically undercounts real coverage, which
    inflates false "new edge" claims for candidates — verified empirically
    on that queue: a 500-file sample reported 2 and 9 "new" edges for two
    candidates that a full-corpus baseline showed actually have 0 and 3
    respectively. Format-agnostic — afl-showmap reads whatever's in
    queue_dir and runs it through the real target; it doesn't care what
    format the files are.
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
                    baseline.add(edge_id)
        return baseline
    finally:
        out_path.unlink(missing_ok=True)


def evaluate_candidate(afl_showmap_bin, target, target_args, candidate_path: Path,
                        baseline: set, timeout: int = 30) -> dict:
    """
    Classify one candidate seed against a precomputed full-queue baseline.
    Format-agnostic: purely a function of what afl-showmap reports for this
    file against this target — never the file's own content or structure.

    Returns:
      {seed, status, new_edges, total_edges}
    status is one of: "CRASH", "TIMEOUT", "NO_COVERAGE",
    "GOOD (novel coverage)", "BAD (redundant)".
    """
    status, edges = run_showmap(afl_showmap_bin, target, target_args, candidate_path,
                                 timeout=timeout)

    if status in ("crash", "timeout"):
        return {
            "seed": candidate_path.name, "status": status.upper(),
            "new_edges": 0, "total_edges": len(edges),
        }
    if status == "no_coverage":
        return {
            "seed": candidate_path.name, "status": "NO_COVERAGE",
            "new_edges": 0, "total_edges": 0,
        }

    new_edges = edges - baseline
    good = len(new_edges) > 0
    return {
        "seed": candidate_path.name,
        "status": "GOOD (novel coverage)" if good else "BAD (redundant)",
        "new_edges": len(new_edges),
        "total_edges": len(edges),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", required=True)
    ap.add_argument("--target-args", default="@@",
                     help="space-separated args, use @@ as the input placeholder — "
                     "MUST match the live campaign's cmdline file (e.g. libxml2 in "
                     "this project needs \"--noout @@\", not just \"@@\")")
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
            fh, fieldnames=["seed", "status", "new_edges", "total_edges"]
        )
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nNew-coverage rate: {n_good}/{len(candidates)} "
          f"({n_good/len(candidates)*100:.1f}%)")
    print(f"Full results written to {args.out}")


if __name__ == "__main__":
    main()