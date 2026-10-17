#!/bin/bash
# setup_all.sh - the one command to prepare a fresh VM for the whole experiment.
#
#   ./setup_all.sh
#
# Runs, in order:
#   1. setup_machine.sh    packages + AFL++ (pinned) + the FYP repo
#   2. build_libxml2.sh    xmllint-afl / xmllint-cmplog
#   3. build_libical.sh    libical_fuzzer-afl / -cmplog
#   4. build_tidy.sh       tidy_parse_string_fuzzer-afl / -cmplog
#   5. preflight_resource_aware.sh   the minyee (Level-1) layer
#   6. preflight_agentafl.sh         the combined (Level-2) layer, per target
#
# Then prints one GO / NO-GO line. Run this ONCE, check it says GO, then make
# the AMI. Every VM in the experiment is launched from that one image, so all 45
# runs share one toolchain, one set of target builds and one commit.
#
# Takes roughly 25-45 min on a c7i.xlarge, most of it AFL++ and the three builds.
#
# The AgentAFL API key is deliberately NOT part of this: it is pasted into
# AgentAFL/.env per VM after launch, so it never enters the AMI snapshot. The
# `combined` preflight below will therefore report a missing key here, and that
# is expected - it is reported as a warning, not a NO-GO.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="${WORK:-$HOME/fyp}"
export WORK

# shellcheck source=targets.sh
TARGETS_SH_DIR="$HERE"
source "$HERE/targets.sh"

FAILED=()
FAILED_WHY=()
step() {  # <label> <command...>
  local label="$1"; shift
  echo
  echo "================================================================"
  echo "== $label"
  echo "================================================================"
  if "$@"; then
    echo "[+] $label OK"
  else
    echo "[-] $label FAILED" >&2
    FAILED+=("$label")
    FAILED_WHY+=("see the output above")
  fi
}

# The AgentAFL preflight gets its own runner because exactly one of its failures
# is EXPECTED here: the API key is written per VM after launch, so it is
# deliberately absent when the image is baked. Everything else it reports is a
# real problem. Classifying that here means this script can give a straight
# GO / NO-GO instead of printing "one of these is fine, you work out which".
DEFERRED=()
step_agentafl() {  # <target>
  local t="$1" label out rc hard
  label="6/6 preflight_agentafl.sh ($t)"
  out="$(mktemp)"
  echo
  echo "================================================================"
  echo "== $label"
  echo "================================================================"
  env "AGENTAFL_TARGET=$t" "$HERE/preflight_agentafl.sh" 2>&1 | tee "$out"
  rc=${PIPESTATUS[0]}
  if [[ "$rc" -eq 0 ]]; then
    echo "[+] $label OK"
    rm -f "$out"
    return
  fi
  # Every [-] line that is NOT just the missing key.
  hard="$(grep -E '^[[:space:]]*\[-\]' "$out" \
          | grep -vE "no usable (OPENAI|CLAUDE)_API_KEY" || true)"
  if [[ -z "$hard" ]]; then
    echo "[=] $label: only the API key is missing, which is expected before"
    echo "    baking - you write it per VM after launch. Not a blocker."
    DEFERRED+=("$t")
  else
    echo "[-] $label FAILED on something other than the API key:" >&2
    echo "$hard" >&2
    FAILED+=("$label")
    FAILED_WHY+=("$(echo "$hard" | sed 's/^[[:space:]]*/      /' | head -4)")
  fi
  rm -f "$out"
}

step "1/6 setup_machine.sh"  "$HERE/setup_machine.sh"
step "2/6 build_libxml2.sh"  "$HERE/build_libxml2.sh"
step "3/6 build_libical.sh"  "$HERE/build_libical.sh"
step "4/6 build_tidy.sh"     "$HERE/build_tidy.sh"
step "5/6 preflight_resource_aware.sh" "$HERE/preflight_resource_aware.sh"

# Per-target: each run checks that target's binary, its showmap @@ handling and
# its format probe. The shared checks (SDK imports, key, hook, unit tests) are
# repeated, which is cheap next to the builds above.
for t in "${ALL_TARGETS[@]}"; do
  step_agentafl "$t"
done

# ---------------------------------------------------------------- corpora
echo
echo "================================================================"
echo "== seed corpora"
echo "================================================================"
REPO="${REPO:-$WORK/FYP}"
CORPUS_BAD=0
for t in "${ALL_TARGETS[@]}"; do
  # unset SEEDS each time: targets.sh honours a caller-supplied override, and a
  # value left over from the previous iteration would check the wrong corpus.
  unset SEEDS; load_target "$t"
  read -r n h <<<"$(seeds_fingerprint "$SEEDS")"
  if [[ "$n" == "$SEEDS_COUNT" && "$h" == "$SEEDS_SHA256_16" ]]; then
    printf '  [+] %-8s %s files, %s\n' "$t" "$n" "$h"
  else
    printf '  [-] %-8s expected %s/%s, got %s/%s\n' \
      "$t" "$SEEDS_COUNT" "$SEEDS_SHA256_16" "$n" "$h"
    CORPUS_BAD=1
  fi
done

# ---------------------------------------------------------------- verdict
echo
echo "================================================================"
if [[ ${#FAILED[@]} -eq 0 && "$CORPUS_BAD" -eq 0 ]]; then
  echo "GO - this machine is ready to be imaged."
  if [[ ${#DEFERRED[@]} -gt 0 ]]; then
    echo
    echo "  (The combined preflight reported a missing API key for"
    echo "   ${DEFERRED[*]} - that is expected: the key is written per VM"
    echo "   after launch so it never enters the AMI snapshot.)"
  fi
  cat <<'MSG'

  1. rm -rf ~/fyp/runs/* ~/fyp/results/*      (don't bake test runs into the AMI)
  2. EC2 console -> Actions -> Image and templates -> Create image
  3. Launch every campaign VM from that image, and do not re-bake mid-experiment:
     all 45 runs must share one toolchain and one commit or the matrix is void.

  Per combined VM, after launch:
     printf 'OPENAI_API_KEY=KEY_ONE\nOPENAI_API_KEY_2=KEY_TWO\n' \
       > ~/fyp/FYP/AgentAFL/.env
     AGENTAFL_TARGET=<target> ./preflight_agentafl.sh
MSG
  exit 0
fi
echo "NO-GO - do not image this machine yet."
echo
[[ "$CORPUS_BAD" -eq 1 ]] && echo "  - a seed corpus does not match its frozen fingerprint"
i=0
for f in ${FAILED[@]+"${FAILED[@]}"}; do
  echo "  - $f"
  [[ -n "${FAILED_WHY[$i]:-}" ]] && echo "${FAILED_WHY[$i]}"
  i=$((i+1))
done
echo
echo "A missing API key is NOT counted above - that one is expected before"
echo "baking and is reported separately. Everything listed here is a real"
echo "problem. To see the full detail for one of them again:"
echo "  AGENTAFL_TARGET=libxml2 $HERE/preflight_agentafl.sh"
exit 1
