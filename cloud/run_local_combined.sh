#!/bin/bash
# run_local_combined.sh - start the whole combined stack on THIS machine.
#
#   ./run_local_combined.sh [duration_s]          # default 1800 (30 min)
#
# One command instead of the seven terminals in LOCAL_TEST.md. Starts:
#   3x afl-fuzz   (-M main, -S sec1 -p explore, -S sec2 -p exploit)
#   monitor.py    Min Yee's sampler        -> monitor/raw_metrics.json
#   run_analyzer.py  her Level-1 loop      -> adaptation/.adapt_signal
#   agentafl_escalation.py  Level-2        -> agentafl/escalations.jsonl
#
# This is a FUNCTIONAL TEST, not a campaign. Timers and the LLM budget are
# shortened (see local_test_env.sh) so a trigger can fire in minutes. Nothing it
# produces is comparable to a cloud run - use run_campaign.sh for that.
#
# Everything is logged to $TEST/logs/ and every process gets a pidfile, so
# ./stop_local_combined.sh can shut the whole thing down.
#
# Env overrides (all optional, see local_test_env.sh):
#   TEST=~/fyp-test       where everything lands
#   WINDOW_SEC=10         monitor window; 30 to match production
#   CALIB=3 PERSIST=2     run_analyzer.py --calibration / --persistence
#   AF_CALLS=2 AF_SEEDS=3 LLM budget per escalation; 5/5 in production
#   AF_COOLDOWN=60        min gap between escalations; 1500 in production
#   AGENTAFL_HOOK=0       test Min Yee's layer alone (skips the daemon)
#   NO_LLM=1              pass --calls 0: full loop, zero API calls
#   PYLIB=~/pylibs        psutil location if not installed system-wide
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DUR="${1:-1800}"

# shellcheck source=local_test_env.sh
source "$HERE/local_test_env.sh"

PIDS="$TEST/pids"
LOGS="$TEST/logs"
mkdir -p "$PIDS" "$LOGS"

die() { echo "[-] $*" >&2; exit 1; }
say() { echo "[*] $*"; }
ok()  { echo "[+] $*"; }

# ---------------------------------------------------------------- preflight
# Fail here rather than after a 30-minute run that produced nothing.
[[ -x "$TARGET" ]] || die "target not executable: $TARGET"
[[ -d "$SEEDS" ]]  || die "seed dir missing: $SEEDS"
command -v afl-fuzz    >/dev/null || die "afl-fuzz not on PATH"
command -v afl-showmap >/dev/null || die "afl-showmap not on PATH"
command -v afl-addseeds >/dev/null || die "afl-addseeds not on PATH (ships with AFL++)"

# monitor.py is the only piece needing a third-party module, and without it the
# analyzer sits printing "Waiting for raw_metrics.json..." forever.
if ! PYTHONPATH="${PYLIB:-}" python3 -c "import psutil" 2>/dev/null; then
  die "psutil not importable.
      sudo apt-get install -y python3-psutil
    or, without sudo:
      mkdir -p ~/pylibs && $AGENT/.venv/bin/pip install --target ~/pylibs psutil
      PYLIB=~/pylibs $0 $DUR"
fi

# A stale request would fire a spurious escalation the moment the daemon starts.
rm -f "$MINYEE/adaptation/.agentafl_request" "$MINYEE/adaptation/.agentafl_request.tmp"

if pgrep -x afl-fuzz >/dev/null; then
  die "afl-fuzz is already running. adaptation/.adapt_signal is shared per
      checkout, so two campaigns would fight over it. Stop the other one first
      (./stop_local_combined.sh)."
fi

ok "target   $TARGET"
ok "seeds    $SEEDS ($(ls -1 "$SEEDS" | wc -l) files)"
ok "duration ${DUR}s"
echo

