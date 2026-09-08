"""
stage3_strategy_rotation/strategies.py — the "closing_instruction" hook
for build_context.assemble_prompt(closing_instruction=...), implementing
Fuzz4All's three generation strategies (generate-new / mutate-existing /
semantic-equiv), rotated across separate calls rather than baked into one
big multi-strategy prompt — consistent with the "smaller batches, more
calls" conclusion from earlier in this project's design discussion, and
with how Fuzz4All itself uses them (one example + one strategy per call,
not several strategies at once).

Each string uses {fmt}/{n_generate} placeholders, filled in by
assemble_prompt via str.format — see build_context.py's closing_instruction
handling.
"""

from __future__ import annotations

GENERATE_NEW = (
    "Generate {n_generate} new {fmt} files, structurally different from all "
    "of the above."
)

MUTATE_EXISTING = (
    "Generate {n_generate} {fmt} files that are mutated variants of the "
    "examples shown above — same overall structure, changed in a way that "
    "might reach different code."
)

SEMANTIC_EQUIV = (
    "Generate {n_generate} {fmt} files that preserve the same meaning as "
    "the examples above but differ in surface form — different attribute "
    "order, whitespace, encoding, or equivalent syntax choices."
)

STRATEGIES = [GENERATE_NEW, MUTATE_EXISTING, SEMANTIC_EQUIV]


def rotate(call_index: int) -> str:
    """Which strategy string to use for the call_index-th call in a batch
    (0-indexed). Cycles through the three in order; call_index=0 always
    starts with generate-new, matching Fuzz4All's own first-call behavior
    (no example exists yet on the very first call of a fresh trigger)."""
    return STRATEGIES[call_index % len(STRATEGIES)]


if __name__ == "__main__":
    for i in range(5):
        print(i, "->", rotate(i).format(n_generate=1, fmt="XML document"))
