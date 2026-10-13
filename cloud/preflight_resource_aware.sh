#!/bin/bash
# preflight_resource_aware.sh - verify a machine can actually run the "minyee" condition,
# BEFORE committing 12h x 5 instances to it.
#
#   ./preflight_resource_aware.sh
#
# Env vars:
#   WORK=...              base dir (default ~/fyp)
#   EXPECT_BANNER=...     AFL++ banner the committed baselines used
#   MIN_FREE_GB=...       minimum free disk (default 10)
#
# Read-only with respect to the campaign: the mutator harness redirects
# adaptive_mutator.SIGNAL_FILE at a temp file, so the real shared
# ResourceAwareAFL/adaptation/.adapt_signal is never written by this script.
set -uo pipefail

WORK="${WORK:-$HOME/fyp}"
REPO="$WORK/FYP"
MINYEE_DIR="$REPO/ResourceAwareAFL"
EXPECT_BANNER="${EXPECT_BANNER:-afl-fuzz++5.03c}"
MIN_FREE_GB="${MIN_FREE_GB:-10}"

FAIL=0
ok()   { echo "  [ok]   $*"; }
warn() { echo "  [warn] $*"; }
bad()  { echo "  [FAIL] $*"; FAIL=1; }

echo "=== preflight_resource_aware: $MINYEE_DIR"

# ---------------------------------------------------------------- 0. nothing live
# This script does not write the shared signal file, but a campaign running
# concurrently would still invalidate the CPU/timing picture it reports.
echo "[0] machine is idle"
if pgrep -x afl-fuzz >/dev/null; then
  bad "afl-fuzz is already running - run this on an idle machine"
else
  ok "no afl-fuzz running"
fi
if pgrep -f 'ResourceAwareAFL/run_analyzer.py' >/dev/null; then
  bad "run_analyzer.py is already running"
else
  ok "no run_analyzer.py running"
fi

# ---------------------------------------------------------------- 1. files
echo "[1] ResourceAwareAFL files"
for f in monitor.py run_analyzer.py controller.py cea.py adaptive_mutator.py; do
  if [[ -f "$MINYEE_DIR/$f" ]]; then ok "$f"; else bad "missing $MINYEE_DIR/$f"; fi
done

# ---------------------------------------------------------------- 2. AFL++ python
# NOTE: afl-fuzz -h EXITS 1, so under `set -o pipefail` a naive
# `afl-fuzz -h | grep -q python` is a guaranteed false negative. Capture first.
# The env-var list and the "Compiled with" line live in -hh, not -h.
# The version banner is not in either on every build, so use --version.
echo "[2] AFL++ Python mutator support"
if ! command -v afl-fuzz >/dev/null; then
  bad "afl-fuzz not on PATH"
else
  AFLHH="$(afl-fuzz -hh 2>&1 || true)"
  if grep -q 'module support' <<<"$AFLHH"; then
    ok "$(grep -om1 'Compiled with Python [^ ]* module support' <<<"$AFLHH")"
  else
    bad "afl-fuzz built WITHOUT Python module support - rebuild AFL++ with python3-dev present, then re-bake the AMI"
  fi
  if grep -q 'AFL_PYTHON_MODULE' <<<"$AFLHH"; then
    ok "AFL_PYTHON_MODULE supported"
  else
    bad "afl-fuzz does not document AFL_PYTHON_MODULE"
  fi

  BANNER="$(afl-fuzz --version 2>&1 | grep -om1 'afl-fuzz++[0-9][^ ]*' || true)"
  [[ -z "$BANNER" ]] && BANNER="$(grep -om1 'afl-fuzz++[0-9][^ ]*' <<<"$AFLHH" || true)"
  # A different AFL++ between arms silently destroys the baseline comparison, so
  # cross-check against what setup_machine.sh stamped and meta.txt embeds.
  RECORDED="$(awk -F= '/^afl_fuzz_banner=/{print $2}' "$WORK/versions.txt" 2>/dev/null | tr -d '\r')"
  if [[ -n "$BANNER" ]]; then
    ok "afl-fuzz --version: $BANNER"
  else
    warn "could not determine afl-fuzz version from the binary"
  fi
  if [[ -n "$RECORDED" && -n "$BANNER" && "$RECORDED" != "$BANNER" ]]; then
    bad "binary is '$BANNER' but \$WORK/versions.txt records '$RECORDED' - AFL++ changed since setup, re-run setup_machine.sh or re-stamp versions.txt"
  fi
  CHECK="${BANNER:-$RECORDED}"
  if [[ -n "$CHECK" && "$CHECK" == *"$EXPECT_BANNER"* ]]; then
    ok "matches the committed baselines ($EXPECT_BANNER)"
  else
    warn "AFL++ is '${CHECK:-unknown}', baselines used '$EXPECT_BANNER' - minyee results will NOT be comparable to the committed baseline runs"
  fi