# ------------------------------------------------------------------ fuzzers
# -M main matters: a main node syncs from EVERY sibling dir, which is what makes
# afl-addseeds work. Started without -M/-S, afl-fuzz auto-configures as
# '-S default' - a SECONDARY - and secondaries skip any sync dir lacking an
# is_main_node marker, so injected seeds would be imported by nobody, silently.
launch_fuzzer() {  # name  extra-args...
  local name="$1"; shift
  setsid nohup env \
    AFL_SKIP_CPUFREQ=1 \
    AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES=1 \
    AFL_NO_UI=1 \
    AFL_IMPORT_FIRST=1 \
    AFL_SYNC_TIME="$AFL_SYNC_TIME" \
    AFL_FUZZER_STATS_UPDATE_INTERVAL="$AFL_FUZZER_STATS_UPDATE_INTERVAL" \
    AFL_PYTHON_MODULE=adaptive_mutator \
    PYTHONPATH="$MINYEE" \
    afl-fuzz "$@" -i "$SEEDS" -o "$OUT" -V "$DUR" -m none -t 1000 \
      -- "$TARGET" --noout @@ \
    >> "$LOGS/$name.log" 2>&1 &
  echo $! > "$PIDS/$name.pid"
  say "$name (pid $!)"
}

say "starting fuzzers..."
launch_fuzzer main -M main
sleep 2
launch_fuzzer sec1 -S sec1 -p explore
launch_fuzzer sec2 -S sec2 -p exploit

# monitor.py resolves its fuzzer_stats path ONCE in __init__ and logs nothing for
# the whole run if the file is absent at that instant, so wait for it.
say "waiting for main to write fuzzer_stats..."
for _ in $(seq 1 120); do [[ -f "$OUT/main/fuzzer_stats" ]] && break; sleep 1; done
[[ -f "$OUT/main/fuzzer_stats" ]] || {
  echo "--- main.log ---"; tail -20 "$LOGS/main.log"
  die "main never wrote fuzzer_stats; the stack cannot work. See above."
}
ok "fuzzer_stats present"

# Each instance prints "Python mutator ... installed successfully." a moment after
# it starts, and the secondaries lag main, so poll rather than checking once -
# an immediate check reports a false 1/3.
loaded=0
for _ in $(seq 1 30); do
  loaded=0
  for n in main sec1 sec2; do
    grep -q 'installed successfully' "$LOGS/$n.log" 2>/dev/null && loaded=$((loaded+1))
  done
  [[ "$loaded" -eq 3 ]] && break
  sleep 1
done
if [[ "$loaded" -eq 3 ]]; then
  ok "adaptive_mutator loaded in 3/3 instances"
else
  echo "[!] adaptive_mutator loaded in only $loaded/3 after 30s - Min Yee's layer"
  echo "    cannot act on its own decisions, so this is NOT a valid combined run."
  echo "    Check PYTHONPATH and that afl-fuzz has Python support:"
  echo "      afl-fuzz -hh | grep -i 'module support'"
fi

# ------------------------------------------------------- monitor + analyzer
# Both must run from the SAME directory: monitor.py writes raw_metrics.json into
# its CWD and run_analyzer.py reads that bare filename from its CWD.
say "starting monitor (window ${WINDOW_SEC}s)..."
(
  cd "$WORKDIR"
  setsid nohup env PYTHONPATH="${PYLIB:-}" python3 -u -c "
import sys; sys.path.insert(0, '$MINYEE')
from monitor import AFLRuntimeMonitor
AFLRuntimeMonitor(fuzzer_out_dir='$OUT/main', window_sec=$WINDOW_SEC,
                  output_log='raw_metrics.json',
                  experiment_csv='experiment_metrics.csv',
                  calibration_windows=$CALIB).start_monitoring()
" >> "$LOGS/monitor.log" 2>&1 &
  echo $! > "$PIDS/monitor.pid"
)
say "monitor (pid $(cat "$PIDS/monitor.pid"))"

for _ in $(seq 1 60); do [[ -s "$WORKDIR/raw_metrics.json" ]] && break; sleep 2; done
if [[ -s "$WORKDIR/raw_metrics.json" ]]; then
  ok "monitor producing windows"
else
  echo "--- monitor.log ---"; tail -10 "$LOGS/monitor.log"
  die "monitor wrote no window. The analyzer would wait forever. See above."
fi

