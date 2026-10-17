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
  fi
}

step "1/6 setup_machine.sh"  "$HERE/setup_machine.sh"
step "2/6 build_libxml2.sh"  "$HERE/build_libxml2.sh"
step "3/6 build_libical.sh"  "$HERE/build_libical.sh"
step "4/6 build_tidy.sh"     "$HERE/build_tidy.sh"
step "5/6 preflight_resource_aware.sh" "$HERE/preflight_resource_aware.sh"

# The AgentAFL preflight is per-target: it checks that target's binary, its
# showmap path and its format probe. A missing API key is expected at image-bake
# time, so note it separately rather than counting it as a failure.
for t in "${ALL_TARGETS[@]}"; do
  step "6/6 preflight_agentafl.sh ($t)" env "AGENTAFL_TARGET=$t" "$HERE/preflight_agentafl.sh"
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
  cat <<'MSG'
GO - this machine is ready to be imaged.

  1. rm -rf ~/fyp/runs/* ~/fyp/results/*      (don't bake test runs into the AMI)
  2. EC2 console -> Actions -> Image and templates -> Create image
  3. Launch every campaign VM from that image, and do not re-bake mid-experiment:
     all 45 runs must share one toolchain and one commit or the matrix is void.

  Per combined VM, after launch:
     echo 'CLAUDE_API_KEY=sk-...' > ~/fyp/FYP/AgentAFL/.env
     ./preflight_agentafl.sh
MSG
  exit 0
fi
echo "NO-GO - do not image this machine yet."
[[ "$CORPUS_BAD" -eq 1 ]] && echo "  - a seed corpus does not match its frozen fingerprint"
for f in ${FAILED[@]+"${FAILED[@]}"}; do echo "  - $f"; done
echo
echo "A missing AgentAFL API key is the one expected failure at bake time;"
echo "if that is the ONLY thing the combined preflight complained about, this"
echo "machine is fine to image."
exit 1
