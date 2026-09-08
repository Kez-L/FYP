"""
stage1_distillation/distill_seed.py — the "bad_seed_renderer" hook for
build_context.assemble_prompt. Instead of pasting a failed seed's raw bytes
into the "avoid repeating" block, make one extra LLM call asking the model
to explain, in one short sentence, why it thinks that seed likely failed —
then show THAT instead of the raw content.

Grounded in FuzzGPT's ablation: asking a model to produce a short bug/failure
description before generating consistently outperformed skipping that step,
and Reflexion's finding that a verbal reflection on a past failure transfers
better than just replaying the raw failed artifact. Neither paper distills a
prior FAILURE quite this way (FuzzGPT's description is for a bug it's about
to reuse, not a self-critique), so this is a genuine, not-yet-published
adaptation — flag it as such in the write-up.

Cost note: this adds one extra Haiku call per bad seed shown (so +2 calls
per prompt at n_history_failure=2). Cheap with Haiku, but real — factor it
into the cost estimate in PLAN.md.
"""

from __future__ import annotations

from pathlib import Path

from shared.llm_client import call_llm

_DISTILL_SYSTEM = (
    "You analyze failed fuzzing seeds. Given a seed that was tried and "
    "produced no new coverage, state in ONE short sentence your best guess "
    "at why. Be specific about the structural reason if you can tell one "
    "(e.g. malformed syntax, redundant with an existing path, too similar "
    "to something already tried). No preamble, no restating the seed, one "
    "sentence only."
)


def distill_bad_seed(seed_content: str, existing_label: str) -> str:
    """One Haiku call: seed content + why-it-was-marked-bad label in, one
    distilled sentence out. existing_label is the programmatic label
    build_context.py already computed (e.g. "produced no coverage at all
    (did not parse)") — pass it as a hint, not a fact to just restate;
    the point is the model's OWN read on the structural cause, which is
    what actually differs from today's raw-dump baseline."""
    user = (
        f"This seed was flagged: {existing_label}\n\n"
        f"Seed content:\n```\n{seed_content}\n```\n\n"
        "Your one-sentence explanation:"
    )
    resp = call_llm(system=_DISTILL_SYSTEM, user=user, max_tokens=100)
    return resp.text.strip()


def bad_seed_renderer(pick: dict, seed_kind: str) -> str:
    """The actual hook passed to assemble_prompt(bad_seed_renderer=...).
    Signature must match build_context.py's contract: (pick, seed_kind) -> str.

    NOTE: reads the seed as text for the distillation call regardless of
    seed_kind — even for a binary format, asking the model to reason in
    prose about the bytes is the point; only build_context.py's OWN
    good-seed examples need to stay in the seed_kind's native rendering.
    """
    raw = pick["path"].read_bytes()
    text_preview = raw.decode("utf-8", errors="replace")[:1000]  # plenty for a one-line diagnosis
    return distill_bad_seed(text_preview, pick["label"])


if __name__ == "__main__":
    # Manual smoke test (needs ANTHROPIC_API_KEY set): python3 -m stage1_distillation.distill_seed
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".xml", delete=False) as f:
        f.write("<root><a>1</a><b>2</b>")  # deliberately unclosed
        path = Path(f.name)
    label = "AVOID — a seed you generated (cycle 3) produced no coverage at all (did not parse)"
    print(bad_seed_renderer({"path": path, "label": label}, "text"))
