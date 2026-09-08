"""
shared/parse_seeds.py — turn one LLM response (raw text, as returned by
shared.llm_client.call_llm) into a list of individual seed byte-strings,
matching the fenced-block convention build_context.py's system prompt asks
for ("each in its own fenced ``` code block. Nothing else.").

Shared for the same reason as llm_client.py: every stage must parse
responses identically, or a parsing quirk in one stage's own copy could look
like a "this prompt design produced fewer usable seeds" result when it was
actually just a parsing bug.
"""

from __future__ import annotations

import re

# Matches ``` or ```lang ... ``` blocks, non-greedy, across newlines.
_FENCE_RE = re.compile(r"```[a-zA-Z0-9_-]*\n(.*?)```", re.DOTALL)


def parse_seeds(response_text: str, seed_kind: str = "text") -> list[bytes]:
    """Extract every fenced block as one candidate seed.

    - seed_kind="text": each block's raw text, UTF-8 encoded.
    - seed_kind="binary": each block is expected to be a hex byte stream
      (matches format_seed_for_prompt's binary rendering); non-hex
      characters (whitespace, stray "0x") are stripped before decoding.
      A block that still doesn't decode to valid hex is skipped, not
      crashed on — a batch of 5 with 1 malformed block should still yield 4
      usable seeds, not zero.

    Returns [] if there are no fenced blocks at all (e.g. the model ignored
    the "nothing else" instruction and returned prose) — callers should log
    this as a formatting failure, not silently treat it as "0 useful seeds"
    in the same bucket as a batch that tried and failed to find coverage.
    """
    blocks = _FENCE_RE.findall(response_text)
    if not blocks:
        return []

    seeds: list[bytes] = []
    for block in blocks:
        block = block.strip("\n")
        if seed_kind == "binary":
            hex_str = re.sub(r"0x|[^0-9a-fA-F]", "", block)
            if len(hex_str) % 2 != 0:
                hex_str = hex_str[:-1]  # drop a trailing odd nibble rather than fail the block
            try:
                seeds.append(bytes.fromhex(hex_str))
            except ValueError:
                continue  # malformed block — skip it, don't drop the whole batch
        else:
            seeds.append(block.encode("utf-8", errors="replace"))
    return seeds


def write_seeds(seeds: list[bytes], out_dir, prefix: str = "cand") -> list:
    """Write each seed to <out_dir>/<prefix>_NN, returning the list of Paths.
    Plain, boring, and deliberately not clever — every downstream tool
    (afl-showmap, afl-addseeds, your existing evaluate_seeds.py) just wants
    real files on disk."""
    from pathlib import Path

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, seed in enumerate(seeds):
        p = out_dir / f"{prefix}_{i:02d}"
        p.write_bytes(seed)
        paths.append(p)
    return paths
