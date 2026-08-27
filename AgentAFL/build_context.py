#!/usr/bin/env python3
"""
build_context.py — build a SYSTEM/USER prompt for the AgentAFL seed-generation LLM
from an AFL++ campaign's on-disk state.

Usage:
  python3 build_context.py \
      --campaign-root /home/user/Documents/afl-output-libxml2 --instance main \
      --format "XML document" --n-seeds 3 --n-generate 5 --out prompt.txt

  binary format: + --seed-kind binary --format-hint "Byte order little-endian ('II'); "
      "IFD entry = tag(2)+type(2)+count(4)+value(4)."

Sources read (all under <instance>/, plateau_log excepted):
  - cmdline        exact target invocation
  - fuzzer_stats   campaign snapshot numbers
  - <campaign-parent>/plateau_log.csv   campaign-wide plateau duration (one file for
                                        the whole campaign; --plateau-log to override)
  - queue/         few-shot seed candidates + stale-operator tally
  - hangs/         one hang exemplar (falls back to the newest hangs.<timestamp>/
                   backup if a resume just rotated hangs/)
  - crashes/       one crash exemplar, when present

Pipeline:
  - parse cmdline / fuzzer_stats / plateau_log
  - scan_queue: +cov entries only -> candidates; also tally operators seen >=5x with 0 +cov (stale)
  - select_diverse_seeds: rank by capped size + printable ratio, de-dup by content similarity
  - pick one hang + one crash exemplar
  - assemble_prompt: turn all of the above into a SYSTEM/USER pair
      - plateau duration -> explicit "coverage growth has stopped" statement
      - hang framed as a lead toward deep/expensive paths (not a bug); crash framed as reproduce/extend
      - --seed-kind text -> ask for {fmt} content; binary -> ask for a hex byte stream

Output: SYSTEM/USER prompt pair — plain text, or --json (structured) / --flatten (one pasteable block).
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
    """Fraction of bytes that are printable ASCII/whitespace — cheap "looks like text" proxy.
    Relative ranking signal only, never a pass/fail gate (runs low for valid binary too)."""
    if not data:
        return 0.0
    printable = sum(1 for b in data if 32 <= b <= 126 or b in (9, 10, 13))
    return printable / len(data)


def content_sample(path: Path, n: int = 500) -> str:
    """First n bytes as hex — a fingerprint for near-duplicate comparison only (never shown to LLM).
    Hex not UTF-8 decode, so binary seeds don't all collapse into U+FFFD and look similar."""
    try:
        return path.read_bytes()[:n].hex()
    except OSError:
        return ""


def is_similar(sample_a: str, sample_b: str, threshold: float = 0.6) -> bool:
    """True if two content_sample fingerprints are >threshold similar.
    Keeps few-shot examples from being 3 copies of the same mutant lineage."""
    if not sample_a or not sample_b:
        return False
    return difflib.SequenceMatcher(None, sample_a, sample_b).ratio() > threshold


