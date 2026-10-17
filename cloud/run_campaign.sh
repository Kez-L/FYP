#!/bin/bash
# run_campaign.sh - launch one fuzzing campaign (one "run" of the experiment).
#
#   ./run_campaign.sh <target> <condition> <run_id> [num_instances] [duration_s]
#
#   ./run_campaign.sh libxml2 baseline 1 3 43200    # 3 instances, 12h
#   ./run_campaign.sh libxml2 minyee   1 3 43200    # same + ResourceAwareAFL
#   ./run_campaign.sh libxml2 combined 1 3 43200    # same + AgentAFL escalation
#
# What it starts (all detached, so you can close SSH):
#   - N afl-fuzz instances: main (+CMPLOG) and N-1 secondaries with different
#     power schedules, all stopping themselves after duration_s (-V)
#   - Passive loggers that only READ the campaign, never change it:
#       * Min Yee's ResourceAwareAFL/monitor.py  -> monitor/raw_metrics.json
#       * AgentAFL/plateau_watch.py               -> plateau_log.csv
#   - condition=minyee|combined: ResourceAwareAFL/run_analyzer.py, plus
#     adaptive_mutator.py loaded INSIDE each afl-fuzz via AFL_PYTHON_MODULE.
#   - condition=combined only: ResourceAwareAFL/agentafl_escalation.py, which
#     reacts to the .agentafl_request her analyzer writes when a coverage
#     adaptation fails to recover, generates seeds with AgentAFL Stage 4
#     (positive + negative feedback) and injects them with afl-addseeds.
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
# Conditions: baseline | minyee | combined (= minyee + AgentAFL Level 2).
#
# AGENTAFL_* overrides (condition=combined):
#   AGENTAFL_CALLS=5             LLM calls per escalation
#   AGENTAFL_SEEDS_PER_CALL=5    seeds per call
#   AGENTAFL_COOLDOWN_S=1500     minimum gap between escalations (see note below)
#   AGENTAFL_MAX_ESCALATIONS=0   0 = unlimited
#   AGENTAFL_PROVIDER=openai     openai | claude
#   AGENTAFL_MODEL=gpt-5.6-luna  the model the 5x5 budget was measured on
#   AGENTAFL_FMT="XML document"  format name used in the prompt
#
# All conditions export AFL_SYNC_TIME=1 by default, so injected seeds are imported
# within ~30s instead of up to 10min and the arms differ ONLY by the injection.
# Override it to reproduce the original fuzzer config:
#   AFL_SYNC_TIME=20 ./run_campaign.sh libxml2 minyee 1 3 43200
# (20 is AFL++'s stock value, so that is byte-identical to leaving it unset.)
#
# AgentAFL is OFF for baseline and minyee: no daemon, and AGENTAFL_HOOK=0 makes
# the hook in run_analyzer.py dead code, so Min Yee's layer runs as it did before
# the integration existed. Both are recorded in meta.txt.
#
# Run cloud/preflight_resource_aware.sh once per machine before any minyee or
# combined campaign.
set -euo pipefail

