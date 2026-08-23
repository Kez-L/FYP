#!/usr/bin/env python3
"""
build_context_generic.py — build a SYSTEM/USER prompt for the AgentAFL
seed-generation LLM from an AFL++ campaign's on-disk state.

Usage:
  python3 build_context_generic.py \
      --campaign-root /home/user/Documents/afl-output-libtiff \
      --instance main \
      --format "TIFF image" \
      --n-seeds 3 \
      --n-generate 5 \
      --out prompt.txt

Sources read:
  - <instance>/cmdline                     exact target invocation
  - <instance>/fuzzer_stats                campaign snapshot numbers
  - <campaign-root-parent>/plateau_log.csv campaign-wide plateau duration; one
                                            file for the whole campaign,
                                            written by plateau_watch.py;
                                            override with --plateau-log
  - <instance>/queue/                      candidate few-shot seeds + stale-operator tally
  - <instance>/hangs/ (falls back to the most recent
    <instance>/hangs.<timestamp>/ backup if hangs/ was just rotated by a resume)
                                            one hang exemplar
  - <instance>/crashes/                    one crash exemplar, when present

Key aspects of context building:
  - Seed selection (scan_queue/select_diverse_seeds): only +cov queue entries
    are candidates; ranked by size (capped) and printable ratio, then
    de-duplicated by content-sample similarity so the few-shot set isn't
    multiple copies of the same mutant lineage.
  - Stale-operator detection: named mutation operators seen >=5 times with
    zero +cov hits are called out, so the LLM is nudged toward structurally
    new documents rather than more of what havoc/splice already tried.
  - Hang vs. crash framing: a hang is presented as a lead toward
    expensive/deep code paths, not a bug; a crash is presented as something
    to reproduce or extend — the prompt language differs deliberately.
  - Plateau interpretation: plateau_log.csv duration is turned into an
    explicit "coverage growth has stopped" statement rather than left as a
    raw number for the model to interpret itself.

Output: a SYSTEM/USER prompt pair — plain text by default (--json for
structured output, --flatten for a single pasteable block) — asking the
model for --n-generate new seed documents in fenced code blocks.
"""

import argparse
import difflib
import json
import re
import sys
from pathlib import Path
from collections import Counter

# AFL queue filename format:
# id:000123,src:000045,time:12345,execs:6789,op:havoc,rep:2,+cov
# Capture groups: 1=id, 2=src, 3=op — the op is group(3), not group(2).
FNAME_RE = re.compile(
    r"id[:_](\d+).*?(?:src[:_](\d+(?:[+,]\d+)*))?.*?op[:_]([a-zA-Z0-9]+)"
)


def printable_ratio(data: bytes) -> float:
    """Fraction of bytes that are printable ASCII/whitespace — a cheap proxy
    for "does this look like well-formed text" vs. havoc-mangled binary soup.
    Format-agnostic: for a binary format this will legitimately run low even
    for valid files, so it's used only as a relative ranking signal, never a
    pass/fail gate."""
    if not data:
        return 0.0
    printable = sum(1 for b in data if 32 <= b <= 126 or b in (9, 10, 13))
    return printable / len(data)


def content_sample(path: Path, n: int = 500) -> str:
    try:
        return path.read_bytes()[:n].decode("utf-8", errors="replace")
    except OSError:
        return ""


def is_similar(sample_a: str, sample_b: str, threshold: float = 0.6) -> bool:
    """Cheap near-duplicate check so few-shot examples aren't 3 copies of the
    same mutant lineage — quality ranking alone tends to surface exactly that."""
    if not sample_a or not sample_b:
        return False
    return difflib.SequenceMatcher(None, sample_a, sample_b).ratio() > threshold


def parse_fuzzer_stats(path: Path) -> dict:
    stats = {}
    if not path.exists():
        return stats
    for line in path.read_text(errors="replace").splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            stats[k.strip()] = v.strip()
    return stats


