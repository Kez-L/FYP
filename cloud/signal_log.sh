#!/bin/bash
# signal_log.sh - record the adaptation-signal timeline of a minyee campaign.
#
#   ./signal_log.sh <signal_file> <csv> [interval_s]
#
# Started automatically by run_campaign.sh for the "minyee" condition; you don't
# normally run this yourself.
#
# Min Yee's controller.py writes ResourceAwareAFL/adaptation/.adapt_signal with
# no history - each write clobbers the last. Her analyzer's stdout has window_ids
# but no wall clock. This gives the campaign a wall-clock record of what the
# MUTATOR actually saw, joinable against plateau_log.csv and experiment_metrics.csv.
#
# Deliberately NOT set -e: this must survive a transient read failure and keep
# logging for the full 12h rather than dying silently.
set -uo pipefail

SIG="${1:?usage: signal_log.sh <signal_file> <csv> [interval_s]}"
CSV="${2:?usage: signal_log.sh <signal_file> <csv> [interval_s]}"
INT="${3:-5}"

# Heartbeat roughly every 5 min so a mode that never changes is still visible as
# "pinned since T" rather than as a single stale row.
HB=$(( 300 / INT )); (( HB < 1 )) && HB=1

# controller._write_signal() opens with "w" (O_TRUNC) then writes ~45 bytes, so a
# reader can catch the file empty or half-written. Match-or-skip: never log a
# transition we aren't sure of. adaptive_mutator.py tolerates the same torn read
# by falling back to DEFAULT, so this is noise, not a fault.
re='^MODE=([A-Z_]+);TIMESTAMP=([0-9.]+)'

[[ -s "$CSV" ]] || echo "unix_time,utc,mode,signal_unix,event" > "$CSV"

last=""; n=0
while true; do
  raw=""
  [[ -f "$SIG" ]] && raw="$(cat "$SIG" 2>/dev/null || true)"
  if [[ "$raw" =~ $re ]]; then
    mode="${BASH_REMATCH[1]}"; sigts="${BASH_REMATCH[2]}"
    # Key on mode+timestamp, not mode alone: controller.evaluate_adaptation()
    # re-writes the SAME mode when it retains a successful strategy, and that
    # re-write is a real control event we want in the timeline.
    key="$mode|$sigts"
    ev=""
    if [[ "$key" != "$last" ]]; then
      ev="write"; last="$key"
    elif (( n % HB == 0 )); then
      ev="heartbeat"
    fi
    if [[ -n "$ev" ]]; then
      printf '%s,%s,%s,%s,%s\n' \
        "$(date +%s)" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$mode" "$sigts" "$ev" >> "$CSV"
    fi
  fi
  n=$((n+1))
  sleep "$INT"
done
