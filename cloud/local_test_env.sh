# local_test_env.sh - shared paths for a LOCAL test of the `combined` arm.
#
#   source cloud/local_test_env.sh
#
# Source this in every terminal, then run that terminal's one command. This is a
# functional test of the escalation loop on your own machine, NOT a campaign:
# the monitor window and the analyzer's calibration/persistence are shortened so
# a trigger can fire in ~2 minutes instead of ~6, and the LLM budget is cut.
# Nothing here is comparable to a cloud run - use run_campaign.sh for that.

# This file only works when SOURCED: its whole job is to export into your shell.
# Running it (./local_test_env.sh) or piping it (source ... | tail) puts the
# exports in a subshell that then exits, leaving $TEST and friends unset in the
# terminal you are actually using. The failure is nasty rather than obvious:
# `mkdir -p "$TEST/logs"` becomes `mkdir -p /logs`, and afl-fuzz is launched with
# `-o ""`. Catch it here instead.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  echo "local_test_env.sh must be SOURCED, not executed:" >&2
  echo "    source cloud/local_test_env.sh" >&2
  exit 1
fi

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export REPO
export MINYEE="$REPO/ResourceAwareAFL"
export AGENT="$REPO/AgentAFL"

# Scratch, outside the repo, so a test can never be mistaken for real results.
export TEST="${TEST:-$HOME/fyp-test}"
export OUT="$TEST/out"            # afl-fuzz -o  (parent of main/ sec1/ sec2/)
export WORKDIR="$TEST/monitor"    # monitor.py + run_analyzer.py cwd
export AGENTWORK="$TEST/agentafl" # history/, escalations/, timeline csv

export TARGET="$MINYEE/xmllint-afl"
export SEEDS="$MINYEE/seeds-libxml2"
export REQUEST="$MINYEE/adaptation/.agentafl_request"
export SIGNAL="$MINYEE/adaptation/.adapt_signal"

# monitor.py needs psutil. Preferred: sudo apt-get install -y python3-psutil
# (that is what cloud/setup_machine.sh does). If you cannot use sudo, set
# PYLIB to a pip --target dir and it is added to PYTHONPATH for the monitor only.
export PYLIB="${PYLIB:-}"

# Shortened timers - test only. Real campaign values are 30 / 8 / 2.
export WINDOW_SEC="${WINDOW_SEC:-10}"
export CALIB="${CALIB:-3}"
export PERSIST="${PERSIST:-2}"

# afl-fuzz only rewrites fuzzer_stats every STATS_UPDATE_SEC (60s) by default, and
# run_analyzer.py DISCARDS any window whose execs_done has not changed since the
# last one ("Skipping stale AFL++ stats"). With a 10s window that throws away ~5 of
# every 6 windows and a trigger may never fire. Measured here: 2 windows evaluated
# vs 17 skipped. So make the fuzzers publish stats at least as often as the window.
# NOTE this also applies to the real 30s campaign window, where every other window
# is discarded - the effective plateau-detection granularity there is 60s, not 30s.
export AFL_FUZZER_STATS_UPDATE_INTERVAL="${AFL_FUZZER_STATS_UPDATE_INTERVAL:-5}"

# The hook in run_analyzer.py is opt-in. Exported here because the whole point of
# this local test is to exercise the arm, and because BOTH the analyzer terminal
# and the daemon terminal need to agree on it - sourcing this file in each is what
# keeps them in step. Set AGENTAFL_HOOK=0 to test Min Yee's layer on its own, as
# `condition=minyee` does (then the daemon needs --allow-hook-off, or just do not
# start it).
export AGENTAFL_HOOK="${AGENTAFL_HOOK:-1}"

# Small, cheap LLM budget for a test. Real values are 5 / 5 / 1500.
export AF_CALLS="${AF_CALLS:-2}"
export AF_SEEDS="${AF_SEEDS:-3}"
export AF_COOLDOWN="${AF_COOLDOWN:-60}"

mkdir -p "$OUT" "$WORKDIR" "$AGENTWORK" "$MINYEE/adaptation"

echo "REPO     $REPO"
echo "TEST     $TEST"
echo "OUT      $OUT"
echo "TARGET   $TARGET"
echo "timers   window=${WINDOW_SEC}s calibration=$CALIB persistence=$PERSIST"
echo "budget   ${AF_CALLS} calls x ${AF_SEEDS} seeds, cooldown ${AF_COOLDOWN}s"
echo "hook     AGENTAFL_HOOK=$AGENTAFL_HOOK $([ "$AGENTAFL_HOOK" = 1 ] \
  && echo '(escalation enabled)' || echo '(minyee-only: her layer as before)')"