# PYTHONIOENCODING/LC_ALL are not optional: run_analyzer.py prints a literal
# U+0394 inside a try whose except only logs, with the adaptation logic after
# those prints, so under a non-UTF-8 locale every window raises and no trigger
# ever fires while everything still looks healthy.
say "starting analyzer (--calibration $CALIB --persistence $PERSIST)..."
(
  cd "$WORKDIR"
  setsid nohup env PYTHONIOENCODING=utf-8 LC_ALL=C.UTF-8 \
    python3 -u "$MINYEE/run_analyzer.py" \
      --calibration "$CALIB" --persistence "$PERSIST" \
    >> "$LOGS/analyzer.log" 2>&1 &
  echo $! > "$PIDS/analyzer.pid"
)
say "analyzer (pid $(cat "$PIDS/analyzer.pid"))"

# ------------------------------------------------------------- the Level-2 arm
if [[ "${AGENTAFL_HOOK:-0}" != "1" ]]; then
  echo
  echo "[!] AGENTAFL_HOOK=$AGENTAFL_HOOK - skipping the escalation daemon."
  echo "    This is Min Yee's layer on its own, as condition=minyee runs it."
else
  CALLS_ARG=("--calls" "$AF_CALLS")
  if [[ -n "${NO_LLM:-}" ]]; then
    CALLS_ARG=("--calls" "0")
    echo
    echo "[!] NO_LLM set - daemon will make ZERO API calls. It still freezes the"
    echo "    baseline, records escalations and writes the timeline."
  fi
  say "starting AgentAFL escalation daemon..."
  setsid nohup env PYTHONIOENCODING=utf-8 LC_ALL=C.UTF-8 \
    python3 -u "$MINYEE/agentafl_escalation.py" \
      --campaign-root "$OUT" \
      --work-dir "$AGENTWORK" \
      --request-file "$REQUEST" \
      --target "$TARGET" \
      --target-args "--noout @@" \
      --instance main \
      --instances main,sec1,sec2 \
      --fmt "$AGENTAFL_FMT" \
      "${CALLS_ARG[@]}" \
      --seeds-per-call "$AF_SEEDS" \
      --cooldown-s "$AF_COOLDOWN" \
    >> "$LOGS/agentafl.log" 2>&1 &
  echo $! > "$PIDS/agentafl.pid"
  say "agentafl (pid $!)"

  # It exits non-zero on a bad config (no API key, gate off, afl-showmap broken),
  # and that must be loud now rather than silence for the whole run.
  sleep 8
  if ! kill -0 "$(cat "$PIDS/agentafl.pid")" 2>/dev/null; then
    echo "--- agentafl.log ---"; tail -15 "$LOGS/agentafl.log"
    die "the escalation daemon exited during startup. See above."
  fi
  grep -q 'self-test OK' "$LOGS/agentafl.log" \
    && ok "daemon up, afl-showmap self-test passed" \
    || echo "[!] daemon running but no self-test line yet - check $LOGS/agentafl.log"
fi

# ----------------------------------------------------------------- summary
cat <<EOF

================================================================
Stack is up. Fuzzers stop themselves after ${DUR}s (-V).

  watch it:   ./cloud/watch_local_combined.sh
  stop it:    ./cloud/stop_local_combined.sh
  logs:       $LOGS/

What to look for, in order of what it proves:
  1. mutator live      grep -c 'installed successfully' $LOGS/main.log      -> 1
  2. windows used      grep -c '^\[Window' $LOGS/analyzer.log               -> rising
     none discarded    grep -c 'Skipping stale' $LOGS/analyzer.log          -> 0
  3. Level-1 adapting  cat $SIGNAL
  4. Level-1 FAILED    grep 'Coverage Evaluation' $LOGS/analyzer.log        -> Improved: False
  5. escalation ran    cat $AGENTWORK/agentafl_timeline.csv
  6. seeds IMPORTED    od -An -tu4 -N4 $OUT/main/.synced/addseeds           -> advances

(6) is the one that matters: seeds written but never imported means nothing you
measure afterwards is attributable to AgentAFL.

'Improved: True' means her layer recovered on its own and AgentAFL correctly did
NOT fire. Early in a run that is common. To test the AgentAFL half directly:

  printf '{"ts": %s, "window_id": 1, "trigger_reason": "COVERAGE_SLOWDOWN", "improved": false, "total_recovery_edges": 0, "ces": 0.0, "cpu": 25.0, "memory": 48.0, "delta_new_edges": 0}\\n' "\$(date +%s)" > "$REQUEST"
================================================================
EOF
