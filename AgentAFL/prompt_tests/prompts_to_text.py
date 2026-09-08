#!/usr/bin/env python3
"""Convert prompt JSON files into readable plain-text files.

Each prompt JSON (e.g. results/<run>/prompts/call_07.json) holds "system" and
"user" strings with escaped "\n" sequences, so viewing the raw file shows one
long line. This script decodes those into real newlines and writes a sibling
.txt file that is actually readable.

Bulk mode (default): point it at the results folder and it walks every
sub-run, into each "prompts" directory, and creates a .txt for every prompt
JSON that does not already have one.

Usage:
    python3 prompts_to_text.py                 # process ./results (or nearby)
    python3 prompts_to_text.py path/to/results # process a specific folder
    python3 prompts_to_text.py --force         # rewrite even if .txt exists
    python3 prompts_to_text.py a/prompts/call_00.json   # single file
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Keys to render, in order, with the heading to print for each.
SECTIONS = [
    ("system", "SYSTEM"),
    ("user", "USER"),
]

# Extra scalar keys shown in a small header, when present.
META_KEYS = ["call", "provider", "model", "temperature", "instance", "ts"]


def render(data: dict) -> str:
    """Build the readable text for one prompt dict."""
    lines: list[str] = []

    meta = [(k, data[k]) for k in META_KEYS if k in data and data[k] is not None]
    if meta:
        for k, v in meta:
            lines.append(f"{k}: {v}")
        lines.append("")
        lines.append("=" * 70)
        lines.append("")

    for key, heading in SECTIONS:
        if key not in data:
            continue
        value = data[key]
        if not isinstance(value, str):
            value = json.dumps(value, indent=2, ensure_ascii=False)
        lines.append(f"===== {heading} =====")
        lines.append("")
        lines.append(value)
        lines.append("")
        lines.append("=" * 70)
        lines.append("")

    text = "\n".join(lines).rstrip() + "\n"
    return text


def convert_file(json_path: Path, force: bool) -> str:
    """Convert one JSON file. Returns a status string for logging."""
    txt_path = json_path.with_suffix(".txt")
    if txt_path.exists() and not force:
        return f"skip (exists)  {txt_path}"

    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return f"ERROR          {json_path}: {exc}"

    if not isinstance(data, dict) or not any(k in data for k, _ in SECTIONS):
        return f"skip (no system/user)  {json_path}"

    txt_path.write_text(render(data), encoding="utf-8")
    return f"wrote          {txt_path}"


def find_json_files(root: Path) -> list[Path]:
    """Collect prompt JSON files under root.

    - a single .json file  -> just that file
    - a "prompts" folder   -> every .json directly inside it
    - any other folder     -> every */prompts/*.json below it (bulk mode)
    """
    if root.is_file():
        return [root] if root.suffix == ".json" else []

    if root.name == "prompts":
        return sorted(root.glob("*.json"))

    found: list[Path] = []
    for prompts_dir in sorted(root.glob("*/prompts")):
        if prompts_dir.is_dir():
            found.extend(sorted(prompts_dir.glob("*.json")))
    # Also handle being pointed directly at a run folder that has ./prompts.
    if not found and (root / "prompts").is_dir():
        found.extend(sorted((root / "prompts").glob("*.json")))
    return found


def default_root() -> Path:
    """Guess the results folder relative to this script / cwd."""
    here = Path(__file__).resolve().parent
    for candidate in (Path.cwd() / "results", here / "results", here):
        if candidate.is_dir():
            return candidate
    return Path.cwd()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("path", nargs="?", type=Path, default=None,
                        help="results folder, a run folder, a prompts folder, "
                             "or a single .json file (default: ./results)")
    parser.add_argument("--force", action="store_true",
                        help="rewrite .txt files that already exist")
    args = parser.parse_args(argv)

    root = args.path if args.path is not None else default_root()
    if not root.exists():
        print(f"path not found: {root}", file=sys.stderr)
        return 1

    json_files = find_json_files(root)
    if not json_files:
        print(f"no prompt JSON files found under {root}", file=sys.stderr)
        return 1

    wrote = skipped = errors = 0
    for jf in json_files:
        status = convert_file(jf, args.force)
        print(status)
        if status.startswith("wrote"):
            wrote += 1
        elif status.startswith("ERROR"):
            errors += 1
        else:
            skipped += 1

    print(f"\ndone: {wrote} written, {skipped} skipped, {errors} errors "
          f"({len(json_files)} scanned)")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
