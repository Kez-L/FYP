
#!/usr/bin/env bash
set -Eeuo pipefail

# ==========================================================
# Six Independent AFL++ Experiments - libxml2
#
# Adaptive 1  -> 6 hours
# Baseline 1  -> 6 hours
# Adaptive 2  -> 6 hours
# Baseline 2  -> 6 hours
# Adaptive 3  -> 6 hours
# Baseline 3  -> 6 hours
#
# Adaptive: Calibration = 8, Persistence = 2
# ==========================================================

PROJECT="$HOME/FYP/ResourceAwareAFL"
cd "$PROJECT"

DURATION="${DURATION:-6h}"
RESULT_ROOT="${RESULT_ROOT:-$PROJECT/six_hour_results}"

TARGET="$PROJECT/libxml2-afl-src/xmllint"
INPUT="$PROJECT/in_libxml2"

mkdir -p "$RESULT_ROOT"

# ----------------------------------------------------------
# 1. Validate required files
# ----------------------------------------------------------

for file in \
    monitor.py \
    run_analyzer.py \
    controller.py \
    adaptive_mutator.py \
    cea.py
do
    if [[ ! -f "$PROJECT/$file" ]]; then
        echo "ERROR: Missing $file"
        exit 1
    fi
done

if [[ ! -x "$TARGET" ]]; then
    echo "ERROR: xmllint executable not found: $TARGET"
    exit 1
fi

if [[ ! -d "$INPUT" ]]; then
    echo "ERROR: Input corpus not found: $INPUT"
    exit 1
fi

# ----------------------------------------------------------
# 2. Experiment configuration
# ----------------------------------------------------------

MODES=(
    adaptive
    baseline
    adaptive
    baseline
    adaptive
    baseline
)

RUNS=(1 1 2 2 3 3)

ACTIVE_PIDS=()

cleanup() {
    local pid

    for pid in "${ACTIVE_PIDS[@]}"; do
        if kill -0 "$pid" 2>/dev/null; then
            kill -INT -- "-$pid" 2>/dev/null || true
        fi
    done

    sleep 2

    for pid in "${ACTIVE_PIDS[@]}"; do
        kill -TERM -- "-$pid" 2>/dev/null || true
    done

    for pid in "${ACTIVE_PIDS[@]}"; do
        wait "$pid" 2>/dev/null || true
    done

    ACTIVE_PIDS=()
}

trap 'cleanup; exit 130' INT TERM
trap 'cleanup' EXIT

# ----------------------------------------------------------
# 3. Run experiments
# ----------------------------------------------------------

for idx in "${!MODES[@]}"; do

    mode="${MODES[$idx]}"
    run="${RUNS[$idx]}"

    name="${mode}_run_0${run}"
    work="$RESULT_ROOT/$name"

    if [[ -e "$work" ]]; then
        echo "ERROR: $work already exists."
        echo "Results will not be overwritten."
        exit 1
    fi

    mkdir -p "$work"

    echo "============================================"
    echo "Preparing experiment: $name"
    echo "============================================"

    # Copy Python implementation and input seeds
    cp -a "$PROJECT"/*.py "$work/"
    cp -a "$INPUT" "$work/in_libxml2"

    # Isolate adaptation state
    mkdir -p "$work/adaptation"
    rm -f "$work/adaptation/.adapt_signal"

    cd "$work"
    export PYTHONPATH="$work"

    echo "$(date -Is)" > start_time.txt
    echo "$mode" > experiment_mode.txt

    # ------------------------------------------------------
    # 4. Start AFL++
    # ------------------------------------------------------

    if [[ "$mode" == "adaptive" ]]; then

        setsid timeout -s INT -k 30s "$DURATION" \
            env \
                AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES=1 \
                AFL_NO_UI=1 \
                AFL_PYTHON_MODULE=adaptive_mutator \
            afl-fuzz \
                -i in_libxml2 \
                -o out \
                -- "$TARGET" --noout @@ \
            > afl.log 2>&1 &

    else

        setsid timeout -s INT -k 30s "$DURATION" \
            env -u AFL_PYTHON_MODULE \
                AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES=1 \
                AFL_NO_UI=1 \
            afl-fuzz \
                -i in_libxml2 \
                -o out \
                -- "$TARGET" --noout @@ \
            > afl.log 2>&1 &

    fi

    fuzz_pid=$!
    ACTIVE_PIDS+=("$fuzz_pid")

    sleep 5

    if ! kill -0 "$fuzz_pid" 2>/dev/null; then
        echo "ERROR: AFL++ failed to start in $name"
        tail -40 afl.log
        exit 1
    fi

    # ------------------------------------------------------
    # 5. Start monitor
    # ------------------------------------------------------

    setsid python3 -u monitor.py \
        > monitor.log 2>&1 &

    monitor_pid=$!
    ACTIVE_PIDS+=("$monitor_pid")

    # ------------------------------------------------------
    # 6. Start adaptive analyzer
    # ------------------------------------------------------

    analyzer_pid=""

    if [[ "$mode" == "adaptive" ]]; then

        setsid python3 -u run_analyzer.py \
            --calibration 8 \
            --persistence 2 \
            > analyzer.log 2>&1 &

        analyzer_pid=$!
        ACTIVE_PIDS+=("$analyzer_pid")

    fi

    sleep 5

    # Check monitor startup
    if ! kill -0 "$monitor_pid" 2>/dev/null; then
        echo "ERROR: Monitor stopped unexpectedly."
        tail -40 monitor.log
        exit 1
    fi

    # Check analyzer startup
    if [[ "$mode" == "adaptive" ]]; then
        if ! kill -0 "$analyzer_pid" 2>/dev/null; then
            echo "ERROR: Analyzer stopped unexpectedly."
            tail -40 analyzer.log
            exit 1
        fi
    fi

    # ------------------------------------------------------
    # 7. Wait for fuzzing to finish
    # ------------------------------------------------------

    echo "[$(date)] Running $name for $DURATION"

    # Poll processes so monitor/analyzer failures are detected
    while kill -0 "$fuzz_pid" 2>/dev/null; do

        if ! kill -0 "$monitor_pid" 2>/dev/null; then
            echo "ERROR: Monitor died during $name"
            tail -40 monitor.log
            exit 1
        fi

        if [[ "$mode" == "adaptive" ]]; then
            if ! kill -0 "$analyzer_pid" 2>/dev/null; then
                echo "ERROR: Analyzer died during $name"
                tail -40 analyzer.log
                exit 1
            fi
        fi

        sleep 10

    done

    set +e
    wait "$fuzz_pid"
    status=$?
    set -e

    echo "$status" > exit_status.txt

    # ------------------------------------------------------
    # 8. Stop monitoring and clean up
    # ------------------------------------------------------

    cleanup

    echo "$(date -Is)" > end_time.txt

    # Timeout normally returns 124
    if [[ "$status" -ne 0 && "$status" -ne 124 ]]; then
        echo "ERROR: AFL++ exited with status $status"
        tail -40 afl.log
        exit 1
    fi

    if [[ ! -f out/default/fuzzer_stats ]]; then
        echo "ERROR: AFL++ statistics missing in $name"
        exit 1
    fi

    echo "Completed: $name"
    echo "Output: $work"
    echo

    cd "$PROJECT"

done

echo "============================================"
echo "ALL SIX EXPERIMENTS COMPLETED"
echo "============================================"
echo "Results saved at: $RESULT_ROOT"
