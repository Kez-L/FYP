#!/bin/bash
# targets.sh - the ONE definition of every fuzz target in this experiment.
#
# Sourced by run_campaign.sh, preflight_agentafl.sh and verify_run.sh so that a
# target can never be described one way to the campaign and another way to the
# thing that checks it.
#
#   source "$HERE/targets.sh"
#   load_target libxml2     # or libical, tidy
#
# Requires $WORK (default ~/fyp) and $REPO ($WORK/FYP) to be set by the caller.
#
# load_target sets, in the caller's scope:
#
#   BUILD           dir holding the built binaries
#   BIN             the instrumented fuzzing binary (ASan, every target)
#   CMPLOG          the CMPLOG binary for main's -c flag
#   FUZZ_ARGS[]     argv handed to afl-fuzz after the binary
#   SHOWMAP_ARGS[]  argv handed to afl-showmap / AgentAFL seed scoring
#   SEEDS           seed corpus (overridable by the caller's $SEEDS)
#   AGENTAFL_FMT_DEFAULT   prompt format name, exact string
#   SANITY_INPUT    one small valid input, for showmap self-tests
#   TARGET_ENV[]    KEY=VALUE pairs the campaign must export
#   SEEDS_COUNT / SEEDS_SHA256_16   frozen corpus fingerprint
#   EXECS_FLOOR     execs/s below which a run is stalled, not merely slow
#
# ----------------------------------------------------------------------------
# WHY FUZZ_ARGS AND SHOWMAP_ARGS ARE SEPARATE
#
# libxml2 is a normal CLI program: `xmllint --noout <file>`, so one arg list
# serves everything. The libical and tidy harnesses are libFuzzer-style
# (LLVMFuzzerTestOneInput) linked against AFL++'s libAFLDriver, and they behave
# DIFFERENTLY depending on who runs them:
#
#   afl-fuzz        the driver detects afl-fuzz and takes the testcase over
#                   shared memory in a persistent loop. A file argument is
#                   ignored. This is where the exec/s comes from, so passing @@
#                   here would throw away the persistent-mode speedup.
#
#   afl-showmap     not afl-fuzz, so the driver reads a FILE ARGUMENT instead.
#                   evaluate_seeds.run_showmap() passes the candidate ONLY by
#                   substituting @@ in this list (see AgentAFL/evaluate_seeds.py
#                   lines 54-56) - there is no stdin redirect. With no @@ the
#                   target is run with no input at all, parses an empty
#                   document, and returns the SAME map for every candidate.
#
# Measured on libical_fuzzer-asan, three different seeds:
#
#   with @@      701 / 544 / 240 edges      (per-seed, correct)
#   without @@     1 /   1 /   1 edges      (constant, every seed identical)
#
# That failure is silent and plausible-looking: build_baseline ignores
# afl-showmap's exit code, so every AgentAFL candidate scores 0 new edges, no
# seed is ever judged useful, and the `combined` arm degrades to `minyee` for
# the whole 12h with nothing in the logs saying why.
#
# NOTE: afl-showmap's own `-C -i <dir>` collect mode (used by build_baseline)
# handles the directory itself and gives identical results with or without @@
# (ical 982 both ways, tidy 3616 both ways). It is the PER-CANDIDATE path that
# needs @@. SHOWMAP_ARGS carries it for both, because it costs nothing there.
# ----------------------------------------------------------------------------

