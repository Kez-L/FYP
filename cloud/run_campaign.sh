#!/bin/bash
# run_campaign.sh - launch one fuzzing campaign (one "run" of the experiment).
#
#   ./run_campaign.sh <target> <condition> <run_id> [num_instances] [duration_s]
#
#   ./run_campaign.sh libxml2 baseline 1 3 43200    # 3 instances, 12h
#   ./run_campaign.sh libxml2 minyee   1 3 43200    # same + ResourceAwareAFL
#
# What it starts (all detached, so you can close SSH):
#   - N afl-fuzz instances: main (+CMPLOG) and N-1 secondaries with different
#     power schedules, all stopping themselves after duration_s (-V)
#   - Passive loggers that only READ the campaign, never change it:
#       * Min Yee's ResourceAwareAFL/monitor.py  -> monitor/raw_metrics.json
#       * AgentAFL/plateau_watch.py               -> plateau_log.csv
#   - condition=minyee only: ResourceAwareAFL/run_analyzer.py, plus
#     adaptive_mutator.py loaded INSIDE each afl-fuzz via AFL_PYTHON_MODULE.
#     That mutator is the only thing in her stack that touches the fuzzing;
#     without it the analyzer logs decisions nothing ever acts on.
#       * run_analyzer.py -> logs/minyee_analyzer.log
#       * .adapt_signal timeline -> adapt_signal_log.csv
#   - finish_campaign.sh, which waits for the fuzzers to stop, then stops the
#     loggers, packs everything into $WORK/results/<run>.tar.gz and (by default)
#     shuts the machine down so it stops costing money.
#
# Env vars:
#   AUTO_SHUTDOWN=0        don't shut the machine down at the end (default 1)
#   WORK=...               base dir (default ~/fyp)
#   MINYEE_CALIBRATION=8   run_analyzer.py --calibration
#   MINYEE_PERSISTENCE=2   run_analyzer.py --persistence
#   MINYEE_PIN_MODE=MODE   smoke test: pin .adapt_signal to MODE and do NOT
#                          start the analyzer, so one mode can be exercised
#                          against real afl-fuzz. Never use for a real campaign;
#                          it is recorded as pinned_mode= in meta.txt.
#
# Conditions: "baseline" and "minyee" are implemented. "combined" is a
# placeholder until the integrated pipeline exists.
#
# Run cloud/preflight_resource_aware.sh once per machine before any minyee campaign.
set -euo pipefail

usage() { sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }
[[ $# -ge 3 ]] || usage

TARGET="$1"; CONDITION="$2"; RUN_ID="$3"
NUM="${4:-6}"
DUR="${5:-43200}"
WORK="${WORK:-$HOME/fyp}"
AUTO_SHUTDOWN="${AUTO_SHUTDOWN:-1}"
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$WORK/FYP"
SUDO=""; [[ $EUID -ne 0 ]] && SUDO="sudo"

# ---------------------------------------------------------------- targets
# Add ical / html here once their build scripts exist.
case "$TARGET" in
  libxml2)
    BUILD="$WORK/targets/libxml2-build"
    BIN="$BUILD/xmllint-afl"
    CMPLOG="$BUILD/xmllint-cmplog"
    TARGET_ARGS=(--noout @@)
    SEEDS="${SEEDS:-$REPO/AgentAFL/seeds_min}"
    TARGET_ENV=()
    ;;
  *) echo "Unknown target '$TARGET' (only libxml2 is set up so far)" >&2; exit 2 ;;
esac

case "$CONDITION" in
  baseline|minyee) ;;
  combined)
    echo "Condition 'combined' isn't wired up yet - needs the integrated pipeline." >&2; exit 2 ;;
  *) echo "Unknown condition '$CONDITION' (baseline | minyee | combined)" >&2; exit 2 ;;
esac

MINYEE_DIR="$REPO/ResourceAwareAFL"
SIGNAL_FILE="$MINYEE_DIR/adaptation/.adapt_signal"
CALIB="${MINYEE_CALIBRATION:-8}"
PERSIST="${MINYEE_PERSISTENCE:-2}"
PIN_MODE="${MINYEE_PIN_MODE:-}"
MINYEE_COMMIT="$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo unknown)"

