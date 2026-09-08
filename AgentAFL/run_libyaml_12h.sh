#!/bin/bash
# 12h AFL++ campaign against libyaml OSS-Fuzz parser harness. 6 instances.
set -u
AGENTAFL=/home/user/Documents/1git-folder/AgentAFL
BUILD="$AGENTAFL/AFLPlus/libyaml-build"
BIN="$BUILD/libyaml_parser_fuzzer-afl"
CMPLOG="$BUILD/libyaml_parser_fuzzer-cmplog"
DICT="$BUILD/yaml.dict"
SEEDS="$AGENTAFL/AFLPlus/seeds-libyaml"
OUT="$AGENTAFL/afl-output-libyaml"
LOGS="$AGENTAFL/afl-logs-libyaml"
DUR=43200          # 12h

mkdir -p "$OUT" "$LOGS"
export AFL_SKIP_CPUFREQ=1
export AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES=1
export AFL_IMPORT_FIRST=1
export AFL_FAST_CAL=1
export AFL_AUTORESUME=1

launch() {  # name  extra-args...
  local name="$1"; shift
  setsid nohup afl-fuzz -i "$SEEDS" -o "$OUT" -x "$DICT" -V "$DUR" -m none \
    "$@" -- "$BIN" > "$LOGS/$name.log" 2>&1 &
  echo "  $name (pid $!)"
}

echo "[*] launching 6 instances -> $OUT"
AFL_CYCLE_SCHEDULES=1 launch main -M main -c "$CMPLOG"
sleep 3
launch sec1 -S sec1 -p explore
launch sec2 -S sec2 -p exploit
launch sec3 -S sec3 -p fast
launch sec4 -S sec4 -p coe
launch sec5 -S sec5 -p rare
echo "[*] all launched. status: afl-whatsup -s $OUT"