def parse_cmdline(path: Path) -> dict:
    """Parse AFL's cmdline file: target binary path on line 1, one CLI arg per
    remaining line (e.g. "--noout" / "@@")."""
    if not path.exists():
        return {"binary": "<unknown binary>", "args": ""}
    lines = [l.strip() for l in path.read_text(errors="replace").splitlines() if l.strip()]
    if not lines:
        return {"binary": "<unknown binary>", "args": ""}
    return {"binary": lines[0], "args": " ".join(lines[1:])}


def parse_plateau_row(path: Path) -> dict:
    """
    Return the last parseable row of the campaign-wide plateau_log.csv written
    by plateau_watch.py (one row per poll across ALL instances — not
    per-instance), as a dict of ints: timestamp, best_edges, instances,
    total_crashes, total_hangs, plateau_secs. Returns None if the file is
    missing/empty/entirely unparseable.

    The log can end with a truncated/garbage row (matches the same
    trailing-null-byte artifact seen in AFL's own plot_data files) — walk
    backward from the end for the last row that actually parses.
    """
    if not path.exists():
        return None
    raw = path.read_bytes().replace(b"\x00", b"")
    lines = [l.strip() for l in raw.decode("utf-8", errors="replace").splitlines() if l.strip()]
    if len(lines) < 2:
        return None
    header = [h.strip() for h in lines[0].split(",")]

    for line in reversed(lines[1:]):
        row = dict(zip(header, line.split(",")))
        try:
            return {
                "timestamp": int(row["timestamp"]),
                "best_edges": int(row["best_edges"]),
                "instances": int(row["instances"]),
                "total_crashes": int(row["total_crashes"]),
                "total_hangs": int(row["total_hangs"]),
                "plateau_secs": int(row["plateau_secs"]),
            }
        except (KeyError, ValueError):
            continue
    return None


def parse_plateau_log(path: Path) -> str:
    """
    Summarize the campaign-wide plateau_log.csv written by plateau_watch.py
    (one row per poll across ALL instances — not per-instance, so this is
    read once per build_context run, not per --instance).
    Schema: timestamp,best_edges,instances,total_crashes,total_hangs,plateau_secs
    """
    if not path.exists():
        return f"plateau_log.csv not found at {path} — report edges_found/bitmap_cvg from fuzzer_stats only."
    row = parse_plateau_row(path)
    if row is None:
        raw = path.read_bytes().replace(b"\x00", b"")
        lines = [l.strip() for l in raw.decode("utf-8", errors="replace").splitlines() if l.strip()]
        if len(lines) < 2:
            return "plateau_log.csv present but empty."
        return "plateau_log.csv has no parseable data rows (all trailing rows malformed)."
    plateau_h = row["plateau_secs"] / 3600
    return (
        f"No new edge for {plateau_h:.1f}h across {row['instances']} instance(s) "
        f"(best_edges={row['best_edges']}, "
        f"total_crashes={row['total_crashes']}, "
        f"total_hangs={row['total_hangs']})."
    )


def scan_queue(queue_dir: Path):
    """
    Returns:
      candidates: list of (path, printable_ratio, size) for every +cov queue
                  entry — raw material for selection, not yet picked/ranked
                  down to a final few-shot set.
      stale_ops: Counter of op: values seen with NO +cov flag anywhere in the queue
      total_seeds: int
    """
    files = [f for f in queue_dir.iterdir() if f.is_file() and f.name != "README.txt"]
    files.sort(key=lambda f: f.stat().st_mtime)

    candidates = []
    op_seen = Counter()
    op_with_cov = Counter()
    total = len(files)

    for f in files:
        m = FNAME_RE.search(f.name)
        op = m.group(3) if m else "unknown"
        has_cov = "+cov" in f.name
        op_seen[op] += 1
        if has_cov:
            op_with_cov[op] += 1

        try:
            data = f.read_bytes()
        except OSError:
            continue

        if has_cov:
            candidates.append((f, printable_ratio(data), len(data)))

    # stale = named operators that appear often but never produced +cov.
    # "unknown" (orig:/sync: entries with no op: field) isn't a real operator
    # and telling the LLM "operator unknown is stale" isn't actionable.
    stale_ops = Counter()
    for op, seen in op_seen.items():
        if op == "unknown":
            continue
        cov = op_with_cov.get(op, 0)
        if seen >= 5 and cov == 0:
            stale_ops[op] = seen

    return candidates, stale_ops, total


