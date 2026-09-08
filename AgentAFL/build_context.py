#!/usr/bin/env python3
"""
build_context.py — build a SYSTEM/USER prompt for the AgentAFL seed-generation LLM
from an AFL++ campaign's on-disk state.

Usage:
  python3 build_context.py \
      --campaign-root /home/user/Documents/afl-output-libxml2 --instance main \
      --format "XML document" --n-seeds 2 --n-generate 5 --out prompt.txt

  binary format: + --seed-kind binary --format-hint "Byte order little-endian ('II'); "
      "IFD entry = tag(2)+type(2)+count(4)+value(4)."

MINIMAL-PROMPT DESIGN (see conversation notes): state the task once, then let a
couple of real examples do the teaching, instead of explaining campaign mechanics
in prose. Modeled on how ChatAFL / Fuzz4All / CodaMOSA build their own generation
prompts — short instruction + a small number of real examples, no restated
context, no narrated reasoning steps, no campaign telemetry dump. Defaults are
a couple of good seeds (--n-seeds 2) and a couple of bad ones (--n-history-failure
2, unchanged). Hang/crash exemplars are set aside for now — the code to pick them
is untouched, but they're only added to the prompt with --include-hang /
--include-crash, so they can be reintroduced later as their own ablation arm.

Sources read (all under <instance>/, plateau_log excepted):
  - cmdline        exact target invocation
  - <campaign-parent>/plateau_log.csv   campaign-wide plateau duration (one file for
                                        the whole campaign; --plateau-log to override)
  - queue/         few-shot seed candidates
  - hangs/, crashes/   only read when --include-hang / --include-crash is passed

Pipeline:
  - parse cmdline / plateau_log (duration only)
  - scan_queue: +cov entries only -> candidates
  - select_diverse_seeds: restrict to a readable size band, rank smallest-first +
    printable ratio, de-dup by content similarity
  - assemble_prompt: turn the above into a short SYSTEM/USER pair
      - a couple of real seeds that reached new coverage, labeled generically
        (no AFL filename/operator metadata — that's noise for the model)
      - a couple of the LLM's own past seeds that didn't help, labeled with why
      - one closing line asking for N new files, structurally different from
        everything shown
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

# Prior-failed-seed feedback (scan_cycle_history / select_failure_examples):
HISTORY_MAX_CHARS = 700   # per echoed past seed — shorter than the queue examples
HIST_JACCARD_MIN = 0.9    # edge-set overlap for two failed seeds to be "the same mistake"


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
    Keeps few-shot examples from being 3 copies of the same mutant lineage.

    autojunk=False is required: the default heuristic treats any character in
    >1% of a 200+ char string as junk and drops it from matching. content_sample
    is a hex string (16 symbols), so with autojunk on every char is "junk" and
    two near-identical low-alphabet blobs (repeated-digit mutants, RLE binary)
    score ~0 similar and all get kept."""
    if not sample_a or not sample_b:
        return False
    return difflib.SequenceMatcher(None, sample_a, sample_b, autojunk=False).ratio() > threshold


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
    """Kept for standalone logging/debugging use — no longer surfaced in the
    assembled prompt itself (see module docstring: minimal-prompt design)."""
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
    """One-line prose summary of plateau_log.csv (or a fallback message if it's
    missing/empty/unparseable). Kept for standalone logging/debugging use — the
    assembled prompt now uses parse_plateau_row directly for just the duration
    (see module docstring: minimal-prompt design).
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
      (computed for possible future/debugging use — not surfaced in the assembled
      prompt itself; see module docstring)
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


# Few-shot example seeds should be small enough to read and cheap on context,
# but not so minimal they're degenerate. Restrict to this byte band before
# ranking, then rank by distance from FEWSHOT_TARGET_SIZE. The fuzzer's giant
# block-extension mutants (repeated-character walls, all +cov) fall outside the
# band; the sub-32-byte minimized fragments are excluded by the lower bound.
FEWSHOT_MIN_SIZE = 32
FEWSHOT_MAX_SIZE = 3000
FEWSHOT_TARGET_SIZE = 400  # ~ the corpus median for a structurally complete seed


def select_diverse_seeds(candidates, n_seeds):
    """Pick up to n_seeds few-shot exemplars from the candidates.
    Restricted to a readable size band (FEWSHOT_MIN_SIZE..FEWSHOT_MAX_SIZE), then
    ranked by distance from FEWSHOT_TARGET_SIZE (printable ratio as tiebreak),
    then de-duped by content similarity. Returns [(path, reason), ...].

    Nearest-a-target (not largest-first, not smallest-first): a concise but
    structurally complete seed teaches the model more per token than either a big
    mutant blob or a 20-byte minimized fragment. The band falls back to the full
    candidate set only if nothing lands inside it."""
    if not candidates:
        return []

    banded = [c for c in candidates if FEWSHOT_MIN_SIZE <= c[2] <= FEWSHOT_MAX_SIZE]
    ranked = sorted(banded or candidates,
                    key=lambda t: (abs(t[2] - FEWSHOT_TARGET_SIZE), -t[1]))

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
    """Smallest file in hangs/, or None. Only called when --include-hang is set
    (see module docstring — set aside for now, reintroduce as its own ablation).
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
    """Smallest file in crashes/, or None. Only called when --include-crash is
    set (see module docstring). No rotation-backup fallback — unlike hangs/,
    crashes/ isn't rotated on resume."""
    return _pick_smallest(inst_dir / "crashes")


