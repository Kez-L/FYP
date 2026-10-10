#!/usr/bin/env python3
"""
freeze_baseline_only.py — freeze one target's corpus coverage into a shared
baseline_edges.json, without generating or evaluating a single seed. No API
calls, no model, no provider key needed.

Thin CLI over batch_harness.freeze_baseline — the exact function the real runs
use — so the file this writes means precisely what a run's own frozen baseline
would have meant. Point every arm of a format at the result via
--reuse-baseline and they all score against one identical edge set (PLAN.md
Sec 6) instead of each re-scanning the whole queue.

Why this exists: run_build_context.py's --reuse-baseline silently defaults to
results/baseline_edges.json, which is the *xml* baseline. A new format that
doesn't pass its own path either scores against libxml2 edge IDs (meaningless
numbers that still look plausible) or re-scans ~50k queue files once per arm.
Freeze once up front, then pass the path.

    python3 freeze_baseline_only.py \
        --campaign-root ../afl-output-tidy-20261006 \
        --target ../AFLPlus/tidy-html5-build/tidy_parse_string_fuzzer-afl \
        --target-args "@@" \
        --out results/baseline_edges_tidy.json

--target-args "@@" matters: outside afl-fuzz the libAFLDriver harnesses read a
file argument, and feeding them plain stdin silently parses an EMPTY document
(every input yields the same tiny map). If the edge count comes back a small
constant, check this first.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
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

from batch_harness import freeze_baseline                             # noqa: E402

DEFAULT_INSTANCES = "main,sec1,sec2,sec3,sec4,sec5"                   # unioned, as in the runs


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--campaign-root", type=Path, required=True,
                    help="afl-output-* dir holding the <instance>/queue/ folders")
    ap.add_argument("--instances", default=DEFAULT_INSTANCES,
                    help=f"comma-separated, unioned into one edge set (default: {DEFAULT_INSTANCES})")
    ap.add_argument("--target", required=True, help="instrumented -afl binary to run under afl-showmap")
    ap.add_argument("--target-args", default="@@",
                    help='argv for the target; "@@" is the input file (default: "@@")')
    ap.add_argument("--afl-showmap-bin", default="afl-showmap")
    ap.add_argument("--out", type=Path, required=True,
                    help="where to write the baseline json (e.g. results/baseline_edges_tidy.json)")
    ap.add_argument("--force", action="store_true",
                    help="overwrite --out if it already exists")
    args = ap.parse_args()

    if not args.campaign_root.is_dir():
        raise SystemExit(f"--campaign-root is not a directory: {args.campaign_root}")
    # Replacing a frozen baseline retroactively invalidates every run already
    # scored against it, so never do it by accident.
    if args.out.exists() and not args.force:
        raise SystemExit(f"--out already exists: {args.out}\n"
                         "Refusing to overwrite a frozen baseline — runs already scored "
                         "against it would no longer be comparable. Pass --force if you "
                         "really mean to replace it.")

    instances = [s.strip() for s in args.instances.split(",") if s.strip()]
    # freeze_baseline always writes to <out_dir>/baseline_edges.json; give it a
    # throwaway dir and move the result where the caller asked for it.
    with tempfile.TemporaryDirectory() as tmp:
        ints, meta = freeze_baseline(
            args.campaign_root, instances, args.target, args.target_args.split(),
            args.afl_showmap_bin, Path(tmp), None)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path(tmp) / "baseline_edges.json", args.out)

    print(f"\n[freeze] {len(ints)} distinct edges -> {args.out}")
    for inst, st in (meta.get("per_instance") or {}).items():
        print(f"  {inst}: {st['queue_files']} queue files, {st['edges']} edges")
    print("\nScore runs against it with:\n"
          f"  --reuse-baseline {args.out}")


if __name__ == "__main__":
    main()