usage() { sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }
[[ $# -ge 3 ]] || usage

TARGET="$1"; CONDITION="$2"; RUN_ID="$3"
# Default 3, matching the frozen layout (c7i.xlarge, 4 vCPUs: main + sec1 + sec2,
# leaving one vCPU for the loggers, the analyzer and AgentAFL). It used to default
# to 6, which silently oversubscribes that box if the argument is forgotten.
NUM="${4:-3}"
DUR="${5:-43200}"
WORK="${WORK:-$HOME/fyp}"
AUTO_SHUTDOWN="${AUTO_SHUTDOWN:-1}"
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$WORK/FYP"
SUDO=""; [[ $EUID -ne 0 ]] && SUDO="sudo"

# ---------------------------------------------------------------- targets
# One definition per target, shared with preflight_agentafl.sh and verify_run.sh
# so none of them can disagree about a target. Sets BUILD, BIN, CMPLOG,
# FUZZ_ARGS, SHOWMAP_ARGS, SEEDS, AGENTAFL_FMT_DEFAULT, SANITY_INPUT, TARGET_ENV
# and the frozen corpus fingerprint.
#
# FUZZ_ARGS and SHOWMAP_ARGS are deliberately separate: the libical and tidy
# harnesses take their input over shared memory under afl-fuzz (no file
# argument) but need @@ under afl-showmap, and getting that wrong makes every
# AgentAFL candidate score an identical map. targets.sh has the measurements.
TARGETS_SH_DIR="$HERE"
# shellcheck source=targets.sh
source "$HERE/targets.sh"
load_target "$TARGET" || exit 2

case "$CONDITION" in
  baseline|minyee|combined) ;;
  *) echo "Unknown condition '$CONDITION' (baseline | minyee | combined)" >&2; exit 2 ;;
esac

# ADAPTIVE: run Min Yee's Level-1 control loop (monitor + analyzer + mutator).
# AGENTAFL: additionally run the Level-2 AgentAFL escalation daemon, which reacts
# to the .agentafl_request her analyzer writes when a coverage adaptation fails to
# recover. `combined` is both; it is the arm the integrated pipeline is for.
ADAPTIVE=0; AGENTAFL=0
[[ "$CONDITION" == "minyee" || "$CONDITION" == "combined" ]] && ADAPTIVE=1
[[ "$CONDITION" == "combined" ]] && AGENTAFL=1

# The escalation hook inside run_analyzer.py is opt-in, so that a bare
# `python3 run_analyzer.py` (and her own run_six_experiments.sh) behaves exactly
# as it did before the hook existed. Set it EXPLICITLY for every condition rather
# than relying on that default: a `combined` run whose hook never fires would be
# statistically identical to `minyee` and the mistake would only surface at
# analysis time. Exported here because both run_analyzer.py (launched below) and
# agentafl_escalation.py must agree on it, and the daemon refuses to start when it
# is not 1.
export AGENTAFL_HOOK="$AGENTAFL"

# Sync cadence. AFL++ defaults to SYNC_TIME=20min, and a main node syncs at half
# that, so a seed handed to the campaign via afl-addseeds (which lands in
# $OUT/addseeds/queue and is imported by the main node's sync_fuzzers pass) would
# sit idle for up to 10 minutes - far longer than the ~25min escalation period,
# and long enough to blur which coverage came from an injection.
#
# Set on EVERY condition, not just `combined`, so the only difference between
# arms is whether seeds are injected at all. Changing it on one arm only would
# make the comparison against the committed baselines invalid.
#
# Overridable, because the committed Oct-13 runs had it UNSET. `AFL_SYNC_TIME=20`
# reproduces AFL++'s stock behaviour exactly: SYNC_TIME is 20*60*1000 (config.h)
# and the env parser computes time*60*1000 (afl-fuzz-state.c), so the two are the
# same value. Use that to reproduce the original fuzzer config.
export AFL_SYNC_TIME="${AFL_SYNC_TIME:-1}"

# afl-fuzz rewrites fuzzer_stats only every STATS_UPDATE_SEC (60s) by default, and
# run_analyzer.py DISCARDS any window whose execs_done has not changed since the
# last one ("Skipping stale AFL++ stats"). With monitor.py's 30s window that means
# roughly every other window is thrown away, so her effective plateau-detection
# granularity is 60s rather than the 30s the window implies - and every discarded
# window is one that cannot contribute to the stagnation count or to a recovery
# verdict. Measured locally at a 10s window with the default interval: 2 windows
# evaluated, 17 skipped.
#
# Publish stats at least as often as the monitor samples. Set for EVERY condition,
# like AFL_SYNC_TIME, so the arms differ only by the injection.
export AFL_FUZZER_STATS_UPDATE_INTERVAL="${AFL_FUZZER_STATS_UPDATE_INTERVAL:-10}"