# --------------------------------------------------------------------------
# Prior-cycle feedback: echo the LLM's own recurring failed seeds back at it
# --------------------------------------------------------------------------

def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    union = len(a | b)
    return len(a & b) / union if union else 0.0


def _sample_of(entry: dict) -> str:
    """content_sample of a history entry's seed file, cached on the dict."""
    if "sample" not in entry:
        entry["sample"] = content_sample(entry["path"])
    return entry["sample"]


def scan_cycle_history(run_dir: Path) -> list:
    """This run's prior FAILED LLM seeds (added no new coverage), from
    <run_dir>/cycles.jsonl, in file order (newest cycles last).

    Each item: {cycle_id, status, size, edge_ids (set[int]), edge_sig (str),
    path (Path)}. Only BAD (redundant) / NO_COVERAGE candidates with
    new_edges == 0 whose seed file is still on disk. Missing log -> []."""
    run_dir = Path(run_dir)
    log_path = run_dir / "cycles.jsonl"
    if not log_path.exists():
        return []

    out = []
    for line in log_path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("record_type") != "cycle":
            continue
        cycle_id = rec.get("cycle_id")
        for cand in rec.get("candidates", []):
            if cand.get("status") not in ("BAD (redundant)", "NO_COVERAGE"):
                continue
            if cand.get("new_edges", 0) != 0:
                continue
            rel = cand.get("seed_path") or cand.get("raw_path")
            if not rel:
                continue
            path = run_dir / rel
            if not path.is_file():
                continue
            out.append({
                "cycle_id": cycle_id,
                "status": cand.get("status"),
                "size": path.stat().st_size,
                "edge_ids": set(cand.get("edge_ids") or []),
                "edge_sig": cand.get("edge_sig") or "",
                "path": path,
            })
    return out


def _cluster_failures(failures: list) -> list:
    """Greedy clustering of failed-seed entries. Two seeds cluster if they drove
    the same path through the parser — identical edge_sig, or edge_ids overlap
    >= HIST_JACCARD_MIN. Seeds with no edge data fall back to content_sample
    similarity. Returns [{"members": [...], "rep": entry}, ...]."""
    clusters = []
    for f in failures:
        for cl in clusters:
            rep = cl["rep"]
            if f["edge_sig"] and rep["edge_sig"]:
                same = (f["edge_sig"] == rep["edge_sig"]
                        or _jaccard(f["edge_ids"], rep["edge_ids"]) >= HIST_JACCARD_MIN)
            else:
                same = is_similar(_sample_of(f), _sample_of(rep))
            if same:
                cl["members"].append(f)
                break
        else:
            # "by": how members are grouped — "coverage" (edge sets) or
            # "content" (byte similarity, when edge data is absent). Only affects
            # the wording of the AVOID line.
            clusters.append({"members": [f], "rep": f,
                             "by": "coverage" if f["edge_sig"] else "content"})
    return clusters


def _not_parsing(cluster: dict) -> bool:
    """True if at least half the cluster's members produced no coverage at all."""
    nc = sum(1 for m in cluster["members"] if m["status"] == "NO_COVERAGE")
    return nc * 2 >= len(cluster["members"])


