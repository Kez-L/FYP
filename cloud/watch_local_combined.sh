#!/bin/bash
# watch_local_combined.sh - live view of a local combined run.
#
#   ./watch_local_combined.sh           # refresh every 5s
#   ./watch_local_combined.sh 2         # every 2s
#   ONESHOT=1 ./watch_local_combined.sh # print once and exit (for logs/CI)
#
# Reads only files, so it works whether or not anything was redirected to a log.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=local_test_env.sh
source "$HERE/local_test_env.sh" >/dev/null

INTERVAL="${1:-5}"

render() {
  local n
  echo "=== processes ==="
  # `| wc -l` throughout rather than `pgrep -c`: pgrep -c exits 1 on zero, and
  # under `set -o pipefail` plus a `|| echo 0` fallback that prints a stray 0.
  printf "  afl-fuzz=%s monitor=%s analyzer=%s agentafl=%s\n" \
    "$(pgrep -x afl-fuzz 2>/dev/null | wc -l)" \
    "$(pgrep -f 'AFLRuntimeMonitor' 2>/dev/null | wc -l)" \
    "$(pgrep -f 'run_analyzer\.py' 2>/dev/null | wc -l)" \
    "$(pgrep -f 'agentafl_escalation\.py' 2>/dev/null | wc -l)"

  echo "=== coverage (window,edges,new_edges) ==="
  tail -4 "$WORKDIR/experiment_metrics.csv" 2>/dev/null | cut -d, -f1,4,5 | sed 's/^/  /' \
    || echo "  (no windows yet)"

  echo "=== analyzer ==="
  if [[ -f "$TEST/logs/analyzer.log" ]]; then
    printf "  windows evaluated=%s  discarded-as-stale=%s\n" \
      "$(grep -c '^\[Window' "$TEST/logs/analyzer.log" 2>/dev/null || true)" \
      "$(grep -c 'Skipping stale' "$TEST/logs/analyzer.log" 2>/dev/null || true)"
    # Improved: False is the AgentAFL trigger; Improved: True means her layer
    # recovered on its own and the hook correctly stayed quiet.
    grep 'Coverage Evaluation' "$TEST/logs/analyzer.log" 2>/dev/null \
      | tail -3 | sed 's/^/  /' || echo "  (no coverage verdict yet)"
  else
    echo "  (running in its own terminal - read verdicts there)"
  fi

  echo "=== Level-1 mode ==="
  echo "  $(cat "$SIGNAL" 2>/dev/null || echo '(no .adapt_signal yet)')"

  echo "=== Level-2 escalations ==="
  if [[ -s "$AGENTWORK/agentafl_timeline.csv" ]]; then
    column -s, -t < "$AGENTWORK/agentafl_timeline.csv" 2>/dev/null | cut -c1-110 | sed 's/^/  /'
  else
    echo "  (none yet)"
  fi

  echo "=== seeds injected -> imported ==="
  printf "  addseeds/queue=%s" "$(ls "$OUT/addseeds/queue" 2>/dev/null | wc -l)"
  n="$(od -An -tu4 -N4 "$OUT/main/.synced/addseeds" 2>/dev/null | tr -d ' ')"
  printf "  main has imported=%s\n" "${n:-0}"
  # This is the check that proves the loop closed. Seeds written but never
  # imported means nothing measured afterwards is attributable to AgentAFL.

  echo "=== fuzzers ==="
  for i in main sec1 sec2; do
    [[ -f "$OUT/$i/fuzzer_stats" ]] || continue
    printf "  %-5s edges=%-6s corpus=%-6s crashes=%s\n" "$i" \
      "$(awk -F': *' '/^edges_found/{print $2}' "$OUT/$i/fuzzer_stats")" \
      "$(awk -F': *' '/^corpus_count/{print $2}' "$OUT/$i/fuzzer_stats")" \
      "$(awk -F': *' '/^saved_crashes/{print $2}' "$OUT/$i/fuzzer_stats")"
  done
}

if [[ -n "${ONESHOT:-}" ]]; then
  render
else
  export -f render 2>/dev/null || true
  while true; do
    clear
    echo "combined run @ $(date +%H:%M:%S)   (ctrl-c to stop watching)"
    echo
    render
    sleep "$INTERVAL"
  done
fi
