#!/bin/bash
# finish_campaign.sh - started automatically by run_campaign.sh; you don't
# normally run this yourself.
#
# Waits until every afl-fuzz instance of the campaign has exited (they stop
# themselves via -V), then:
#   1. stops the passive loggers
#   2. writes summary.txt (afl-whatsup) and end time into meta.txt
#   3. packs the whole run into $WORK/results/<run>.tar.gz
#   4. if AUTO_SHUTDOWN=1 and no other campaign is still fuzzing, powers the
#      machine off (EC2 "stop": compute billing ends, the disk is kept)
#
# Manual use (e.g. after killing a run early):
#   AUTO_SHUTDOWN=0 ./finish_campaign.sh ~/fyp/runs/libxml2_baseline_run1
set -uo pipefail

RUN="${1:?usage: finish_campaign.sh <run_dir>}"
RUN="$(cd "$RUN" && pwd)"
OUT="$RUN/afl-out"
NAME="$(basename "$RUN")"
WORK="${WORK:-$HOME/fyp}"
AUTO_SHUTDOWN="${AUTO_SHUTDOWN:-1}"
SUDO=""; [[ $EUID -ne 0 ]] && SUDO="sudo"
log() { echo "[$(date -u +%H:%M:%S)] $*"; }

log "waiting for afl-fuzz instances of $NAME to finish..."
sleep 60
while pgrep -f -- "-o $OUT " >/dev/null; do sleep 60; done
log "all fuzzers have exited"

# 1. stop loggers (give them one more poll so the final state is logged)
#
# Explicit list, NOT a glob over pids/*.pid: after 12h the main/sec*.pid files
# hold recycled PIDs, and nullglob is not set in this script.
#
# Order matters. The analyzer goes first so it cannot write a new mode after the
# last window is logged; signal_log goes last so it records that final mode.
# This is not optional for minyee: run_analyzer.py is a `while True` with every
# exception caught inside the loop, so it NEVER exits on its own and would
# survive to fight the next campaign over the shared .adapt_signal.
# For baseline the two minyee names simply do not exist, so nothing changes.
#
# agentafl_escalation goes FIRST: once it is gone no new escalation can start, so
# nothing can inject seeds into a campaign that is being torn down. It also has
# the same never-exits-on-its-own problem as the analyzer (a `while not stop`
# poll loop), so leaving it alive would let it fight the next campaign over the
# shared .agentafl_request. SIGTERM lets it finish an escalation already in
# flight rather than losing that round's seeds.
sleep 15
for p in agentafl_escalation minyee_analyzer minyee_monitor plateau_watch signal_log; do
  f="$RUN/pids/$p.pid"
  [[ -f "$f" ]] || continue
  pid="$(cat "$f" 2>/dev/null || true)"
  [[ -n "$pid" ]] || continue
  kill "$pid" 2>/dev/null && log "stopped $p" || true
done