def select_diverse_seeds(candidates, n_seeds):
    """
    Pick up to n_seeds few-shot exemplars from +cov queue entries, ranked by
    size (capped so one giant file doesn't dominate) and printable ratio as a
    tiebreak, then de-duplicated so few-shot examples aren't 3 copies of the
    same mutant lineage. Returns [(path, reason), ...] so the prompt can say
    why each example was picked.
    """
    if not candidates:
        return []

    ranked = sorted(candidates, key=lambda t: (-min(t[2], 2000), -t[1]))

    chosen = []
    chosen_samples = []
    chosen_paths = set()

    def try_add(path, reason):
        if path in chosen_paths:
            return False
        sample = content_sample(path)
        if any(is_similar(sample, s) for s in chosen_samples):
            return False
        chosen.append((path, reason))
        chosen_samples.append(sample)
        chosen_paths.add(path)
        return True

    for f, _ratio, _size in ranked:
        if len(chosen) >= n_seeds:
            break
        try_add(f, "quality-ranked")

    return chosen


def _pick_smallest(dir_path: Path):
    if not dir_path.exists():
        return None
    candidates = [f for f in dir_path.iterdir() if f.is_file() and f.name != "README.txt"]
    if not candidates:
        return None
    candidates.sort(key=lambda f: f.stat().st_size)
    return candidates[0]


def pick_hang_exemplar(inst_dir: Path):
    """
    Look in the live hangs/ dir first. AFL++ rotates hangs/ into a
    hangs.<ISO-timestamp>/ backup on every resume, so right after a resume
    the live dir is genuinely empty even though real hangs exist — fall back
    to the most recently modified hangs.* backup in that case.
    """
    exemplar = _pick_smallest(inst_dir / "hangs")
    if exemplar:
        return exemplar

    rotated_dirs = sorted(
        (d for d in inst_dir.glob("hangs.*") if d.is_dir()),
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )
    for d in rotated_dirs:
        exemplar = _pick_smallest(d)
        if exemplar:
            return exemplar
    return None


def pick_crash_exemplar(inst_dir: Path):
    """
    Smallest file in crashes/ — cleanest exemplar, cheapest to include.
    Unlike hangs/, crashes/ has no observed <timestamp> rotation backups on
    resume, so no fallback dir search is needed here.
    """
    return _pick_smallest(inst_dir / "crashes")


