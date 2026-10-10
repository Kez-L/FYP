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
sleep 15
for p in minyee_monitor plateau_watch; do
  f="$RUN/pids/$p.pid"
  [[ -f "$f" ]] && kill "$(cat "$f")" 2>/dev/null && log "stopped $p"
done

# 2. summary + end time
afl-whatsup -s "$OUT" > "$RUN/summary.txt" 2>&1 || true
for d in "$OUT"/*/; do
  [[ -f "$d/fuzzer_stats" ]] || continue
  echo "== $(basename "$d")"
  grep -E '^(run_time|execs_done|execs_per_sec|corpus_count|edges_found|total_edges|bitmap_cvg|saved_crashes|saved_hangs|stability) ' "$d/fuzzer_stats"
done >> "$RUN/summary.txt"
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