# 2. summary + end time
afl-whatsup -s "$OUT" > "$RUN/summary.txt" 2>&1 || true
for d in "$OUT"/*/; do
  [[ -f "$d/fuzzer_stats" ]] || continue
  echo "== $(basename "$d")"
  grep -E '^(run_time|execs_done|execs_per_sec|corpus_count|edges_found|total_edges|bitmap_cvg|saved_crashes|saved_hangs|stability) ' "$d/fuzzer_stats"
done >> "$RUN/summary.txt"
# minyee health, so a dead loop is visible at unpack time rather than after analysis
if [[ -f "$RUN/logs/minyee_analyzer.log" || -f "$RUN/adapt_signal_log.csv" ]]; then
  {
    echo
    echo "== ResourceAwareAFL loop"
    if [[ -f "$RUN/monitor/raw_metrics.json" ]]; then
      echo "monitor_windows=$(wc -l < "$RUN/monitor/raw_metrics.json" | tr -d ' ')"
    else
      echo "monitor_windows=0  <-- monitor.py logged NOTHING"
    fi
    # NOTE: `grep -c` prints "0" AND exits 1 when nothing matches, so `|| echo 0`
    # would emit a SECOND "0" on its own line. Always use `|| true` here.
    if [[ -f "$RUN/adapt_signal_log.csv" ]]; then
      echo "signal_writes=$(grep -c ',write$' "$RUN/adapt_signal_log.csv" 2>/dev/null || true)"
      echo "modes_seen=$(awk -F, 'NR>1{print $3}' "$RUN/adapt_signal_log.csv" 2>/dev/null | sort -u | paste -sd' ' -)"
      echo "final_mode=$(awk -F, 'END{print $3}' "$RUN/adapt_signal_log.csv" 2>/dev/null)"
    fi
    if [[ -f "$RUN/logs/minyee_analyzer.log" ]]; then
      A="$RUN/logs/minyee_analyzer.log"
      echo "analyzer_errors=$(grep -c '\[-\] Analyzer error' "$A" 2>/dev/null || true)"
      echo "analyzer_triggers=$(grep -c 'Trigger = ' "$A" 2>/dev/null || true)"
      # run_analyzer.py has no "calibration complete" message. It prints
      # "[Calibration] n/N windows" while calibrating and "[Window NNN] ..." for
      # every window it actually evaluates, so eval_windows > 0 is the real proof
      # that calibration finished and the trigger logic is running.
      echo "analyzer_calibration_windows=$(grep -c '\[Calibration\]' "$A" 2>/dev/null || true)"
      echo "analyzer_eval_windows=$(grep -c '\[Window ' "$A" 2>/dev/null || true)"
      # Non-zero means the U+0394 encoding trap was hit and NO trigger ever ran.
      echo "analyzer_unicode_errors=$(grep -c 'UnicodeEncodeError' "$A" 2>/dev/null || true)"
    fi
    for f in "$RUN"/logs/main.log "$RUN"/logs/sec*.log; do
      [[ -f "$f" ]] || continue
      echo "$(basename "$f" .log): mutator_loaded=$(grep -c 'installed successfully' "$f" 2>/dev/null || true) mode_changes=$(grep -c 'Adaptive Mutator. Mode changed' "$f" 2>/dev/null || true)"
    done
  } >> "$RUN/summary.txt"
fi

# AgentAFL Level-2 health. The failure mode to catch here is "the arm ran but
# never actually escalated", which looks identical to a minyee run in every other
# artifact. escalations=0 with requests_dropped=0 means her hook never fired;
# escalations>0 with injected=0 means afl-addseeds refused; injected>0 with
# synced_advance=0 means the seeds were written but no fuzzer ever imported them
# (the single-instance is_main_node trap).
if [[ -d "$RUN/agentafl" ]]; then
  {
    echo
    echo "== AgentAFL Level-2"
    J="$RUN/agentafl/escalations.jsonl"
    if [[ -f "$J" ]]; then
      python3 - "$J" "$RUN/afl-out" <<'PYESC'
import json, sys, pathlib
recs = []
for line in pathlib.Path(sys.argv[1]).read_text(errors="replace").splitlines():
    line = line.strip()
    if line:
        try:
            recs.append(json.loads(line))
        except ValueError:
            pass
esc = [r for r in recs if r.get("event") == "escalation"]
print(f"escalations={len(esc)}")
print(f"requests_dropped={sum(1 for r in recs if r.get('event') == 'dropped')}")
print(f"errors={sum(1 for r in recs if r.get('event') == 'error')}")
print(f"aborted={sum(1 for r in recs if r.get('event') == 'aborted')}")

print(f"llm_calls={sum(r.get('calls_made', 0) for r in esc)}")
print(f"seeds_generated={sum(r.get('seeds_generated', 0) for r in esc)}")
print(f"seeds_injected={sum(r.get('injected', 0) for r in esc)}")
print(f"seeds_with_new_coverage={sum(r.get('seeds_with_new_coverage', 0) for r in esc)}")
print(f"new_edges_per_escalation={[r.get('new_edges_total', 0) for r in esc]}")


def hms(s):
    if s is None:
        return "?"
    s = int(s)
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"

# API key failover. key_retired rows are fine - that is the failsafe working.
# credits_exhausted means every key died and the campaign was stopped early, so
# this run is PARTIAL: keep it to inspect, but re-run the cell.
retired = [r for r in recs if r.get("event") == "key_retired"]
for r in retired:
    print(f"key_retired={r.get('key_var')} (index {r.get('key_index')})")
dead = [r for r in recs if r.get("event") == "credits_exhausted"]
if dead:
    print(f"credits_exhausted=1 at T+{hms(dead[0].get('elapsed_s'))}"
          f"  <-- ALL API KEYS DIED; campaign stopped early, run is PARTIAL")
else:
    print("credits_exhausted=0")


# When AgentAFL actually ran, relative to afl-fuzz's own start_time - the same
# clock as afl-out/*/plot_data's relative_time column, so these stamps line up
# directly with the coverage curve.
if esc:
    print("escalation_times_T+=" + " ".join(hms(r.get("elapsed_s")) for r in esc))
    print("escalation_elapsed_s=" + ",".join(
        str(r.get("elapsed_s")) for r in esc))
    gaps = [round(b.get("elapsed_s", 0) - a.get("elapsed_s", 0))
            for a, b in zip(esc, esc[1:])
            if a.get("elapsed_s") is not None and b.get("elapsed_s") is not None]
    if gaps:
        print(f"escalation_gaps_s={gaps}  (min={min(gaps)} should be >= cooldown)")
    first = esc[0].get("elapsed_s")
    if first is not None:
        print(f"first_escalation_T+={hms(first)}")
