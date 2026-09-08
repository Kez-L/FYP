"""
shared/llm_client.py — one small wrapper around the Anthropic API, used by
every stage so the model, retry behavior, and call-shape are identical across
every arm of every test. If this weren't shared, an accidental difference in
retry logic or temperature between two stages' own copies would be a real
confound — exactly the kind of thing the rest of this project has been
careful to avoid.

Requires: pip install anthropic --break-system-packages
Requires: ANTHROPIC_API_KEY set in the environment.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

import anthropic

DEFAULT_MODEL = "claude-haiku-4-5-20251001"  # pin the exact model string used
                                              # for the whole study — see PLAN.md


@dataclass
class LLMResponse:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_s: float


def call_llm(
    system: str,
    user: str,
    model: str = DEFAULT_MODEL,
    temperature: float = 0.8,   # CodaMOSA's most robust single lever — see PLAN.md
    max_tokens: int = 2048,
    max_retries: int = 3,
) -> LLMResponse:
    """One call, system+user in, raw text out. Retries on transient API
    errors with linear backoff; raises on the last failure rather than
    silently returning something misleading (a scoring script that gets an
    empty string back should know that's a call failure, not '0 useful
    seeds')."""
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    last_err = None
    for attempt in range(max_retries):
        t0 = time.monotonic()
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            text = "".join(
                block.text for block in resp.content if block.type == "text"
            )
            return LLMResponse(
                text=text,
                model=model,
                input_tokens=resp.usage.input_tokens,
                output_tokens=resp.usage.output_tokens,
                latency_s=time.monotonic() - t0,
            )
        except Exception as e:  # noqa: BLE001 — genuinely want to retry on anything transient
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"call_llm failed after {max_retries} attempts: {last_err}")


if __name__ == "__main__":
    # Quick manual smoke test: python3 shared/llm_client.py
    r = call_llm(
        system="Reply with exactly one word.",
        user="Say hello.",
    )
    print(f"[{r.model}] {r.output_tokens} out tokens, {r.latency_s:.2f}s -> {r.text!r}")