MINYEE_DIR="$REPO/ResourceAwareAFL"
SIGNAL_FILE="$MINYEE_DIR/adaptation/.adapt_signal"
CALIB="${MINYEE_CALIBRATION:-8}"
PERSIST="${MINYEE_PERSISTENCE:-2}"
PIN_MODE="${MINYEE_PIN_MODE:-}"

# AgentAFL Level-2 knobs (condition=combined only). The 5x5 budget is not
# arbitrary: replayed over every stage4_both run in AgentAFL/prompt_tests/results3,
# the first 5 calls captured 90% (xml) / 81% (ical) of a 15-call run's new edges at
# the best edges-per-call of any budget retaining ~90% yield, and the single
# biggest-yield call fell within the first 5 in every xml and ical run.
#
# The cooldown is load-bearing, not a safety net. run_analyzer.py's Coverage Retry
# clears the stagnation latch whenever recovery yields zero edges, so
# COVERAGE_SLOWDOWN re-fires about every 5 windows (150s); replayed over her
# six_hour_results that is 98-212 opportunities per 12h. 1500s throttles it to the
# ~28 escalations the 5x5 budget assumes (~140 LLM calls, ~49min of LLM time).
AGENTAFL_CALLS="${AGENTAFL_CALLS:-5}"
AGENTAFL_SEEDS_PER_CALL="${AGENTAFL_SEEDS_PER_CALL:-5}"
AGENTAFL_COOLDOWN_S="${AGENTAFL_COOLDOWN_S:-1800}"
AGENTAFL_MAX_ESCALATIONS="${AGENTAFL_MAX_ESCALATIONS:-0}"   # 0 = unlimited
# openai, not claude: this experiment uses an OpenAI key, and the prompt_tests
# runs the 5x5 budget was measured on were themselves provider=openai (see the
# `provider` key in any prompt_tests/results3/*/stage4_both/*/summary_*.json).
# Defaulted rather than left to a per-VM env var because it has to be right on
# all 15 combined VMs, and getting it wrong means the key never resolves.
AGENTAFL_PROVIDER="${AGENTAFL_PROVIDER:-openai}"
# Pinned, not left to the provider default. batch_harness.py's openai default is
# gpt-5.4-mini, but every prompt_tests/results3 run that the 5x5 budget was
# measured on used gpt-5.6-luna (the `model` key in their summary_*.json). Using
# the SDK default would mean the "first 5 calls capture 90% (xml) / 81% (ical) of
# a 15-call run's new edges" evidence was gathered on a different model from the
# one actually generating seeds here.
AGENTAFL_MODEL="${AGENTAFL_MODEL:-gpt-5.6-luna}"
# Per-target, from targets.sh. These strings are NOT free text: there is no
# registry validating --fmt anywhere downstream (it is spliced straight into the
# prompt), so a typo degrades generation silently. The defaults are the exact
# values recorded in the `fmt` key of the prompt_tests/results3 runs the 5x5
# budget was derived from: "XML document", "iCalendar document", "HTML document".
AGENTAFL_FMT="${AGENTAFL_FMT:-$AGENTAFL_FMT_DEFAULT}"
case "$AGENTAFL_PROVIDER" in
  claude|openai) ;;
  *) echo "AGENTAFL_PROVIDER must be 'claude' or 'openai', got '$AGENTAFL_PROVIDER'" >&2; exit 2 ;;
esac
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

