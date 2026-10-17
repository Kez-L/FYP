#!/bin/bash
# build_tidy.sh - build the instrumented tidy-html5 fuzz harness.
#
# Produces, in $WORK/targets/tidy-build/:
#   tidy_parse_string_fuzzer-afl     main fuzzing binary (AFL++ instr. + ASan)
#   tidy_parse_string_fuzzer-cmplog  CMPLOG binary for main's -c flag
#
#   ./build_tidy.sh
#
# The harness is cloud/harnesses/tidy_parse_string_fuzzer.c, the upstream
# OSS-Fuzz harness (tidyParseString + tidyCleanAndRepair + tidyRunDiagnostics +
# tidySaveBuffer), vendored here so a fresh VM does not need an OSS-Fuzz
# checkout.
#
# IMPORTANT: keep these identical for every run in the experiment.
#   TIDY_REF   pinned commit on branch `next` (version.txt 5.9.20). The
#              campaign tree was a grafted shallow clone with no tags, so the
#              commit hash is the only stable identifier.
#   USE_ASAN   1 = AddressSanitizer on, via AFL_USE_ASAN at compile time.
#
# NOTE for anything that RUNS this binary outside afl-fuzz: libtidy leaks ~168
# bytes per parse upstream (src/alloc.c defaultAlloc), so LeakSanitizer reports
# every single input as a failure. ASAN_OPTIONS=detect_leaks=0 is required, and
# run_campaign.sh sets it for this target via targets.sh TARGET_ENV.
set -euo pipefail

TIDY_REF="${TIDY_REF:-d08ddc2860aa95ba8e301343a30837f157977cba}"
TIDY_BRANCH="${TIDY_BRANCH:-next}"
USE_ASAN="${USE_ASAN:-1}"
WORK="${WORK:-$HOME/fyp}"
HERE="$(cd "$(dirname "$0")" && pwd)"

SRC="$WORK/targets/tidy-src"
OUT="$WORK/targets/tidy-build"
HARNESS="$HERE/harnesses/tidy_parse_string_fuzzer.c"

# Recovered from the surviving CMakeCache.txt of the campaign builds.
CMAKE_FLAGS=(
  -DCMAKE_BUILD_TYPE=Release
  -DCMAKE_C_COMPILER=afl-clang-fast
  -DCMAKE_CXX_COMPILER=afl-clang-fast++
  -DBUILD_SHARED_LIB=OFF
  -DBUILD_SAMPLE_CODE=OFF
  -DSUPPORT_CONSOLE_APP=ON
  -DSUPPORT_LOCALIZATIONS=ON
  -DENABLE_CONFIG_FILES=ON
  -DENABLE_DEBUG_LOG=OFF
  -DENABLE_MEMORY_DEBUG=OFF
  -DENABLE_ALLOC_DEBUG=OFF
  -DTIDY_COMPAT_HEADERS=OFF
  -DTIDY_CONSOLE_SHARED=OFF
)

command -v afl-clang-fast >/dev/null || { echo "afl-clang-fast not found - run setup_machine.sh first" >&2; exit 2; }
[[ -f "$HARNESS" ]] || { echo "harness missing: $HARNESS" >&2; exit 2; }
DRIVER="$(ls /usr/local/lib/afl/libAFLDriver.a 2>/dev/null || true)"
[[ -n "$DRIVER" ]] || { echo "libAFLDriver.a not found in /usr/local/lib/afl - re-run 'make install' in AFLplusplus" >&2; exit 2; }

# ---------------------------------------------------------------- source
if [[ ! -d "$SRC/.git" ]]; then
  echo "[*] Cloning tidy-html5 branch $TIDY_BRANCH..."
  # Full clone: we check out a 2022 commit, far behind the branch tip.
  git clone --branch "$TIDY_BRANCH" https://github.com/htacg/tidy-html5.git "$SRC"