def assemble_prompt(
    campaign_root: Path,
    instance: str,
    fmt: str,
    n_seeds: int,
    n_generate: int,
    plateau_log: Path,
    asan_hint: bool = True,
    fence_lang: str = "",
) -> dict:
    """Build the actual SYSTEM/USER prompt pair — not just a context dump."""
    inst_dir = campaign_root / instance
    stats = parse_fuzzer_stats(inst_dir / "fuzzer_stats")
    cmdline = parse_cmdline(inst_dir / "cmdline")
    plateau_line = parse_plateau_log(plateau_log)
    candidates, stale_ops, total = scan_queue(inst_dir / "queue")
    chosen_seeds = select_diverse_seeds(candidates, n_seeds)
    hang = pick_hang_exemplar(inst_dir)
    crash = pick_crash_exemplar(inst_dir)

    edges_found = stats.get("edges_found", "?")
    bitmap_cvg = stats.get("bitmap_cvg", "?")
    corpus_count = stats.get("corpus_count", "?")
    saved_crashes = stats.get("saved_crashes", "?")
    saved_hangs = stats.get("saved_hangs", "?")

    fence = f"```{fence_lang}" if fence_lang else "```"

    ##### Prompt elements from suggested areas as given by documentation by Anthropic in Prompt Engineering Guide #####

    ##### Prompt element 2: Task context #####
    system_lines = [
        f"You are an advanced security engineer and fuzzing expert, taking the role of "
        f"creating seeds for a coverage-guided AFL++ fuzzing campaign against a "
        f"{fmt} parser. The goal is to have these seeds be well-formed and valid files "
        f"that exercise new code paths in the target program.",
        f"Target invocation: {cmdline['binary']} {cmdline['args']}".strip() + ".",
    ]
    if asan_hint:
        system_lines.append(
            "This binary is instrumented with AddressSanitizer, so a memory-safety bug "
            "(use-after-free, heap buffer overflow, double-free, memory leak) is a valid "
            "and higher-value finding than new coverage alone — treat either as a win."
        )

    ##### Prompt element 9: Output formatting #####
    system_lines.append(
        f"Respond with exactly {n_generate} candidate {fmt} files, each in its own "
        f"fenced {fence} code block, in the order you'd try them. No prose before, between, "
        "or after the blocks — the response is parsed mechanically into seed files. Do not "
        "explain, summarize, or critique the campaign data below — that is context for you "
        "to use, not something to comment on."
    )
    system = "\n".join(system_lines)

    ##### Prompt element 6: Input data to process (campaign state) #####
    u = []
    u.append("=== CAMPAIGN STATE ===")
    u.append(
        f"edges_found: {edges_found} | bitmap_cvg: {bitmap_cvg} | corpus_count: {corpus_count} "
        f"| saved_crashes: {saved_crashes} | saved_hangs: {saved_hangs}"
    )
    u.append(plateau_line)
    u.append(
        "Interpretation: coverage growth has stopped. Small byte-level mutation "
        "by AFL++ has saturated what it can reach from the "
        "current queue — the fuzzer needs structurally new documents, not incremental "
        "tweaks of what's already there."
    )
    if stale_ops:
        ops_str = ", ".join(f"{op} ({n}x, 0 new-coverage finds)" for op, n in stale_ops.most_common())
        u.append(f"Mutation operators that have stopped paying off recently: {ops_str}.")
    u.append("")

    u.append(f"=== {len(chosen_seeds)} REAL EXAMPLE SEEDS — this is the format to match ===")
    u.append("Chosen for size/printability and mutual diversity, not just recency.")
    for f, reason in chosen_seeds:
        try:
            content = f.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            content = "<unreadable>"
        u.append(f"--- {f.name} [{reason}] ---")
        u.append(content[:2000])
        u.append("")

    u.append("=== A KNOWN EXPENSIVE INPUT (not a crash — a lead) ===")
    if hang:
        try:
            content = hang.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            content = "<unreadable>"
        u.append(
            "This input makes the parser spend abnormal time/CPU — evidence of deep, "
            "expensive code paths, not itself a bug to reproduce. Use it as a hint that "
            "adjacent/nested structures near it may reach unexplored — or unsafe — branches."
        )
        u.append(f"--- {hang.name} ---")
        u.append(content[:2000])
    else:
        u.append("(none available this run)")
    u.append("")

    u.append("=== A KNOWN CRASH (reproduce/extend, not just a lead) ===")
    if crash:
        try:
            content = crash.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            content = "<unreadable>"
        u.append(
            "This input already crashes the target. A structurally similar but distinct "
            "candidate that reaches the same neighborhood of code — or goes further past "
            "it — is a high-value target."
        )
        u.append(f"--- {crash.name} ---")
        u.append(content[:2000])
    else:
        u.append("(none available this run)")
    u.append("")

    ##### Prompt element 7: Immediate task description #####
    u.append(
        f"Generate {n_generate} new {fmt} files that are structurally novel compared to the "
        "examples above — not incremental tweaks of what's already there."
    )

    ##### Prompt element 8: Precognition #####
    u.append(
        "Before you give the seeds, think step by step about what the current seeds and "
        "campaign state indicate about the program, the types of inputs in this particular "
        "file format that are likely to exercise new code paths, and how to generate new "
        "seeds that are well-formed and valid files that exercise new code paths in the "
        "target program."
    )

    ##### Prompt element 9: Output formatting (reiterated) #####
    u.append(
        "Prefer well-formed or near-well-formed files; a crash is a better outcome than "
        f"mere new coverage, but either is a win. Output only the fenced {fence} code "
        "blocks — no analysis, no commentary."
    )

    return {"system": system, "user": "\n".join(u)}