def _cluster_label(cluster: dict) -> str:
    """'AVOID — ...' line describing why a whole cluster of past seeds failed.
    Rendered under a section header that already says "avoid repeating", so
    assemble_prompt strips the leading "AVOID — " to not say it twice."""
    members = cluster["members"]
    cs = sorted({m["cycle_id"] for m in members if m["cycle_id"] is not None})
    where = (f"cycle {cs[0]}" if len(cs) == 1
             else f"cycles {','.join(str(c) for c in cs)}" if cs else "earlier cycles")
    if len(members) == 1:
        why = ("produced no coverage at all (did not parse)" if _not_parsing(cluster)
               else "added 0 new edges")
        return f"AVOID — a seed you generated ({where}) {why}"
    if _not_parsing(cluster):
        why = "produced no coverage at all (did not parse)"
    elif cluster.get("by") == "coverage":
        why = "drove the same already-covered path with 0 new edges"
    else:
        why = "share the same structure and added 0 new edges"
    return f"AVOID — {len(members)} of your past seeds ({where}) {why}"


def select_failure_examples(failures: list, n: int, exclude_samples=None) -> list:
    """Pick up to min(n, 2) prior failed seeds to show the LLM as 'do not repeat'.

    Slot 1: smallest seed in the biggest recurring failure cluster.
    Slot 2 (n >= 2): a NO_COVERAGE seed outside slot 1's cluster ("did not
      parse"); if there is none, the next cluster's representative.
    A pick is dropped if it looks like an already-shown queue example
    (exclude_samples) or like slot 1. Returns [{"label": str, "path": Path}]."""
    n = min(n, 2)
    if n <= 0 or not failures:
        return []

    clusters = _cluster_failures(failures)
    clusters.sort(
        key=lambda cl: (len(cl["members"]),
                        max((m["cycle_id"] or 0) for m in cl["members"])),
        reverse=True,
    )

    chosen = []
    seen_samples = list(exclude_samples or [])

    def accept(path: Path, label: str) -> bool:
        s = content_sample(path)
        if any(is_similar(s, x) for x in seen_samples):
            return False
        chosen.append({"label": label, "path": path})
        seen_samples.append(s)
        return True

    def smallest(members):
        return min(members, key=lambda m: m["size"])

    c1 = clusters[0]
    c1_np = _not_parsing(c1)
    accept(smallest(c1["members"])["path"], _cluster_label(c1))

    if n < 2 or not chosen:
        return chosen

    c1_ids = {id(m) for m in c1["members"]}
    if not c1_np:
        lone = sorted(
            (m for m in failures
             if m["status"] == "NO_COVERAGE" and id(m) not in c1_ids),
            key=lambda m: m["size"],
        )
        for m in lone:
            if accept(m["path"],
                      f"AVOID — a seed you generated (cycle {m['cycle_id']}) "
                      f"produced no coverage at all (did not parse)"):
                return chosen

    for cl in clusters[1:]:
        if accept(smallest(cl["members"])["path"], _cluster_label(cl)):
            return chosen

    return chosen


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
    run_dir: Path | None = None,
    n_history_failure: int = 2,
    include_hang: bool = False,
    include_crash: bool = False,
) -> dict:
    """Build the SYSTEM/USER prompt pair from campaign state.

    MINIMAL-PROMPT DESIGN: state the task once, then let a couple of real
    examples do the teaching — a couple of good seeds (reached new coverage)
    and a couple of bad ones (the LLM's own past misses) — instead of
    explaining campaign mechanics, stats, or reasoning steps in prose.
    Modeled on ChatAFL's plateau-escape prompt, Fuzz4All's per-iteration
    prompt, and CodaMOSA's single-example prompt, all of which are a short
    instruction plus a handful of real examples, nothing more.

    - seed_kind: "text" asks for {fmt} content directly; "binary" asks for a hex byte stream
    - format_hint: optional one-line grammar/schema hint appended to the system
      prompt. Effectively required for binary — without a stated byte order, a
      low validity rate is uninterpretable (bad model, or just wrong guess?)
    - run_dir: this run's output dir. When given (and n_history_failure > 0),
      echo up to n_history_failure (<=2) of the LLM's own past failed seeds from
      <run_dir>/cycles.jsonl back into the prompt as "avoid" examples.
      None -> no failure block is added.
    - include_hang / include_crash: both default False — set aside for now.
      pick_hang_exemplar / pick_crash_exemplar are unchanged and ready for a
      later ablation; pass True to add that block back into the user prompt.
    """
    inst_dir = campaign_root / instance
    cmdline = parse_cmdline(inst_dir / "cmdline")
    plateau_row = parse_plateau_row(plateau_log)
    candidates, _stale_ops, _total = scan_queue(inst_dir / "queue")
    chosen_seeds = select_diverse_seeds(candidates, n_seeds)

    hang = pick_hang_exemplar(inst_dir) if include_hang else None
    crash = pick_crash_exemplar(inst_dir) if include_crash else None

    # Prior failed seeds from this run, rendered for the "avoid" block.
    history_examples = []  # [(label, rendered_content), ...]
    if run_dir is not None and n_history_failure > 0:
        try:
            failures = scan_cycle_history(Path(run_dir))
            exclude = [content_sample(f) for f, _ in chosen_seeds]
            for pick in select_failure_examples(failures, n_history_failure, exclude):
                try:
                    body = format_seed_for_prompt(
                        pick["path"].read_bytes(), seed_kind, max_chars=HISTORY_MAX_CHARS
                    )
                except OSError:
                    continue
                history_examples.append((pick["label"], body))
        except Exception:
            history_examples = []  # never break a cycle over history

    fence = f"```{fence_lang}" if fence_lang else "```"

    # --- SYSTEM: state the task once, no role-play framing, no restated context ---
    system_lines = [
        f"Generate seed files for AFL++ fuzzing of a {fmt} parser.",
        f"Target: {cmdline['binary']} {cmdline['args']}".strip() + ".",
    ]
    if asan_hint:
        system_lines.append("Built with ASan — a memory-safety crash is as valuable as new coverage.")
    if format_hint:
        system_lines.append(format_hint)
    if seed_kind == "binary":
        system_lines.append(
            f"Respond with exactly {n_generate} hex byte streams (pairs of 0-9a-f, no "
            f"'0x', spaces optional), each in its own fenced {fence} block. Nothing else."
        )
    else:
        system_lines.append(
            f"Respond with exactly {n_generate} {fmt} files, each in its own fenced "
            f"{fence} code block. Nothing else."
        )
    system = "\n".join(system_lines)

    # --- USER: a plateau line, a couple of good examples, a couple of bad ones, the ask ---
    u = []

    if plateau_row is not None:
        u.append(f"No new coverage for {plateau_row['plateau_secs'] / 3600:.1f}h.")
        u.append("")

    if chosen_seeds:
        u.append("Reached new coverage:")
        for i, (f, _reason) in enumerate(chosen_seeds, 1):
            try:
                content = format_seed_for_prompt(f.read_bytes(), seed_kind)
            except OSError:
                content = "<unreadable>"
            u.append(f"--- example {i} ---")
            u.append(content)
            u.append("")

    if history_examples:
        u.append("Tried before — no new coverage, avoid repeating:")
        for label, body in history_examples:
            u.append(f"--- {label.removeprefix('AVOID — ')} ---")
            u.append(body)
            u.append("")

    if include_hang and hang:
        try:
            content = format_seed_for_prompt(hang.read_bytes(), seed_kind)
        except OSError:
            content = "<unreadable>"
        u.append("Costly to run (a lead on deep paths, not itself a bug):")
        u.append("--- hang ---")
        u.append(content)
        u.append("")

    if include_crash and crash:
        try:
            content = format_seed_for_prompt(crash.read_bytes(), seed_kind)
        except OSError:
            content = "<unreadable>"
        u.append("Already crashes the target — a near neighbor is high-value:")
        u.append("--- crash ---")
        u.append(content)
        u.append("")

    u.append(f"Generate {n_generate} new {fmt} files, structurally different from all of the above.")

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
        help="Optional one-line grammar/schema hint appended to the system prompt (e.g. "
        "byte order and field widths for a binary format). Effectively necessary for "
        "--seed-kind binary — see assemble_prompt's docstring.",
    )
    ap.add_argument(
        "--n-seeds", type=int, default=2,
        help="Few-shot 'good' example seeds to include (a couple is the default and the "
        "tested design point — see module docstring).",
    )
    ap.add_argument("--n-generate", type=int, default=5, help="How many new seeds to ask the LLM for.")
    ap.add_argument(
        "--run-dir",
        type=Path,
        default=None,
        help="An agentafl_runs/<run> dir. If given, echo up to --n-history-failure of "
        "the LLM's own past failed ('bad') seeds from its cycles.jsonl into the prompt.",
    )
    ap.add_argument(
        "--n-history-failure",
        type=int,
        default=2,
        help="Past failed 'bad' seeds (0-2) to show as 'avoid repeating'. Needs --run-dir.",
    )
    ap.add_argument(
        "--include-hang",
        action="store_true",
        help="Add a hang exemplar to the prompt. Off by default — set aside for a later "
        "ablation (see module docstring).",
    )
    ap.add_argument(
        "--include-crash",
        action="store_true",
        help="Add a crash exemplar to the prompt. Off by default — set aside for a later "
        "ablation (see module docstring).",
    )
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
        run_dir=args.run_dir,
        n_history_failure=args.n_history_failure,
        include_hang=args.include_hang,
        include_crash=args.include_crash,
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