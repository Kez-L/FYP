#!/bin/bash
# Kill the wedged libyaml campaign and relaunch it with deadlock-proof settings.
set -u
cd /home/user/Documents/1git-folder/AgentAFL

OUT=afl-output-libyaml-20260908
SEEDS=AFLPlus/seeds-libyaml
DICT=AFLPlus/libyaml-build/yaml.dict
BIN=AFLPlus/libyaml-build/libyaml_parser_fuzzer-afl
CL=AFLPlus/libyaml-build/libyaml_parser_fuzzer-cmplog
LOGS=afl-logs-libyaml-20260908
mkdir -p "$LOGS"

echo "[*] killing existing libyaml fuzzers..."
pkill -f "afl-fuzz.*$OUT" 2>/dev/null
sleep 3
pkill -9 -f "afl-fuzz.*$OUT" 2>/dev/null
sleep 1

# --- key changes vs the runs that froze ---
#   -t 1000                : hard 1s per-exec timeout -> a wedged handshake is
#                            killed, logged as a hang, and fuzzing continues
#   AFL_FUZZER_LOOPCOUNT=1  : no persistent loop; every exec is a fresh fork off
#                            the forkserver (same robust mode your xml run uses)
#   AFL_FAST_CAL dropped    : it shortened calibration and skewed the timeout
export AFL_SKIP_CPUFREQ=1
export AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES=1
export AFL_AUTORESUME=1
export AFL_IMPORT_FIRST=1
export AFL_FUZZER_LOOPCOUNT=1

launch() { # name  extra-args...
  local name=$1; shift
  nohup setsid afl-fuzz -i "$SEEDS" -o "$OUT" -x "$DICT" -V 43200 -m none -t 1000 \
        "$@" -- "$BIN" > "$LOGS/$name.log" 2>&1 &
  echo "  launched $name (pid $!)  log: $LOGS/$name.log"
}

echo "[*] launching 6 instances (fork mode, -t 1000)..."
AFL_CYCLE_SCHEDULES=1 launch main -M main -c "$CL"
sleep 3
launch sec1 -S sec1 -p explore
launch sec2 -S sec2 -p exploit
launch sec3 -S sec3 -p fast
launch sec4 -S sec4 -p coe
launch sec5 -S sec5 -p rare

sleep 25
echo
echo "[*] status after 25s:"
afl-whatsup -s "$OUT" 2>&1 | sed -n '1,18p'
echo
echo "watch live with:  watch -n5 'afl-whatsup -s $OUT'"
