#!/usr/bin/env python3
"""AgentAFL Level-2 escalation daemon.

Watches for the escalation request written by run_analyzer.py's hook and, when
Level 1 has adapted and failed to recover a coverage plateau, runs one AgentAFL
Stage 4 (positive + negative feedback) generation round and injects the seeds into
the live campaign.

Usage (as launched by cloud/run_campaign.sh for the `agentafl` condition):

    python3 agentafl_escalation.py \
        --campaign-root  <run>/afl-out \
        --work-dir       <run>/agentafl \
        --target         <build>/xmllint-afl \
        --target-args    "--noout @@" \
        --fmt            "XML document" \
        --reuse-baseline <frozen baseline_edges.json>   # optional
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import agentafl_cycle as ac                                  # noqa: E402
import batch_harness                                         # noqa: E402

DEFAULT_COOLDOWN_S = 1500
DEFAULT_POLL_S = 2.0
# How many escalation opportunities the current Level-1 loop generates:
# run_analyzer.py's Coverage Retry (added 2026-10-15) clears the stagnation latch
# whenever recovery yields zero edges, so COVERAGE_SLOWDOWN re-fires roughly every
# K=2 detect + 3 observe = 5 windows = 150s. Replayed over her committed
# six_hour_results that is 49-106 per 6h run (98-212 per 12h), against ~27 under
# the old latched code. So the cooldown is load-bearing, not a backstop: 1500s
# gives ~28 escalations per 12h, which is the budget the 5x5 sizing assumes.


class Daemon:
    def __init__(self, args):
        self.args = args
        self.campaign_root = Path(args.campaign_root).resolve()
        self.work_dir = Path(args.work_dir).resolve()
        self.history_dir = self.work_dir / "history"
        self.esc_dir = self.work_dir / "escalations"
        self.log_path = self.work_dir / "escalations.jsonl"
        self.request_path = Path(args.request_file).resolve()

        # Absolute, because we chdir below and because afl-showmap /
        # afl-addseeds are handed this path from a different cwd.
        self.target = str(Path(args.target).resolve())
        self.target_args = args.target_args.split()
        self.instances = [s.strip() for s in args.instances.split(",") if s.strip()]

        self.edge_cache: dict = {}
        self.call_ctx: dict = {"call": None}
        self.last_escalation_at = 0.0
        self.escalations = 0          # this process only, for the stop message
        self.dropped = 0
        self.stop = False
        self._baseline_cache: tuple[int, set[int]] | None = None
        self._campaign_start: int | None = None

        for d in (self.work_dir, self.history_dir, self.esc_dir):
            d.mkdir(parents=True, exist_ok=True)

        self.timeline_path = self.work_dir / "agentafl_timeline.csv"
        if not self.timeline_path.exists():
            self.timeline_path.write_text(
                "unix_time,elapsed_s,elapsed_hms,event,escalation_id,calls,"
                "seeds,new_edges,injected,synced_main,recovery_edges,detail\n")

    # ------------------------------------------------------------------ clock
    def campaign_start(self) -> int | None:
        """afl-fuzz's start epoch, cached once it is available.

        Resolved lazily rather than at startup because run_campaign.sh may launch
        this before afl-fuzz has written fuzzer_stats.
        """
        if self._campaign_start is None:
            self._campaign_start = ac.campaign_start_time(
                self.campaign_root, self.args.instance)
        return self._campaign_start

    def elapsed(self, at=None):
        """Seconds since afl-fuzz started: plot_data's relative_time clock."""
        start = self.campaign_start()
        if start is None:
            return None
        return round((at if at is not None else time.time()) - start, 1)

    # ------------------------------------------------------------------ logging
    def log(self, msg):
        print(f"{time.strftime('%H:%M:%S')} T+{ac._hms(self.elapsed())} {msg}",
              flush=True)

    def record(self, rec: dict):
        """Append to escalations.jsonl, and mirror one row into the CSV timeline.

        Two formats on purpose: the JSONL keeps everything (per-call yield, seed
        memory sizes, prompts paths) while the CSV is a flat, joinable timeline in
        the same spirit as plateau_log.csv and adapt_signal_log.csv - one row per
        event, keyed on the same relative_time axis as afl-out/*/plot_data.
        """
        rec.setdefault("ts", time.time())
        rec.setdefault("elapsed_s", self.elapsed(rec["ts"]))
        with self.log_path.open("a") as f:
            f.write(json.dumps(rec) + "\n")

        def csv_cell(v):
            s = "" if v is None else str(v)
            return '"' + s.replace('"', "'") + '"' if ("," in s or '"' in s) else s

        row = [
            round(rec["ts"], 3),
            rec.get("elapsed_s"),
            ac._hms(rec.get("elapsed_s")),
            rec.get("event", "?"),
            rec.get("escalation_id"),
            rec.get("calls_made"),
            rec.get("seeds_generated"),
            rec.get("new_edges_total"),
            rec.get("injected"),
            ac.synced_count(self.campaign_root, self.args.instance),
            (rec.get("request") or {}).get("total_recovery_edges"),
            rec.get("reason") or rec.get("error") or rec.get("inject_detail") or "",
        ]
        with self.timeline_path.open("a") as f:
            f.write(",".join(csv_cell(c) for c in row) + "\n")

    # ------------------------------------------------------------------ startup
    def build_caller(self):
        """ProviderCaller is built ONCE and reused for the whole campaign.

        It exits the process on a missing API key, so do this during startup where
        the failure is loud, not on the first escalation hours into a run.
        """
        try:
            return batch_harness.ProviderCaller(
                self.args.provider,
                self.args.model or batch_harness.DEFAULT_MODEL[self.args.provider],
                None if self.args.no_temperature else self.args.temperature,
                Path(self.args.env_file) if self.args.env_file else None,
            )
        except SystemExit as exc:
            self.log(f"[AgentAFL] FATAL: cannot build LLM caller ({exc}). "
                     "Escalation disabled; the campaign continues without it.")
            raise

    def preflight(self):
        missing = []
        if not self.campaign_root.is_dir():
            missing.append(f"campaign root {self.campaign_root}")
        if not Path(self.target).is_file():
            missing.append(f"target {self.target}")
        if missing:
            self.log("[AgentAFL] FATAL: " + "; ".join(missing))
            return False

        # run_campaign.sh exports AFL_PYTHON_MODULE before launching us, and
        # afl-showmap aborts if it cannot import that module. See
        # agentafl_cycle.sanitize_afl_env for why this is silent and fatal.
        stripped = ac.sanitize_afl_env()
        if stripped:
            self.log("[AgentAFL] stripped inherited mutator env vars so child "
                     f"afl-* tools do not abort: {', '.join(stripped)}")


        hook = os.environ.get("AGENTAFL_HOOK")
        if hook != "1" and not self.args.allow_hook_off:
            self.log(
                f"[AgentAFL] FATAL: AGENTAFL_HOOK={hook!r} (needs '1'). "
                "run_analyzer.py's escalation hook is opt-in, so nothing would "
                "ever write a request and this arm would equal `minyee`. "
                "cloud/run_campaign.sh exports it for condition=combined; "
                "for a manual run export AGENTAFL_HOOK=1 in the analyzer's "
                "environment too, or pass --allow-hook-off if you intend to "
                "drive escalations by writing the request file yourself.")
            return False
        if hook != "1":
            self.log("[AgentAFL] WARNING: AGENTAFL_HOOK is not set and "
                     "--allow-hook-off was passed - only manually written "
                     "requests will fire an escalation.")

        if not self.showmap_selftest():
            return False
        if "@@" not in self.target_args:
            self.log("[AgentAFL] WARNING: --target-args has no '@@'; afl-showmap "
                     "would read stdin and measure an empty input.")
        # afl-addseeds refuses to run until some instance has written fuzzer_stats
        # ("not an 'afl-fuzz -o ...' output directory"), so a campaign that has not
        # started yet would accept generated seeds and inject none of them.
        if not any(self.campaign_root.glob("*/fuzzer_stats")):
            self.log("[AgentAFL] WARNING: no */fuzzer_stats under "
                     f"{self.campaign_root} yet - afl-addseeds will refuse until "
                     "afl-fuzz writes it. Normal at startup; escalations cannot "
                     "fire for several minutes anyway.")
        self.log(f"[AgentAFL] watching {self.request_path}")
        self.log(f"[AgentAFL] campaign {self.campaign_root}, "
                 f"budget {self.args.calls}x{self.args.seeds_per_call}, "
                 f"cooldown {self.args.cooldown_s}s, "
                 f"max {self.args.max_escalations or 'unlimited'}")
        start = self.campaign_start()
        if start is None:
            self.log("[AgentAFL] campaign start not readable yet; elapsed_s will "
                     "be filled in once fuzzer_stats appears")
        else:
            self.log(f"[AgentAFL] campaign start {start} "
                     f"(already T+{ac._hms(self.elapsed())}); elapsed_s in the logs "
                     "is on the same clock as afl-out/*/plot_data relative_time")
        self.log(f"[AgentAFL] timeline -> {self.timeline_path}")
        mem = ac.memory_stats(self.history_dir)
        self.log(f"[AgentAFL] seed memory at start: {mem['positive_seeds']} positive / "
                 f"{mem['negative_seeds']} negative, next cycle_id {mem['next_cycle_id']}")
        return True

    def showmap_selftest(self) -> bool:
        """Run afl-showmap on one real input and require a non-empty edge set.

        Catches, at startup: a mutator env var still leaking through, a target
        built without instrumentation, a wrong --target-args (no `@@`, so the
        harness reads stdin and sees an empty document), and a missing
        afl-showmap. All of those otherwise surface as "0 edges" on the first
        escalation, which `build_baseline` reports as an empty baseline rather
        than an error.
        """
        import tempfile
        probe = None
        for inst in self.instances:
            q = self.campaign_root / inst / "queue"
            if q.is_dir():
                probe = next((p for p in sorted(q.glob("id:*")) if p.is_file()), None)
                if probe:
                    break
        tmp_probe = None
        if probe is None:
            # No queue yet (daemon started before the fuzzers produced one).
            # --probe-input names a small valid input for THIS format; without
            # it we fall back to an XML document, which is only meaningful for
            # the libxml2 target. Feeding XML to libical makes the self-test
            # prove nothing about the real corpus.
            if self.args.probe_input and Path(self.args.probe_input).is_file():
                probe = Path(self.args.probe_input)
            else:
                tmp_probe = Path(tempfile.mkstemp(suffix=".xml")[1])
                tmp_probe.write_bytes(b"<?xml version=\"1.0\"?><r><a k=\"1\"/></r>")
                probe = tmp_probe
                if self.args.probe_input:
                    self.log(f"[AgentAFL] WARNING: --probe-input "
                             f"{self.args.probe_input} not found; falling back "
                             "to a generic XML probe")
        try:
            from evaluate_seeds import run_showmap
            status, edges = run_showmap(
                self.args.showmap_bin, self.target, self.target_args, probe)
        except Exception as exc:                            # noqa: BLE001
            self.log(f"[AgentAFL] FATAL: afl-showmap self-test raised "
                     f"{type(exc).__name__}: {exc}")
            return False
        finally:
            if tmp_probe is not None:
                tmp_probe.unlink(missing_ok=True)

        if not edges:
            self.log(f"[AgentAFL] FATAL: afl-showmap returned 0 edges for "
                     f"{probe.name} (status={status}). Every seed would score 0 "
                     "new edges. Check the target is instrumented, that "
                     f"--target-args ({' '.join(self.target_args)}) contains "
                     "'@@', and that no AFL_PYTHON_MODULE / "
                     "AFL_CUSTOM_MUTATOR_* var is set.")
            return False
        self.log(f"[AgentAFL] afl-showmap self-test OK: {len(edges)} edges "
                 f"on {probe.name} (status={status})")
        return True

    # ----------------------------------------------------------------- baseline
    def baseline(self, out_dir: Path) -> set[int]:
        """Edge set to score 'new coverage' against, re-frozen as the campaign moves.

        AgentAFL's pipeline assumes a frozen queue and a frozen baseline; a live
        campaign has neither. Re-freezing per escalation keeps the GOOD/BAD
        classification honest, which is the whole point of Stage 4 feedback.
        Cached by total queue file count so back-to-back escalations do not repeat
        the full afl-showmap -C pass (~1 min on a ~6.5k-file queue).

        Historical GOOD labels in cycles.jsonl are deliberately NOT rewritten when
        a past seed's edges later enter the baseline: it *was* a contributor, which
        is what makes it a useful example.
        """
        if self.args.reuse_baseline:
            ids, _ = batch_harness.freeze_baseline(
                self.campaign_root, self.instances, self.target,
                self.target_args, self.args.showmap_bin, out_dir,
                Path(self.args.reuse_baseline))
            return ids

        n = 0
        for inst in self.instances:
            q = self.campaign_root / inst / "queue"
            if q.is_dir():
                n += sum(1 for _ in q.glob("id:*"))
        if self._baseline_cache and self._baseline_cache[0] == n:
            self.log(f"[AgentAFL]   reusing baseline ({n} queue files unchanged)")
            ids = self._baseline_cache[1]
            (out_dir / "baseline_edges.json").write_text(json.dumps(sorted(ids)))
            return ids

        t0 = time.time()
        ids, _ = batch_harness.freeze_baseline(
            self.campaign_root, self.instances, self.target,
            self.target_args, self.args.showmap_bin, out_dir, None)
        self.log(f"[AgentAFL]   froze baseline: {len(ids)} edges from {n} queue "
                 f"files in {time.time() - t0:.0f}s")
        self._baseline_cache = (n, ids)
        return ids

    # ------------------------------------------------------------------ request
    def take_request(self) -> dict | None:
        """Read and consume the request file, so one request fires at most once.

        Unlinks before acting: if an escalation crashes we lose that request rather
        than re-firing it forever. A partial or malformed file is discarded.
        """
        try:
            raw = self.request_path.read_text()
        except OSError:
            return None
        try:
            self.request_path.unlink()
        except OSError:
            pass
        try:
            req = json.loads(raw)
        except ValueError:
            self.log("[AgentAFL] ignoring malformed request file")
            return None
        return req if isinstance(req, dict) else None

    def should_escalate(self, req: dict) -> tuple[bool, str]:
        if req.get("trigger_reason") != "COVERAGE_SLOWDOWN":
            return False, f"trigger_reason={req.get('trigger_reason')!r}"
        if req.get("improved"):
            return False, "Level 1 recovered"
        # Campaign-wide, not per-process: a daemon restart must not silently
        # double the LLM budget.
        if self.args.max_escalations:
            done = ac.completed_escalations(self.log_path)
            if done >= self.args.max_escalations:
                return False, (f"max_escalations ({self.args.max_escalations}) "
                               f"reached for this campaign ({done} done)")
        waited = time.time() - self.last_escalation_at
        if self.last_escalation_at and waited < self.args.cooldown_s:
            return False, f"cooldown ({self.args.cooldown_s - waited:.0f}s left)"
        return True, ""

    # --------------------------------------------------------------------- loop
    def run(self):
        # afl-showmap drops a .afl-showmap-temp-<pid> scratch file into its CWD.
        # Run from the work dir so that lands with the run's own artifacts rather
        # than littering the repo checkout (which may also be read-only). Done
        # here, not in __init__, so constructing a Daemon has no side effects.
        os.chdir(self.work_dir)

        if not self.preflight():
            return 2
        caller = self.build_caller()
        topup = ac.make_selectors(
            target=self.target, target_args=self.target_args,
            showmap_bin=self.args.showmap_bin, edge_cache=self.edge_cache,
            jaccard_max=self.args.jaccard_max,
            log_path=self.work_dir / "selector_fallback.log",
            call_ctx=self.call_ctx)

        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: setattr(self, "stop", True))

        while not self.stop:
            req = self.take_request()
            if req is None:
                time.sleep(self.args.poll_s)
                continue

            go, why = self.should_escalate(req)
            if not go:
                self.dropped += 1
                self.log(f"[AgentAFL] request dropped: {why}")
                self.record({"event": "dropped", "ts": time.time(),
                             "reason": why, "request": req})
                continue

            self.escalations += 1
            # Derived from the log, NOT the in-process counter: a restart would
            # otherwise reuse esc_001 and overwrite the first escalation's
            # prompts, responses, seeds and frozen baseline.
            eid = ac.next_escalation_id(self.log_path)
            out_dir = self.esc_dir / f"esc_{eid:03d}"
            out_dir.mkdir(parents=True, exist_ok=True)
            self.last_escalation_at = time.time()

            self.log(f"[AgentAFL] escalating (#{eid}): window {req.get('window_id')}, "
                     f"recovery edges {req.get('total_recovery_edges')}")
            try:
                baseline_ints = self.baseline(out_dir)
                summary = ac.run_escalation(
                    campaign_root=self.campaign_root,
                    history_dir=self.history_dir,
                    out_dir=out_dir,
                    escalation_id=eid,
                    caller=caller,
                    baseline_ints=baseline_ints,
                    target=self.target,
                    target_args=self.target_args,
                    topup_selector=topup,
                    edge_cache=self.edge_cache,
                    instance=self.args.instance,
                    calls=self.args.calls,
                    seeds_per_call=self.args.seeds_per_call,
                    n_seeds=self.args.n_seeds,
                    n_history_failure=self.args.n_history_failure,
                    fmt=self.args.fmt,
                    seed_kind=self.args.seed_kind,
                    showmap_bin=self.args.showmap_bin,
                    jaccard_max=self.args.jaccard_max,
                    topup_buffer=self.args.topup_buffer,
                    asan_hint=not self.args.no_asan_hint,
                    addseeds_bin=self.args.addseeds_bin,
                    early_stop_patience=self.args.early_stop_patience,
                    max_calls=self.args.max_calls,
                    call_ctx=self.call_ctx,
                    campaign_start=self.campaign_start(),
                    log=self.log,
                )
                summary["event"] = "escalation"
                summary["request"] = req
                self.record(summary)

            except SystemExit as exc:
                # batch_harness exits 3 when the account is out of credits, and
                # bails on an empty baseline. Neither should take the campaign
                # down with it - stop escalating and let the fuzzers run on.
                code = exc.code if isinstance(exc.code, int) else 1
                self.log(f"[AgentAFL] generation aborted (exit {code}). "
                         "Escalation disabled for the rest of the run.")
                self.record({"event": "aborted", "ts": time.time(),
                             "exit_code": code, "escalation_id": eid})
                return 0
            except Exception as exc:                       # noqa: BLE001
                self.log(f"[AgentAFL] escalation {eid} failed: "
                         f"{type(exc).__name__}: {exc}")
                self.record({"event": "error", "ts": time.time(),
                             "escalation_id": eid,
                             "error": f"{type(exc).__name__}: {exc}"})

        self.log(f"[AgentAFL] stopping: {self.escalations} escalations, "
                 f"{self.dropped} requests dropped")
        return 0


