#!/bin/bash
# verify_run.sh - "is this 12h run actually working?" Run it ~15 min after launch.
#
#   ./verify_run.sh                  # every run on this machine
#   ./verify_run.sh ~/fyp/runs/libxml2_combined_run1
#   SAMPLE_SECS=0 ./verify_run.sh    # skip the coverage-progress sample (instant)
#
# Exits 0 only if every check passed. Prints one GOOD / STOP line per run and a
# machine-wide verdict, so this is the one thing to look at before closing the
# laptop on a 12h campaign.
#
# This is deliberately NOT status.sh. status.sh prints what is happening;
# verify_run.sh decides whether what is happening is correct, including the
# NEGATIVE cases that status.sh cannot catch by printing. The three conditions
# fail in different ways, and every one of those failures is silent - the
# campaign fuzzes happily and you only find out at analysis time:
#
#   baseline  a stray AFL_PYTHON_MODULE in the operator's shell loads the
#             adaptive mutator into a run labelled "baseline"
#   minyee    the mutator fails to load, or the analyzer calibrates forever and
#             evaluates zero windows, so the arm equals baseline
#   combined  the escalation daemon dies, or the showmap arg list has no @@ so
#             every generated seed scores an identical map, so the arm equals
#             minyee
#
# It reads condition= and target= from meta.txt rather than taking them as
# arguments, so it cannot be pointed at a run with the wrong expectations.
set -uo pipefail

WORK="${WORK:-$HOME/fyp}"
HERE="$(cd "$(dirname "$0")" && pwd)"
SAMPLE_SECS="${SAMPLE_SECS:-45}"
TARGETS_SH_DIR="$HERE"
# shellcheck source=targets.sh
source "$HERE/targets.sh"

now() { date +%s; }
meta() { awk -F= -v k="$1" '$1==k{sub(/^[^=]*=/,""); print; exit}' "$RUN/meta.txt" 2>/dev/null; }
stat_of() { awk -F' *: *' -v k="$2" '$1==k{print $2; exit}' "$1" 2>/dev/null; }

RUN_PASS=0; RUN_FAIL=0
pass() { printf '    [+] %s\n' "$1"; RUN_PASS=$((RUN_PASS+1)); }
fail() { printf '    [-] %s\n' "$1"; RUN_FAIL=$((RUN_FAIL+1)); }
info() { printf '    [i] %s\n' "$1"; }

