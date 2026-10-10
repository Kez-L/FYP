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
done

echo
packed=("$WORK"/results/*.tar.gz)
echo "Packed results: ${#packed[@]} file(s) in $WORK/results"
echo "Disk free: $(df -h "$WORK" | awk 'NR==2{print $4}')"
if [[ -f /run/systemd/shutdown/scheduled ]]; then
  echo "Shutdown scheduled: $(date -d @"$(awk -F= '/USEC/{print int($2/1000000)}' /run/systemd/shutdown/scheduled)" 2>/dev/null)"
fi
