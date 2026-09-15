#!/bin/bash
# libical AFL++ campaign launcher - adapted from run_libyaml_fixed.sh.
#
# Includes the same AFL_OLD_CHILD_SYNC=1 workaround for the futex-based
# persistent-mode child-sync deadlock seen under CPU oversubscription on this
# AFL++ build (fuzzer_stats stops updating, afl-whatsup still says "N alive"
# but no exec progress). 4 instances (not 6) to avoid oversubscribing the box,
# with an explicit -t so a slow exec under load isn't misread as a hang.
#
# No -x dictionary is used here, deliberately: the xml (libxml2/xmllint-afl)
# campaign this is meant to be compared against didn't use one either, so
# ical stays dictionary-free too for a fair comparison. (ical.dict still
# exists in libical-build/ if you want to opt in later - just add -x "$DICT"
# back to the launch() call below.)
#
# NOT launched automatically - run this script yourself when ready:
#   ./run_libical.sh                  # 12h campaign, default output dir
#   ./run_libical.sh /some/out 3600   # custom output dir, 1h campaign
set -u
AGENTAFL=/home/user/Documents/1git-folder/AgentAFL
BUILD="$AGENTAFL/AFLPlus/libical-build"
BIN="$BUILD/libical_fuzzer-afl"
CMPLOG="$BUILD/libical_fuzzer-cmplog"
#DICT="$BUILD/ical.dict"
SEEDS="$AGENTAFL/AFLPlus/seeds-libical"
OUT="${1:-$AGENTAFL/afl-output-libical}"
LOGS="$AGENTAFL/afl-logs-libical"
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
  setsid nohup afl-fuzz -i "$SEEDS" -o "$OUT" -V "$DUR" -m none -t 1000 \
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
