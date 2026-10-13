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
sleep 15
for p in minyee_analyzer minyee_monitor plateau_watch signal_log; do
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
    if [[ -f "$RUN/adapt_signal_log.csv" ]]; then
      echo "signal_writes=$(grep -c ',write$' "$RUN/adapt_signal_log.csv" 2>/dev/null || echo 0)"
      echo "modes_seen=$(awk -F, 'NR>1{print $3}' "$RUN/adapt_signal_log.csv" 2>/dev/null | sort -u | paste -sd' ' -)"
      echo "final_mode=$(awk -F, 'END{print $3}' "$RUN/adapt_signal_log.csv" 2>/dev/null)"
    fi
    if [[ -f "$RUN/logs/minyee_analyzer.log" ]]; then
      echo "analyzer_errors=$(grep -c '\[-\] Analyzer error' "$RUN/logs/minyee_analyzer.log" 2>/dev/null || echo 0)"
      echo "analyzer_triggers=$(grep -c 'Trigger = ' "$RUN/logs/minyee_analyzer.log" 2>/dev/null || echo 0)"
      echo "analyzer_calibrated=$(grep -c 'Calibration complete' "$RUN/logs/minyee_analyzer.log" 2>/dev/null || echo 0)"
      # Non-zero means the U+0394 encoding trap was hit and NO trigger ever ran.
      echo "analyzer_unicode_errors=$(grep -c 'UnicodeEncodeError' "$RUN/logs/minyee_analyzer.log" 2>/dev/null || echo 0)"
    fi
    for f in "$RUN"/logs/main.log "$RUN"/logs/sec*.log; do
      [[ -f "$f" ]] || continue
      echo "$(basename "$f" .log): mutator_loaded=$(grep -c 'installed successfully' "$f" 2>/dev/null || echo 0) mode_changes=$(grep -c 'Adaptive Mutator. Mode changed' "$f" 2>/dev/null || echo 0)"
    done
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
