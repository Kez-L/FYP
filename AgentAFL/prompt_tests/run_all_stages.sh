#!/usr/bin/env bash
# run_all_stages.sh — one run-through of every ablation stage, xml then ical
# then html.
#
# Each invocation of run_build_context.py picks the next free run number
# (_next_run_number), so every stage gets a fresh
# results3/<fmt>/<stage>/<stage>_run_N_gpt-5-6-luna/ folder. Nothing existing
# is overwritten or deleted.
#
# The stage list is hard-coded (not discovered from results3/). Commands are
# copied from commands.md. Deliberately left out: Stage 1 Raw
# (--seed-selector default) for every format.
#
# Usage:
#   bash run_all_stages.sh                 # run all 27 stages sequentially
#   bash run_all_stages.sh --dry-run       # print the commands only (no API calls)
#   bash run_all_stages.sh --only html     # just one format (xml|ical|html)
#   bash run_all_stages.sh --skip-eval     # any other args are passed through
#                                          # to every run_build_context.py call
#
# Prerequisite for html: results/baseline_edges_tidy.json must exist — freeze it
# once with freeze_baseline_only.py (no API calls). The sweep refuses to start
# an html stage without it rather than re-scan 46,970 queue files nine times.
#
# Out of credits/quota: the sweep stops immediately (exit 3) and prints which
# stage hit it; other stage failures are recorded and the sweep continues.
#
# Interpreter: $PYTHON if set, else ../.venv/bin/python (system python3 has no
# openai module).

set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"  # --reuse-baseline results/... below is relative to prompt_tests/

PYTHON="${PYTHON:-$HERE/../.venv/bin/python}"

DRY_RUN=0
ONLY=""
STAGES_FILTER=""
PASSTHRU=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=1; shift ;;
        --only)    ONLY="${2:-}"; shift 2 ;;
        --stages)  STAGES_FILTER="${2:-}"; shift 2 ;;
        *)         PASSTHRU+=("$1"); shift ;;
    esac
done
case "$ONLY" in
    ""|xml|ical|html) ;;
    *) echo "--only must be xml, ical or html" >&2; exit 2 ;;
esac

OUT_OF_CREDITS_RE="insufficient_quota|credit_balance_exhausted|no credits remaining|exceeded your current quota|credit balance is too low|billing_hard_limit_reached|OUT OF CREDITS"

COMMON=(--provider openai --model gpt-5.6-luna --temperature 1.0)

ICAL_ARGS=(
    --format-dir ical
    --campaign-root /home/user/Documents/1git-folder/AgentAFL/afl-output-libical-20260915
    --target /home/user/Documents/1git-folder/AgentAFL/AFLPlus/libical-build/libical_fuzzer-afl
    --target-args "@@"
    --fmt "iCalendar document"
    --reuse-baseline results/baseline_edges_libical.json
)

# Same harness shape as libical (whole input = one document), so html stays
# comparable to ical. --target-args "@@" is required, not cosmetic: outside
# afl-fuzz the libAFLDriver harness reads a file argument, and plain stdin
# silently parses an EMPTY document. --reuse-baseline is also not optional —
# run_build_context.py would otherwise fall back to the *xml* baseline.
HTML_ARGS=(
    --format-dir html
    --campaign-root /home/user/Documents/1git-folder/AgentAFL/afl-output-tidy-20261006
    --target /home/user/Documents/1git-folder/AgentAFL/AFLPlus/tidy-html5-build/tidy_parse_string_fuzzer-afl
    --target-args "@@"
    --fmt "HTML document"
    --reuse-baseline results/baseline_edges_tidy.json
)

# "stage folder|stage-specific flags" — same list for every format.
STAGES=(
    "stage1_jaccard|--seed-selector stage1-jaccard"
    "stage1_jaccard_nofeedback|--seed-selector stage1-jaccard --no-feedback"
    "stage2_coverage|--seed-selector coverage"
    "stage2_coverage_per_byte|--seed-selector coverage-per-byte"
    "stage2_rare_coverage|--seed-selector rare-coverage"
    "stage4_negative_only|--seed-selector no-seed --label stage4_negative_only"
    "stage4_positive_only|--seed-selector coverage --positive-feedback --no-feedback"
    "stage4_both|--seed-selector coverage --positive-feedback"
    "stage4_negative_bootstrap1|--seed-selector coverage --good-seeds-first-round-only --label stage4_negative_bootstrap1"
)

# --stages narrows the sweep to the named stage folders, in the order they
# appear in STAGES above (not the order you list them). Use it to resume an
# aborted sweep: pass only the stages that are short a run. An unknown name is
# a hard error, so a typo can't silently run nothing.
if [[ -n "$STAGES_FILTER" ]]; then
    IFS=',' read -r -a want <<< "$STAGES_FILTER"
    for w in "${want[@]}"; do
        found=0
        for entry in "${STAGES[@]}"; do
            [[ "${entry%%|*}" == "$w" ]] && { found=1; break; }
        done
        if [[ $found -eq 0 ]]; then
            echo "unknown stage: $w" >&2
            echo "known stages:" >&2
            printf '  %s\n' "${STAGES[@]%%|*}" >&2
            exit 2
        fi
    done
    filtered=()
    for entry in "${STAGES[@]}"; do
        for w in "${want[@]}"; do
            [[ "${entry%%|*}" == "$w" ]] && { filtered+=("$entry"); break; }
        done
    done
    STAGES=("${filtered[@]}")