# ----------------------------------------------------------------------------
verify_one() {
  RUN="${1%/}"
  local NAME OUT COND TGT NUM
  NAME="$(basename "$RUN")"; OUT="$RUN/afl-out"
  RUN_PASS=0; RUN_FAIL=0

  echo "=== $NAME"
  if [[ ! -f "$RUN/meta.txt" ]]; then
    fail "no meta.txt - not a campaign dir"; return 1
  fi
  COND="$(meta condition)"; TGT="$(meta target)"; NUM="$(meta num_instances)"
  local start dur el
  start="$(meta start_unix)"; dur="$(meta duration_s)"
  el=$(( $(now) - ${start:-$(now)} ))
  printf '    target=%s condition=%s instances=%s elapsed=%dh%02dm\n' \
    "$TGT" "$COND" "$NUM" $((el/3600)) $((el%3600/60))

  # unset SEEDS: targets.sh honours a caller-supplied override, and with several
  # runs on one machine a value left from the previous iteration would silently
  # check this run against the previous target's corpus.
  unset SEEDS
  if ! load_target "$TGT" 2>/dev/null; then
    fail "meta.txt names target '$TGT', which targets.sh does not know"
    return 1
  fi

  # A finished run has no live processes and no fresh stats by definition, so the
  # liveness checks below would report a "deadlock" on a perfectly good 12h
  # campaign. Skip them and check only what is still meaningful after the fact.
  local LIVE=1
  if grep -q '^end_unix=' "$RUN/meta.txt" 2>/dev/null; then
    LIVE=0
    info "run already FINISHED - skipping liveness checks (this script is for a live run)"
  fi

  # ------------------------------------------------------- fuzzers are alive
  if [[ "$LIVE" -eq 1 ]]; then
    local alive
    alive="$(pgrep -fc -- "-o $OUT " 2>/dev/null || true)"
    if [[ "${alive:-0}" -eq "${NUM:-3}" ]]; then
      pass "$alive/$NUM afl-fuzz instances alive"
    else
      fail "only ${alive:-0}/$NUM afl-fuzz instances alive"
      for f in "$RUN"/logs/main.log "$RUN"/logs/sec*.log; do
        [[ -f "$f" ]] || continue
        sed 's/\x1b\[[0-9;]*m//g' "$f" | grep -iE 'PROGRAM ABORT|FATAL' | head -1 \
          | sed "s|^|        $(basename "$f" .log): |"
      done
    fi
  fi

  # ----------------------------------- stats are being written (deadlock test)
  # This is the AFL_OLD_CHILD_SYNC detector. The futex persistent-mode child
  # sync deadlocks under CPU oversubscription on the libical and tidy harnesses:
  # fuzzer_stats stops updating while afl-whatsup still reports every instance
  # "alive". Without this check a run can sit dead for 11 hours looking healthy.
  local inst_n=0
  for d in "$OUT"/*/; do
    [[ -f "$d/fuzzer_stats" ]] && inst_n=$((inst_n+1))
  done
  if [[ "$inst_n" -eq 0 ]]; then
    fail "no fuzzer_stats at all - nothing has started calibrating"
  elif [[ "$LIVE" -eq 1 ]]; then
    local stale=0
    for d in "$OUT"/*/; do
      local s="$d/fuzzer_stats"; [[ -f "$s" ]] || continue
      local lu age
      lu="$(stat_of "$s" last_update)"; age=$(( $(now) - ${lu:-0} ))
      (( age > 120 )) && { stale=$((stale+1)); info "$(basename "$d") stats ${age}s old"; }
    done
    if [[ "$stale" -eq 0 ]]; then
      pass "all $inst_n instances updating fuzzer_stats (< 120s)"
    else
      fail "$stale/$inst_n instances have stale fuzzer_stats - likely the persistent-mode child-sync deadlock (AFL_OLD_CHILD_SYNC)"
    fi
  else
    pass "$inst_n instances wrote fuzzer_stats"
  fi

  # ------------------------------------------------- throughput and coverage
  local floor_bad=0 e0=0 x
  for d in "$OUT"/*/; do
    local s="$d/fuzzer_stats"; [[ -f "$s" ]] || continue
    x="$(stat_of "$s" execs_per_sec)"; x="${x%%.*}"
    e0=$(( e0 + $(stat_of "$s" edges_found 2>/dev/null || echo 0) ))
    if [[ "${x:-0}" -lt "$EXECS_FLOOR" ]]; then
      floor_bad=1; info "$(basename "$d") execs/s=${x:-0} (floor $EXECS_FLOOR)"
    fi
  done
  if [[ "$floor_bad" -eq 0 ]]; then
    pass "throughput above the $TGT floor ($EXECS_FLOOR execs/s) on every instance"
  else
    fail "an instance is below the $EXECS_FLOOR execs/s floor - check for an oversized seed pinning the queue"
  fi

  if [[ "$SAMPLE_SECS" -gt 0 && "$LIVE" -eq 1 ]]; then
    sleep "$SAMPLE_SECS"
    local e1=0
    for d in "$OUT"/*/; do
      [[ -f "$d/fuzzer_stats" ]] || continue
      e1=$(( e1 + $(stat_of "$d/fuzzer_stats" edges_found 2>/dev/null || echo 0) ))
    done
    if [[ "$e1" -gt "$e0" ]]; then
      pass "coverage rising ($e0 -> $e1 edges over ${SAMPLE_SECS}s)"
    else
      info "coverage flat ($e0 -> $e1 over ${SAMPLE_SECS}s) - normal in a plateau, suspicious this early"
    fi
  else
    info "coverage total $e0 edges (progress sample skipped)"
  fi

  # ---------------------------------------------------------- frozen settings
  local env="$RUN/afl_env.txt"
  for kv in AFL_SYNC_TIME=1 AFL_FUZZER_STATS_UPDATE_INTERVAL=10; do
    grep -qx "$kv" "$env" 2>/dev/null && pass "$kv" || fail "$kv missing from afl_env.txt - this arm is not comparable to the others"
  done
  for kv in ${TARGET_ENV[@]+"${TARGET_ENV[@]}"}; do
    case "$kv" in
      AFL_*|ASAN_*)
        grep -qx "$kv" "$env" 2>/dev/null && pass "$kv ($TGT)" \
          || fail "$kv missing from afl_env.txt - required for $TGT" ;;
    esac
  done

  # seed corpus fingerprint
  local sc sh
  sc="$(meta seeds_count)"; sh="$(meta seeds_sha256_16)"
  if [[ "$sc" == "$SEEDS_COUNT" && "$sh" == "$SEEDS_SHA256_16" ]]; then
    pass "seed corpus matches the frozen fingerprint ($sc files, $sh)"
  else
    fail "seed corpus is $sc/$sh, frozen is $SEEDS_COUNT/$SEEDS_SHA256_16"
  fi

  # ------------------------------------------------------- condition-specific
  local hook; hook="$(meta agentafl_hook)"
  case "$COND" in
    baseline) verify_baseline ;;
    minyee)   verify_minyee ;;
    combined) verify_minyee; verify_combined ;;
    *)        fail "meta.txt condition='$COND' is not baseline|minyee|combined" ;;
  esac

  # ----------------------------------------------------------------- verdict
  echo
  if [[ "$RUN_FAIL" -eq 0 ]]; then
    echo "    GOOD - $NAME looks like a valid $COND run ($RUN_PASS checks passed)"
    return 0
  fi
  echo "    STOP - $NAME has $RUN_FAIL problem(s); do not let this burn 12h"
  return 1
}