print(f"llm_seconds_total={round(sum(r.get('llm_latency_s', 0) for r in esc), 1)}")
if esc:
    m = esc[-1].get("memory_after", {})
    print(f"final_seed_memory={m.get('positive_seeds', '?')} positive / "
          f"{m.get('negative_seeds', '?')} negative, "
          f"cycles={m.get('next_cycle_id', 0) - 1}")
# Proof the injected seeds actually reached a fuzzer.
for inst in sorted(p.name for p in pathlib.Path(sys.argv[2]).glob("*") if p.is_dir()):
    f = pathlib.Path(sys.argv[2]) / inst / ".synced" / "addseeds"
    try:
        raw = f.read_bytes()[:4]
    except OSError:
        continue
    if len(raw) == 4:
        print(f"synced_addseeds_{inst}={int.from_bytes(raw, 'little')}")
PYESC
    else
      echo "escalations=0  <-- escalations.jsonl missing: the daemon never ran"
    fi
    if [[ -f "$RUN/logs/agentafl_escalation.log" ]]; then
      L="$RUN/logs/agentafl_escalation.log"
      echo "daemon_log_lines=$(wc -l < "$L" | tr -d ' ')"
      echo "daemon_fatal=$(grep -c 'FATAL' "$L" 2>/dev/null || true)"
    fi
  } >> "$RUN/summary.txt"
fi

{
  echo "end_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "end_unix=$(date +%s)"
} >> "$RUN/meta.txt"
rm -f "$RUN/afl-out"/*/.cur_input 2>/dev/null

# 3. pack
mkdir -p "$WORK/results"
tar -czf "$WORK/results/$NAME.tar.gz" -C "$(dirname "$RUN")" "$NAME" \
  && log "packed -> $WORK/results/$NAME.tar.gz ($(du -h "$WORK/results/$NAME.tar.gz" | cut -f1))" \
  || log "ERROR: tar failed - run dir left in place at $RUN"

# 4. shutdown, only if nothing else on this machine is still fuzzing
if [[ "$AUTO_SHUTDOWN" == "1" ]]; then
  if pgrep -x afl-fuzz >/dev/null; then
    log "other campaigns still running - not shutting down"
  else
    log "no fuzzers left - shutting down in 2 minutes (cancel: sudo shutdown -c)"
    $SUDO shutdown -h +2
  fi
fi
log "done"