if [[ "$ADAPTIVE" == 1 ]]; then
  for f in monitor.py run_analyzer.py controller.py cea.py adaptive_mutator.py; do
    [[ -f "$MINYEE_DIR/$f" ]] || die "missing $MINYEE_DIR/$f"
  done
  if [[ "$AGENTAFL" == 1 ]]; then
    for f in agentafl_escalation.py agentafl_cycle.py; do
      [[ -f "$MINYEE_DIR/$f" ]] || die "missing $MINYEE_DIR/$f (condition=combined)"
    done
    # Without the hook in her analyzer nothing ever writes .agentafl_request, so
    # the daemon would idle for 12h and this arm would be identical to minyee.
    grep -q 'agentafl_request' "$MINYEE_DIR/run_analyzer.py" \
      || die "run_analyzer.py has no .agentafl_request hook - the escalation would never fire"
    command -v afl-addseeds >/dev/null \
      || die "afl-addseeds not on PATH - generated seeds could not be injected"
    command -v afl-showmap  >/dev/null || die "afl-showmap not on PATH"
    [[ -d "$REPO/AgentAFL/prompt_tests" ]] || die "missing $REPO/AgentAFL/prompt_tests"
    # Resolve the API key the same way AgentAFL does, so a missing one fails here
    # rather than on the first escalation hours in. Also counts how many keys are
    # available: a second one lets the campaign survive the first running out of
    # credits mid-run, which is otherwise a silent degradation to `minyee`.
    AGENTAFL_KEYS="$(python3 - "$REPO" "$AGENTAFL_PROVIDER" <<'PYKEY'
import sys, os, pathlib
repo, provider = sys.argv[1], sys.argv[2]
names = {"claude": ["CLAUDE_API_KEY"],
         "openai": ["OPENAI_API_KEY", "OPENAI_API_KEY_2", "OPENAI_API_KEY_3"]}[provider]
env = dict(os.environ)
# .env does not override an exported value, matching batch_harness/load_dotenv.
for f in (pathlib.Path(repo) / "AgentAFL" / ".env",
          pathlib.Path.home() / "Documents" / ".env"):
    try:
        for line in f.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except OSError:
        pass
seen, found = set(), []
for n in names:
    v = (env.get(n) or "").strip()
    if v and v not in seen:          # dedupe: the same key twice is one key
        seen.add(v)
        found.append(n)
print(len(found))
sys.exit(0 if found else 1)
PYKEY
)" || die "no API key for provider '$AGENTAFL_PROVIDER' (checked env, AgentAFL/.env, ~/Documents/.env)"
    if [[ "$AGENTAFL_PROVIDER" == "openai" && "${AGENTAFL_KEYS:-0}" -lt 2 ]]; then
      echo "[!] WARNING: only $AGENTAFL_KEYS OpenAI key configured - no failover."
      echo "[!] If it runs out of credits mid-run this campaign will STOP and the"
      echo "[!] cell will need re-running. Set OPENAI_API_KEY_2 in AgentAFL/.env."
    fi
  fi
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

if [[ "$ADAPTIVE" == 1 ]]; then
  ACTIVE="ResourceAwareAFL closed loop: monitor.py -> run_analyzer.py --calibration $CALIB --persistence $PERSIST -> controller.py -> adaptive_mutator.py"
  [[ -n "$PIN_MODE" ]] && ACTIVE="ResourceAwareAFL adaptive_mutator.py PINNED to $PIN_MODE (no analyzer - SMOKE TEST, not a campaign)"
  [[ "$AGENTAFL" == 1 ]] && ACTIVE="$ACTIVE; AgentAFL Level-2: .agentafl_request -> agentafl_escalation.py -> Stage 4 (${AGENTAFL_CALLS}x${AGENTAFL_SEEDS_PER_CALL}) -> afl-addseeds"
else
  ACTIVE="none (baseline)"
fi

SEED_COUNT="$(ls -1 "$SEEDS" | wc -l)"
# LC_ALL=C is load-bearing: `sort -z` collates by locale, so the SAME corpus
# hashes differently under en_US.UTF-8 than under C. Without it this "drift
# detector" changes when the machine changes and the corpus has not, which is
# worse than having none. The frozen libxml2 value 52974a6a2190a399 recorded in
# every committed baseline run only reproduces under C.
SEED_HASH="$(cd "$SEEDS" && LC_ALL=C bash -c \
  'find . -type f -print0 | sort -z | xargs -0 sha256sum | sha256sum | cut -c1-16')"
