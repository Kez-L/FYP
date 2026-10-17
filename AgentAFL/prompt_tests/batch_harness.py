#!/usr/bin/env python3
"""
batch_harness.py — shared engine for the prompt-design ablation batch tests.

Every test in this study is the same loop: build a prompt, call an LLM, parse
the fenced blocks into seed files, check each seed for new edges against a
frozen corpus baseline, and report which seeds bought real coverage. The only
thing that changes between tests is HOW the prompt is built — so this module
owns everything else, and each test file (run_baseline.py, run_build_context.py,
...) is a thin wrapper that supplies a `prompt_fn`.

Output layout (identical across tests) under <out_dir>/ :
    prompts/call_NN.json     the exact {system,user} sent for call NN, + params
    responses/call_NN.txt    the verbatim model response
    seeds/seed_NNN.<ext>     one file per parsed seed, numbered across the run
    seeds.jsonl              one row per seed (tokens are per-call; coverage
                             fields added by evaluation)
    calls.jsonl              one row per call (the group of seeds)
    baseline_edges.json      frozen corpus edge set — reuse across tests with
                             reuse_baseline= so the per-queue scan runs once
    contributors_<stamp>.md  batch contribution analysis (PLAN.md Sec 3)
    summary_<stamp>.json     run-level totals

History feedback (for tests whose prompt feeds prior seeds back in, e.g.
build_context's "tried before, avoid" block): the harness maintains
<out_dir>/_history/ as a mini orchestrator run_dir — cycles.jsonl +
candidates/cycle_XXXX/ — populated from THIS batch's own evaluated seeds. The
prompt_fn is handed that dir's path for calls after the first (the first call
has no history yet), or None when history feedback is off.

Providers: `claude` (agentafl_orchestrator.call_claude, stdlib urllib, key
CLAUDE_API_KEY) or `openai` (openai SDK chat.completions, key OPENAI_API_KEY).
Both wrap into LLMResult and share the same drop-temperature-on-400 fallback.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
# Study-local copies (build_context, parse_seeds, score_batch, ...) must win over
# the repo-root originals of the same name; REPO_ROOT stays on the path after
# HERE for modules that only live there (evaluate_seeds). Remove-then-reinsert
# because the interpreter may already have HERE on sys.path in the wrong spot.
for _p in (str(HERE), str(REPO_ROOT)):
    while _p in sys.path:
        sys.path.remove(_p)
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(HERE))

from parse_seeds import parse_seeds                                   # noqa: E402
from evaluate_seeds import build_baseline, evaluate_candidate         # noqa: E402

DEFAULT_ENV_FILES = [REPO_ROOT / ".env", Path("/home/user/Documents/.env")]
EXT = {"text": "xml", "binary": "bin"}
DEFAULT_MODEL = {"claude": "claude-haiku-4-5-20251001", "openai": "gpt-5.4-mini"}


def _env_candidates(explicit):
    return [explicit] if explicit else DEFAULT_ENV_FILES


# Exit code used when a call fails because the account is out of credits /
# quota — run_all_stages.sh stops the whole sweep on it. Retrying or moving on
# to the next call is pointless: every later call fails the same way, and the
# run would otherwise finish "successfully" with a 0-seed summary.
OUT_OF_CREDITS_EXIT = 3
_OUT_OF_CREDITS_CODES = ("insufficient_quota", "credit_balance_exhausted", "billing_hard_limit_reached")
_OUT_OF_CREDITS_MARKERS = _OUT_OF_CREDITS_CODES + (
    "no credits remaining",               # OpenAI 429: "You have no credits remaining. ..."
    "exceeded your current quota",        # older OpenAI message
    "credit balance is too low",          # Anthropic message
)


class OutOfCreditsError(RuntimeError):
    """Every available API key is out of credits.

    Distinct from the provider's own 429: that one means "this key is done", and
    ProviderCaller reacts by moving to the next key. This one means there is no
    next key, so the caller above has to decide what to do with the campaign.
    `is_out_of_credits()` recognises it, so run_batch still exits
    OUT_OF_CREDITS_EXIT and run_all_stages.sh still aborts the sweep.
    """


def is_out_of_credits(err: BaseException) -> bool:
    # Raised by ProviderCaller once every key has been retired.
    if isinstance(err, OutOfCreditsError):
        return True
    # OpenAI 429 shape: type='insufficient_quota', code='credit_balance_exhausted'
    for attr in ("code", "type"):
        if getattr(err, attr, None) in _OUT_OF_CREDITS_CODES:
            return True
    msg = str(err).lower()
    return any(m in msg for m in _OUT_OF_CREDITS_MARKERS)


# Key variables tried in order, highest priority first. A second key lets a 12h
# campaign survive the first one running out of credits mid-run; without it, an
# exhausted key silently turns the `combined` arm into `minyee` for the rest of
# the run. Extend by adding OPENAI_API_KEY_3 etc. to the environment or .env -
# the scan stops at the first gap.
OPENAI_KEY_VARS = ("OPENAI_API_KEY", "OPENAI_API_KEY_2", "OPENAI_API_KEY_3")


def openai_keys(env: dict | None = None) -> list[tuple[str, str]]:
    """Return [(var_name, key), ...] in priority order, de-duplicated.

    Reads os.environ, which is the single source of truth once
    ProviderCaller has run load_dotenv(.env, override=False) - so an exported
    variable beats the .env file, matching the rest of this pipeline.

    De-duplication matters: the same key pasted into both variables would make
    failover a no-op that still *looks* like it worked, which is worse than
    having one key, because the preflight would report failover as available.
    """
    src = os.environ if env is None else env
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for var in OPENAI_KEY_VARS:
        val = (src.get(var) or "").strip().strip('"').strip("'")
        if not val or val in seen:
            continue
        seen.add(val)
        out.append((var, val))
    return out


def model_tag(provider: str, model: str) -> str:
    """Short slug for output-folder names: 'haiku', 'opus', 'gpt-5.4-mini', ..."""
    m = model.lower()
    if provider == "claude":
        for k in ("haiku", "opus", "sonnet", "fable"):
            if k in m:
                return k
    slug = re.sub(r"[^a-z0-9]+", "-", m).strip("-")
    return slug or provider


# ---------------------------------------------------------------------------
# LLM providers
# ---------------------------------------------------------------------------

@dataclass
class LLMResult:
    text: str
    model: str
    input_tokens: int | None
    output_tokens: int | None
    finish_reason: str | None
    attempt_count: int | None


class ProviderCaller:
    """Callable: (system, user) -> LLMResult. Holds the resolved key/client and
    the current temperature (which drops to None for the rest of the run if the
    API rejects a top-level temperature — matters for PLAN.md Sec 7)."""

    def __init__(self, provider: str, model: str, temperature: float | None, env_file: Path | None):
        self.provider = provider
        self.model = model
        self.requested_temperature = temperature
        self.temperature = temperature
        self.dropped_temperature = False
        if provider == "claude":
            from agentafl_orchestrator import call_claude, load_api_key, ClaudeAPIError
            self._call_claude = call_claude
            self._ClaudeAPIError = ClaudeAPIError
            key = None
            for f in _env_candidates(env_file):
                try:
                    key = load_api_key(f, "CLAUDE_API_KEY")
                    break
                except SystemExit:
                    continue
            if not key:
                raise SystemExit("CLAUDE_API_KEY not found in env or "
                                 + ", ".join(str(f) for f in _env_candidates(env_file)))
            self._key = key
        elif provider == "openai":
            import openai
            try:
                from dotenv import load_dotenv
                for f in _env_candidates(env_file):
                    if f and Path(f).exists():
                        load_dotenv(f, override=False)
            except ImportError:
                pass
            self._openai = openai
            # An ordered key list rather than one key, so a campaign can survive
            # the first key running out of credits. api_key is passed EXPLICITLY:
            # openai.OpenAI() would read os.environ, which is the wrong key the
            # moment we fail over.
            self._keys = openai_keys()
            self._key_idx = 0
            self._dead_keys: list[int] = []
            self._dead_key_cb = None
            if not self._keys:
                raise SystemExit(
                    "OPENAI_API_KEY not found in env or "
                    + ", ".join(str(f) for f in _env_candidates(env_file)))
            self._client = openai.OpenAI(api_key=self._keys[0][1])
        else:
            raise SystemExit(f"unknown provider {provider!r} (use claude or openai)")

    def __call__(self, system: str, user: str) -> LLMResult:
        return getattr(self, f"_run_{self.provider}")(system, user)

    # ------------------------------------------------------------------ keys
    @property
    def key_var(self) -> str | None:
        """Name of the key variable currently in use (never the key itself)."""
        if self.provider != "openai" or self._key_idx >= len(self._keys):
            return None
        return self._keys[self._key_idx][0]

    def key_count(self) -> int:
        return len(self._keys) if self.provider == "openai" else 1

    def set_dead_key_hook(self, cb) -> None:
        """cb(index, var_name) is called when a key is retired, so the caller can
        persist it and never probe that key again after a restart."""
        if self.provider == "openai":
            self._dead_key_cb = cb

    def skip_dead_keys(self, indices) -> bool:
        """Retire keys already known dead (e.g. from a previous daemon run).

        Returns False if that leaves nothing usable. No-op for non-openai
        providers so the claude path and the prompt_tests entry points are
        unaffected.
        """
        if self.provider != "openai":
            return True
        dead = {int(i) for i in indices}
        if not dead:
            return True
        self._dead_keys = sorted(dead)
        while self._key_idx < len(self._keys) and self._key_idx in dead:
            self._key_idx += 1
        if self._key_idx >= len(self._keys):
            return False
        self._client = self._openai.OpenAI(api_key=self._keys[self._key_idx][1])
        return True

    def _advance_key(self) -> bool:
        """Retire the current key and move to the next. False if none are left.

        The log line deliberately names only the VARIABLE and never includes the
        provider's error text: run_all_stages.sh greps stage logs for
        "insufficient_quota" and friends and aborts the whole sweep on a match,
        even when the stage exited 0. A successful failover must not trip that.
        """
        dead_idx = self._key_idx
        dead_var = self._keys[dead_idx][0]
        self._dead_keys.append(dead_idx)
        self._key_idx += 1
        if self._dead_key_cb is not None:
            try:
                self._dead_key_cb(dead_idx, dead_var)
            except Exception:                              # noqa: BLE001
                # Persisting the marker is best-effort; failing to write it must
                # not break the failover itself.
                pass
        if self._key_idx >= len(self._keys):
            print(f"  ! {dead_var} exhausted and no further key is configured")
            return False
        nxt = self._keys[self._key_idx][0]
        print(f"  ! {dead_var} exhausted - switching to {nxt} "
              f"for the rest of this run")
        self._client = self._openai.OpenAI(api_key=self._keys[self._key_idx][1])
        return True

    def _drop_temp(self, why: str):
        self.temperature = None
        self.dropped_temperature = True
        print(f"  ! {self.provider} rejected temperature ({why}) — dropping it for the rest of the run")

    def _run_claude(self, system, user) -> LLMResult:
        try:
            r = self._call_claude(self._key, self.model, system, user, temperature=self.temperature)
        except self._ClaudeAPIError as e:
            if self.temperature is not None and getattr(e, "http_status", None) == 400 \
                    and "temperature" in str(e).lower():
                self._drop_temp("HTTP 400")
                r = self._call_claude(self._key, self.model, system, user, temperature=None)
            else:
                raise
        u = (r.get("raw") or {}).get("usage") or {}
        return LLMResult(r["text"], self.model, u.get("input_tokens"), u.get("output_tokens"),
                         r.get("finish_reason"), r.get("attempt_count"))

    def _run_openai(self, system, user) -> LLMResult:
        kw = dict(model=self.model, messages=[{"role": "system", "content": system},
                                              {"role": "user", "content": user}])
        if self.temperature is not None:
            kw["temperature"] = self.temperature
        # Loop only to retry the SAME prompt on the next key after a credits
        # failure. Bounded by the number of keys, so it cannot spin.
        attempt = 0
        while True:
            attempt += 1
            try:
                r = self._client.chat.completions.create(**kw)
                break
            except self._openai.BadRequestError as e:
                if self.temperature is not None and "temperature" in str(e).lower():
                    self._drop_temp("400 bad_request")
                    kw.pop("temperature", None)
                    r = self._client.chat.completions.create(**kw)
                    break
                raise
            except Exception as e:                         # noqa: BLE001
                # An exhausted key is permanent, so fail over rather than retry.
                # Everything else (rate limits, 5xx) the SDK has already retried
                # internally and is not ours to swallow.
                if is_out_of_credits(e) and self._advance_key():
                    continue
                if is_out_of_credits(e):
                    raise OutOfCreditsError(
                        f"all {len(self._keys)} configured OpenAI key(s) are out "
                        "of credits") from e
                raise
        ch = r.choices[0]
        usage = getattr(r, "usage", None)
        return LLMResult(ch.message.content or "", getattr(r, "model", self.model),
                         getattr(usage, "prompt_tokens", None) if usage else None,
                         getattr(usage, "completion_tokens", None) if usage else None,
                         # >1 means this call was retried on a fallback key, so a
                         # jsonl row with attempt_count=2 marks the changeover.
                         ch.finish_reason, attempt)


# ---------------------------------------------------------------------------
# Coverage baseline + per-seed evaluation
# ---------------------------------------------------------------------------

def freeze_baseline(campaign_root: Path, instances: list[str], target: str,
                    target_args: list[str], showmap_bin: str, out_dir: Path,
                    reuse: Path | None):
    """Return (baseline_ints:set[int], meta:dict). Writes baseline_edges.json
    into out_dir either way so every run keeps the exact set it scored against.
    If `reuse` points at an existing baseline_edges.json, load that instead of
    re-scanning ~50k queue files (the point of --reuse-baseline)."""
    dest = out_dir / "baseline_edges.json"
    if reuse and Path(reuse).is_file():
        ids = [int(e) for e in json.loads(Path(reuse).read_text())]
        dest.write_text(json.dumps(sorted(ids)))
        print(f"[baseline] reused {reuse} — {len(ids)} edges (no queue scan)")
        return set(ids), {"reused_from": str(reuse), "baseline_edges": len(ids)}

    baseline: set[str] = set()
    per_instance = {}
    print(f"[baseline] freezing corpus coverage of {campaign_root.name} over "
          f"{len(instances)} queues (afl-showmap once per queue)...")
    for inst in instances:
        qdir = campaign_root / inst / "queue"
        if not qdir.is_dir():
            print(f"  ! {qdir} missing — skipping")
            continue
        n_files = sum(1 for _ in qdir.iterdir())
        edges = build_baseline(showmap_bin, target, target_args, qdir)
        before = len(baseline)
        baseline |= edges
        per_instance[inst] = {"queue_files": n_files, "edges": len(edges)}
        print(f"  {inst}: {n_files} files, {len(edges)} edges "
              f"(+{len(baseline) - before} to union, {len(baseline)} total)")
    if not baseline:
        raise SystemExit("baseline edge set empty — check --target / --afl-showmap-bin / campaign path")
    ints = {int(e) for e in baseline}
    dest.write_text(json.dumps(sorted(ints)))
    print(f"[baseline] {len(ints)} distinct edges frozen -> baseline_edges.json")
    return ints, {"per_instance": per_instance, "baseline_edges": len(ints)}


def evaluate_row(row: dict, seed_path: Path, baseline_ints: set[int],
                 target: str, target_args: list[str], showmap_bin: str):
    """Run one seed through afl-showmap, add coverage fields to `row` in place."""
    res = evaluate_candidate(showmap_bin, target, target_args, seed_path, baseline_ints)
    produced_new = res["status"] == "GOOD (novel coverage)"
    new_ids = sorted(set(res.get("edge_ids", [])) - baseline_ints) if produced_new else []
    row.update({
        "coverage_status": res["status"],
        "new_edges": res["new_edges"],
        "total_edges": res["total_edges"],
        "produced_new_edge": produced_new,
        "new_edge_ids": new_ids,
        "new_edge_sig": hashlib.sha1(",".join(map(str, new_ids)).encode()).hexdigest() if new_ids else "",
        "edge_sig": res["edge_sig"],
        "edge_ids": sorted(res.get("edge_ids", [])),
    })
    return res


# ---------------------------------------------------------------------------
# History run_dir (this batch's own seeds, fed back to the next prompt)
# ---------------------------------------------------------------------------

def append_history_cycle(history_dir: Path, cycle_id: int, rows: list[dict],
                         seeds_dir: Path, ext: str):
    """Append one `cycle` record to <history_dir>/cycles.jsonl and copy this
    call's seed files under candidates/cycle_XXXX/, in the shape
    build_context.scan_cycle_history expects."""
    cdir = history_dir / "candidates" / f"cycle_{cycle_id:04d}"
    cdir.mkdir(parents=True, exist_ok=True)
    cands = []
    for j, row in enumerate(rows):
        rel = f"candidates/cycle_{cycle_id:04d}/cand_{j:02d}.{ext}"
        shutil.copyfile(seeds_dir / Path(row["file"]).name, history_dir / rel)
        cands.append({
            "candidate_idx": j,
            "seed_path": rel,
            "raw_path": rel,
            "status": row.get("coverage_status", "NO_COVERAGE"),
            "new_edges": row.get("new_edges", 0),
            "total_edges": row.get("total_edges", 0),
            "injected": True,
            "edge_sig": row.get("edge_sig", ""),
            "edge_ids": row.get("edge_ids", []),
        })
    with (history_dir / "cycles.jsonl").open("a") as f:
        f.write(json.dumps({"record_type": "cycle", "cycle_id": cycle_id,
                            "candidates": cands}) + "\n")


# ---------------------------------------------------------------------------
# Batch contribution report (PLAN.md Sec 3)
# ---------------------------------------------------------------------------

def write_contribution_report(out_dir: Path, run_stamp: str, title: str, seed_rows: list[dict],
                              baseline_n: int, campaign: str, instances: list[str]):
    with_new = sorted((r for r in seed_rows if r.get("produced_new_edge")),
                      key=lambda r: r["seed_index"])  # generation order

    # SECONDARY metric — greedy, order-dependent, same rule as score_batch.py:
    # a seed counts only if it adds a new edge no earlier seed in the batch claimed.
    claimed: set[int] = set()
    union: set[int] = set()
    for r in with_new:
        nid = set(r["new_edge_ids"])
        r["contributor"] = bool(nid - claimed)
        claimed |= nid
        union |= nid
    n_contributors = sum(1 for r in with_new if r["contributor"])

    groups: dict[frozenset, list[dict]] = {}
    for r in with_new:
        groups.setdefault(frozenset(r["new_edge_ids"]), []).append(r)
    ordered = sorted(groups.items(),
                     key=lambda kv: (-len(kv[1]), min(x["seed_index"] for x in kv[1])))

    report = {
        "run_stamp": run_stamp, "title": title, "campaign": campaign, "instances": instances,
        "corpus_baseline_edges": baseline_n,
        "seeds_total": len(seed_rows),
        "seeds_with_new_edges": len(with_new),
        "distinct_new_edge_contributors": n_contributors,
        "distinct_new_edge_sets": len(groups),
        "new_edges_total_batch": len(union),
        "groups": [{
            "new_edge_count": len(k), "new_edge_ids": sorted(k),
            "seeds": [x["file"].split("/")[-1] for x in v],
            "redundant_within_batch": len(v) > 1,
        } for k, v in ordered],
    }
    (out_dir / f"contributors_{run_stamp}.json").write_text(json.dumps(report, indent=2))

    lines = [
        f"# {title} — batch contribution analysis", "",
        f"Run: `{run_stamp}`  |  Corpus baseline: **{baseline_n}** edges "
        f"({campaign}, {'+'.join(instances)})", "",
        f"- Seeds generated: **{len(seed_rows)}**",
        f"- Seeds with >=1 new edge vs the corpus: **{len(with_new)}**",
        f"- Distinct new-edge-contributing seeds: **{n_contributors}**  "
        f"(generation-order greedy — a seed counts only if it adds a new edge no "
        f"earlier seed in this batch already claimed; PLAN.md Sec 3 secondary metric)",
        f"- Distinct new-edge sets (groups below): **{len(groups)}**",
        f"- Total distinct new edges across the batch: **{len(union)}**  "
        f"(PLAN.md Sec 3 primary metric)", "",
        "## Coverage groups — seeds producing the same new-edge set", "",
    ]
    for i, (k, v) in enumerate(ordered, 1):
        names = ", ".join(f"`{x['file'].split('/')[-1]}`" for x in v)
        tag = "  _(redundant within batch — counts as 1 contributor)_" if len(v) > 1 else ""
        ids = sorted(k)
        shown = ", ".join(map(str, ids[:24])) + (" …" if len(ids) > 24 else "")
        lines += [f"### Group {i} — {len(k)} new edge(s) — {len(v)} seed(s){tag}",
                  f"- seeds: {names}", f"- new edge ids: {shown}", ""]
    if not ordered:
        lines += ["_No seed produced an edge outside the frozen corpus baseline._", ""]
    (out_dir / f"contributors_{run_stamp}.md").write_text("\n".join(lines))
    print(f"[report]  {n_contributors} distinct contributors / {len(with_new)} new-edge seeds "
          f"/ {len(groups)} distinct new-edge sets  -> contributors_{run_stamp}.md")
    return report


# ---------------------------------------------------------------------------
# The batch loop
# ---------------------------------------------------------------------------

@dataclass
class BatchConfig:
    out_dir: Path
    title: str                       # e.g. "Stage 0 baseline", "build_context stage1_raw"
    arm: str                         # short id for the summary
    calls: int
    seeds_per_call: int
    seed_kind: str
    fmt: str
    caller: ProviderCaller
    prompt_fn: object                # (call_index:int, history_dir:Path|None) -> {"system","user"[,"meta"]}
    uses_history: bool
    # evaluation
    do_eval: bool
    campaign_root: Path
    instances: list[str]
    target: str
    target_args: list[str]
    showmap_bin: str
    reuse_baseline: Path | None
    extra_summary: dict = field(default_factory=dict)


def run_batch(cfg: BatchConfig) -> dict:
    out_dir = cfg.out_dir
    prompts_dir, resp_dir, seeds_dir = out_dir / "prompts", out_dir / "responses", out_dir / "seeds"
    for d in (prompts_dir, resp_dir, seeds_dir):
        d.mkdir(parents=True, exist_ok=True)
    run_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    ext = EXT.get(cfg.seed_kind, "seed")
    expected = cfg.calls * cfg.seeds_per_call

    print(f"{cfg.title}: {cfg.calls} calls x {cfg.seeds_per_call} seeds = {expected} target")
    print(f"provider={cfg.caller.provider}  model={cfg.caller.model}  "
          f"temp={cfg.caller.requested_temperature}  history_feedback={cfg.uses_history}")
    print(f"Output: {out_dir}\n")

    baseline_ints: set[int] = set()
    baseline_meta = None
    if cfg.do_eval:
        baseline_ints, baseline_meta = freeze_baseline(
            cfg.campaign_root, cfg.instances, cfg.target, cfg.target_args,
            cfg.showmap_bin, out_dir, cfg.reuse_baseline)

    history_dir = out_dir / "_history" if cfg.uses_history else None
    if history_dir:
        (history_dir / "candidates").mkdir(parents=True, exist_ok=True)
        (history_dir / "cycles.jsonl").write_text("")

    seed_rows: list[dict] = []
    call_rows: list[dict] = []
    in_tok = out_tok = 0

    cf = (out_dir / "calls.jsonl").open("w")
    try:
        for i in range(cfg.calls):
            hist_arg = history_dir if (cfg.uses_history and i > 0) else None
            pr = cfg.prompt_fn(i, hist_arg)
            system, user = pr["system"], pr["user"]

            prompt_record = {
                "call": i, "provider": cfg.caller.provider, "model": cfg.caller.model,
                "temperature": cfg.caller.temperature,
                "history_dir_passed": hist_arg is not None,
                "system": system, "user": user,
                **(pr.get("meta") or {}),
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            (prompts_dir / f"call_{i:02d}.json").write_text(json.dumps(prompt_record, indent=2))

            t0 = time.monotonic()
            try:
                res = cfg.caller(system, user)
            except Exception as e:  # noqa: BLE001 — record and move on
                print(f"[call {i:02d}] FAILED: {e}")
                cf.write(json.dumps({"call": i, "error": str(e),
                                     "ts": datetime.now(timezone.utc).isoformat()}) + "\n")
                cf.flush()
                if is_out_of_credits(e):
                    print(f"[call {i:02d}] OUT OF CREDITS — aborting run (no summary written): {cfg.out_dir}")
                    sys.exit(OUT_OF_CREDITS_EXIT)
                continue
            latency = time.monotonic() - t0

            (resp_dir / f"call_{i:02d}.txt").write_text(res.text)
            seeds = parse_seeds(res.text, cfg.seed_kind)
            in_tok += res.input_tokens or 0
            out_tok += res.output_tokens or 0
            temp_applied = cfg.caller.temperature

            call_seed_rows = []
            seed_names = []
            for j, seed in enumerate(seeds):
                gidx = len(seed_rows) + len(call_seed_rows)
                name = f"seed_{gidx:03d}.{ext}"
                (seeds_dir / name).write_bytes(seed)
                seed_names.append(name)
                call_seed_rows.append({
                    "seed_index": gidx, "file": f"seeds/{name}", "call": i, "index_in_call": j,
                    "provider": cfg.caller.provider, "model": res.model, "temperature": temp_applied,
                    "bytes": len(seed), "sha256": hashlib.sha256(seed).hexdigest(),
                    "call_input_tokens": res.input_tokens, "call_output_tokens": res.output_tokens,
                    "seeds_in_call": len(seeds),
                    "call_output_tokens_per_seed_est":
                        round(res.output_tokens / len(seeds), 1) if res.output_tokens and seeds else None,
                    "finish_reason": res.finish_reason,
                    "ts": datetime.now(timezone.utc).isoformat(),
                })

            if cfg.do_eval:
                for row in call_seed_rows:
                    r = evaluate_row(row, seeds_dir / Path(row["file"]).name,
                                     baseline_ints, cfg.target, cfg.target_args, cfg.showmap_bin)
                    print(f"  {row['file']}: {r['status']}  (+{r['new_edges']} new / {r['total_edges']} total)")
                if history_dir is not None:
                    append_history_cycle(history_dir, i + 1, call_seed_rows, seeds_dir, ext)

            seed_rows.extend(call_seed_rows)

            call_rows.append({
                "call": i, "provider": cfg.caller.provider, "model": res.model,
                "temperature": temp_applied, "fmt": cfg.fmt, "seed_kind": cfg.seed_kind,
                "seeds_requested": cfg.seeds_per_call, "seeds_parsed": len(seeds),
                "seed_files": seed_names,
                "input_tokens": res.input_tokens, "output_tokens": res.output_tokens,
                "finish_reason": res.finish_reason, "attempt_count": res.attempt_count,
                "used_history": hist_arg is not None,
                "prompt_file": f"prompts/call_{i:02d}.json",
                "response_file": f"responses/call_{i:02d}.txt",
                "latency_s": round(latency, 2),
                "ts": datetime.now(timezone.utc).isoformat(),
            })
            cf.write(json.dumps(call_rows[-1]) + "\n")
            cf.flush()
            flag = "" if len(seeds) == cfg.seeds_per_call else "  <-- short"
            print(f"[call {i:02d}] {len(seeds)}/{cfg.seeds_per_call} seeds  "
                  f"in={res.input_tokens} out={res.output_tokens}  "
                  f"({res.finish_reason}, {latency:.1f}s){flag}")
    finally:
        cf.close()

    with (out_dir / "seeds.jsonl").open("w") as sf:
        for row in seed_rows:
            sf.write(json.dumps(row) + "\n")

    contrib = None
    if cfg.do_eval and seed_rows:
        n_new = sum(1 for r in seed_rows if r.get("produced_new_edge"))
        n_bad = sum(1 for r in seed_rows if r.get("coverage_status") == "BAD (redundant)")
        n_other = len(seed_rows) - n_new - n_bad
        print(f"\n[evaluate] new coverage: {n_new}/{len(seed_rows)} "
              f"({n_new / len(seed_rows) * 100:.1f}%)  |  redundant: {n_bad}  |  crash/timeout/no-cov: {n_other}")
        contrib = write_contribution_report(
            out_dir, run_stamp, cfg.title, seed_rows, len(baseline_ints),
            cfg.campaign_root.name, cfg.instances)

    summary = {
        "run_stamp": run_stamp, "arm": cfg.arm, "title": cfg.title,
        "provider": cfg.caller.provider, "model": cfg.caller.model,
        "temperature_requested": cfg.caller.requested_temperature,
        "temperature_applied": cfg.caller.temperature is not None,
        "temperature_dropped_by_api": cfg.caller.dropped_temperature,
        "calls": cfg.calls, "seeds_per_call": cfg.seeds_per_call,
        "seeds_expected": expected, "seeds_generated": len(seed_rows),
        "input_tokens_total": in_tok, "output_tokens_total": out_tok,
        "fmt": cfg.fmt, "seed_kind": cfg.seed_kind, "history_feedback": cfg.uses_history,
        "out_dir": str(out_dir),
        "baseline": baseline_meta,
        "campaign": cfg.campaign_root.name if cfg.do_eval else None,
        "coverage": None if contrib is None else {
            "seeds_with_new_coverage": contrib["seeds_with_new_edges"],
            "distinct_new_edge_contributors": contrib["distinct_new_edge_contributors"],
            "distinct_new_edge_sets": contrib["distinct_new_edge_sets"],
            "new_edges_total_batch": contrib["new_edges_total_batch"],
            "new_coverage_rate": round(contrib["seeds_with_new_edges"] / len(seed_rows), 4) if seed_rows else None,
            "contributors_report_md": f"contributors_{run_stamp}.md",
        },
        **cfg.extra_summary,
    }
    (out_dir / f"summary_{run_stamp}.json").write_text(json.dumps(summary, indent=2))

    print(f"\nDone: {len(seed_rows)}/{expected} seeds -> {seeds_dir}")
    print(f"Tokens: {in_tok} in / {out_tok} out")
    print(f"Prompts:  {prompts_dir}")
    print(f"Per-seed: {out_dir / 'seeds.jsonl'}")
    print(f"Per-call: {out_dir / 'calls.jsonl'}")
    print(f"Summary:  {out_dir / f'summary_{run_stamp}.json'}")
    if cfg.caller.dropped_temperature:
        print("NOTE: temperature was NOT applied (API rejected it) — record per PLAN.md Sec 7.")
    if len(seed_rows) < expected:
        print(f"NOTE: {expected - len(seed_rows)} seed(s) short of target.")
    return summary