fi

FORMATS=(xml ical html)
[[ -n "$ONLY" ]] && FORMATS=("$ONLY")

# html has no baseline checked into results/ the way xml and ical do. Without it
# freeze_baseline would re-scan 46,970 queue files at the start of every one of
# the 9 html stages, so fail loudly here instead.
HTML_BASELINE="$HERE/results/baseline_edges_tidy.json"
if [[ $DRY_RUN -eq 0 ]] && printf '%s\n' "${FORMATS[@]}" | grep -qx html; then
    if [[ ! -f "$HTML_BASELINE" ]]; then
        echo "html corpus baseline missing: $HTML_BASELINE" >&2
        echo >&2
        echo "Freeze it once first (no API calls, one afl-showmap pass):" >&2
        echo >&2
        echo "  $PYTHON freeze_baseline_only.py \\" >&2
        echo "      --campaign-root /home/user/Documents/1git-folder/AgentAFL/afl-output-tidy-20261006 \\" >&2
        echo "      --target /home/user/Documents/1git-folder/AgentAFL/AFLPlus/tidy-html5-build/tidy_parse_string_fuzzer-afl \\" >&2
        echo "      --target-args \"@@\" \\" >&2
        echo "      --out results/baseline_edges_tidy.json" >&2
        exit 2
    fi
fi

STAMP="$(date +%Y%m%dT%H%M%S)"
LOG_DIR="$HERE/run_logs/$STAMP"
if [[ $DRY_RUN -eq 0 ]]; then
    if [[ ! -x "$PYTHON" ]]; then
        echo "python interpreter not found: $PYTHON (set PYTHON=...)" >&2; exit 2
    fi
    mkdir -p "$LOG_DIR"
fi

RESULTS=()
FAILED=0
START_ALL=$SECONDS
TOTAL=$(( ${#FORMATS[@]} * ${#STAGES[@]} ))
N=0

for fmt in "${FORMATS[@]}"; do
    for entry in "${STAGES[@]}"; do
        N=$(( N + 1 ))
        stage="${entry%%|*}"
        read -r -a flags <<< "${entry#*|}"
        cmd=("$PYTHON" run_build_context.py "${COMMON[@]}")
        # xml needs no extra args — it is run_build_context.py's own default.
        case "$fmt" in
            ical) cmd+=("${ICAL_ARGS[@]}") ;;
            html) cmd+=("${HTML_ARGS[@]}") ;;
        esac
        cmd+=("${flags[@]}" "${PASSTHRU[@]}")

        if [[ $DRY_RUN -eq 1 ]]; then
            echo "# [$N/$TOTAL] $fmt / $stage"
            printf '%q ' "${cmd[@]}"; echo; echo
            continue
        fi

        log="$LOG_DIR/${fmt}_${stage}.log"
        echo "=================================================================="
        echo "[$(date +%H:%M:%S)] stage $N/$TOTAL: $fmt / $stage"
        echo "log: $log"
        echo "=================================================================="
        t0=$SECONDS
        "${cmd[@]}" 2>&1 | tee "$log"
        rc=${PIPESTATUS[0]}
        dt=$(( SECONDS - t0 ))
        # Out of credits: batch_harness exits with OUT_OF_CREDITS_EXIT (3); the
        # grep also catches quota errors raised outside the main call loop
        # (e.g. an uncaught traceback). Every later stage would fail the same
        # way, so stop the whole sweep here.
        if [[ $rc -eq 3 ]] || grep -qiE "$OUT_OF_CREDITS_RE" "$log"; then
            RESULTS+=("$(printf '%-5s %-28s OUT OF CREDITS %5ds' "$fmt" "$stage" "$dt")")
            echo
            echo "##################################################################"
            echo "STOPPED: out of API credits/quota"
            echo "  failed stage : $fmt / $stage ($N/$TOTAL)"
            echo "  stage log    : $log"
            echo "  its run folder (partial, no summary) is the newest one in:"
            echo "                 $HERE/results3/$fmt/$stage/"
            echo "  remaining stages were NOT run."
            echo "##################################################################"
            echo
            printf '%s\n' "${RESULTS[@]}"
            exit 3
        fi
        if [[ $rc -eq 0 ]]; then
            RESULTS+=("$(printf '%-5s %-28s OK        %5ds' "$fmt" "$stage" "$dt")")
        else
            RESULTS+=("$(printf '%-5s %-28s FAILED(%s) %5ds' "$fmt" "$stage" "$rc" "$dt")")
            FAILED=$(( FAILED + 1 ))
        fi
    done
done

[[ $DRY_RUN -eq 1 ]] && exit 0

echo
echo "=================================================================="
echo "Summary ($(( (SECONDS - START_ALL) / 60 )) min total, logs in $LOG_DIR)"
echo "=================================================================="
printf '%s\n' "${RESULTS[@]}"
[[ $FAILED -gt 0 ]] && { echo "$FAILED stage(s) failed"; exit 1; }
exit 0
