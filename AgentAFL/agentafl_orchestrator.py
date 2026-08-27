#!/usr/bin/env python3
"""
agentafl_orchestrator.py — Unattended plateau-triggered LLM seed injection for a
live AFL++ campaign. Polls plateau_log.csv (written by plateau_watch.py); when the
campaign has genuinely plateaued (with a cooldown and a hard call-budget cap so a
24h stall can't quietly run away with API spend), it assembles a prompt from live
campaign state (build_context.py), calls the LLM, parses fenced code blocks out
of the response, and materializes each one into a seed file. Every materialized
seed then gets a quick per-candidate afl-showmap check (evaluate_seeds.py) —
informational only, for logging real "how many showed new coverage" numbers —
and is injected into the campaign via afl-addseeds regardless of that result.
Every cycle, and the whole run, is logged: cycles.jsonl gets one record per
cycle with full per-candidate detail; the run-end record has run-wide totals.

Format-agnostic by design: --format names the target file format for the
prompt, and --seed-kind controls how a fenced block becomes a seed file.
  --seed-kind text   (default): the fenced block IS the seed file's content,
                      written as-is (e.g. XML).
  --seed-kind binary: the fenced block is a hex byte stream; converted to raw
                      bytes by a fixed, format-agnostic hex-to-bytes step
                      (hex_block_to_bytes) before being written. This is the
                      naive path — no structured intermediate representation,
                      no per-format byte-layout assembler — the LLM's hex has
                      to be correct as given. See build_context.py's module
                      docstring for why this is a deliberate baseline, not
                      the intended long-run mechanism for binary formats.
No targeting happens on the format name itself beyond substituting it into
the prompt text (via build_context.py) — the only behavioural branch in this
whole pipeline is the binary/text one above.

Every fenced block the LLM produces is saved verbatim (cand_XX.raw) before
any decoding is attempted, so nothing generated is ever lost even if it
later fails to decode or turns out useless.

Usage:
  python3 agentafl_orchestrator.py \
      --campaign-root /home/user/Documents/afl-output-libxml2-treatment1 \
      --format "XML document" \
      --run-label treatment1

  python3 agentafl_orchestrator.py \
      --campaign-root /home/user/Documents/afl-output-libtiff-treatment1 \
      --format "TIFF image" \
      --seed-kind binary \
      --format-hint "Byte order little-endian ('II'); IFD entry = tag(2)+type(2)+count(4)+value(4)." \
      --run-label treatment1

Requires CLAUDE_API_KEY in the environment or in --env-file (default
/home/user/Documents/.env). Never logs the key value.
"""

import argparse
import hashlib
import json
import logging
import os
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import build_context
import evaluate_seeds

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_API_VERSION = "2023-06-01"

# Fenced code block: opening ``` optionally followed by a language-tag word
# (xml, hex, json, or nothing), then the body, then a closing ```.
# Format-agnostic — doesn't assume any particular tag, since build_context.py
# picks a tag suited to --seed-kind (bare or "xml" for text, "hex" for
# binary) but the model sometimes drops it regardless. Non-greedy so a
# truncated/unterminated trailing block (e.g. cut off by MAX_TOKENS) simply
# doesn't match rather than swallowing everything after it — earlier,
# properly-closed blocks in the same response still parse correctly.
# Known limitation: content containing a literal ``` sequence would
# terminate its own block early — not defended against in V1; low-risk for
# XML (backticks have no special meaning) and even lower risk for a hex
# stream (only hex digits/spaces are valid output there).
FENCE_RE = re.compile(r"```[ \t]*[A-Za-z0-9_+-]*[ \t]*\n(.*?)```", re.DOTALL)


class GeminiAPIError(Exception):
    def __init__(self, message, retryable=False, http_status=None):
        super().__init__(message)
        self.retryable = retryable
        self.http_status = http_status


# The Claude path raises the same error shape; aliased (not a new type) so the
# existing `except GeminiAPIError` sites catch both providers unchanged.
ClaudeAPIError = GeminiAPIError


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cycle_tag(cycle_id: int) -> str:
    return f"cycle_{cycle_id:04d}"


def _count_queue(queue_dir: Path) -> int:
    try:
        return sum(1 for f in queue_dir.iterdir() if f.is_file())
    except OSError:
        return 0