if [[ "$SEED_HASH" != "$SEEDS_SHA256_16" || "$SEED_COUNT" != "$SEEDS_COUNT" ]]; then
  echo "[!] WARNING: seed corpus does not match the frozen fingerprint for $TARGET"
  echo "[!]   expected $SEEDS_COUNT files / $SEEDS_SHA256_16"
  echo "[!]   got      $SEED_COUNT files / $SEED_HASH"
  echo "[!] Runs with different corpora are not comparable. Continuing anyway;"
  echo "[!] both values are recorded in meta.txt."
fi

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
  echo "target_cmd=$BIN ${FUZZ_ARGS[*]+"${FUZZ_ARGS[*]}"}"
  # Recorded separately because they legitimately differ for the persistent-mode
  # harnesses (no file arg under afl-fuzz, @@ under afl-showmap), and a mismatch
  # here is the difference between real per-seed coverage scoring and an
  # identical map for every candidate.
  echo "showmap_cmd=$BIN ${SHOWMAP_ARGS[*]+"${SHOWMAP_ARGS[*]}"}"
  echo "target_env=${TARGET_ENV[*]+"${TARGET_ENV[*]}"}"
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
  # Recorded for EVERY condition, not just combined. afl_env.txt only captures
  # ^AFL_|^ASAN_, so AGENTAFL_HOOK would otherwise go unrecorded - and whether
  # the hook was live is the difference between a minyee run and a crippled
  # combined one. afl_sync_time is here too because it is the one fuzzer setting
  # that differs from the committed Oct-13 runs.
  echo "agentafl_hook=$AGENTAFL_HOOK"
  echo "afl_sync_time=$AFL_SYNC_TIME  (AFL++ default is 20)"
  echo "afl_fuzzer_stats_update_interval=$AFL_FUZZER_STATS_UPDATE_INTERVAL  (AFL++ default is 60; must be <= monitor window or windows are discarded)"
  if [[ "$AGENTAFL" == 1 ]]; then
    echo "agentafl_calls=$AGENTAFL_CALLS"
    echo "agentafl_seeds_per_call=$AGENTAFL_SEEDS_PER_CALL"
    echo "agentafl_cooldown_s=$AGENTAFL_COOLDOWN_S"
    echo "agentafl_max_escalations=$AGENTAFL_MAX_ESCALATIONS"
    echo "agentafl_provider=$AGENTAFL_PROVIDER"
    # How many keys the failover had to work with. 1 means a mid-run credit
    # exhaustion stops the campaign; 2+ means it switches over and carries on.
    echo "agentafl_keys_available=${AGENTAFL_KEYS:-1}"
    echo "agentafl_model=${AGENTAFL_MODEL:-<provider default>}"
    echo "agentafl_fmt=$AGENTAFL_FMT"
    echo "agentafl_request_file=$MINYEE_DIR/adaptation/.agentafl_request"
  fi
  if [[ "$ADAPTIVE" == 1 ]]; then
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
if [[ "$ADAPTIVE" == 1 ]]; then
  export AFL_PYTHON_MODULE=adaptive_mutator   # exported, so it lands in afl_env.txt
  ENVPFX=(env "PYTHONPATH=$MINYEE_DIR")
fi
env | grep -E '^(AFL_|ASAN_)' | sort > "$RUN/afl_env.txt"
# PYTHONPATH is not an AFL_/ASAN_ var, so the snapshot above misses it.
[[ "$ADAPTIVE" == 1 ]] && echo "PYTHONPATH=$MINYEE_DIR" >> "$RUN/afl_env.txt"