def build_parser():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--campaign-root", required=True,
                   help="afl-fuzz -o dir (the PARENT of main/sec1/..)")
    p.add_argument("--work-dir", required=True,
                   help="where history/, escalations/ and escalations.jsonl live")
    p.add_argument("--target", required=True)
    p.add_argument("--target-args", default="--noout @@")
    p.add_argument("--probe-input", default=None,
                   help="small valid input for this target's format, used by "
                        "the afl-showmap self-test when no instance queue "
                        "exists yet. Defaults to a generic XML document, which "
                        "is only appropriate for libxml2.")
    p.add_argument("--request-file",
                   default=str(HERE / "adaptation" / ".agentafl_request"),
                   help="written by run_analyzer.py's hook")
    p.add_argument("--instance", default="main",
                   help="instance whose queue supplies bootstrap good seeds")
    p.add_argument("--instances", default="main,sec1,sec2",
                   help="instances unioned when freezing the baseline")

    p.add_argument("--calls", type=int, default=ac.DEFAULT_CALLS)
    p.add_argument("--seeds-per-call", type=int, default=ac.DEFAULT_SEEDS_PER_CALL)
    p.add_argument("--n-seeds", type=int, default=ac.DEFAULT_N_SEEDS)
    p.add_argument("--n-history-failure", type=int,
                   default=ac.DEFAULT_N_HISTORY_FAILURE)
    p.add_argument("--jaccard-max", type=float, default=ac.DEFAULT_JACCARD_MAX)
    p.add_argument("--topup-buffer", type=int, default=ac.DEFAULT_TOPUP_BUFFER)
    p.add_argument("--early-stop-patience", type=int, default=0,
                   help="0 = fixed --calls budget (default). N = stop after N "
                        "consecutive zero-yield calls, extending up to --max-calls")
    p.add_argument("--max-calls", type=int, default=None,
                   help="cap when --early-stop-patience is set (e.g. 8)")

    p.add_argument("--allow-hook-off", action="store_true",
                   help="start even when AGENTAFL_HOOK is not 1. Only for driving "
                        "escalations by writing the request file by hand; a real "
                        "campaign needs the hook enabled or nothing will fire.")
    p.add_argument("--cooldown-s", type=float, default=DEFAULT_COOLDOWN_S)
    p.add_argument("--max-escalations", type=int, default=0, help="0 = unlimited")
    p.add_argument("--poll-s", type=float, default=DEFAULT_POLL_S)

    p.add_argument("--fmt", default="XML document")
    p.add_argument("--seed-kind", default="text", choices=["text", "binary"])
    p.add_argument("--no-asan-hint", action="store_true")
    p.add_argument("--showmap-bin", default="afl-showmap")
    p.add_argument("--addseeds-bin", default="afl-addseeds")
    p.add_argument("--reuse-baseline", default=None,
                   help="frozen baseline_edges.json; omit to re-freeze per escalation")

    p.add_argument("--provider", default="claude", choices=["claude", "openai"])
    p.add_argument("--model", default=None)
    p.add_argument("--temperature", type=float, default=1.0)
    p.add_argument("--no-temperature", action="store_true")
    p.add_argument("--env-file", default=None)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    return Daemon(args).run()


if __name__ == "__main__":
    sys.exit(main())
