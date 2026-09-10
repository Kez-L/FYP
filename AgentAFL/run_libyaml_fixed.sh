#!/bin/bash
# libyaml AFL++ campaign - fixed for the "wedges after a few minutes" problem.
#
# Root cause: this AFL++ build (v4.40c-100-g59b74094) uses a new futex-based
# persistent-mode child sync. Under CPU oversubscription it deadlocks individual
# instances: the afl-fuzz process stays alive but stops executing, fuzzer_stats
# stops updating, afl-whatsup still says "N alive". Reproduced here in <30s with
# 6 instances; fixed by AFL_OLD_CHILD_SYNC=1 (falls back to fd-based sync).
#
# Also: fewer instances (4, not 6) so the box is not oversubscribed, and an
# explicit -t so a slow exec under load is not misread as a hang.
set -u
AGENTAFL=/home/user/Documents/1git-folder/AgentAFL
BUILD="$AGENTAFL/AFLPlus/libyaml-build"
BIN="$BUILD/libyaml_parser_fuzzer-afl"
CMPLOG="$BUILD/libyaml_parser_fuzzer-cmplog"
DICT="$BUILD/yaml.dict"
SEEDS="$AGENTAFL/AFLPlus/seeds-libyaml"
OUT="${1:-$AGENTAFL/afl-output-libyaml}"
LOGS="$AGENTAFL/afl-logs-libyaml"
DUR="${2:-43200}"     # default 12h; pass seconds as $2 to override

mkdir -p "$OUT" "$LOGS"

# --- the fix ---
export AFL_OLD_CHILD_SYNC=1            # disable futex persistent sync (deadlocks under load)
# --- normal knobs ---
export AFL_SKIP_CPUFREQ=1
export AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES=1   # see note below about core_pattern
export AFL_AUTORESUME=1
export AFL_IMPORT_FIRST=1
# NOTE: AFL_FAST_CAL is intentionally NOT set - it skews the auto timeout.

launch() {  # name  extra-args...
  local name="$1"; shift
  setsid nohup afl-fuzz -i "$SEEDS" -o "$OUT" -x "$DICT" -V "$DUR" -m none -t 1000 \
    "$@" -- "$BIN" > "$LOGS/$name.log" 2>&1 &
  echo "  $name (pid $!)  -> $LOGS/$name.log"
}

echo "[*] core_pattern is: $(cat /proc/sys/kernel/core_pattern)"
case "$(cat /proc/sys/kernel/core_pattern)" in
  core|/*) ;;
  *) echo "    ^ pipes crashes to an external handler. Run once (persists until reboot):"
     echo "        echo core | sudo tee /proc/sys/kernel/core_pattern" ;;
esac

echo "[*] launching 4 instances -> $OUT"
AFL_CYCLE_SCHEDULES=1 launch main -M main -c "$CMPLOG"
sleep 3
launch sec1 -S sec1 -p explore
launch sec2 -S sec2 -p exploit
launch sec3 -S sec3 -p coe

sleep 20
echo
echo "[*] status after 20s (all should show a rising run_time on the next check):"
afl-whatsup -s "$OUT" 2>&1 | sed -n '1,20p'
echo
echo "watch live:   watch -n10 'afl-whatsup -s $OUT'"
echo "health check: for d in $OUT/*/; do echo \"\$(basename \$d) last_update=\$(( \$(date +%s) - \$(awk -F: '/^last_update/{print \$2}' \$d/fuzzer_stats) ))s ago\"; done"
