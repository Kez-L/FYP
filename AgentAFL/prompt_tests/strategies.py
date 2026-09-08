"""
stage3_strategy_rotation/strategies.py — the "closing_instruction" hook
for build_context.assemble_prompt(closing_instruction=...), implementing
Fuzz4All's three generation strategies (generate-new / mutate-existing /
semantic-equiv). Stage 3 rotates ONE of these per call across the batch
(call 0 -> generate-new, call 1 -> mutate-existing, call 2 -> semantic-equiv,
call 3 -> generate-new, ...) instead of a single fixed instruction for every
call — Fuzz4All's ablation found rotating beat a fixed instruction by cutting
duplicate output over a long run.

Each string uses {fmt}/{n_generate} placeholders, filled in by
assemble_prompt via str.format — see build_context.py's closing_instruction
handling.
"""

from __future__ import annotations

# {fmt}/{n_generate} placeholders are filled by assemble_prompt via str.format.
# One of these is the whole batch's closing instruction for a given call, so
# they're phrased for {n_generate} seeds at once.
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
STRATEGY_NAMES = ["generate-new", "mutate-existing", "semantic-equiv"]


def rotate(call_index: int) -> str:
    """Which strategy string to use for the call_index-th call in a batch
    (0-indexed). Cycles through the three in order; call_index=0 always
    starts with generate-new, matching Fuzz4All's own first-call behavior
    (no example exists yet on the very first call of a fresh trigger)."""
    # return STRATEGIES[0] # this strategy was the only one to actually generate seeds unlike the other 2.
    return STRATEGIES[call_index % len(STRATEGIES)]


def name_for(call_index: int) -> str:
    """Short label for the strategy rotate() picks at call_index — for logging."""
    return STRATEGY_NAMES[call_index % len(STRATEGY_NAMES)]


if __name__ == "__main__":
    for i in range(5):
        print(i, name_for(i), "->", rotate(i).format(n_generate=5, fmt="XML document"))
