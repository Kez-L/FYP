#!/bin/bash
# status.sh - quick health check of every campaign on this machine.
#
#   ./status.sh            # all runs
#   watch -n 60 ./status.sh
set -uo pipefail
WORK="${WORK:-$HOME/fyp}"
now=$(date +%s)

shopt -s nullglob
runs=("$WORK"/runs/*/)
[[ ${#runs[@]} -gt 0 ]] || { echo "No runs in $WORK/runs yet."; exit 0; }

for RUN in "${runs[@]}"; do
  RUN="${RUN%/}"; OUT="$RUN/afl-out"; NAME="$(basename "$RUN")"
  start=$(awk -F= '/^start_unix=/{print $2}' "$RUN/meta.txt" 2>/dev/null)
  dur=$(awk -F= '/^duration_s=/{print $2}' "$RUN/meta.txt" 2>/dev/null)
  alive=$(pgrep -fc -- "-o $OUT " || true)
  if grep -q '^end_unix=' "$RUN/meta.txt" 2>/dev/null; then state="FINISHED"
  elif [[ "$alive" -gt 0 ]]; then state="RUNNING ($alive fuzzers)"
  else state="STOPPED (no fuzzers, not packed yet)"; fi

  echo "=== $NAME  [$state]"
  if [[ -n "$start" && -n "$dur" ]]; then
    el=$(( now - start )); rem=$(( dur - el )); (( rem < 0 )) && rem=0
    printf "    elapsed %dh%02dm, remaining %dh%02dm\n" $((el/3600)) $((el%3600/60)) $((rem/3600)) $((rem%3600/60))
  fi
  for d in "$OUT"/*/; do
    s="$d/fuzzer_stats"; [[ -f "$s" ]] || continue
    get() { awk -F' *: *' -v k="$1" '$1==k{print $2}' "$s"; }
    lu=$(get last_update); age=$(( now - ${lu:-$now} ))
    flag=""; [[ "$state" == RUNNING* && $age -gt 300 ]] && flag="  <-- stats not updating!"
    printf "    %-6s edges=%-6s execs/s=%-8s corpus=%-6s crashes=%-4s updated %ss ago%s\n" \
      "$(basename "$d")" "$(get edges_found)" "$(get execs_per_sec)" \
      "$(get corpus_count)" "$(get saved_crashes)" "$age" "$flag"
  done
  if [[ -f "$RUN/plateau_log.csv" ]]; then n=$(wc -l < "$RUN/plateau_log.csv"); echo "    plateau_log.csv: $(( n > 0 ? n - 1 : 0 )) rows"; fi
  [[ -f "$RUN/monitor/raw_metrics.json" ]] && echo "    raw_metrics.json: $(wc -l < "$RUN/monitor/raw_metrics.json") windows"

  # minyee condition: is the adaptation loop actually alive and moving?
  alog="$RUN/logs/minyee_analyzer.log"
  scsv="$RUN/adapt_signal_log.csv"
  if [[ -f "$alog" || -f "$scsv" ]]; then
    apid="$(cat "$RUN/pids/minyee_analyzer.pid" 2>/dev/null || true)"
    if [[ -n "$apid" ]] && kill -0 "$apid" 2>/dev/null; then astate="alive (pid $apid)"
    elif [[ -n "$apid" ]]; then astate="DEAD  <-- adaptation has stopped"
    else astate="not started (pinned-mode smoke test?)"; fi
    echo "    analyzer: $astate"
    if [[ -f "$alog" ]]; then
      aerr=$(grep -c '\[-\] Analyzer error' "$alog" 2>/dev/null || true)
      atrg=$(grep -c 'Trigger = ' "$alog" 2>/dev/null || true)
      # run_analyzer.py never says "calibration complete". It prints
      # "[Calibration] n/N windows" while calibrating, then "[Window NNN] ..."
      # for each window it evaluates, so eval>0 is what proves the trigger
      # logic is actually running.
      acal=$(grep -c '\[Calibration\]' "$alog" 2>/dev/null || true)
      aev=$(grep -c '\[Window ' "$alog" 2>/dev/null || true)
      # A climbing error count with a frozen mode is the run_analyzer.py:420
      # stall: controller.last_adaptation is None, the TypeError is swallowed,
      # awaiting_recovery stays True and the signal pins for the rest of the run.
      flag=""; [[ "${aerr:-0}" -gt 0 ]] && flag="  <-- check for the analyzer stall"
      # Past calibration but evaluating nothing would mean windows are arriving
      # and being dropped - the encoding trap, or stale AFL++ stats.
      [[ "${acal:-0}" -gt 0 && "${aev:-0}" -eq 0 ]] && flag="$flag  <-- calibrating only, 0 windows evaluated yet"
      echo "    analyzer: calib_windows=${acal:-0} eval_windows=${aev:-0} triggers=${atrg:-0} errors=${aerr:-0}$flag"
    fi
    if [[ -f "$scsv" ]]; then
      writes=$(grep -c ',write$' "$scsv" 2>/dev/null || true)
      mode=$(awk -F, 'END{print $3}' "$scsv" 2>/dev/null)
      lastw=$(awk -F, '$5=="write"{t=$1} END{print t}' "$scsv" 2>/dev/null)
      dwell=""
      if [[ -n "$lastw" ]]; then
        d=$(( now - lastw ))
        dwell=" for $((d/60))m"
        # >30 min in a non-DEFAULT mode is the stall signature.
        [[ "$mode" != "DEFAULT" && $d -gt 1800 ]] && dwell="$dwell  <-- pinned >30m, likely stalled"
      fi
      echo "    adapt: mode=${mode:-?}$dwell, $((${writes:-0})) signal write(s)"
    fi
    mload=0; mchg=0
    for f in "$RUN"/logs/main.log "$RUN"/logs/sec*.log; do
      [[ -f "$f" ]] || continue
      c=$(grep -c 'installed successfully' "$f" 2>/dev/null || true); mload=$(( mload + ${c:-0} ))
      c=$(grep -c 'Adaptive Mutator. Mode changed' "$f" 2>/dev/null || true); mchg=$(( mchg + ${c:-0} ))
    done
    flag=""; [[ "$mload" -eq 0 ]] && flag="  <-- MUTATOR NEVER LOADED, this is not a valid minyee run"
    echo "    mutator: loaded in $mload instance(s), $mchg mode change(s) seen$flag"
  fi
done

echo
packed=("$WORK"/results/*.tar.gz)
echo "Packed results: ${#packed[@]} file(s) in $WORK/results"
echo "Disk free: $(df -h "$WORK" | awk 'NR==2{print $4}')"
if [[ -f /run/systemd/shutdown/scheduled ]]; then
  echo "Shutdown scheduled: $(date -d @"$(awk -F= '/USEC/{print int($2/1000000)}' /run/systemd/shutdown/scheduled)" 2>/dev/null)"
fi