load_target() {
  local t="$1"
  local work="${WORK:-$HOME/fyp}"
  local repo="${REPO:-$work/FYP}"
  local here="${TARGETS_SH_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"

  case "$t" in
    libxml2)
      BUILD="$work/targets/libxml2-build"
      BIN="$BUILD/xmllint-afl"
      CMPLOG="$BUILD/xmllint-cmplog"
      FUZZ_ARGS=(--noout @@)
      SHOWMAP_ARGS=(--noout @@)
      SEEDS="${SEEDS:-$repo/AgentAFL/seeds_min}"
      AGENTAFL_FMT_DEFAULT="XML document"
      SANITY_INPUT="$here/sanity/sanity.xml"
      TARGET_ENV=()
      SEEDS_COUNT=111
      SEEDS_SHA256_16=52974a6a2190a399
      EXECS_FLOOR=50
      ;;

    libical)
      BUILD="$work/targets/libical-build"
      BIN="$BUILD/libical_fuzzer-afl"
      CMPLOG="$BUILD/libical_fuzzer-cmplog"
      # Persistent shmem harness: NO file argument under afl-fuzz.
      FUZZ_ARGS=()
      SHOWMAP_ARGS=(@@)
      SEEDS="${SEEDS:-$here/seeds/ical}"
      AGENTAFL_FMT_DEFAULT="iCalendar document"
      SANITY_INPUT="$here/sanity/sanity.ics"
      # AFL_OLD_CHILD_SYNC is not optional. The futex-based persistent-mode
      # child sync deadlocks under CPU oversubscription on this AFL++ build:
      # fuzzer_stats stops updating and afl-whatsup still reports every
      # instance "alive", so a campaign can sit dead for hours looking healthy.
      # See AgentAFL/run_libical.sh lines 1-8.
      TARGET_ENV=(AFL_OLD_CHILD_SYNC=1)
      SEEDS_COUNT=33
      SEEDS_SHA256_16=2e4bd513ed8eab50
      EXECS_FLOOR=50
      ;;

    tidy)
      BUILD="$work/targets/tidy-build"
      BIN="$BUILD/tidy_parse_string_fuzzer-afl"
      CMPLOG="$BUILD/tidy_parse_string_fuzzer-cmplog"
      FUZZ_ARGS=()
      SHOWMAP_ARGS=(@@)
      SEEDS="${SEEDS:-$here/seeds/html}"
      AGENTAFL_FMT_DEFAULT="HTML document"
      SANITY_INPUT="$here/sanity/sanity.html"
      # Same persistent-mode deadlock as libical, plus: libtidy leaks ~168
      # bytes per parse upstream (src/alloc.c defaultAlloc), so with LSan on
      # EVERY input is reported as a failure and the crash dir fills with
      # noise. See AgentAFL/common_commands.md lines 135-141.
      #
      # abort_on_error=1 and symbolize=0 are NOT optional, and are the reason
      # this is one string rather than just detect_leaks=0: ASAN_OPTIONS is
      # replaced wholesale, not merged, so setting detect_leaks=0 alone drops
      # the defaults AFL++ relies on. afl-fuzz's check_asan_opts()
      # (src/afl-fuzz-init.c) then PROGRAM ABORTs before writing fuzzer_stats,
      # which takes out the minyee and combined arms for this target. Without
      # abort_on_error=1 an ASAN report would exit cleanly instead of raising
      # SIGABRT, so every real crash would be silently lost.
      TARGET_ENV=(AFL_OLD_CHILD_SYNC=1 ASAN_OPTIONS=detect_leaks=0:abort_on_error=1:symbolize=0)
      SEEDS_COUNT=317
      SEEDS_SHA256_16=d7bf98169294ab62
      # tidy's parse+repair+diagnostics+serialize is super-linear in document
      # length; the corpus is capped at 8KB for exactly that reason, but it is
      # still the slowest of the three. Floor set low enough to catch only a
      # real stall (the uncapped corpus pinned instances at ~4.5 execs/s).
      EXECS_FLOOR=20
      ;;

    *)
      echo "Unknown target '$t' (libxml2 | libical | tidy)" >&2
      return 2
      ;;
  esac
}

# All targets, in the order the experiment matrix runs them.
ALL_TARGETS=(libxml2 libical tidy)

# Canonical corpus fingerprint, computed the way run_campaign.sh records it.
# LC_ALL=C is NOT optional: `sort -z` collates by locale, so the same bytes
# hash differently under en_US.UTF-8 than under C, and a drift detector that
# moves on its own is worse than none. The frozen libxml2 value
# (52974a6a2190a399, recorded in every committed baseline run's meta.txt) only
# reproduces under C.
seeds_fingerprint() {  # <dir> -> "<count> <sha256_16>"
  local d="$1"
  [[ -d "$d" ]] || { echo "0 missing"; return; }
  local n h
  n="$(ls -1 "$d" | wc -l)"
  h="$(cd "$d" && LC_ALL=C bash -c \
        'find . -type f -print0 | sort -z | xargs -0 sha256sum | sha256sum | cut -c1-16')"
  echo "$n $h"
}