def format_seed_for_prompt(data: bytes, seed_kind: str, max_chars: int = 2000) -> str:
    """Render a seed for the prompt: as-is text (seed_kind="text") or a space-separated
    hex dump (seed_kind="binary"). Truncation is always marked, never silent.
    Binary shown as hex, not decode(errors="replace"), which would be a U+FFFD wall
    that teaches the model the wrong pattern."""
    if seed_kind == "binary":
        n_bytes = max(1, max_chars // 3)  # "xx " per byte, roughly
        snippet = data[:n_bytes]
        hex_str = " ".join(f"{b:02x}" for b in snippet)
        if len(data) > n_bytes:
            hex_str += f" ...[truncated, {len(data)} bytes total]"
        return hex_str
    text = data.decode("utf-8", errors="replace")
    if len(text) > max_chars:
        return text[:max_chars] + f"...[truncated, {len(text)} chars total]"
    return text


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
    """Last parseable row of campaign-wide plateau_log.csv (plateau_watch.py, one row
    per poll across ALL instances), as a dict of ints: timestamp, best_edges,
    instances, total_crashes, total_hangs, plateau_secs. None if missing/empty/unparseable.

    Walks backward from the end: the log can end in a truncated/null-byte row
    (same artifact as AFL's plot_data files)."""
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
    """One-line prose summary of plateau_log.csv for the prompt (or a fallback
    message if it's missing/empty/unparseable).
    Schema: timestamp,best_edges,instances,total_crashes,total_hangs,plateau_secs"""
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
    """Scan the queue dir once. Returns (candidates, stale_ops, total):
    - candidates: (path, printable_ratio, size) per +cov entry — raw material for selection
    - stale_ops: Counter of named operators seen >=5x with 0 +cov anywhere in the queue
    - total: seed count
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
    """Pick up to n_seeds few-shot exemplars from the candidates.
    Ranked by capped size (so one giant file can't dominate), printable ratio as
    tiebreak, then de-duped by content similarity. Returns [(path, reason), ...]."""
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
    """Smallest file in hangs/, or None.
    Falls back to the newest hangs.<timestamp>/ backup: AFL++ rotates hangs/ on
    every resume, so the live dir can be empty even when real hangs exist."""
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
    """Smallest file in crashes/, or None. No rotation-backup fallback — unlike
    hangs/, crashes/ isn't rotated on resume."""
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
    seed_kind: str = "text",
    format_hint: str = "",
) -> dict:
    """Build the SYSTEM/USER prompt pair from campaign state.

    - seed_kind: "text" asks for {fmt} content directly; "binary" asks for a hex byte stream
    - format_hint: optional grammar/schema/byte-order hint appended to the system prompt.
      Effectively required for binary — without a stated byte order, a low validity
      rate is uninterpretable (bad model, or just wrong guess?)
    """
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

    # System/user structure follows Anthropic's prompt-engineering guide;
    # element numbers below are its section labels.

    # element 2: task context
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
    if format_hint:
        system_lines.append(f"Format-specific hint: {format_hint}")

    # element 9: output formatting
    if seed_kind == "binary":
        system_lines.append(
            f"Respond with exactly {n_generate} candidate {fmt} files, each represented as "
            "a hex byte stream: only the digits 0-9 and a-f, two per byte, optionally "
            "separated by single spaces, no '0x' prefixes and no other characters. Each "
            f"stream goes in its own fenced {fence} code block, in the order you'd try them. "
            "This hex is converted to raw bytes by a fixed hex-to-bytes function with no "
            "further correction of any kind, so every field's byte width, byte order, and "
            "position in the stream must be exactly right the first time. No prose before, "
            "between, or after the blocks — the response is parsed mechanically into seed "
            "files. Do not explain, summarize, or critique the campaign data below — that "
            "is context for you to use, not something to comment on."
        )
    else:
        system_lines.append(
            f"Respond with exactly {n_generate} candidate {fmt} files, each in its own "
            f"fenced {fence} code block, in the order you'd try them. No prose before, between, "
            "or after the blocks — the response is parsed mechanically into seed files. Do not "
            "explain, summarize, or critique the campaign data below — that is context for you "
            "to use, not something to comment on."
        )
    system = "\n".join(system_lines)

    # element 6: input data to process (campaign state)
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

    seed_label = "hex dump" if seed_kind == "binary" else "format"
    u.append(f"=== {len(chosen_seeds)} REAL EXAMPLE SEEDS (shown as {seed_label}) — this is the format to match ===")
    u.append("Chosen for size/printability and mutual diversity, not just recency.")
    for f, reason in chosen_seeds:
        try:
            content = format_seed_for_prompt(f.read_bytes(), seed_kind)
        except OSError:
            content = "<unreadable>"
        u.append(f"--- {f.name} [{reason}] ---")
        u.append(content)
        u.append("")

    u.append("=== A HIGH-COST INPUT (coverage lead) ===")
    if hang:
        try:
            content = format_seed_for_prompt(hang.read_bytes(), seed_kind)
        except OSError:
            content = "<unreadable>"
        u.append(
            "This input makes the parser spend abnormal time/CPU — evidence of deep, "
            "expensive code paths, not itself a bug to reproduce. Use it as a hint that "
            "adjacent/nested structures near it may reach unexplored — or unsafe — branches."
        )
        u.append(f"--- {hang.name} ---")
        u.append(content)
    else:
        u.append("(none available this run)")
    u.append("")

    u.append("=== A PREVIOUSLY FLAGGED INPUT (coverage lead) ===")
    if crash:
        try:
            content = format_seed_for_prompt(crash.read_bytes(), seed_kind)
        except OSError:
            content = "<unreadable>"
        u.append(
            "This input already crashes the target. A structurally similar but distinct "
            "candidate that reaches the same neighborhood of code — or goes further past "
            "it — is a high-value target."
        )
        u.append(f"--- {crash.name} ---")
        u.append(content)
    else:
        u.append("(none available this run)")
    u.append("")

    # element 7: immediate task description
    u.append(
        f"Generate {n_generate} new {fmt} files that are structurally novel compared to the "
        "examples above — not incremental tweaks of what's already there."
    )

    # element 8: precognition (think step by step)
    u.append(
        "Before you give the seeds, think step by step about what the current seeds and "
        "campaign state indicate about the program, the types of inputs in this particular "
        "file format that are likely to exercise new code paths, and how to generate new "
        "seeds that are well-formed and valid files that exercise new code paths in the "
        "target program."
    )

    # element 9: output formatting (reiterated)
    if seed_kind == "binary":
        u.append(
            "Produce byte structures that are valid or near-valid for the format and that "
            "reach code the current corpus does not. Coverage of untested code is the goal. "
            f"Output only the fenced {fence} hex blocks — no analysis, no commentary."
        )
    else:
        u.append(
            "Produce well-formed or near-well-formed files that reach code the current "
            "corpus does not. Coverage of untested code is the goal. Output only the "
            f"fenced {fence} code blocks — no analysis, no commentary."
        )

    return {"system": system, "user": "\n".join(u)}


def render_prompt(prompt: dict, flatten: bool = False, fence_lang: str = "") -> str:
    """Render the prompt dict to text.
    - default (API mode): keep SYSTEM/USER as labelled blocks so roles are obvious
    - --flatten (paste mode): one self-contained turn for a plain chat box — drop the
      role labels, restate the do-not-analyze instruction at the end (a long pasted
      block otherwise pulls a chat model toward "let me summarize this")
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
    ap.add_argument(
        "--seed-kind",
        choices=["text", "binary"],
        default="text",
        help='"text" (default): ask the LLM for {format} content directly. "binary": ask '
        "for a hex byte stream per candidate instead, for a downstream hex-to-bytes step. "
        "Also controls whether example seeds/hangs/crashes are shown as-is or as hex dumps.",
    )
    ap.add_argument(
        "--format-hint",
        default="",
        help="Optional one- or two-sentence grammar/schema hint appended to the system "
        "prompt (e.g. byte order and field widths for a binary format). Effectively "
        "necessary for --seed-kind binary — see assemble_prompt's docstring.",
    )
    ap.add_argument("--n-seeds", type=int, default=3, help="Few-shot example seeds to include.")
    ap.add_argument("--n-generate", type=int, default=5, help="How many new seeds to ask the LLM for.")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument(
        "--plateau-log",
        type=Path,
        default=None,
        help="Campaign-wide plateau_log.csv from plateau_watch.py (one file for the whole "
        "campaign, not per-instance). Defaults to <campaign-root's parent>/plateau_log.csv.",
    )
    ap.add_argument(
        "--no-asan-hint",
        action="store_true",
        help="Omit the ASan/memory-safety framing (use if the target isn't ASan-built).",
    )
    ap.add_argument(
        "--fence-lang",
        default=None,
        help='Language tag for fenced blocks, e.g. "xml". Default: bare ``` for text, "hex" '
        "for binary. Human-readable only — the orchestrator's parser accepts any tag or none.",
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

    fence_lang = args.fence_lang
    if fence_lang is None:
        fence_lang = "hex" if args.seed_kind == "binary" else ""

    plateau_log = args.plateau_log or (args.campaign_root.parent / "plateau_log.csv")
    prompt = assemble_prompt(
        args.campaign_root,
        args.instance,
        args.fmt,
        args.n_seeds,
        args.n_generate,
        plateau_log,
        asan_hint=not args.no_asan_hint,
        fence_lang=fence_lang,
        seed_kind=args.seed_kind,
        format_hint=args.format_hint,
    )
    out_text = (
        json.dumps(prompt, indent=2)
        if args.json
        else render_prompt(prompt, flatten=args.flatten, fence_lang=fence_lang)
    )

    if args.out:
        args.out.write_text(out_text)
        print(f"Wrote prompt to {args.out}", file=sys.stderr)
    else:
        print(out_text)


if __name__ == "__main__":
    main()