# ----------------------------------------------------------------------------
verify_baseline() {
  # The whole point of a baseline is that none of our machinery is attached.
  # "No minyee section printed" is not evidence of that, so assert it.
  local env="$RUN/afl_env.txt"
  local dirty=0
  for v in AFL_PYTHON_MODULE AFL_CUSTOM_MUTATOR_LIBRARY AFL_CUSTOM_MUTATOR_ONLY; do
    grep -q "^$v=" "$env" 2>/dev/null && { fail "$v is set - this is NOT a clean baseline"; dirty=1; }
  done
  [[ "$dirty" -eq 0 ]] && pass "no mutator env vars (clean baseline)"

  local loaded=0
  for f in "$RUN"/logs/main.log "$RUN"/logs/sec*.log; do
    [[ -f "$f" ]] || continue
    grep -q 'installed successfully' "$f" && loaded=$((loaded+1))
  done
  [[ "$loaded" -eq 0 ]] && pass "no Python mutator loaded in any instance" \
    || fail "a Python mutator loaded in $loaded instance(s) of a baseline run"

  [[ "$(meta agentafl_hook)" == "0" ]] && pass "agentafl_hook=0" \
    || fail "agentafl_hook=$(meta agentafl_hook), expected 0"

  for p in minyee_analyzer agentafl_escalation; do
    [[ -f "$RUN/pids/$p.pid" ]] && fail "$p was started in a baseline run" \
      || pass "$p not started"
  done
}

