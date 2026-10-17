#!/bin/bash
# stop_local_combined.sh - shut down everything run_local_combined.sh started.
#
#   ./stop_local_combined.sh            # stop, keep results in $TEST
#   ./stop_local_combined.sh --clean    # stop, then delete $TEST entirely
#
# Order matters, same reasoning as cloud/finish_campaign.sh:
#   agentafl first  - once it is gone nothing can inject into a campaign that is
#                     being torn down; SIGTERM lets an in-flight escalation
#                     finish rather than losing that round's seeds.
#   analyzer next   - run_analyzer.py is a `while True` with every exception
#                     caught inside the loop, so it NEVER exits on its own and
#                     would survive to fight the next run over .adapt_signal.
#   then monitor, then the fuzzers.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=local_test_env.sh
source "$HERE/local_test_env.sh" >/dev/null

PIDS="$TEST/pids"

stop_one() {  # name  signal
  local name="$1" sig="${2:-TERM}" f="$PIDS/$1.pid" pid
  [[ -f "$f" ]] || return 0
  pid="$(cat "$f" 2>/dev/null || true)"
  [[ -n "$pid" ]] || return 0
  if kill -0 "$pid" 2>/dev/null; then
    kill "-$sig" "$pid" 2>/dev/null && echo "[*] stopped $name (pid $pid)"
  fi
  rm -f "$f"
}

for p in agentafl analyzer monitor; do stop_one "$p"; done
# Give an in-flight escalation a moment to finish writing its record.
sleep 3
for p in main sec1 sec2; do stop_one "$p"; done

# Anything that escaped its pidfile - started by hand from LOCAL_TEST.md, or
# launched via `setsid`, where $! is setsid's pid and may already be gone by the
# time we read it.
#
# SIGTERM first, SIGKILL only if it ignores that: agentafl_escalation.py handles
# SIGTERM by finishing the escalation in flight and then exiting, so a -9 here
# would throw away that round's seeds and its escalations.jsonl record.
sleep 2
for pat in 'agentafl_escalation\.py' 'run_analyzer\.py' 'AFLRuntimeMonitor'; do
  for pid in $(pgrep -f "$pat" 2>/dev/null || true); do
    # Never signal our own shell, which has the pattern in its command line.
    [[ "$pid" == "$$" || "$pid" == "$PPID" ]] && continue
    kill -TERM "$pid" 2>/dev/null && echo "[*] signalled stray $pat (pid $pid)"
  done
done
sleep 3
for pat in 'agentafl_escalation\.py' 'run_analyzer\.py' 'AFLRuntimeMonitor'; do
  for pid in $(pgrep -f "$pat" 2>/dev/null || true); do
    [[ "$pid" == "$$" || "$pid" == "$PPID" ]] && continue
    kill -9 "$pid" 2>/dev/null && echo "[*] force-killed $pat (pid $pid)"
  done
done
pkill -x afl-fuzz 2>/dev/null && echo "[*] killed remaining afl-fuzz" || true

sleep 1
left="$(ps -eo args --no-headers \
  | grep -E 'afl-fuzz|run_analyzer\.py|AFLRuntimeMonitor|agentafl_escalation\.py' \
  | grep -v grep | wc -l)"
if [[ "$left" -eq 0 ]]; then
  echo "[+] all stopped"
else
  echo "[!] $left process(es) still alive:"
  ps -eo pid,args --no-headers \
    | grep -E 'afl-fuzz|run_analyzer\.py|AFLRuntimeMonitor|agentafl_escalation\.py' \
    | grep -v grep | sed 's/^/    /'
fi

# These live in the repo checkout and are shared by every run on the machine, so
# a leftover request would fire a spurious escalation at the next daemon start.
rm -f "$MINYEE/adaptation/.adapt_signal" \
      "$MINYEE/adaptation/.agentafl_request" \
      "$MINYEE/adaptation/.agentafl_request.tmp"
echo "[+] cleared adaptation/ runtime files"

if [[ "${1:-}" == "--clean" ]]; then
  rm -rf "$TEST"
  echo "[+] removed $TEST"
else
  echo "[=] results kept in $TEST (./stop_local_combined.sh --clean to remove)"
fi
