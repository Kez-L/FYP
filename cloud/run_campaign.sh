#!/bin/bash
# run_campaign.sh - launch one fuzzing campaign (one "run" of the experiment).
#
#   ./run_campaign.sh <target> <condition> <run_id> [num_instances] [duration_s]
#
#   ./run_campaign.sh libxml2 baseline 1            # 6 instances, 12h
#   ./run_campaign.sh libxml2 baseline 0 2 600      # 10-minute smoke test
#
# What it starts (all detached, so you can close SSH):
#   - N afl-fuzz instances: main (+CMPLOG) and N-1 secondaries with different
#     power schedules, all stopping themselves after duration_s (-V)
#   - Passive loggers that only READ the campaign, never change it:
#       * Min Yee's ResourceAwareAFL/monitor.py  -> monitor/raw_metrics.json
#       * AgentAFL/plateau_watch.py               -> plateau_log.csv
#   - finish_campaign.sh, which waits for the fuzzers to stop, then stops the
#     loggers, packs everything into $WORK/results/<run>.tar.gz and (by default)
#     shuts the machine down so it stops costing money.
#
# Env vars:
#   AUTO_SHUTDOWN=0   don't shut the machine down at the end (default 1)
#   WORK=...          base dir (default ~/fyp)
#
# Conditions: only "baseline" is implemented. "minyee" and "combined" are
# placeholders until the integrated pipeline exists.
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
  baseline) ;;
  minyee|combined)
    echo "Condition '$CONDITION' isn't wired up yet - needs the integrated pipeline." >&2; exit 2 ;;
  *) echo "Unknown condition '$CONDITION' (baseline | minyee | combined)" >&2; exit 2 ;;
esac

# Secondary power schedules, in order (same layout as run_tidy.sh / run_libical.sh)
SCHEDULES=(explore exploit coe fast rare explore exploit coe fast rare)

# ---------------------------------------------------------------- pre-flight
die() { echo "[-] $*" >&2; exit 2; }
for f in "$BIN" "$CMPLOG"; do [[ -x "$f" ]] || die "missing binary: $f (run build_${TARGET}.sh)"; done
[[ -d "$SEEDS" && -n "$(ls -A "$SEEDS")" ]] || die "seed dir missing or empty: $SEEDS"
[[ -f "$REPO/ResourceAwareAFL/monitor.py" ]] || die "Min Yee's monitor.py not found in $REPO"
[[ -f "$REPO/AgentAFL/plateau_watch.py" ]]  || die "plateau_watch.py not found in $REPO"
[[ "$NUM" -ge 1 && "$NUM" -le 11 ]] || die "num_instances must be 1-11"

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
  echo "active_components=none (baseline)"
  [[ -f "$WORK/versions.txt" ]] && cat "$WORK/versions.txt"
  [[ -f "$BUILD/build_info.txt" ]] && cat "$BUILD/build_info.txt"
} > "$RUN/meta.txt"

# ---------------------------------------------------------------- launch fuzzers
export AFL_SKIP_CPUFREQ=1
export AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES=1
export AFL_IMPORT_FIRST=1
export AFL_NO_UI=1
for kv in "${TARGET_ENV[@]+"${TARGET_ENV[@]}"}"; do export "$kv"; done
env | grep -E '^(AFL_|ASAN_)' | sort > "$RUN/afl_env.txt"

launch() {  # name  extra-args...
  local name="$1"; shift
  setsid nohup afl-fuzz -i "$SEEDS" -o "$OUT" -V "$DUR" -m none -t 1000 \
    "$@" -- "$BIN" "${TARGET_ARGS[@]}" > "$RUN/logs/$name.log" 2>&1 &
  echo $! > "$RUN/pids/$name.pid"
  echo "  $name (pid $!)"
}

echo "[*] $NAME: launching $NUM instance(s) for ${DUR}s on $ITYPE ($CPUS vCPUs)"
AFL_CYCLE_SCHEDULES=1 launch main -M main -c "$CMPLOG"
sleep 3
for ((i = 1; i < NUM; i++)); do
  launch "sec$i" -S "sec$i" -p "${SCHEDULES[$((i-1))]}"
done

# ---------------------------------------------------------------- passive loggers
# Min Yee's monitor: writes raw_metrics.json to its working dir, so run it from
# monitor/. Pointing it at afl-out/main makes it read afl-out/main/fuzzer_stats.
( cd "$RUN/monitor"; setsid nohup python3 -u "$REPO/ResourceAwareAFL/monitor.py" "$OUT/main" \
    > "$RUN/logs/minyee_monitor.log" 2>&1 & echo $! > "$RUN/pids/minyee_monitor.pid" )

setsid nohup python3 -u "$REPO/AgentAFL/plateau_watch.py" --output-dir "$OUT" \
  --interval 60 --plateau-secs 900 --log "$RUN/plateau_log.csv" \
  > "$RUN/logs/plateau_watch.log" 2>&1 &
echo $! > "$RUN/pids/plateau_watch.pid"

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
echo
echo "[+] Running. Results will be packed into: $WORK/results/$NAME.tar.gz"
echo "    Check progress any time:  $HERE/status.sh"