launch() {  # name  extra-args...
  local name="$1"; shift
  setsid nohup "${ENVPFX[@]+"${ENVPFX[@]}"}" afl-fuzz -i "$SEEDS" -o "$OUT" -V "$DUR" -m none -t 1000 \
    "$@" -- "$BIN" ${FUZZ_ARGS[@]+"${FUZZ_ARGS[@]}"} > "$RUN/logs/$name.log" 2>&1 &
  echo $! > "$RUN/pids/$name.pid"
  echo "  $name (pid $!)"
}

# ------------------------------------------------- adaptation signal + analyzer
# Started BEFORE the fuzzers on purpose. run_analyzer.py constructs
# AdaptiveFuzzingController (which writes MODE=DEFAULT) before it waits for
# raw_metrics.json, so starting it first guarantees the signal is DEFAULT
# before the mutator's first fuzz_count() instead of racing a stale mode left
# behind by a previous campaign.
if [[ "$ADAPTIVE" == 1 ]]; then
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
elif [[ "$ADAPTIVE" == 1 ]]; then
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
if [[ "$ADAPTIVE" == 1 && -z "$PIN_MODE" ]]; then
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
if [[ "$ADAPTIVE" == 1 ]]; then
  setsid nohup bash "$HERE/signal_log.sh" "$SIGNAL_FILE" "$RUN/adapt_signal_log.csv" 5 \
    > "$RUN/logs/signal_log.log" 2>&1 &
  echo $! > "$RUN/pids/signal_log.pid"
fi

# AgentAFL Level-2 escalation. Passive like the loggers above: it only reacts to
# the .agentafl_request her analyzer writes when a coverage adaptation failed to
# recover, so it cannot stall the fuzzers or her control loop. Deliberately NOT
# called from inside run_analyzer.py - that loop advances on lines of
# raw_metrics.json, so a ~105s LLM call there would let the windows describing the
# call itself be consumed as the post-injection recovery evidence.
if [[ "$AGENTAFL" == 1 ]]; then
  # -u the mutator vars: this script exports AFL_PYTHON_MODULE for the fuzzers,
  # but afl-showmap and afl-addseeds honour it too and PROGRAM ABORT when they
  # cannot import the module (PYTHONPATH is scoped to the fuzzers only). That
  # failure is silent - build_baseline ignores afl-showmap's exit code, reads an
  # empty map and reports an empty baseline - so the arm would disable its own
  # escalation and quietly equal `minyee` for the whole run. The daemon also
  # strips these itself; both, because this one is cheap and the cost of missing
  # it is a wasted 12h campaign.
  setsid nohup env -u AFL_PYTHON_MODULE -u AFL_CUSTOM_MUTATOR_LIBRARY \
    -u AFL_CUSTOM_MUTATOR_ONLY -u AFL_NO_PYTHON \
    PYTHONIOENCODING=utf-8 LC_ALL=C.UTF-8 \
    python3 -u "$MINYEE_DIR/agentafl_escalation.py" \
      --campaign-root "$OUT" \
      --work-dir "$RUN/agentafl" \
      --request-file "$MINYEE_DIR/adaptation/.agentafl_request" \
      --target "$BIN" \
      --target-args "${SHOWMAP_ARGS[*]+"${SHOWMAP_ARGS[*]}"}" \
      --probe-input "$SANITY_INPUT" \
      --instance main \
      --instances "$INSTANCES" \
      --fmt "$AGENTAFL_FMT" \
      --calls "$AGENTAFL_CALLS" \
      --seeds-per-call "$AGENTAFL_SEEDS_PER_CALL" \
      --cooldown-s "$AGENTAFL_COOLDOWN_S" \
      --max-escalations "$AGENTAFL_MAX_ESCALATIONS" \
      --provider "$AGENTAFL_PROVIDER" \
      ${AGENTAFL_MODEL:+--model "$AGENTAFL_MODEL"} \
    > "$RUN/logs/agentafl_escalation.log" 2>&1 &
  echo $! > "$RUN/pids/agentafl_escalation.pid"
  echo "  agentafl_escalation (pid $!)"
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
if [[ "$ADAPTIVE" == 1 ]]; then
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