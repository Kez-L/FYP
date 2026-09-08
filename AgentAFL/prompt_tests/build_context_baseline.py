"""
stage0_baseline/build_context_baseline.py — the floor reference. Told only
the file format and to generate seeds for fuzzing it; no campaign state, no
example seeds, no plateau framing, no ASan hint. This is what "no prompt
engineering at all" looks like, and every other stage's payoff is measured
against this number, per the study's final comparison (Stage 4).

Deliberately standalone (does not import build_context.py) — the whole
point is that this arm has none of build_context.py's machinery, so it
can't accidentally inherit a fix or a nicety from the real pipeline.
"""

from __future__ import annotations


def assemble_baseline_prompt(fmt: str, n_generate: int, seed_kind: str = "text",
                              fence_lang: str = "") -> dict:
    """The absolute minimum viable prompt: format name + generation count +
    output-format instruction. Nothing else — see module docstring."""
    fence = f"```{fence_lang}" if fence_lang else "```"

    if seed_kind == "binary":
        system = (
            f"Generate seed files for AFL++ fuzzing of a {fmt} parser. "
            f"Respond with exactly {n_generate} hex byte streams (pairs of "
            f"0-9a-f, no '0x', spaces optional), each in its own fenced "
            f"{fence} block. Nothing else."
        )
    else:
        system = (
            f"Generate seed files for AFL++ fuzzing of a {fmt} parser. "
            f"Respond with exactly {n_generate} {fmt} files, each in its "
            f"own fenced {fence} code block. Nothing else."
        )

    user = f"Generate {n_generate} {fmt} files."

    return {"system": system, "user": user}


if __name__ == "__main__":
    import json
    p = assemble_baseline_prompt("XML document", 5)
    print(json.dumps(p, indent=2))
