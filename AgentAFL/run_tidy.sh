#!/bin/bash
# tidy-html5 AFL++ campaign launcher - adapted from run_libical.sh.
#
# Target is the OSS-Fuzz tidy_parse_string_fuzzer harness
# (oss-fuzz/projects/tidy-html5/tidy_parse_string_fuzzer.c): the whole input is
# one HTML document, fed to tidyParseString + tidyCleanAndRepair +
# tidyRunDiagnostics + tidySaveBuffer. Structurally the same shape as the
# libical harness, so the html arm stays comparable to the ical one.
#
# 6 instances (xml/tiff layout), not libical's 4. That oversubscribes 8 cores,
# which is exactly the condition that triggers the futex-based persistent-mode
# child-sync deadlock on this AFL++ build (fuzzer_stats stops updating,
# afl-whatsup still says "N alive" but no exec progress) - so AFL_OLD_CHILD_SYNC=1
# below is load-bearing here, and -t is explicit so a slow exec under load isn't
# misread as a hang.
#
# Seeds are seeds-tidy-8k (317 of the 324 cmin'd seeds, capped at 8KB), NOT
# seeds-tidy. tidy's parse+repair+diagnostics+serialize is super-linear in
# document length, so a 53KB seed costs ~253ms/exec; with the uncapped corpus
# four of six instances sat pinned on that one queue entry at ~4.5 execs/s for
# over an hour while main (which has trimming disabled by -M) and sec2 ran
# normally. The 7 dropped seeds (8KB-53KB) are worth only 34 of 3642 edges
# (0.93%). Trimming is NOT the fix - an A/B test with AFL_DISABLE_TRIM=1 showed
# no difference.
#
# No -x dictionary, deliberately: the xml and ical campaigns this is meant to be
# compared against are both dictionary-free, so html stays dictionary-free too.
# (AFL++ ships dictionaries/html_tags.dict if you ever want to opt in.)
#
# No @@ in the target argv, deliberately: libAFLDriver.a ignores argv files when
# it detects it is running under afl-fuzz and takes input over shared memory in a
# persistent loop instead - that is where the exec/s comes from. Same as
# run_libical.sh. (Outside afl-fuzz the driver DOES read a file argument, which
# is why afl-showmap/evaluate_seeds need @@ but this script must not use it.)
#
# NOT launched automatically - run this script yourself when ready:
#   ./run_tidy.sh                  # 12h campaign, default output dir
#   ./run_tidy.sh /some/out 3600   # custom output dir, 1h campaign
set -u
AGENTAFL=/home/user/Documents/1git-folder/AgentAFL
BUILD="$AGENTAFL/AFLPlus/tidy-html5-build"
BIN="$BUILD/tidy_parse_string_fuzzer-afl"
CMPLOG="$BUILD/tidy_parse_string_fuzzer-cmplog"
SEEDS="$AGENTAFL/AFLPlus/seeds-tidy-8k"
OUT="${1:-$AGENTAFL/afl-output-tidy-20261006b}"
LOGS="$AGENTAFL/afl-logs-tidy"
DUR="${2:-43200}"     # default 12h; pass seconds as $2 to override

for f in "$BIN" "$CMPLOG"; do
  [[ -x "$f" ]] || { echo "missing binary: $f" >&2; exit 2; }
done
[[ -d "$SEEDS" ]] && [[ -n "$(ls -A "$SEEDS" 2>/dev/null)" ]] || {
  echo "seed dir missing or empty: $SEEDS" >&2; exit 2; }

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

echo "[*] launching 6 instances -> $OUT"
AFL_CYCLE_SCHEDULES=1 launch main -M main -c "$CMPLOG"
sleep 3
launch sec1 -S sec1 -p explore
launch sec2 -S sec2 -p exploit
launch sec3 -S sec3 -p coe
launch sec4 -S sec4 -p fast
launch sec5 -S sec5 -p rare

sleep 20
echo
echo "[*] status after 20s (all should show a rising run_time on the next check):"
afl-whatsup -s "$OUT" 2>&1 | sed -n '1,20p'
echo
echo "watch live:   watch -n10 'afl-whatsup -s $OUT'"
echo "health check: for d in $OUT/*/; do echo \"\$(basename \$d) last_update=\$(( \$(date +%s) - \$(awk -F: '/^last_update/{print \$2}' \$d/fuzzer_stats) ))s ago\"; done"