def render_prompt(prompt: dict, flatten: bool = False, fence_lang: str = "") -> str:
    """
    Two audiences, two shapes:
      - API mode (default): keeps system/user as visually distinct blocks so
        it's obvious which text to send under which role — a real API call
        gives the system message extra instruction-following weight that a
        single flattened turn doesn't get.
      - --flatten (paste mode): collapses both into one self-contained turn
        for pasting into a plain chat box with no separate system-prompt
        field. The "=== SYSTEM ===" / "=== USER ===" labels are dropped
        (they're role markers, not content — in a flattened single turn
        they'd just read as more text to react to) and the do-not-analyze
        instruction is restated at the very end, since a single long pasted
        block otherwise tends to pull a chat model toward "let me summarize
        this for you" rather than compliance.
    """
    fence = f"```{fence_lang}" if fence_lang else "```"
    if flatten:
        return (
            f"{prompt['system']}\n\n"
            "---\n\n"
            f"{prompt['user']}\n\n"
            "---\n"
            f"Reminder: output ONLY the fenced {fence} code blocks requested above — "
            "no analysis, no summary, no commentary."
        )
    return f"=== SYSTEM ===\n{prompt['system']}\n\n=== USER ===\n{prompt['user']}\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--campaign-root", required=True, type=Path)
    ap.add_argument("--instance", default="main")
    ap.add_argument(
        "--format",
        required=True,
        dest="fmt",
        help='Human-readable target file format, e.g. "XML document" or "TIFF image". '
        "Threaded into the prompt wherever the format needs naming.",
    )
    ap.add_argument("--n-seeds", type=int, default=3, help="Few-shot example seeds to include.")
    ap.add_argument("--n-generate", type=int, default=5, help="How many new seeds to ask the LLM for.")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument(
        "--plateau-log",
        type=Path,
        default=None,
        help="Path to the campaign-wide plateau_log.csv written by plateau_watch.py "
        "(it is one file for the whole campaign, not per-instance). "
        "Defaults to <campaign-root's parent>/plateau_log.csv.",
    )
    ap.add_argument(
        "--no-asan-hint",
        action="store_true",
        help="Omit the ASan/memory-safety framing (use if the target isn't ASan-built).",
    )
    ap.add_argument(
        "--fence-lang",
        default="",
        help='Language tag for fenced code blocks, e.g. "xml". Defaults to bare ``` '
        "since there's no single right tag across formats.",
    )
    ap.add_argument(
        "--json",
        action="store_true",
        help='Emit {"system": ..., "user": ...} JSON instead of the SYSTEM/USER text block. '
        "Use this for an API call, where system/user go in as separate messages.",
    )
    ap.add_argument(
        "--flatten",
        action="store_true",
        help="Collapse system+user into one self-contained block for pasting into a plain "
        "chat UI with no separate system-prompt field. Ignored if --json is also set.",
    )
    args = ap.parse_args()

    plateau_log = args.plateau_log or (args.campaign_root.parent / "plateau_log.csv")
    prompt = assemble_prompt(
        args.campaign_root,
        args.instance,
        args.fmt,
        args.n_seeds,
        args.n_generate,
        plateau_log,
        asan_hint=not args.no_asan_hint,
        fence_lang=args.fence_lang,
    )
    out_text = (
        json.dumps(prompt, indent=2)
        if args.json
        else render_prompt(prompt, flatten=args.flatten, fence_lang=args.fence_lang)
    )

    if args.out:
        args.out.write_text(out_text)
        print(f"Wrote prompt to {args.out}", file=sys.stderr)
    else:
        print(out_text)


if __name__ == "__main__":
    main()