fi
git -C "$SRC" checkout -q "$TIDY_REF"
COMMIT="$(git -C "$SRC" rev-parse HEAD)"
VERSION="$(cat "$SRC/version.txt" 2>/dev/null | head -1 || echo unknown)"
mkdir -p "$OUT"

# ---------------------------------------------------------------- build
build() {
  local name="$1"; shift
  local dir="$WORK/targets/tidy-$name"
  echo "[*] Building libtidy ($name) in $dir ..."
  rm -rf "$dir"; mkdir -p "$dir"
  (
    cd "$dir"
    env "$@" cmake "${CMAKE_FLAGS[@]}" "$SRC" > cmake.log 2>&1
    env "$@" make -j"$(nproc)" tidy-static > make.log 2>&1 \
      || env "$@" make -j"$(nproc)" > make.log 2>&1
  ) || { echo "[-] libtidy '$name' build failed - see $dir/{cmake,make}.log" >&2; exit 1; }

  local lib
  lib="$(find "$dir" -name 'libtidy*.a' | head -1)"
  [[ -n "$lib" ]] || { echo "[-] libtidy.a not found under $dir" >&2; exit 1; }

  # The harness is C but libAFLDriver.a is C++, so compile with the C driver and
  # link with the C++ one (the OSS-Fuzz build.sh does the same: $CC -c, $CXX link).
  echo "[*] Linking harness ($name) against $(basename "$lib") ..."
  env "$@" afl-clang-fast -O2 -c -I"$SRC/include" "$HARNESS" -o "$dir/harness.o" \
    || { echo "[-] harness compile '$name' failed" >&2; exit 1; }
  env "$@" afl-clang-fast++ -O2 "$dir/harness.o" "$lib" "$DRIVER" \
      -lpthread -ldl -lm \
      -o "$OUT/tidy_parse_string_fuzzer-$name" \
    || { echo "[-] harness link '$name' failed" >&2; exit 1; }
}

if [[ "$USE_ASAN" == "1" ]]; then
  build afl AFL_USE_ASAN=1
else
  build afl AFL_QUIET=1
fi
build cmplog AFL_LLVM_CMPLOG=1

# ---------------------------------------------------------------- record + check
{
  echo "tidy_ref=$TIDY_REF"
  echo "tidy_branch=$TIDY_BRANCH"
  echo "tidy_commit=$COMMIT"
  echo "tidy_version=$VERSION"
  echo "use_asan=$USE_ASAN"
  echo "cmake_flags=${CMAKE_FLAGS[*]}"
  echo "harness=harnesses/tidy_parse_string_fuzzer.c"
  echo "built_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "$OUT/build_info.txt"

SANITY="$HERE/sanity/sanity.html"
# detect_leaks=0: see the note at the top. Without it this check reports a leak
# and looks like a broken build.
ASAN_OPTIONS=detect_leaks=0 "$OUT/tidy_parse_string_fuzzer-afl" "$SANITY" >/dev/null 2>&1 \
  && echo "[+] tidy_parse_string_fuzzer-afl runs"
if [[ "$USE_ASAN" == "1" ]]; then
  if [[ "$(nm "$OUT/tidy_parse_string_fuzzer-afl" | grep -c __asan_init)" -gt 0 ]]; then
    echo "[+] ASan is compiled in"
  else
    echo "[-] WARNING: ASan symbols not found in tidy_parse_string_fuzzer-afl" >&2
  fi
fi

edges="$(ASAN_OPTIONS=detect_leaks=0 afl-showmap -e -q -o - \
          -- "$OUT/tidy_parse_string_fuzzer-afl" "$SANITY" 2>/dev/null | grep -c ':' || true)"
if [[ "${edges:-0}" -gt 0 ]]; then
  echo "[+] AFL++ instrumentation detected ($edges edges on sanity.html via @@)"
else
  echo "[-] WARNING: afl-showmap saw no coverage - seed scoring would be dead" >&2
fi

echo
cat "$OUT/build_info.txt"
echo
echo "Next: ./run_campaign.sh tidy baseline 1 3 43200"