def append_jsonl_record(path: Path, record: dict):
    """Append one event to cycles.jsonl and flush immediately — the log is
    append-only/event-sourced and meant to be safe to tail live during a 24h
    run. default=str is a defensive catch-all so an unexpected non-JSON-
    serializable value in a record can't crash the run mid-cycle."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(record, default=str) + "\n")
        fh.flush()


def run_subprocess_safe(cmd, logger=None, **kwargs):
    """Run a subprocess, catching everything so one external-tool failure logs
    and returns rather than killing the 24h run. Returns (ok, result_or_exc)."""
    try:
        result = subprocess.run(cmd, capture_output=True, **kwargs)
        return True, result
    except Exception as e:
        if logger:
            logger.error(f"Subprocess failed: {' '.join(str(c) for c in cmd)}: {e}")
        return False, e


def make_run_id(label: str) -> str:
    return f"{label}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire_run_lock(runs_root: Path, campaign_root: Path) -> Path:
    """Refuse to start a second orchestrator against the same campaign_root
    while one is already running — cheap insurance against afl-addseeds's
    id-assignment TOCTOU (it assigns ids by scanning addseeds/queue/'s
    current contents at call time)."""
    runs_root.mkdir(parents=True, exist_ok=True)
    campaign_hash = hashlib.sha1(str(Path(campaign_root).resolve()).encode()).hexdigest()[:12]
    lock_path = runs_root / f".orchestrator-{campaign_hash}.lock"
    if lock_path.exists():
        try:
            existing = json.loads(lock_path.read_text())
            pid = existing.get("pid")
            if pid and _pid_alive(pid):
                raise SystemExit(
                    f"Another orchestrator (pid {pid}) appears to already be running "
                    f"against {campaign_root} (lock: {lock_path}). Refusing to start a "
                    "second one against the same campaign."
                )
        except (json.JSONDecodeError, OSError):
            pass  # stale/corrupt lock — proceed and overwrite
    lock_path.write_text(json.dumps({
        "pid": os.getpid(),
        "campaign_root": str(Path(campaign_root).resolve()),
        "started_at": _now_iso(),
    }))
    return lock_path


def release_run_lock(lock_path: Path):
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        pass


# --------------------------------------------------------------------------
# .env / Gemini API client
# --------------------------------------------------------------------------

def load_api_key(env_file: Path, var_name: str = "CLAUDE_API_KEY") -> str:
    """os.environ[var_name] wins if set, else parse env_file: split each
    non-blank/non-'#' line on the first '=', strip optional surrounding
    quotes. var_name is CLAUDE_API_KEY for --llm-provider claude (default),
    GEMINI_API_KEY for --llm-provider gemini. Raises SystemExit with a
    value-free message if missing. Never logs/prints the key; callers must
    not interpolate it into any log or exception message."""
    key = os.environ.get(var_name)
    if key:
        return key.strip()
    if env_file.exists():
        for line in env_file.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() != var_name:
                continue
            v = v.strip().strip('"').strip("'")
            if v:
                return v
    raise SystemExit(
        f"{var_name} not found in the environment or in {env_file} — "
        "set it before running the orchestrator."
    )


def _sleep_backoff(attempt: int, base_s: float, logger=None):
    delay = min(30.0, base_s * (2 ** (attempt - 1))) + random.uniform(0, 1)
    if logger:
        logger.debug(f"Retrying LLM call in {delay:.1f}s (attempt {attempt})")
    time.sleep(delay)


_FATAL_HTTP = (400, 401, 403)
_RETRYABLE_HTTP = (429, 500, 502, 503, 504)
_BLOCKED_FINISH_REASONS = {"SAFETY", "RECITATION", "PROHIBITED_CONTENT", "BLOCKLIST", "OTHER"}


def call_gemini(api_key, model, system_text, user_text, max_output_tokens=4096,
                 temperature=0.9, timeout_s=60, max_retries=3, backoff_base_s=2.0,
                 logger=None) -> dict:
    """POST to Gemini's generateContent endpoint via stdlib urllib (no extra
    dependency needed for an unattended 24h script). Retries transient
    failures with exponential backoff + jitter; fails fast on anything that
    retrying can't fix (bad key, bad request, safety block).

    Returns {text, finish_reason, block_reason, raw, http_status,
    attempt_count, response_truncated} on success, or raises GeminiAPIError.
    """
    url = f"{GEMINI_API_BASE}/{model}:generateContent"
    body = {
        "systemInstruction": {"parts": {"text": system_text}},
        "contents": [{"parts": [{"text": user_text}]}],
        "generationConfig": {"temperature": temperature, "maxOutputTokens": max_output_tokens},
    }
    payload = json.dumps(body).encode("utf-8")

    last_err = None
    for attempt in range(1, max_retries + 1):
        req = urllib.request.Request(
            url, data=payload, method="POST",
            headers={"Content-Type": "application/json", "X-goog-api-key": api_key},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                raw_text = resp.read().decode("utf-8", errors="replace")
                http_status = resp.status
        except urllib.error.HTTPError as e:
            http_status = e.code
            raw_text = e.read().decode("utf-8", errors="replace")
            if http_status in _FATAL_HTTP:
                raise GeminiAPIError(
                    f"Gemini API fatal error {http_status}: {raw_text[:500]}",
                    retryable=False, http_status=http_status,
                )
            last_err = GeminiAPIError(
                f"Gemini API HTTP {http_status}: {raw_text[:500]}",
                retryable=True, http_status=http_status,
            )
            if attempt < max_retries:
                _sleep_backoff(attempt, backoff_base_s, logger)
            continue
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = GeminiAPIError(f"Gemini API network error: {e}", retryable=True)
            if attempt < max_retries:
                _sleep_backoff(attempt, backoff_base_s, logger)
            continue

        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError as e:
            last_err = GeminiAPIError(f"Gemini API returned non-JSON response: {e}", retryable=True)
            if attempt < max_retries:
                _sleep_backoff(attempt, backoff_base_s, logger)
            continue

        block_reason = (parsed.get("promptFeedback") or {}).get("blockReason")
        if block_reason:
            raise GeminiAPIError(f"Prompt blocked by Gemini: {block_reason}", retryable=False)

        candidates = parsed.get("candidates") or []
        if not candidates:
            last_err = GeminiAPIError("Gemini API returned zero candidates", retryable=True)
            if attempt < max_retries:
                _sleep_backoff(attempt, backoff_base_s, logger)
            continue

        cand = candidates[0]
        finish_reason = cand.get("finishReason")
        parts = (cand.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts)

        if not text:
            if finish_reason in _BLOCKED_FINISH_REASONS:
                raise GeminiAPIError(
                    f"Gemini candidate blocked/empty, finishReason={finish_reason}", retryable=False,
                )
            last_err = GeminiAPIError(
                f"Gemini API returned empty text, finishReason={finish_reason}", retryable=True,
            )
            if attempt < max_retries:
                _sleep_backoff(attempt, backoff_base_s, logger)
            continue

        return {
            "text": text,
            "finish_reason": finish_reason,
            "block_reason": None,
            "raw": parsed,
            "http_status": http_status,
            "attempt_count": attempt,
            "response_truncated": finish_reason == "MAX_TOKENS",
        }

    raise last_err or GeminiAPIError("Gemini API call failed after retries", retryable=True)


def call_claude(api_key, model, system_text, user_text, max_output_tokens=4096,
                temperature=None, timeout_s=60, max_retries=3, backoff_base_s=2.0,
                logger=None) -> dict:
    """POST to Anthropic's /v1/messages endpoint via stdlib urllib — same
    no-extra-dependency contract as call_gemini() so the 24h script stays
    importable without a virtualenv. Retries transient failures with
    exponential backoff + jitter; fails fast on anything retrying can't fix
    (bad key, bad request, safety refusal).

    Returns the same dict shape as call_gemini(): {text, finish_reason,
    block_reason, raw, http_status, attempt_count, response_truncated}, or
    raises ClaudeAPIError (== GeminiAPIError).

    temperature: left unset by default. The current Claude models
    (claude-opus-5 / claude-sonnet-5 and the 4.6+ family) reject top-level
    sampling params — passing `temperature` returns HTTP 400 — so it is only
    sent when a caller explicitly provides one (e.g. for an older model).
    Seed diversity comes from the model's default sampling instead.
    """
    body = {
        "model": model,
        "max_tokens": max_output_tokens,
        "system": system_text,
        "messages": [{"role": "user", "content": user_text}],
    }
    if temperature is not None:
        body["temperature"] = temperature
    payload = json.dumps(body).encode("utf-8")

    last_err = None
    for attempt in range(1, max_retries + 1):
        req = urllib.request.Request(
            ANTHROPIC_API_URL, data=payload, method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Api-Key": api_key,
                "Anthropic-Version": ANTHROPIC_API_VERSION,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                raw_text = resp.read().decode("utf-8", errors="replace")
                http_status = resp.status
        except urllib.error.HTTPError as e:
            http_status = e.code
            raw_text = e.read().decode("utf-8", errors="replace")
            if http_status in _FATAL_HTTP or http_status == 404:
                raise ClaudeAPIError(
                    f"Claude API fatal error {http_status}: {raw_text[:500]}",
                    retryable=False, http_status=http_status,
                )
            last_err = ClaudeAPIError(
                f"Claude API HTTP {http_status}: {raw_text[:500]}",
                retryable=True, http_status=http_status,
            )
            if attempt < max_retries:
                _sleep_backoff(attempt, backoff_base_s, logger)
            continue
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = ClaudeAPIError(f"Claude API network error: {e}", retryable=True)
            if attempt < max_retries:
                _sleep_backoff(attempt, backoff_base_s, logger)
            continue

        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError as e:
            last_err = ClaudeAPIError(f"Claude API returned non-JSON response: {e}", retryable=True)
            if attempt < max_retries:
                _sleep_backoff(attempt, backoff_base_s, logger)
            continue

        if parsed.get("type") == "error":
            err_msg = (parsed.get("error") or {}).get("message", "unknown error")
            raise ClaudeAPIError(f"Claude API error: {err_msg}", retryable=False)

        stop_reason = parsed.get("stop_reason")
        if stop_reason == "refusal":
            det = parsed.get("stop_details") or {}
            cat = det.get("category")
            expl = det.get("explanation")
            extra = f" category={cat}" if cat else ""
            extra += f" explanation={expl!r}" if expl else ""
            raise ClaudeAPIError(
                f"Prompt refused by Claude (stop_reason=refusal){extra}", retryable=False,
            )

        blocks = parsed.get("content") or []
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")

        if not text:
            last_err = ClaudeAPIError(
                f"Claude API returned empty text, stop_reason={stop_reason}", retryable=True,
            )
            if attempt < max_retries:
                _sleep_backoff(attempt, backoff_base_s, logger)
            continue

        return {
            "text": text,
            "finish_reason": stop_reason,
            "block_reason": None,
            "raw": parsed,
            "http_status": http_status,
            "attempt_count": attempt,
            "response_truncated": stop_reason == "max_tokens",
        }

    raise last_err or ClaudeAPIError("Claude API call failed after retries", retryable=True)


def parse_fenced_blocks(text: str) -> list:
    """Extract every fenced code block's body from an LLM response, in
    order. Format-agnostic: doesn't care whether the body is well-formed
    text or a hex byte stream — that distinction is handled downstream,
    based on seed_kind. Pure function, no I/O — unit-tested directly against
    mocks. Zero blocks parsed is a valid, expected outcome (unterminated
    fence, no fencing at all, stray prose only) — the caller decides what to
    do about it, this function just reports what it found."""
    if not text:
        return []
    blocks = []
    for m in FENCE_RE.finditer(text):
        block = m.group(1).strip("\n")
        if block.strip():
            blocks.append(block)
    return blocks


_HEX_PREFIX_RE = re.compile(r"(?<![0-9a-fA-F])0[xX]")
_HEX_CLEAN_RE = re.compile(r"[^0-9a-fA-F]")


def hex_block_to_bytes(text: str):
    """Convert one fenced hex block into raw bytes for --seed-kind binary.

    Two cleanup passes, in order, because they're not the same operation:
      1. Strip '0x'/'0X' prefixes as literal two-character tokens — but only
         where they're not preceded by another hex digit, so a genuine
         prefix ("0x49") is removed while a coincidental "...a0x1f..." run
         isn't mangled by assuming a prefix that wasn't intended. Order
         matters: a naive single-pass "delete every character that isn't
         0-9a-f" removes only the 'x', leaving the leading '0' behind —
         '0x49' becomes '049', not '49', silently shifting every byte after
         it by one nibble.
      2. Strip everything else that isn't a hex digit (whitespace, commas,
         newlines) — safe now that step 1 has already handled the one case
         where a "non-hex" character was hiding a hex digit right next to it.

    What survives both passes and still isn't valid — an odd number of hex
    digits (each byte needs exactly two), or nothing at all — is a genuine
    failure, not something to paper over: there's no byte sequence to write
    to disk without it, unlike the structural/semantic validity checks this
    pipeline deliberately doesn't otherwise perform.

    Returns (True, bytes) on success or (False, error_message) on failure;
    never raises.
    """
    text = _HEX_PREFIX_RE.sub("", text)
    cleaned = _HEX_CLEAN_RE.sub("", text)
    if not cleaned:
        return False, "no hex digits found in block"
    if len(cleaned) % 2 != 0:
        return False, f"odd number of hex digits ({len(cleaned)}) after cleanup — likely truncated output"
    try:
        return True, bytes.fromhex(cleaned)
    except ValueError as e:
        return False, f"bytes.fromhex rejected cleaned hex: {e}"


def _dry_run_fixture_response(n_generate: int = 5, seed_kind: str = "text") -> dict:
    """Canned response used when --dry-run is set and no mock_llm_fn is
    supplied — exercises the real parse -> (hex-decode ->) write path
    without a real API call. For seed_kind="text", n_generate trivial
    well-formed fenced XML-shaped blocks; for "binary", n_generate copies of
    a small valid hex byte stream, so the dry run also exercises
    hex_block_to_bytes on real hex rather than only ever the empty-input
    code path."""
    if seed_kind == "binary":
        sample_hex = "49 49 2a 00 08 00 00 00"
        blocks = [f"```hex\n{sample_hex}\n```" for _ in range(n_generate)]
    else:
        blocks = [
            f"```xml\n<doc id=\"{i}\"><child>dry-run fixture</child></doc>\n```"
            for i in range(n_generate)
        ]
    return {
        "text": "\n\n".join(blocks),
        "finish_reason": "STOP",
        "block_reason": None,
        "raw": {"note": "dry-run fixture, no real API call made"},
        "http_status": None,
        "attempt_count": 0,
        "response_truncated": False,
    }


# --------------------------------------------------------------------------
# One full generate -> materialize -> mini-test -> inject cycle
# --------------------------------------------------------------------------

def _candidate_record(idx, raw_path, seed_path, run_dir, status, new_edges, total_edges, injected):
    return {
        "candidate_idx": idx,
        "raw_path": str(raw_path.relative_to(run_dir)),
        "seed_path": str(seed_path.relative_to(run_dir)) if seed_path else None,
        "status": status,
        "new_edges": new_edges,
        "total_edges": total_edges,
        "injected": injected,
    }


def run_cycle(cycle_id, campaign_root, instance, plateau_log, run_dir, api_key, model,
              fmt, n_seeds, n_generate, afl_showmap_bin, target, target_args,
              afl_addseeds_bin, main_queue_dir, plateau_row=None,
              seed_kind="text", seed_ext=None, format_hint="",
              temperature=None, max_output_tokens=4096, timeout_s=60, max_retries=3,
              asan_hint=False, afl_showmap_timeout_s=30, dry_run=False, mock_llm_fn=None,
              inject=True, baseline=None, provider="claude", logger=None):
    """
    Runs one full generate -> materialize -> mini-test -> inject cycle.
    Never raises — any failure is captured in the returned record's "errors"
    list so a single bad cycle can't kill the 24h run.

    fmt: human-readable target format name (e.g. "XML document", "TIFF
    image"), passed straight through to build_context.assemble_prompt.

    seed_kind: "text" writes each fenced block to a seed file as-is.
    "binary" treats each fenced block as a hex byte stream and converts it
    via hex_block_to_bytes before writing. Either way, the raw block text is
    always saved first (cand_XX.raw), before any decoding is attempted — a
    block that fails to decode is recorded as "HEX_DECODE_ERROR" and simply
    has no seed_path, since there's no byte sequence to evaluate or inject.

    seed_ext: candidate seed-file extension. Defaults to ".bin" for binary,
    ".seed" for text, if not given explicitly.

    Every candidate that DOES materialize into a seed file is injected via
    afl-addseeds in one batched call, regardless of what the mini
    afl-showmap check below says about it — see the module docstring for
    why. inject=False skips that call entirely (candidates are still
    generated, materialized, and evaluated) — useful for offline/bulk runs
    against a campaign you don't want to mutate.

    baseline: precomputed full-queue edge set to reuse instead of calling
    evaluate_seeds.build_baseline() fresh this cycle. Pass this when the
    queue is known static across many cycles in the same run (e.g. a bulk
    generation run against an idle campaign) to avoid paying the full-queue
    afl-showmap pass on every single cycle.
    """
    log = logger or logging.getLogger("agentafl_orchestrator")
    tag = _cycle_tag(cycle_id)
    t_start = time.time()
    errors = []
    seed_ext = seed_ext or (".bin" if seed_kind == "binary" else ".seed")

    record = {
        "record_type": "cycle", "cycle_id": cycle_id, "triggered_at": _now_iso(),
        "trigger_plateau_secs": plateau_row["plateau_secs"] if plateau_row else None,
        "seed_kind": seed_kind,
        "llm_model": model, "llm_finish_reason": None, "llm_attempt_count": None,
        "prompt_path": None, "response_path": None,
        "n_generate_requested": n_generate, "n_candidates_parsed": 0,
        "n_materialized": 0, "n_new_coverage": 0, "n_injected": 0,
        "response_truncated": False, "candidates": [],
        "afl_addseeds_invoked": False, "afl_addseeds_exit_code": None,
        "afl_addseeds_stderr_tail": "",
        "errors": errors, "cycle_duration_s": None,
    }

    def finish():
        record["cycle_duration_s"] = time.time() - t_start
        return record

    # 1. Assemble prompt from live campaign state (in-process, no subprocess).
    try:
        prompt = build_context.assemble_prompt(
            Path(campaign_root), instance, fmt, n_seeds, n_generate, plateau_log,
            asan_hint=asan_hint, seed_kind=seed_kind, format_hint=format_hint,
        )
    except Exception as e:
        errors.append(f"assemble_prompt failed: {e}")
        log.error(f"[{tag}] assemble_prompt failed: {e}")
        return finish()

    prompt_path = run_dir / "prompts" / f"{tag}.json"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(json.dumps({**prompt, "assembled_at": _now_iso()}, indent=2))
    record["prompt_path"] = str(prompt_path.relative_to(run_dir))

    # 2. Call the LLM (or a canned fixture under --dry-run).
    if dry_run:
        llm_result = (mock_llm_fn or (lambda n: _dry_run_fixture_response(n, seed_kind)))(n_generate)
        log.info(f"[{tag}] DRY RUN: skipped real API call")
    else:
        call_llm = call_gemini if provider == "gemini" else call_claude
        try:
            llm_result = call_llm(
                api_key, model, prompt["system"], prompt["user"],
                max_output_tokens=max_output_tokens, temperature=temperature,
                timeout_s=timeout_s, max_retries=max_retries, logger=log,
            )
        except GeminiAPIError as e:
            errors.append(f"LLM API call failed: {e}")
            log.error(f"[{tag}] LLM API call failed: {e}")
            return finish()

    response_path = run_dir / "responses" / f"{tag}.json"
    response_path.parent.mkdir(parents=True, exist_ok=True)
    response_path.write_text(json.dumps(llm_result, indent=2, default=str))
    record["response_path"] = str(response_path.relative_to(run_dir))
    record["llm_finish_reason"] = llm_result.get("finish_reason")
    record["llm_attempt_count"] = llm_result.get("attempt_count")
    record["response_truncated"] = bool(llm_result.get("response_truncated"))

    # 3. Parse fenced blocks and materialize each one into a seed file.
    # Every block is saved raw (cand_XX.raw) BEFORE any decoding is
    # attempted — nothing the LLM produced is ever lost, even a candidate
    # that later fails hex decoding.
    blocks = parse_fenced_blocks(llm_result.get("text", ""))
    record["n_candidates_parsed"] = len(blocks)
    if len(blocks) != n_generate:
        msg = f"Expected {n_generate} fenced blocks, parsed {len(blocks)}"
        errors.append(msg)
        log.warning(f"[{tag}] {msg}")
    if not blocks:
        errors.append("Zero fenced blocks parsed — see raw response for inspection.")
        log.warning(f"[{tag}] Zero fenced blocks parsed; see {response_path}")
        return finish()

    cand_dir = run_dir / "candidates" / tag
    cand_dir.mkdir(parents=True, exist_ok=True)
    materialized = []  # (idx, raw_path, seed_path)
    for idx, block in enumerate(blocks):
        raw_path = cand_dir / f"cand_{idx:02d}.raw"
        raw_path.write_text(block)

        seed_path = cand_dir / f"cand_{idx:02d}{seed_ext}"
        if seed_kind == "binary":
            ok, payload = hex_block_to_bytes(block)
            if not ok:
                record["candidates"].append(_candidate_record(
                    idx, raw_path, None, run_dir,
                    status="HEX_DECODE_ERROR", new_edges=0, total_edges=0, injected=False,
                ))
                msg = f"candidate {idx}: hex decode failed: {payload}"
                errors.append(msg)
                log.warning(f"[{tag}] {msg}")
                continue
            seed_path.write_bytes(payload)
        else:
            seed_path.write_text(block)
        materialized.append((idx, raw_path, seed_path))

    record["n_materialized"] = len(materialized)
    if not materialized:
        errors.append("No candidate produced a writable seed file this cycle "
                       "(all blocks failed hex decoding).")
        log.warning(f"[{tag}] No writable candidates this cycle.")
        return finish()

    # 4. Mini test: quick per-candidate afl-showmap check, informational
    # only (see module docstring) — never gates step 5. Skipped under
    # --dry-run, same as the real API call.
    if dry_run:
        for idx, raw_path, seed_path in materialized:
            record["candidates"].append(_candidate_record(
                idx, raw_path, seed_path, run_dir,
                status="DRY_RUN_SKIPPED", new_edges=0, total_edges=0, injected=False,
            ))
        log.info(f"[{tag}] DRY RUN: skipped afl-showmap/afl-addseeds")
        return finish()

    if baseline is None:
        try:
            baseline = evaluate_seeds.build_baseline(
                afl_showmap_bin, target, target_args, main_queue_dir, timeout=afl_showmap_timeout_s,
            )
        except Exception as e:
            errors.append(f"build_baseline failed: {e}")
            log.error(f"[{tag}] build_baseline failed: {e}")
            # Fall back to an empty baseline rather than aborting the cycle —
            # candidates still get evaluated (everything will look "new",
            # which is wrong) and, per this module's design, still get
            # injected regardless either way. The wrong new_edges numbers
            # this cycle are visible via the logged error above rather than
            # silently trusted.
            baseline = set()

    for idx, raw_path, seed_path in materialized:
        try:
            ev = evaluate_seeds.evaluate_candidate(
                afl_showmap_bin, target, target_args, seed_path, baseline, timeout=afl_showmap_timeout_s,
            )
        except Exception as e:
            errors.append(f"evaluate_candidate failed for {seed_path.name}: {e}")
            log.error(f"[{tag}] evaluate_candidate failed for {seed_path.name}: {e}")
            ev = {"status": "EVAL_ERROR", "new_edges": 0, "total_edges": 0}

        record["candidates"].append(_candidate_record(
            idx, raw_path, seed_path, run_dir,
            status=ev["status"], new_edges=ev["new_edges"], total_edges=ev["total_edges"],
            injected=False,  # set True below if/once actually injected
        ))
        if ev["status"] == "GOOD (novel coverage)":
            record["n_new_coverage"] += 1

    # 5. Inject every materialized candidate in one batched afl-addseeds
    # call, regardless of what step 4 found. AFL++'s own calibration/culling
    # is the real filter here, not this script.
    if inject:
        seed_paths = [seed_path for _, _, seed_path in materialized]
        cmd = [afl_addseeds_bin, "-o", str(campaign_root)] + [str(p) for p in seed_paths]
        ok, result = run_subprocess_safe(cmd, logger=log, timeout=120)
        record["afl_addseeds_invoked"] = True
        if ok:
            record["afl_addseeds_exit_code"] = result.returncode
            record["afl_addseeds_stderr_tail"] = result.stderr.decode(errors="replace")[-500:]
            if result.returncode == 0:
                for cand in record["candidates"]:
                    if cand["seed_path"] is not None:
                        cand["injected"] = True
                record["n_injected"] = len(materialized)
            else:
                errors.append(f"afl-addseeds exited {result.returncode}")
                log.error(f"[{tag}] afl-addseeds exited {result.returncode}: "
                          f"{record['afl_addseeds_stderr_tail']}")
        else:
            errors.append(f"afl-addseeds subprocess failed: {result}")

    return finish()


# --------------------------------------------------------------------------
# CLI / main loop
# --------------------------------------------------------------------------

def _setup_logging(run_dir: Path, log_level: str) -> logging.Logger:
    logger = logging.getLogger("agentafl_orchestrator")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    fh = logging.FileHandler(run_dir / "orchestrator.log")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--campaign-root", required=True, type=Path)
    ap.add_argument("--instance", default="main")
    ap.add_argument(
        "--format",
        required=True,
        dest="fmt",
        help='Human-readable target file format, e.g. "XML document" or "TIFF image". '
        "Passed straight through to build_context.assemble_prompt. The only other "
        "user input this pipeline branches on is --seed-kind — the format name itself "
        "is never used for special-casing beyond naming it in the prompt.",
    )
    ap.add_argument(
        "--seed-kind",
        choices=["text", "binary"],
        default="text",
        help='"text" (default): fenced blocks are seed content as-is. "binary": fenced '
        "blocks are hex byte streams, converted via a fixed hex-to-bytes step before "
        "being written — see the module docstring.",
    )
    ap.add_argument(
        "--seed-ext",
        default=None,
        help='Candidate seed file extension, e.g. ".tiff". Defaults to ".bin" for '
        '--seed-kind binary, ".seed" for text.',
    )
    ap.add_argument(
        "--format-hint",
        default="",
        help="Optional grammar/schema hint passed to build_context.py's system prompt "
        "(e.g. byte order for a binary format).",
    )
    ap.add_argument("--plateau-log", type=Path, default=None,
                     help="Defaults to <campaign-root's parent>/plateau_log.csv")
    ap.add_argument("--runs-root", type=Path, default=Path("/home/user/Documents/agentafl_runs"))
    ap.add_argument("--run-label", default="run", help='e.g. "treatment1"')
    ap.add_argument("--poll-interval-s", type=int, default=300)
    ap.add_argument("--plateau-threshold-s", type=int, default=900)
    ap.add_argument("--injection-cooldown-s", type=int, default=900)
    ap.add_argument("--max-llm-calls", type=int, default=500)
    ap.add_argument("--n-seeds", type=int, default=3)
    ap.add_argument("--n-generate", type=int, default=2)
    ap.add_argument("--max-queue-size", type=int, default=500)
    ap.add_argument("--llm-provider", choices=("claude", "gemini"), default="claude",
                     help="Which API to call. 'claude' (default) -> Anthropic /v1/messages, "
                     "key CLAUDE_API_KEY. 'gemini' -> Google generateContent, key "
                     "GEMINI_API_KEY. Both code paths are kept so you can switch back.")
    ap.add_argument("--llm-model", default=None,
                     help="Pin to a specific model id, not a -latest alias, so it can't "
                     "silently drift across a multi-day sequence of runs. Defaults per "
                     "provider: claude -> 'claude-opus-5', gemini -> 'gemini-3.5-flash-lite'. "
                     "gemini-2.5-flash-lite/-flash were the cheaper tier but returned "
                     "HTTP 404 'no longer available to new users' when sanity-checked "
                     "against this project's real API key (2026-08-06) — confirm current "
                     "availability/pricing again before a real run if this changes.")
    ap.add_argument("--llm-temperature", type=float, default=None,
                     help="Only sent to the API when set. Current Claude models reject "
                     "top-level sampling params (HTTP 400), so leave unset for claude; "
                     "the gemini path used 0.9 historically.")
    ap.add_argument("--llm-max-output-tokens", type=int, default=32000)
    ap.add_argument("--llm-timeout-s", type=int, default=300)
    ap.add_argument("--llm-max-retries", type=int, default=3)
    ap.add_argument("--asan-hint", action="store_true",
                     help="Include the ASan/memory-safety framing in prompts. OFF by "
                     "default: no ASan-instrumented build exists for this target, so "
                     "leaving this on would mislead the model.")
    ap.add_argument("--target", default=None,
                     help="Defaults to the binary in <instance>/cmdline.")
    ap.add_argument("--target-args", default=None,
                     help="Space-separated, @@ as input placeholder. Defaults to the args "
                     "in <instance>/cmdline — matches evaluate_seeds.py's own warning that "
                     "these MUST match the live campaign's invocation.")
    ap.add_argument("--afl-showmap-bin", default="afl-showmap")
    ap.add_argument("--afl-addseeds-bin", default="afl-addseeds")
    ap.add_argument("--afl-showmap-timeout-s", type=int, default=30)
    ap.add_argument("--env-file", type=Path, default=Path("/home/user/Documents/.env"))
    ap.add_argument("--run-duration-hours", type=float, default=24.0)
    ap.add_argument("--no-inject", action="store_true",
                     help="Generate, materialize, and evaluate candidates as usual, but "
                     "never call afl-addseeds. For offline/bulk runs against a campaign "
                     "you don't want to mutate.")
    ap.add_argument("--dry-run", action="store_true",
                     help="Skip real API/afl-showmap/afl-addseeds calls; use a canned "
                     "LLM fixture instead. For testing the loop end-to-end at zero cost.")
    ap.add_argument("--once", action="store_true",
                     help="Run a single poll iteration then exit, instead of looping for "
                     "--run-duration-hours. For testing.")
    ap.add_argument("--log-level", default="INFO")
    return ap


def main():
    args = build_arg_parser().parse_args()

    plateau_log = args.plateau_log or (args.campaign_root.parent / "plateau_log.csv")
    inst_dir = args.campaign_root / args.instance
    main_queue_dir = inst_dir / "queue"

    cmdline_info = build_context.parse_cmdline(inst_dir / "cmdline")
    target = args.target or cmdline_info["binary"]
    target_args = args.target_args.split() if args.target_args else cmdline_info["args"].split()

    seed_ext = args.seed_ext or (".bin" if args.seed_kind == "binary" else ".seed")

    run_id = make_run_id(args.run_label)
    run_dir = args.runs_root / run_id
    suffix = 2
    while True:
        try:
            run_dir.mkdir(parents=True, exist_ok=False)
            break
        except FileExistsError:
            run_dir = args.runs_root / f"{run_id}-{suffix}"
            suffix += 1

    logger = _setup_logging(run_dir, args.log_level)
    jsonl_path = run_dir / "cycles.jsonl"

    llm_model = args.llm_model or (
        "claude-opus-5" if args.llm_provider == "claude" else "gemini-3.5-flash-lite"
    )
    key_var = "GEMINI_API_KEY" if args.llm_provider == "gemini" else "CLAUDE_API_KEY"
    api_key = None if args.dry_run else load_api_key(args.env_file, key_var)
    if args.llm_provider == "claude" and args.llm_temperature is not None:
        logger.warning("--llm-temperature is set but current Claude models reject it "
                       "(HTTP 400); it will NOT be sent. Drop the flag to silence this.")
        args.llm_temperature = None

    run_config = {k: (str(v) if isinstance(v, Path) else v) for k, v in vars(args).items()}
    run_config.update({
        "run_id": run_id, "plateau_log": str(plateau_log),
        "target": target, "target_args": target_args, "seed_ext": seed_ext,
        "llm_model_resolved": llm_model,
        "started_at": _now_iso(),
    })
    (run_dir / "run_config.json").write_text(json.dumps(run_config, indent=2))

    logger.info(f"Starting run {run_id} against {args.campaign_root} (instance={args.instance})")
    logger.info(f"format={args.fmt!r} seed_kind={args.seed_kind} seed_ext={seed_ext}")
    logger.info(f"target={target} target_args={target_args}")
    logger.info(f"plateau_log={plateau_log}")

    lock_path = acquire_run_lock(args.runs_root, args.campaign_root)

    append_jsonl_record(jsonl_path, {
        "record_type": "run_start", "run_id": run_id, "started_at": _now_iso(),
        "config_path": "run_config.json",
    })

    cycle_id = 0
    llm_calls_this_run = 0
    run_start_time = time.time()
    deadline = run_start_time + args.run_duration_hours * 3600
    stop_reason = "duration_elapsed"

    # Run-wide totals — the "log total number of seeds, how many successful"
    # summary, updated every cycle and written into the run_end record.
    totals = {
        "total_generated": 0, "total_materialized": 0,
        "total_new_coverage": 0, "total_injected": 0,
    }

    try:
        while True:
            if not args.once:
                time.sleep(args.poll_interval_s)

            row = build_context.parse_plateau_row(plateau_log)
            plateau_secs = row["plateau_secs"] if row else 0
            queue_size = _count_queue(main_queue_dir)
            now = time.time()

            gates_ok = (
                plateau_secs >= args.plateau_threshold_s
                and llm_calls_this_run < args.max_llm_calls
                and queue_size < args.max_queue_size
            )

            if gates_ok:
                cycle_id += 1
                logger.info(f"Triggering cycle {cycle_id} (plateau_secs={plateau_secs}, "
                            f"queue_size={queue_size})")
                record = run_cycle(
                    cycle_id=cycle_id, campaign_root=args.campaign_root, instance=args.instance,
                    plateau_log=plateau_log, run_dir=run_dir, api_key=api_key, model=llm_model,
                    fmt=args.fmt, n_seeds=args.n_seeds, n_generate=args.n_generate,
                    afl_showmap_bin=args.afl_showmap_bin, target=target, target_args=target_args,
                    afl_addseeds_bin=args.afl_addseeds_bin, main_queue_dir=main_queue_dir,
                    plateau_row=row, seed_kind=args.seed_kind, seed_ext=seed_ext,
                    format_hint=args.format_hint,
                    temperature=args.llm_temperature, max_output_tokens=args.llm_max_output_tokens,
                    timeout_s=args.llm_timeout_s, max_retries=args.llm_max_retries,
                    asan_hint=args.asan_hint, afl_showmap_timeout_s=args.afl_showmap_timeout_s,
                    dry_run=args.dry_run, inject=not args.no_inject, provider=args.llm_provider,
                    logger=logger,
                )
                append_jsonl_record(jsonl_path, record)

                totals["total_generated"] += record["n_candidates_parsed"]
                totals["total_materialized"] += record["n_materialized"]
                totals["total_new_coverage"] += record["n_new_coverage"]
                totals["total_injected"] += record["n_injected"]
                llm_calls_this_run += 1

                logger.info(
                    f"Cycle {cycle_id} done: {record['n_materialized']}/{record['n_candidates_parsed']} "
                    f"materialized, {record['n_new_coverage']} showed new coverage, "
                    f"{record['n_injected']} injected, {len(record['errors'])} error(s)"
                )

                if llm_calls_this_run >= args.max_llm_calls:
                    append_jsonl_record(jsonl_path, {
                        "record_type": "budget_exhausted", "at": _now_iso(),
                        "calls_made": llm_calls_this_run, "max_llm_calls": args.max_llm_calls,
                    })
                    stop_reason = "budget_exhausted"
                    if not args.once:
                        logger.info("LLM call budget exhausted, stopping.")
                        break
            else:
                logger.debug(f"No cycle: plateau_secs={plateau_secs} queue_size={queue_size} "
                             f"llm_calls={llm_calls_this_run}")

            if args.once:
                stop_reason = "once"
                break
            if time.time() >= deadline:
                logger.info("Run duration elapsed, stopping.")
                break
    finally:
        append_jsonl_record(jsonl_path, {
            "record_type": "run_end", "ended_at": _now_iso(), "reason": stop_reason,
            "total_cycles": cycle_id, **totals,
        })
        logger.info(
            f"Run {run_id} ended: {cycle_id} cycles | generated {totals['total_generated']} | "
            f"materialized {totals['total_materialized']} | "
            f"new-coverage {totals['total_new_coverage']} | injected {totals['total_injected']}"
        )
        release_run_lock(lock_path)


if __name__ == "__main__":
    main()