fi

# ---------------------------------------------------------------- 3. env hygiene
echo "[3] environment is clean"
for v in AFL_CUSTOM_MUTATOR_ONLY AFL_NO_PYTHON AFL_CUSTOM_MUTATOR_LIBRARY; do
  if [[ -n "${!v:-}" ]]; then bad "$v is set (${!v}) - unset it"; else ok "$v unset"; fi
done

# ---------------------------------------------------------------- 4. python deps
echo "[4] Python dependencies"
if python3 -c 'import psutil' 2>/dev/null; then
  ok "psutil importable ($(python3 -c 'import psutil;print(psutil.__version__)' 2>/dev/null))"
else
  bad "psutil missing - sudo apt-get install -y python3-psutil"
fi

# run_analyzer.py --help exits in argparse BEFORE AdaptiveFuzzingController is
# constructed, so this proves cea/controller import with zero side effects:
# no adaptation/ dir created, no signal written.
if [[ -f "$MINYEE_DIR/run_analyzer.py" ]]; then
  if ( cd /tmp && timeout 10 python3 "$MINYEE_DIR/run_analyzer.py" --help ) >/dev/null 2>&1; then
    ok "run_analyzer.py imports cleanly (cea, controller)"
  else
    bad "run_analyzer.py --help failed - import error in cea/controller, fix before burning 12h"
  fi
fi

# ---------------------------------------------------------------- 5. signal dir
echo "[5] adaptation signal directory"
if mkdir -p "$MINYEE_DIR/adaptation" 2>/dev/null && [[ -w "$MINYEE_DIR/adaptation" ]]; then
  ok "$MINYEE_DIR/adaptation writable"
else
  bad "cannot create/write $MINYEE_DIR/adaptation"
fi

# ---------------------------------------------------------------- 6. disk
# A failed print() inside adaptive_mutator.fuzz() escapes to PyErr_Print() and
# AFL++ FATALs, so a full disk mid-campaign can kill the fuzzers outright.
echo "[6] disk space"
FREE_GB="$(df -BG --output=avail "$WORK" 2>/dev/null | tail -1 | tr -dc '0-9')"
if [[ -n "$FREE_GB" ]] && (( FREE_GB >= MIN_FREE_GB )); then
  ok "${FREE_GB}G free on $WORK (need ${MIN_FREE_GB}G)"
else
  bad "only ${FREE_GB:-?}G free on $WORK - need ${MIN_FREE_GB}G for ASan crash dirs + ~1440 monitor windows + logs"
fi

# ---------------------------------------------------------------- 7. mutator
echo "[7] adaptive_mutator across all modes"
if [[ -f "$MINYEE_DIR/adaptive_mutator.py" ]]; then
  if python3 - "$MINYEE_DIR" <<'PY'
import os, sys, tempfile, traceback

sys.path.insert(0, sys.argv[1])
import adaptive_mutator as am

# Redirect the module's signal path at a temp file so the real shared
# .adapt_signal is never touched by this check.
tmp = tempfile.mkdtemp(prefix="preflight_resource_aware_")
am.SIGNAL_FILE = os.path.join(tmp, ".adapt_signal")