# Secondary power schedules, in order (same layout as run_tidy.sh / run_libical.sh)
SCHEDULES=(explore exploit coe fast rare explore exploit coe fast rare)

# ---------------------------------------------------------------- pre-flight
die() { echo "[-] $*" >&2; exit 2; }

# Abort a campaign that has already started processes. Safe to iterate pid files
# here (unlike in finish_campaign.sh) because we are seconds in, so no PID has
# been recycled yet.
abort_run() {
  echo "[-] $*" >&2
  if [[ -d "${RUN:-}/pids" ]]; then
    for f in "$RUN"/pids/*.pid; do
      [[ -f "$f" ]] || continue
      pid="$(cat "$f" 2>/dev/null || true)"
      [[ -n "$pid" ]] && kill "$pid" 2>/dev/null || true
    done
  fi
  echo "[-] stopped this campaign's processes; run dir kept at $RUN for inspection" >&2
  exit 2
}
for f in "$BIN" "$CMPLOG"; do [[ -x "$f" ]] || die "missing binary: $f (run build_${TARGET}.sh)"; done
[[ -d "$SEEDS" && -n "$(ls -A "$SEEDS")" ]] || die "seed dir missing or empty: $SEEDS"
[[ -f "$REPO/ResourceAwareAFL/monitor.py" ]] || die "Min Yee's monitor.py not found in $REPO"
[[ -f "$REPO/AgentAFL/plateau_watch.py" ]]  || die "plateau_watch.py not found in $REPO"
[[ "$NUM" -ge 1 && "$NUM" -le 11 ]] || die "num_instances must be 1-11"

# A stray AFL_PYTHON_MODULE in the operator's shell would silently load Min
# Yee's mutator into a "baseline" run and nothing downstream would complain.
if [[ "$CONDITION" == "baseline" ]]; then
  for v in AFL_PYTHON_MODULE AFL_CUSTOM_MUTATOR_LIBRARY AFL_CUSTOM_MUTATOR_ONLY AFL_NO_PYTHON; do
    [[ -z "${!v:-}" ]] || die "$v is set in the environment - baseline must run clean"
  done
else
  [[ -z "${AFL_CUSTOM_MUTATOR_ONLY:-}" ]] || die "AFL_CUSTOM_MUTATOR_ONLY must be unset (we want AFL++'s internal mutators too)"
  [[ -z "${AFL_NO_PYTHON:-}" ]] || die "AFL_NO_PYTHON is set - it disables the mutator this condition depends on"
fi

if [[ "$CONDITION" == "minyee" ]]; then
  for f in monitor.py run_analyzer.py controller.py cea.py adaptive_mutator.py; do
    [[ -f "$MINYEE_DIR/$f" ]] || die "missing $MINYEE_DIR/$f"
  done
  # afl-fuzz -h EXITS 1, so `afl-fuzz -h | grep -q` is a guaranteed false
  # negative under `set -o pipefail`. Capture first. The env-var list and the
  # "Compiled with Python" line are in -hh, not -h.
  AFLHH="$(afl-fuzz -hh 2>&1 || true)"
  grep -q 'module support' <<<"$AFLHH" \
    || die "afl-fuzz built without Python mutator support - rebuild AFL++ with python3-dev present and re-bake the AMI (see preflight_resource_aware.sh)"
  grep -q 'AFL_PYTHON_MODULE' <<<"$AFLHH" \
    || die "afl-fuzz does not document AFL_PYTHON_MODULE"
  python3 -c 'import psutil' 2>/dev/null \
    || die "psutil missing - sudo apt-get install -y python3-psutil"
  if [[ -n "$PIN_MODE" ]]; then
    case "$PIN_MODE" in
      DEFAULT|THROTTLE_POWER|REDUCE_MUTATION_SIZE|STRONG_THROTTLE|EXPLOIT_CRASH|SKIP_TIMEOUTS|EXPLORE_HEAVY) ;;
      *) die "MINYEE_PIN_MODE='$PIN_MODE' is not a mode adaptive_mutator.py recognises" ;;
    esac
  fi
  # controller.py anchors .adapt_signal to its own __file__, so every campaign on
  # this machine shares one signal file. Two at once would fight over it.
  if pgrep -f 'ResourceAwareAFL/run_analyzer.py' >/dev/null; then
    die "another run_analyzer.py is running - .adapt_signal is shared, one adaptive campaign per machine"
  fi
  if pgrep -x afl-fuzz >/dev/null; then
    die "afl-fuzz is already running on this box - an adaptive campaign must not share .adapt_signal"
  fi
fi

CPUS="$(nproc)"
if (( NUM > CPUS )); then
  echo "[!] WARNING: $NUM instances on $CPUS vCPUs - oversubscribed, results will be skewed."
fi

# EC2 metadata (IMDSv2) - records exactly what hardware this ran on
TOKEN="$(curl -sf -m 2 -X PUT http://169.254.169.254/latest/api/token \
          -H 'X-aws-ec2-metadata-token-ttl-seconds: 300' 2>/dev/null || true)"
imds() { local v; v="$(curl -sf -m 2 -H "X-aws-ec2-metadata-token: $TOKEN" \
          "http://169.254.169.254/latest/meta-data/$1" 2>/dev/null)" || v=""; echo "${v:-unknown}"; }
ITYPE="$(imds instance-type)"; AZ="$(imds placement/availability-zone)"; IID="$(imds instance-id)"
if [[ "$ITYPE" == t* ]]; then
  echo "[!] WARNING: $ITYPE is a burstable type - it WILL throttle under fuzzing. Use c7i.*"
fi

# AFL++ needs crashes to be written as plain core files, not piped to apport
if [[ "$(cat /proc/sys/kernel/core_pattern)" != "core" ]]; then
  echo core | $SUDO tee /proc/sys/kernel/core_pattern >/dev/null || true
fi

# ---------------------------------------------------------------- run dir
NAME="${TARGET}_${CONDITION}_run${RUN_ID}"
RUN="$WORK/runs/$NAME"
OUT="$RUN/afl-out"
[[ -e "$RUN" ]] && die "$RUN already exists - pick a new run_id or move the old one"
mkdir -p "$OUT" "$RUN/logs" "$RUN/monitor" "$RUN/pids" "$WORK/results"

# Instance names, for mutator_scope in meta.txt
INSTANCES="main"
for ((i = 1; i < NUM; i++)); do INSTANCES="$INSTANCES,sec$i"; done

if [[ "$CONDITION" == "minyee" ]]; then
  ACTIVE="ResourceAwareAFL closed loop: monitor.py -> run_analyzer.py --calibration $CALIB --persistence $PERSIST -> controller.py -> adaptive_mutator.py"
  [[ -n "$PIN_MODE" ]] && ACTIVE="ResourceAwareAFL adaptive_mutator.py PINNED to $PIN_MODE (no analyzer - SMOKE TEST, not a campaign)"
else
  ACTIVE="none (baseline)"
fi

SEED_COUNT="$(ls -1 "$SEEDS" | wc -l)"
SEED_HASH="$(cd "$SEEDS" && find . -type f -print0 | sort -z | xargs -0 sha256sum | sha256sum | cut -c1-16)"

{
  echo "name=$NAME"
  echo "target=$TARGET"
  echo "condition=$CONDITION"
  echo "run_id=$RUN_ID"
  echo "start_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "start_unix=$(date +%s)"
  echo "duration_s=$DUR"
  echo "num_instances=$NUM"
  echo "layout=main(-c cmplog, AFL_CYCLE_SCHEDULES=1) + secondaries -p ${SCHEDULES[*]:0:$((NUM-1))}"
  echo "target_cmd=$BIN ${TARGET_ARGS[*]}"
  echo "seeds_dir=$SEEDS"
  echo "seeds_count=$SEED_COUNT"
  echo "seeds_sha256_16=$SEED_HASH"
  echo "instance_type=$ITYPE"
  echo "instance_id=$IID"
  echo "availability_zone=$AZ"
  echo "vcpus=$CPUS"
  echo "mem_total_kb=$(awk '/MemTotal/{print $2}' /proc/meminfo)"
  echo "passive_loggers=ResourceAwareAFL/monitor.py, AgentAFL/plateau_watch.py"
  echo "active_components=$ACTIVE"
  if [[ "$CONDITION" == "minyee" ]]; then
    echo "mutator_scope=$INSTANCES (AFL_PYTHON_MODULE=adaptive_mutator)"
    # monitor.py only watches afl-out/main, so the control signal is derived
    # from ONE instance's process tree and applied to all of them. sec*
    # resource pressure is invisible to the controller.
    echo "control_scope=signal derived from afl-out/main only, applied to all instances"
    echo "monitor_window_s=30 (hardcoded in monitor.py)"
    echo "analyzer_calibration=$CALIB"
    echo "analyzer_persistence=$PERSIST"
    echo "signal_file=$SIGNAL_FILE (shared per machine)"
    echo "pythonpath=$MINYEE_DIR"
    echo "pinned_mode=$PIN_MODE"
    echo "minyee_commit=$MINYEE_COMMIT"
  fi
  [[ -f "$WORK/versions.txt" ]] && cat "$WORK/versions.txt"
  [[ -f "$BUILD/build_info.txt" ]] && cat "$BUILD/build_info.txt"
} > "$RUN/meta.txt"

# ---------------------------------------------------------------- launch fuzzers
export AFL_SKIP_CPUFREQ=1
export AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES=1
export AFL_IMPORT_FIRST=1
export AFL_NO_UI=1
for kv in "${TARGET_ENV[@]+"${TARGET_ENV[@]}"}"; do export "$kv"; done

# AFL++ does NOT add cwd to sys.path, so PYTHONPATH is what makes
# `AFL_PYTHON_MODULE=adaptive_mutator` resolvable. It is scoped to the fuzzers
# via an env prefix rather than exported, because exporting it would also put
# ResourceAwareAFL/ on the import path of monitor.py and plateau_watch.py,
# where monitor/controller/cea/main could shadow their imports. With an empty
# array the expansion below is zero words, so baseline stays byte-identical.
ENVPFX=()
if [[ "$CONDITION" == "minyee" ]]; then
  export AFL_PYTHON_MODULE=adaptive_mutator   # exported, so it lands in afl_env.txt
  ENVPFX=(env "PYTHONPATH=$MINYEE_DIR")
fi
env | grep -E '^(AFL_|ASAN_)' | sort > "$RUN/afl_env.txt"
# PYTHONPATH is not an AFL_/ASAN_ var, so the snapshot above misses it.
[[ "$CONDITION" == "minyee" ]] && echo "PYTHONPATH=$MINYEE_DIR" >> "$RUN/afl_env.txt"

launch() {  # name  extra-args...
  local name="$1"; shift
  setsid nohup "${ENVPFX[@]+"${ENVPFX[@]}"}" afl-fuzz -i "$SEEDS" -o "$OUT" -V "$DUR" -m none -t 1000 \
    "$@" -- "$BIN" "${TARGET_ARGS[@]}" > "$RUN/logs/$name.log" 2>&1 &
  echo $! > "$RUN/pids/$name.pid"
  echo "  $name (pid $!)"
}

# ------------------------------------------------- adaptation signal + analyzer
# Started BEFORE the fuzzers on purpose. run_analyzer.py constructs
# AdaptiveFuzzingController (which writes MODE=DEFAULT) before it waits for
# raw_metrics.json, so starting it first guarantees the signal is DEFAULT
# before the mutator's first fuzz_count() instead of racing a stale mode left
# behind by a previous campaign.
if [[ "$CONDITION" == "minyee" ]]; then
  mkdir -p "$MINYEE_DIR/adaptation"
  if [[ -n "$PIN_MODE" ]]; then
    printf 'MODE=%s;TIMESTAMP=%s\n' "$PIN_MODE" "$(date +%s)" > "$SIGNAL_FILE"
    echo "[*] SMOKE TEST: signal pinned to $PIN_MODE, analyzer NOT started"
  else
    printf 'MODE=DEFAULT;TIMESTAMP=%s\n' "$(date +%s)" > "$SIGNAL_FILE"
    # cwd must be $RUN/monitor: run_analyzer.py hardcodes log_file="raw_metrics.json"
    # relative to cwd with no CLI override, and it has to match monitor.py's cwd.
    # PYTHONIOENCODING/LC_ALL are NOT optional - run_analyzer.py prints a literal
    # U+0394 inside its per-record try, whose `except Exception` only logs and
    # continues, and the adaptation logic sits after those prints. Under a
    # non-UTF-8 locale every window would raise and NO trigger would ever fire
    # for the whole campaign, while the pid file and status.sh looked healthy.
    ( cd "$RUN/monitor"
      setsid nohup env PYTHONIOENCODING=utf-8 LC_ALL=C.UTF-8 \
        python3 -u "$MINYEE_DIR/run_analyzer.py" --calibration "$CALIB" --persistence "$PERSIST" \
        > "$RUN/logs/minyee_analyzer.log" 2>&1 & echo $! > "$RUN/pids/minyee_analyzer.pid" )
    echo "[*] started run_analyzer.py (--calibration $CALIB --persistence $PERSIST)"
  fi
fi

echo "[*] $NAME: launching $NUM instance(s) for ${DUR}s on $ITYPE ($CPUS vCPUs)"
AFL_CYCLE_SCHEDULES=1 launch main -M main -c "$CMPLOG"
sleep 3
for ((i = 1; i < NUM; i++)); do
  launch "sec$i" -S "sec$i" -p "${SCHEDULES[$((i-1))]}"
done

# ---------------------------------------------------------------- passive loggers
# Min Yee's monitor picks which fuzzer_stats path to use ONCE, at startup, and
# gets stuck on the wrong one if afl-out/main/fuzzer_stats doesn't exist yet at
# that exact moment (AFL++ can take a while to finish calibrating, especially
# under ASan with a large seed corpus). Waiting here for the real file first
# avoids that race without touching her code.
echo "[*] waiting for AFL++ to write fuzzer_stats before starting the monitor..."
STATS_WAIT="${STATS_WAIT:-300}"
waited=0
while [[ ! -f "$OUT/main/fuzzer_stats" && $waited -lt $STATS_WAIT ]]; do
  # If main has already died there is nothing left to wait for. Common causes:
  # a CMPLOG binary that doesn't match the target build, or an OOM.
  mpid="$(cat "$RUN/pids/main.pid" 2>/dev/null || true)"
  if [[ -n "$mpid" ]] && ! kill -0 "$mpid" 2>/dev/null; then
    echo "[!] main afl-fuzz exited before writing fuzzer_stats:"
    sed 's/\x1b\[[0-9;]*m//g' "$RUN/logs/main.log" 2>/dev/null \
      | grep -iE "PROGRAM ABORT|FATAL|Location :" | head -4 | sed 's/^/      /'
    break
  fi
  sleep 1; waited=$((waited+1))
done
if [[ -f "$OUT/main/fuzzer_stats" ]]; then
  echo "[*] fuzzer_stats present after ${waited}s"
elif [[ "$CONDITION" == "minyee" ]]; then
  # monitor.py resolves its stats path ONCE in __init__ and would lock onto
  # afl-out/main/default/fuzzer_stats, which never appears for a -M main
  # instance. It would then log nothing all run, so run_analyzer.py would never
  # evaluate a window and the adaptive arm would be silently identical to
  # baseline. Fail now rather than discover it in 12h.
  abort_run "afl-out/main/fuzzer_stats never appeared after ${waited}s - the minyee condition cannot work without it. See $RUN/logs/main.log"
else
  echo "[!] WARNING: fuzzer_stats still missing after ${waited}s - monitor.py may log nothing"
fi

# Min Yee's monitor: writes raw_metrics.json to its working dir, so run it from
# monitor/. Pointing it at afl-out/main makes it read afl-out/main/fuzzer_stats.
( cd "$RUN/monitor"; setsid nohup python3 -u "$REPO/ResourceAwareAFL/monitor.py" "$OUT/main" \
    > "$RUN/logs/minyee_monitor.log" 2>&1 & echo $! > "$RUN/pids/minyee_monitor.pid" )

# monitor.py writes its first window after window_sec (30s). If raw_metrics.json
# is still absent well past that, it locked onto the wrong fuzzer_stats path or
# died - either way the analyzer has nothing to read.
if [[ "$CONDITION" == "minyee" && -z "$PIN_MODE" ]]; then
  mwait=0
  while [[ ! -f "$RUN/monitor/raw_metrics.json" && $mwait -lt 120 ]]; do
    sleep 2; mwait=$((mwait+2))
  done
  if [[ -f "$RUN/monitor/raw_metrics.json" ]]; then
    echo "[*] monitor.py producing windows after ${mwait}s"
  else
    echo "[!] monitor.py wrote no window in ${mwait}s. Its resolved stats path:"
    grep -i "Looking for AFL++ stats" "$RUN/logs/minyee_monitor.log" 2>/dev/null | sed 's/^/      /'
    abort_run "monitor.py is not logging - run_analyzer.py would never evaluate a window, so this would not be a valid minyee run"
  fi
fi

setsid nohup python3 -u "$REPO/AgentAFL/plateau_watch.py" --output-dir "$OUT" \
  --interval 60 --plateau-secs 900 --log "$RUN/plateau_log.csv" \
  > "$RUN/logs/plateau_watch.log" 2>&1 &
echo $! > "$RUN/pids/plateau_watch.pid"

# Wall-clock timeline of what the MUTATOR actually saw. controller.py clobbers
# .adapt_signal on every write and the analyzer's stdout has window_ids but no
# wall clock, so without this there is no joinable record of mode dwell times.
if [[ "$CONDITION" == "minyee" ]]; then
  setsid nohup bash "$HERE/signal_log.sh" "$SIGNAL_FILE" "$RUN/adapt_signal_log.csv" 5 \
    > "$RUN/logs/signal_log.log" 2>&1 &
  echo $! > "$RUN/pids/signal_log.pid"
fi

# ---------------------------------------------------------------- finisher + failsafe
AUTO_SHUTDOWN="$AUTO_SHUTDOWN" WORK="$WORK" setsid nohup bash "$HERE/finish_campaign.sh" "$RUN" \
  > "$RUN/logs/finish.log" 2>&1 &

if [[ "$AUTO_SHUTDOWN" == "1" ]]; then
  # Failsafe: power off 1h after the planned end even if something hangs.
  # Cancel with: sudo shutdown -c
  $SUDO shutdown -h "+$(( DUR / 60 + 60 ))" >/dev/null 2>&1 \
    && echo "[*] Failsafe shutdown scheduled in $(( DUR / 60 + 60 )) min" \
    || echo "[!] Could not schedule failsafe shutdown"
fi

sleep 30
echo
afl-whatsup -s "$OUT" 2>&1 | sed -n '1,25p' || true

# ------------------------------------------------- did the loop actually close?
# AFL++ logs "Python mutator '...' installed successfully." per instance. If that
# is missing, the mutator never loaded and this run is just baseline with extra
# logging - which is worth knowing now, not in 12h.
if [[ "$CONDITION" == "minyee" ]]; then
  echo
  loaded=0
  for f in "$RUN"/logs/main.log "$RUN"/logs/sec*.log; do
    [[ -f "$f" ]] || continue
    if grep -q "installed successfully" "$f"; then
      loaded=$((loaded+1))
    else
      echo "[!] $(basename "$f" .log): Python mutator NOT loaded"
      grep -iE "Failed to load python|init function not found|deinit function not found|mutually exclusive" "$f" | head -3 || true
    fi
  done
  if [[ "$loaded" -eq "$NUM" ]]; then
    echo "[*] adaptive_mutator loaded in all $NUM instance(s)"
  else
    echo "[!] WARNING: Python mutator loaded in only $loaded/$NUM instances."
    echo "[!] This run would NOT be a valid minyee arm. Inspect $RUN/logs now."
  fi
  if [[ -z "$PIN_MODE" ]]; then
    apid="$(cat "$RUN/pids/minyee_analyzer.pid" 2>/dev/null || true)"
    if [[ -n "$apid" ]] && kill -0 "$apid" 2>/dev/null; then
      echo "[*] run_analyzer.py alive (pid $apid), mode=$(cut -d';' -f1 "$SIGNAL_FILE" 2>/dev/null || echo '?')"
    else
      echo "[!] WARNING: run_analyzer.py already dead - see $RUN/logs/minyee_analyzer.log"
    fi
  fi
fi

echo
echo "[+] Running. Results will be packed into: $WORK/results/$NAME.tar.gz"
echo "    Check progress any time:  $HERE/status.sh"