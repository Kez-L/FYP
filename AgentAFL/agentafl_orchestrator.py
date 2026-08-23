#!/usr/bin/env python3
"""
agentafl_orchestrator.py — Unattended plateau-triggered LLM seed injection for a
live AFL++ campaign. Polls plateau_log.csv (written by plateau_watch.py); when the
campaign has genuinely plateaued (with a cooldown and a hard call-budget cap so a
24h stall can't quietly run away with API spend), it assembles a prompt from live
campaign state (build_context.py), calls the Gemini API, parses fenced ```xml
blocks out of the response, classifies each candidate via afl-showmap
(evaluate_seeds.py), and injects only the ones that hit novel coverage via
afl-addseeds. Every cycle — success or failure — is logged to cycles.jsonl.

Usage:
  python3 agentafl_orchestrator.py \
      --campaign-root /home/user/Documents/afl-output-libxml2-treatment1 \
      --run-label treatment1

Crash/timeout candidates are classified and logged only in this version — no
afl-tmin, no ASan report (no ASan-instrumented build exists for this target).

Requires GEMINI_API_KEY in the environment or in --env-file (default
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
import xml_utils

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# Fenced code block, ```xml or bare ``` (models sometimes drop the language
# tag). Non-greedy so a truncated/unterminated trailing block (e.g. cut off by
# MAX_TOKENS) simply doesn't match rather than swallowing everything after it —
# earlier, properly-closed blocks in the same response still parse correctly.
# Known limitation: an XML document containing a literal ``` sequence (e.g.
# inside a comment) would terminate its own block early — extremely unlikely
# in practice since backticks have no special meaning in XML, not defended
# against in V1.
FENCE_RE = re.compile(r"```(?:xml)?[ \t]*\n(.*?)```", re.IGNORECASE | re.DOTALL)


class GeminiAPIError(Exception):
    def __init__(self, message, retryable=False, http_status=None):
        super().__init__(message)
        self.retryable = retryable
        self.http_status = http_status


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cycle_tag(cycle_id: int) -> str:
    return f"cycle_{cycle_id:04d}"


def _list_names(dir_path: Path) -> set:
    try:
        return {f.name for f in dir_path.iterdir() if f.is_file()}
    except OSError:
        return set()


def _count_queue(queue_dir: Path) -> int:
    try:
        return sum(1 for f in queue_dir.iterdir() if f.is_file())
    except OSError:
        return 0


def _targeted_constructs(content: bytes) -> list:
    """Which of build_context.py's tracked XML constructs (DOCTYPE, ENTITY,
    CDATA, xmlns, PI, comment) this candidate's content hits — reuses
    build_context.CONSTRUCTS directly rather than re-deriving the list."""
    return sorted(name for name, pat in build_context.CONSTRUCTS.items() if pat.search(content))


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

def load_api_key(env_file: Path) -> str:
    """os.environ['GEMINI_API_KEY'] wins if set, else parse env_file: split
    each non-blank/non-'#' line on the first '=', strip optional surrounding
    quotes. Raises SystemExit with a value-free message if missing. Never
    logs/prints the key; callers must not interpolate it into any log or
    exception message."""
    key = os.environ.get("GEMINI_API_KEY")
    if key:
        return key.strip()
    if env_file.exists():
        for line in env_file.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() != "GEMINI_API_KEY":
                continue
            v = v.strip().strip('"').strip("'")
            if v:
                return v
    raise SystemExit(
        f"GEMINI_API_KEY not found in the environment or in {env_file} — "
        "set it before running the orchestrator."
    )


def _sleep_backoff(attempt: int, base_s: float, logger=None):
    delay = min(30.0, base_s * (2 ** (attempt - 1))) + random.uniform(0, 1)
    if logger:
        logger.debug(f"Retrying Gemini call in {delay:.1f}s (attempt {attempt})")
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


def parse_fenced_xml_blocks(text: str) -> list:
    """Extract every fenced code block's body from an LLM response, in order.
    Pure function, no I/O — unit-tested directly against mocks. Zero blocks
    parsed is a valid, expected outcome (unterminated fence, no fencing at
    all, stray prose only) — the caller decides what to do about it, this
    function just reports what it found."""
    if not text:
        return []
    blocks = []
    for m in FENCE_RE.finditer(text):
        block = m.group(1).strip("\n")
        if block.strip():
            blocks.append(block)
    return blocks


def _dry_run_fixture_response(n_generate: int = 5) -> dict:
    """Canned response used when --dry-run is set and no mock_llm_fn is
    supplied — exactly n_generate trivial well-formed fenced XML blocks, so
    the dry run exercises the real parse -> write -> (skipped-eval) path."""
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
# Two-stage queue-id resolution
# --------------------------------------------------------------------------

def resolve_pending_injections(pending: list, main_queue_dir: Path, jsonl_path: Path,
                                logger=None) -> list:
    """
    pending: list of {"cycle_id", "candidate_idx", "addseeds_local_id"} for
    candidates already handed to afl-addseeds but not yet observed synced
    into main's queue (afl-addseeds only writes into addseeds/queue/; main's
    own -M sync mechanism picks it up asynchronously — empirically ~7.5 min
    in this campaign, but not guaranteed within any fixed window).

    Called every poll (not just on cycles that fire) so late syncs get picked
    up promptly. Appends an "injection_resolved" event to cycles.jsonl for
    each newly-resolved id and returns the still-unresolved remainder.
    """
    if not pending:
        return pending
    local_ids = {p["addseeds_local_id"] for p in pending}
    found = xml_utils.find_synced_entries_bulk(main_queue_dir, "addseeds", local_ids)
    if not found:
        return pending

    still_pending = []
    for p in pending:
        fields = found.get(p["addseeds_local_id"])
        if fields is None:
            still_pending.append(p)
            continue
        event = {
            "record_type": "injection_resolved",
            "cycle_id": p["cycle_id"],
            "candidate_idx": p["candidate_idx"],
            "addseeds_local_id": p["addseeds_local_id"],
            "main_queue_id": fields["id"],
            "main_queue_filename": fields["raw"],
            "resolved_at": _now_iso(),
        }
        append_jsonl_record(jsonl_path, event)
        if logger:
            logger.info(
                f"Resolved injection: cycle {p['cycle_id']} candidate {p['candidate_idx']} "
                f"-> main queue id {fields['id']}"
            )
    return still_pending


# --------------------------------------------------------------------------
# One full generate -> parse -> evaluate -> inject cycle
# --------------------------------------------------------------------------

def _base_candidate_record(idx, fname, fpath, run_dir, well_formed, status,
                            new_edges, total_edges, targeted_constructs):
    return {
        "candidate_idx": idx, "candidate_filename": fname,
        "candidate_path": str(fpath.relative_to(run_dir)),
        "well_formed": well_formed, "status": status,
        "new_edges": new_edges, "total_edges": total_edges,
        "targeted_constructs": targeted_constructs,
        "injected": False, "addseeds_local_id": None,
        "addseeds_local_filename": None, "addseeds_injected_at": None,
        "main_queue_id": None, "main_queue_filename": None,
        "main_queue_resolved": False, "main_queue_resolved_at": None,
    }


def run_cycle(cycle_id, campaign_root, instance, plateau_log, run_dir, api_key, model,
              n_seeds, n_generate, afl_showmap_bin, target, target_args,
              afl_addseeds_bin, addseeds_queue_dir, main_queue_dir, plateau_row,
              temperature=0.9, max_output_tokens=4096, timeout_s=60, max_retries=3,
              asan_hint=False, afl_showmap_timeout_s=30, dry_run=False, mock_llm_fn=None,
              inject=True, baseline=None, logger=None):
    """
    Runs one full cycle. Never raises — any failure is captured in the
    returned record's "errors" list so a single bad cycle can't kill the 24h
    run. Returns (cycle_record: dict, new_pending: list[dict]).

    inject: when False, skip the afl-addseeds step entirely (candidates can
    still be classified "GOOD (novel coverage)", they just never get written
    into addseeds_queue_dir and stay injected=False). For offline/bulk
    evaluation runs against a campaign you don't want to mutate.

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
    new_pending = []

    inst_dir = Path(campaign_root) / instance
    stats = build_context.parse_fuzzer_stats(inst_dir / "fuzzer_stats")
    campaign_state_before = {
        "edges_found": stats.get("edges_found", "?"),
        "bitmap_cvg": stats.get("bitmap_cvg", "?"),
        "corpus_count": stats.get("corpus_count", "?"),
        "saved_crashes": stats.get("saved_crashes", "?"),
        "saved_hangs": stats.get("saved_hangs", "?"),
    }

    record = {
        "record_type": "cycle", "cycle_id": cycle_id, "triggered_at": _now_iso(),
        "trigger_plateau_secs": plateau_row["plateau_secs"] if plateau_row else None,
        "campaign_state_before": campaign_state_before,
        "llm_model": model, "llm_temperature": temperature,
        "llm_max_output_tokens": max_output_tokens,
        "llm_finish_reason": None, "llm_attempt_count": None,
        "prompt_path": None, "response_path": None,
        "n_generate_requested": n_generate, "n_candidates_parsed": 0,
        "response_truncated": False, "candidates": [],
        "injected_count": 0, "afl_addseeds_invoked": False,
        "afl_addseeds_exit_code": None, "afl_addseeds_stderr_tail": "",
        "errors": errors, "cycle_duration_s": None,
    }

    def finish():
        record["cycle_duration_s"] = time.time() - t_start
        return record, new_pending

    # 1. Assemble prompt from live campaign state (in-process, no subprocess).
    try:
        prompt = build_context.assemble_prompt(
            Path(campaign_root), instance, n_seeds, n_generate, plateau_log,
            asan_hint=asan_hint,
        )
    except Exception as e:
        errors.append(f"assemble_prompt failed: {e}")
        log.error(f"[{tag}] assemble_prompt failed: {e}")
        return finish()

    prompt_path = run_dir / "prompts" / f"{tag}.json"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(json.dumps(
        {**prompt, "assembled_at": _now_iso(), "n_seeds": n_seeds, "n_generate": n_generate},
        indent=2,
    ))
    record["prompt_path"] = str(prompt_path.relative_to(run_dir))

    # 2. Call Gemini (or a canned fixture under --dry-run).
    if dry_run:
        llm_result = (mock_llm_fn or _dry_run_fixture_response)(n_generate)
        log.info(f"[{tag}] DRY RUN: skipped real Gemini call")
    else:
        try:
            llm_result = call_gemini(
                api_key, model, prompt["system"], prompt["user"],
                max_output_tokens=max_output_tokens, temperature=temperature,
                timeout_s=timeout_s, max_retries=max_retries, logger=log,
            )
        except GeminiAPIError as e:
            errors.append(f"Gemini API call failed: {e}")
            log.error(f"[{tag}] Gemini API call failed: {e}")
            return finish()

    response_path = run_dir / "responses" / f"{tag}.json"
    response_path.parent.mkdir(parents=True, exist_ok=True)
    response_path.write_text(json.dumps(llm_result, indent=2, default=str))
    record["response_path"] = str(response_path.relative_to(run_dir))
    record["llm_finish_reason"] = llm_result.get("finish_reason")
    record["llm_attempt_count"] = llm_result.get("attempt_count")
    record["response_truncated"] = bool(llm_result.get("response_truncated"))

    # 3. Parse fenced ```xml blocks.
    blocks = parse_fenced_xml_blocks(llm_result.get("text", ""))
    record["n_candidates_parsed"] = len(blocks)
    if len(blocks) != n_generate:
        msg = f"Expected {n_generate} fenced blocks, parsed {len(blocks)}"
        errors.append(msg)
        log.warning(f"[{tag}] {msg}")
    if not blocks:
        errors.append("Zero fenced XML blocks parsed — raw response saved for inspection, "
                       "skipping injection this cycle.")
        log.warning(f"[{tag}] Zero fenced blocks parsed; see {response_path}")
        return finish()

    cand_dir = run_dir / "candidates" / tag
    cand_dir.mkdir(parents=True, exist_ok=True)
    candidate_files = []
    for idx, block in enumerate(blocks):
        fname = f"cand_{idx:02d}.xml"
        fpath = cand_dir / fname
        fpath.write_text(block)
        candidate_files.append((idx, fname, fpath))

    # 4. Evaluate (or skip under --dry-run — no real afl-showmap/afl-addseeds calls).
    if dry_run:
        for idx, fname, fpath in candidate_files:
            content = fpath.read_bytes()
            record["candidates"].append(_base_candidate_record(
                idx, fname, fpath, run_dir,
                well_formed=xml_utils.is_well_formed_xml(fpath),
                status="DRY_RUN_SKIPPED", new_edges=0, total_edges=0,
                targeted_constructs=_targeted_constructs(content),
            ))
        log.info(f"[{tag}] DRY RUN: skipped real afl-showmap/afl-addseeds calls")
        return finish()

    if baseline is None:
        try:
            baseline = evaluate_seeds.build_baseline(
                afl_showmap_bin, target, target_args, main_queue_dir, timeout=afl_showmap_timeout_s,
            )
        except Exception as e:
            errors.append(f"build_baseline failed: {e}")
            log.error(f"[{tag}] build_baseline failed: {e}")
            for idx, fname, fpath in candidate_files:
                record["candidates"].append(_base_candidate_record(
                    idx, fname, fpath, run_dir,
                    well_formed=xml_utils.is_well_formed_xml(fpath),
                    status="EVAL_ERROR", new_edges=0, total_edges=0,
                    targeted_constructs=_targeted_constructs(fpath.read_bytes()),
                ))
            return finish()

    good = []  # (fname, fpath, cand_record)
    for idx, fname, fpath in candidate_files:
        content = fpath.read_bytes()
        try:
            ev = evaluate_seeds.evaluate_candidate(
                afl_showmap_bin, target, target_args, fpath, baseline, timeout=afl_showmap_timeout_s,
            )
        except Exception as e:
            errors.append(f"evaluate_candidate failed for {fname}: {e}")
            log.error(f"[{tag}] evaluate_candidate failed for {fname}: {e}")
            ev = {"well_formed": xml_utils.is_well_formed_xml(fpath),
                  "status": "EVAL_ERROR", "new_edges": 0, "total_edges": 0}

        cand_record = _base_candidate_record(
            idx, fname, fpath, run_dir,
            well_formed=ev["well_formed"], status=ev["status"],
            new_edges=ev["new_edges"], total_edges=ev["total_edges"],
            targeted_constructs=_targeted_constructs(content),
        )
        record["candidates"].append(cand_record)
        if ev["status"] == "GOOD (novel coverage)":
            good.append((fname, fpath, cand_record))

    # 5. Inject GOOD candidates via one batched afl-addseeds call.
    if good and inject:
        before = _list_names(addseeds_queue_dir)
        cmd = [afl_addseeds_bin, "-o", str(campaign_root)] + [str(fpath) for _, fpath, _ in good]
        ok, result = run_subprocess_safe(cmd, logger=log, timeout=120)
        record["afl_addseeds_invoked"] = True
        if ok:
            record["afl_addseeds_exit_code"] = result.returncode
            record["afl_addseeds_stderr_tail"] = result.stderr.decode(errors="replace")[-500:]
            if result.returncode != 0:
                errors.append(f"afl-addseeds exited {result.returncode}")
                log.error(f"[{tag}] afl-addseeds exited {result.returncode}: "
                          f"{record['afl_addseeds_stderr_tail']}")
        else:
            errors.append(f"afl-addseeds subprocess failed: {result}")

        after = _list_names(addseeds_queue_dir)
        by_orig = {}
        for name in after - before:
            fields = xml_utils.parse_queue_filename(name)
            if fields["orig"]:
                by_orig[fields["orig"]] = (name, fields)

        now_iso = _now_iso()
        for fname, fpath, cand_record in good:
            match = by_orig.get(fname)
            if match:
                name, fields = match
                cand_record["injected"] = True
                cand_record["addseeds_local_id"] = fields["id"]
                cand_record["addseeds_local_filename"] = name
                cand_record["addseeds_injected_at"] = now_iso
                record["injected_count"] += 1
                new_pending.append({
                    "cycle_id": cycle_id, "candidate_idx": cand_record["candidate_idx"],
                    "addseeds_local_id": fields["id"],
                })
            else:
                msg = (f"Could not find addseeds/queue entry for injected candidate {fname} "
                       "after afl-addseeds call — injection status unknown.")
                errors.append(msg)
                log.warning(f"[{tag}] {msg}")

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
    ap.add_argument("--plateau-log", type=Path, default=None,
                     help="Defaults to <campaign-root's parent>/plateau_log.csv")
    ap.add_argument("--runs-root", type=Path, default=Path("/home/user/Documents/agentafl_runs"))
    ap.add_argument("--run-label", required=True, help='e.g. "treatment1"')
    ap.add_argument("--poll-interval-s", type=int, default=300)
    ap.add_argument("--plateau-threshold-s", type=int, default=900)
    ap.add_argument("--injection-cooldown-s", type=int, default=900)
    ap.add_argument("--max-llm-calls", type=int, default=500)
    ap.add_argument("--n-seeds", type=int, default=3)
    ap.add_argument("--n-generate", type=int, default=5)
    ap.add_argument("--max-queue-size", type=int, default=500)
    ap.add_argument("--llm-model", default="gemini-3.5-flash-lite",
                     help="Pin to a specific model id, not a -latest alias, so it can't "
                     "silently drift across a multi-day sequence of runs. "
                     "gemini-2.5-flash-lite/-flash were the cheaper tier but returned "
                     "HTTP 404 'no longer available to new users' when sanity-checked "
                     "against this project's real API key (2026-08-06) — confirm current "
                     "availability/pricing again before a real run if this changes.")
    ap.add_argument("--llm-temperature", type=float, default=0.9)
    ap.add_argument("--llm-max-output-tokens", type=int, default=4096)
    ap.add_argument("--llm-timeout-s", type=int, default=60)
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
    ap.add_argument("--dry-run", action="store_true",
                     help="Skip real Gemini/afl-showmap/afl-addseeds calls; use a canned "
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
    addseeds_queue_dir = args.campaign_root / "addseeds" / "queue"

    cmdline_info = build_context.parse_cmdline(inst_dir / "cmdline")
    target = args.target or cmdline_info["binary"]
    target_args = args.target_args.split() if args.target_args else cmdline_info["args"].split()

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

    api_key = None if args.dry_run else load_api_key(args.env_file)

    run_config = {k: (str(v) if isinstance(v, Path) else v) for k, v in vars(args).items()}
    run_config.update({
        "run_id": run_id, "plateau_log": str(plateau_log),
        "target": target, "target_args": target_args,
        "started_at": _now_iso(),
    })
    (run_dir / "run_config.json").write_text(json.dumps(run_config, indent=2))

    logger.info(f"Starting run {run_id} against {args.campaign_root} (instance={args.instance})")
    logger.info(f"target={target} target_args={target_args}")
    logger.info(f"plateau_log={plateau_log}")

    lock_path = acquire_run_lock(args.runs_root, args.campaign_root)

    append_jsonl_record(jsonl_path, {
        "record_type": "run_start", "run_id": run_id, "started_at": _now_iso(),
        "config_path": "run_config.json",
    })

    cycle_id = 0
    llm_calls_this_run = 0
    total_injected = 0
    last_injection_time = 0.0
    pending_resolutions = []
    run_start_time = time.time()
    deadline = run_start_time + args.run_duration_hours * 3600
    stop_reason = "duration_elapsed"

    try:
        while True:
            if not args.once:
                time.sleep(args.poll_interval_s)

            pending_resolutions = resolve_pending_injections(
                pending_resolutions, main_queue_dir, jsonl_path, logger,
            )

            row = build_context.parse_plateau_row(plateau_log)
            plateau_secs = row["plateau_secs"] if row else 0
            queue_size = _count_queue(main_queue_dir)
            now = time.time()

            gates_ok = (
                plateau_secs >= args.plateau_threshold_s
                and (now - last_injection_time) >= args.injection_cooldown_s
                and llm_calls_this_run < args.max_llm_calls
                and queue_size < args.max_queue_size
            )

            if gates_ok:
                cycle_id += 1
                logger.info(f"Triggering cycle {cycle_id} (plateau_secs={plateau_secs}, "
                            f"queue_size={queue_size})")
                record, new_pending = run_cycle(
                    cycle_id=cycle_id, campaign_root=args.campaign_root, instance=args.instance,
                    plateau_log=plateau_log, run_dir=run_dir, api_key=api_key, model=args.llm_model,
                    n_seeds=args.n_seeds, n_generate=args.n_generate,
                    afl_showmap_bin=args.afl_showmap_bin, target=target, target_args=target_args,
                    afl_addseeds_bin=args.afl_addseeds_bin, addseeds_queue_dir=addseeds_queue_dir,
                    main_queue_dir=main_queue_dir, plateau_row=row,
                    temperature=args.llm_temperature, max_output_tokens=args.llm_max_output_tokens,
                    timeout_s=args.llm_timeout_s, max_retries=args.llm_max_retries,
                    asan_hint=args.asan_hint, afl_showmap_timeout_s=args.afl_showmap_timeout_s,
                    dry_run=args.dry_run, logger=logger,
                )
                append_jsonl_record(jsonl_path, record)
                pending_resolutions.extend(new_pending)
                total_injected += record["injected_count"]
                llm_calls_this_run += 1
                last_injection_time = now
                logger.info(f"Cycle {cycle_id} done: {record['injected_count']} injected, "
                            f"{len(record['errors'])} error(s)")

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
        pending_resolutions = resolve_pending_injections(
            pending_resolutions, main_queue_dir, jsonl_path, logger,
        )
        append_jsonl_record(jsonl_path, {
            "record_type": "run_end", "ended_at": _now_iso(), "reason": stop_reason,
            "total_cycles": cycle_id, "total_injected": total_injected,
            "unresolved_injections": len(pending_resolutions),
        })
        logger.info(f"Run {run_id} ended: {cycle_id} cycles, {total_injected} injected, "
                    f"{len(pending_resolutions)} unresolved.")
        release_run_lock(lock_path)


if __name__ == "__main__":
    main()