MODES = ["DEFAULT", "THROTTLE_POWER", "REDUCE_MUTATION_SIZE", "STRONG_THROTTLE",
         "EXPLOIT_CRASH", "SKIP_TIMEOUTS", "EXPLORE_HEAVY"]

# splice_optout() is defined, so AFL++ passes add_buf=None. Test what AFL++
# actually does, not what a naive harness would pass.
ADD_BUF = None

BUFS = {
    "empty":      bytearray(b""),
    "one_byte":   bytearray(b"<"),
    "two_byte":   bytearray(b"<a"),
    "small_xml":  bytearray(b"<?xml version='1.0'?><r><a x='1'/></r>"),
    "large_64k":  bytearray(b"<r>" + b"A" * 65536 + b"</r>"),
}

am.init(0)
fails = []
reps = 200   # these are randomised; one pass proves nothing

for mode in MODES:
    with open(am.SIGNAL_FILE, "w") as fh:
        fh.write("MODE=%s;TIMESTAMP=1.0\n" % mode)
    if am.read_adaptation_mode() != mode:
        fails.append("%s: read_adaptation_mode() returned %r"
                     % (mode, am.read_adaptation_mode()))
    for bname, buf in BUFS.items():
        # include max_size < len(buf): the C side does NOT enforce max_size,
        # her trailing truncate is the only thing that does.
        for max_size in (1, 16, len(buf) or 1, max(len(buf) // 2, 1), 1 << 20):
            for _ in range(reps):
                try:
                    n = am.fuzz_count(bytearray(buf))
                    if not isinstance(n, int) or n < 0:
                        fails.append("%s/%s: fuzz_count -> %r" % (mode, bname, n))
                        break
                    out = am.fuzz(bytearray(buf), ADD_BUF, max_size)
                except Exception:
                    fails.append("%s/%s max_size=%d: RAISED\n%s"
                                 % (mode, bname, max_size, traceback.format_exc()))
                    break
                if not isinstance(out, (bytes, bytearray)):
                    fails.append("%s/%s max_size=%d: returned %s, AFL++ FATALs on "
                                 "anything but bytes/bytearray"
                                 % (mode, bname, max_size, type(out).__name__))
                    break
                if len(out) > max_size:
                    fails.append("%s/%s max_size=%d: returned %d bytes (over max_size)"
                                 % (mode, bname, max_size, len(out)))
                    break

am.deinit()
os.remove(am.SIGNAL_FILE)
os.rmdir(tmp)

if fails:
    print("  mutator harness FAILED:")
    for f in fails[:10]:
        print("    - %s" % f)
    sys.exit(1)

print("  %d modes x %d buffers x 5 max_sizes x %d reps clean"
      % (len(MODES), len(BUFS), reps))
sys.exit(0)
PY
  then
    ok "fuzz()/fuzz_count() never raised, always returned bytes-like within max_size"
  else
    bad "adaptive_mutator harness failed - a raise inside fuzz() FATALs afl-fuzz mid-campaign"
  fi
fi

# ---------------------------------------------------------------- verdict
echo
if (( FAIL )); then
  echo "[-] PREFLIGHT FAILED - do not launch campaigns on this machine yet."
  exit 1
fi
cat <<'MSG'
[+] PREFLIGHT PASSED.

    This checks the Python layer only. It cannot exercise the C-side type
    conversion, the real -G max_size, module loading, or -c cmplog. Do the
    pinned-mode smoke test next:

      for m in THROTTLE_POWER REDUCE_MUTATION_SIZE STRONG_THROTTLE \
               EXPLOIT_CRASH SKIP_TIMEOUTS EXPLORE_HEAVY; do
        MINYEE_PIN_MODE=$m AUTO_SHUTDOWN=0 ./run_campaign.sh libxml2 minyee 90 3 600
        rm -rf ~/fyp/runs/libxml2_minyee_run90
      done
MSG
