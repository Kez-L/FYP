#!/bin/bash
# preflight_agentafl.sh - run once per machine before any `combined` campaign.
#
#   ./preflight_agentafl.sh
#
# Checks everything the AgentAFL Level-2 escalation needs that
# preflight_resource_aware.sh does not already cover. Every check here guards a
# failure that is SILENT at runtime: the campaign would fuzz happily for 12h and
# the arm would be statistically identical to `minyee`, with nothing in the logs
# saying why.
#
# Does NOT make an LLM call (that costs money); it verifies a key resolves.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="${REPO:-$(cd "$HERE/.." && pwd)}"
MINYEE_DIR="$REPO/ResourceAwareAFL"
AGENT_DIR="$REPO/AgentAFL"
PROVIDER="${AGENTAFL_PROVIDER:-claude}"
TARGET="${TARGET:-$REPO/../targets/libxml2-afl/xmllint}"
[[ -x "$TARGET" ]] || TARGET="$MINYEE_DIR/xmllint-afl"

PASS=0; FAIL=0
ok()   { echo "  [+] $1"; PASS=$((PASS+1)); }
bad()  { echo "  [-] $1"; FAIL=$((FAIL+1)); }
note() { echo "      $1"; }

echo "== files"
for f in agentafl_escalation.py agentafl_cycle.py run_analyzer.py controller.py; do
  [[ -f "$MINYEE_DIR/$f" ]] && ok "$f" || bad "missing $MINYEE_DIR/$f"
done
[[ -d "$AGENT_DIR/prompt_tests" ]] && ok "AgentAFL/prompt_tests" \
  || bad "missing $AGENT_DIR/prompt_tests"

echo "== the hook (without it nothing ever escalates)"
if grep -q 'agentafl_request' "$MINYEE_DIR/run_analyzer.py" 2>/dev/null; then
  ok "run_analyzer.py writes .agentafl_request"
  grep -q 'COVERAGE_SLOWDOWN' "$MINYEE_DIR/run_analyzer.py" \
    && ok "hook is gated on COVERAGE_SLOWDOWN" \
    || bad "hook is not gated on COVERAGE_SLOWDOWN - would fire on resource spikes too"
  # The hook is opt-in, so the gate and its export are both load-bearing. A hook
  # with no gate silently changes a minyee run; a gate with no export silently
  # makes a combined run equal to minyee. Both are invisible at runtime.
  if grep -q 'AGENTAFL_HOOK' "$MINYEE_DIR/run_analyzer.py"; then
    ok "hook is opt-in via AGENTAFL_HOOK (minyee/baseline runs stay pristine)"
  else
    bad "hook has NO AGENTAFL_HOOK gate - it would fire in every condition"
    note "a minyee run would write .agentafl_request, unlike her committed code"
  fi
  if grep -q 'export AGENTAFL_HOOK=' "$HERE/run_campaign.sh"; then
    ok "run_campaign.sh exports AGENTAFL_HOOK per condition"
  else
    bad "run_campaign.sh does not export AGENTAFL_HOOK"
    note "a 'combined' run would never escalate and would equal 'minyee'"
  fi
else
  bad "run_analyzer.py has NO .agentafl_request hook"
  note "the daemon would idle all run and 'combined' would equal 'minyee'"
fi

echo "== AFL++ tools"
command -v afl-showmap  >/dev/null && ok "afl-showmap"  || bad "afl-showmap not on PATH"
if command -v afl-addseeds >/dev/null; then
  ok "afl-addseeds"
else
  bad "afl-addseeds not on PATH - seeds could never be injected"
  note "it ships with AFL++ >= 4.x; check the AFL++ install"
fi

echo "== python imports (stdlib only: AgentAFL's Claude path uses urllib)"
if python3 -c "
import sys; sys.path.insert(0, '$MINYEE_DIR')
import agentafl_cycle, agentafl_escalation
print('   modules import clean')
" 2>&1 | grep -q "import clean"; then
  ok "agentafl_cycle + agentafl_escalation import"
else
  bad "import failed:"
  python3 -c "
import sys; sys.path.insert(0, '$MINYEE_DIR')
import agentafl_cycle" 2>&1 | tail -5 | sed 's/^/      /'
fi

echo "== API key for provider '$PROVIDER'"
if python3 - "$AGENT_DIR" "$PROVIDER" <<'PYKEY'
import sys, os, pathlib
agent, provider = sys.argv[1], sys.argv[2]
name = {"claude": "CLAUDE_API_KEY", "openai": "OPENAI_API_KEY"}.get(provider)
if not name:
    sys.exit(2)
if os.environ.get(name):
    print("env")
    sys.exit(0)
for f in (pathlib.Path(agent) / ".env", pathlib.Path.home() / "Documents" / ".env"):
    try:
        for line in f.read_text().splitlines():
            k, _, v = line.partition("=")
            if k.strip() == name and v.strip():
                print(f)
                sys.exit(0)
    except OSError:
        pass
sys.exit(1)
PYKEY
then ok "key resolves"; else bad "no key found (env, AgentAFL/.env, ~/Documents/.env)"; fi

echo "== target + coverage measurement"
if [[ -x "$TARGET" ]]; then
  ok "target $TARGET"
  if afl-showmap -q -o /dev/null -- "$TARGET" --noout "$MINYEE_DIR/test_libxml.xml" >/dev/null 2>&1; then
    ok "afl-showmap produces a map for the target"
  else
    bad "afl-showmap failed on the target - every seed would score 0 new edges"
  fi
else
  bad "no instrumented target found (set TARGET=...)"
fi

echo "== sync plumbing"
# A campaign started without -M/-S is a SECONDARY and skips any sync dir lacking
# is_main_node, so afl-addseeds would import nothing at all. run_campaign.sh always
# uses -M main; this only matters for hand-rolled local runs.
note "run_campaign.sh launches '-M main', which syncs from every sibling dir - OK."
note "for a hand-rolled single-instance run, use '-M main' or let"
note "agentafl_cycle.ensure_syncable() write addseeds/is_main_node."
grep -q 'AFL_SYNC_TIME' "$HERE/run_campaign.sh" \
  && ok "run_campaign.sh sets AFL_SYNC_TIME (seeds imported in ~30s, not ~10min)" \
  || bad "run_campaign.sh does not set AFL_SYNC_TIME"

echo "== unit tests"
if ( cd "$MINYEE_DIR" && python3 test_agentafl_cycle.py >/tmp/.pf_agentafl.$$ 2>&1 ); then
  ok "test_agentafl_cycle.py: $(tail -1 /tmp/.pf_agentafl.$$)"
else
  bad "test_agentafl_cycle.py FAILED"
  grep -E "FAIL|ERROR" /tmp/.pf_agentafl.$$ | head -5 | sed 's/^/      /'
fi
rm -f /tmp/.pf_agentafl.$$

echo
echo "$PASS passed, $FAIL failed"
if [[ "$FAIL" -gt 0 ]]; then
  echo "DO NOT start a 'combined' campaign until these are fixed."
  exit 1
fi
echo "Ready for: ./run_campaign.sh libxml2 combined <run_id> 3 43200"