verify_minyee() {
  local n; n="$(meta num_instances)"
  # Without the mutator the analyzer logs decisions nothing acts on, and the arm
  # is statistically identical to baseline.
  local loaded=0
  for f in "$RUN"/logs/main.log "$RUN"/logs/sec*.log; do
    [[ -f "$f" ]] || continue
    grep -q 'installed successfully' "$f" && loaded=$((loaded+1))
  done
  if [[ "$loaded" -eq "${n:-3}" ]]; then
    pass "adaptive_mutator loaded in all $loaded instances"
  else
    fail "adaptive_mutator loaded in only $loaded/${n:-3} instances - NOT a valid minyee arm"
  fi

  if [[ "$LIVE" -eq 1 ]]; then
    local apid; apid="$(cat "$RUN/pids/minyee_analyzer.pid" 2>/dev/null || true)"
    if [[ -n "$apid" ]] && kill -0 "$apid" 2>/dev/null; then
      pass "run_analyzer.py alive (pid $apid)"
    else
      fail "run_analyzer.py is not running - adaptation has stopped"
    fi
  fi

  local mon="$RUN/monitor/raw_metrics.json"
  if [[ -s "$mon" ]]; then
    pass "monitor.py producing windows ($(wc -l < "$mon") so far)"
  else
    fail "monitor.py has written no window - it locked onto the wrong fuzzer_stats path"
  fi

  local A="$RUN/logs/minyee_analyzer.log"
  if [[ -f "$A" ]]; then
    local cal ev uni
    cal="$(grep -c '\[Calibration\]' "$A" 2>/dev/null || true)"
    ev="$(grep -c '\[Window ' "$A" 2>/dev/null || true)"
    uni="$(grep -c 'UnicodeEncodeError' "$A" 2>/dev/null || true)"
    # eval_windows > 0 is the only real proof calibration finished and the
    # trigger logic is running; there is no "calibration complete" message.
    #
    # But it CANNOT be true early: run_analyzer.py calibrates for --calibration
    # windows (8) at monitor.py's 30s window, so nothing is evaluated for the
    # first ~4 minutes. Failing on that would make an early verify_run.sh tell
    # you to kill a perfectly healthy campaign. CALIB_GRACE is 7 min, which is
    # 240s of calibration plus slack for startup and discarded windows.
    local calib_grace="${CALIB_GRACE:-420}"
    if [[ "${ev:-0}" -gt 0 ]]; then
      pass "analyzer evaluating windows (calib=$cal eval=$ev)"
    elif [[ "$el" -lt "$calib_grace" ]]; then
      info "analyzer still calibrating (calib=$cal eval=0) - expected for the first ~4 min; re-check at T+15min"
    else
      fail "analyzer has evaluated 0 windows after $((el/60))m (calib=$cal) - triggers never run"
    fi
    [[ "${uni:-0}" -eq 0 ]] && pass "no UnicodeEncodeError in the analyzer" \
      || fail "$uni UnicodeEncodeError(s) - the U+0394 trap; no trigger will ever fire"
  else
    fail "no analyzer log at all"
  fi
}

verify_combined() {
  [[ "$(meta agentafl_hook)" == "1" ]] && pass "agentafl_hook=1" \
    || fail "agentafl_hook=$(meta agentafl_hook), expected 1 - this run can never escalate"

  if [[ "$LIVE" -eq 1 ]]; then
    local epid; epid="$(cat "$RUN/pids/agentafl_escalation.pid" 2>/dev/null || true)"
    if [[ -n "$epid" ]] && kill -0 "$epid" 2>/dev/null; then
      pass "agentafl_escalation.py alive (pid $epid)"
    else
      fail "agentafl_escalation.py is not running - no escalation will ever happen"
    fi
  fi

  local L="$RUN/logs/agentafl_escalation.log"
  if [[ -f "$L" ]]; then
    local nf; nf="$(grep -c 'FATAL' "$L" 2>/dev/null || true)"
    if [[ "${nf:-0}" -eq 0 ]]; then
      pass "no FATAL in the escalation log"
    else
      fail "escalation log has $nf FATAL line(s): $(grep -m1 'FATAL' "$L")"
    fi

    # The @@ detector. The daemon's startup self-test runs afl-showmap through
    # exactly the path AgentAFL uses to score candidates. A tiny edge count that
    # is identical for every input is what a missing @@ looks like, and it is
    # otherwise invisible: build_baseline ignores showmap's exit code, so every
    # candidate scores 0 new edges and the arm quietly equals minyee.
    local se
    se="$(grep -o 'self-test OK: [0-9]* edges' "$L" 2>/dev/null | tail -1 | tr -dc 0-9)"
    if [[ -n "$se" ]]; then
      if [[ "$se" -gt 50 ]]; then
        pass "showmap self-test saw $se edges (per-seed coverage is real)"
      else
        fail "showmap self-test saw only $se edges - SHOWMAP_ARGS probably lacks '@@', so every candidate will score an identical map"
      fi
    else
      info "showmap self-test line not in the log yet"
    fi
  else
    fail "no escalation daemon log"
  fi

  # Escalations legitimately take ~25-30 min to appear: the hook only fires
  # after a Level-1 coverage adaptation has FAILED to recover. Zero at 15 min is
  # expected, so report it rather than failing on it.
  local J="$RUN/agentafl/escalations.jsonl"
  if [[ -f "$J" ]]; then
    local ne nd
    ne="$(grep -c '"event": "escalation"' "$J" 2>/dev/null || true)"
    nd="$(grep -c '"event": "dropped"' "$J" 2>/dev/null || true)"
    info "escalations=${ne:-0} dropped=${nd:-0} (0 is normal before ~30 min)"
    if [[ "${ne:-0}" -gt 0 ]]; then
      local syn
      syn="$(python3 - "$RUN/afl-out" <<'PY' 2>/dev/null || echo 0
import pathlib, sys
f = pathlib.Path(sys.argv[1]) / "main" / ".synced" / "addseeds"
try:
    b = f.read_bytes()[:4]
    print(int.from_bytes(b, "little") if len(b) == 4 else 0)
except OSError:
    print(0)
PY
)"
      [[ "${syn:-0}" -gt 0 ]] && pass "main has imported $syn injected seed(s)" \
        || fail "escalations happened but main imported 0 seeds"
    fi
  else
    info "no escalations.jsonl yet (normal in the first ~30 min)"
  fi

  # A key that does not resolve means every escalation dies at the LLM call.
  if python3 - "$REPO_FOR_KEY" "${AGENTAFL_PROVIDER:-claude}" <<'PY' >/dev/null 2>&1
import sys, os, pathlib
agent, provider = sys.argv[1], sys.argv[2]
name = {"claude": "CLAUDE_API_KEY", "openai": "OPENAI_API_KEY"}.get(provider)
if name and os.environ.get(name):
    sys.exit(0)
for f in (pathlib.Path(agent) / "AgentAFL" / ".env",
          pathlib.Path.home() / "Documents" / ".env"):
    try:
        for line in f.read_text().splitlines():
            k, _, v = line.partition("=")
            if k.strip() == name and v.strip():
                sys.exit(0)
    except OSError:
        pass
sys.exit(1)
PY
  then pass "API key resolves"
  else fail "no API key - every escalation will fail at the LLM call"
  fi
}

# ----------------------------------------------------------------------------
REPO_FOR_KEY="${REPO:-$WORK/FYP}"
shopt -s nullglob
if [[ $# -gt 0 ]]; then
  RUNS=("$@")
else
  RUNS=("$WORK"/runs/*/)
fi
[[ ${#RUNS[@]} -gt 0 ]] || { echo "No runs found in $WORK/runs"; exit 1; }

BAD=0
for r in "${RUNS[@]}"; do
  verify_one "$r" || BAD=$((BAD+1))
  echo
done

echo "================================================================"
if [[ "$BAD" -eq 0 ]]; then
  echo "GOOD - all ${#RUNS[@]} run(s) on this machine look valid."
  echo "Re-check with ./status.sh later, or leave it; finish_campaign.sh packs"
  echo "and powers off on its own."
  exit 0
fi
echo "STOP - $BAD of ${#RUNS[@]} run(s) failed verification."
echo "Fix and relaunch now rather than discovering it after 12h. To stop a run:"
echo "  pkill -f 'afl-fuzz'  &&  sudo shutdown -c"
exit 1
