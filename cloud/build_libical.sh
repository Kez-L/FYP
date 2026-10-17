#!/bin/bash
# build_libical.sh - build the instrumented libical fuzz harness.
#
# Produces, in $WORK/targets/libical-build/:
#   libical_fuzzer-afl     main fuzzing binary (AFL++ instrumentation + ASan)
#   libical_fuzzer-cmplog  CMPLOG binary for the main instance's -c flag
#
#   ./build_libical.sh
#
# The harness is cloud/harnesses/libical_fuzzer.cc, a libFuzzer-style
# LLVMFuzzerTestOneInput around icalparser_parse_string(), derived from the
# OSS-Fuzz harness. It is vendored in this repo on purpose: it is not part of
# upstream libical and OSS-Fuzz has no libical project, so there is nowhere else
# to fetch it from.
#
# IMPORTANT: keep these identical for every run in the experiment.
#   LIBICAL_REF   pinned commit (NOT a tag - the campaign was built off branch
#                 4.0 at 44 commits past v4.0.5, so a tag would be a different
#                 tree)
#   USE_ASAN      1 = AddressSanitizer on. ASan comes from AFL_USE_ASAN at
#                 compile time, NOT from LIBICAL_DEVMODE_ADDRESS_SANITIZER,
#                 which stays OFF.
set -euo pipefail

LIBICAL_REF="${LIBICAL_REF:-3d0c8c360d98c2211955dd6090bebea2e08e742c}"
LIBICAL_BRANCH="${LIBICAL_BRANCH:-4.0}"
USE_ASAN="${USE_ASAN:-1}"
WORK="${WORK:-$HOME/fyp}"
HERE="$(cd "$(dirname "$0")" && pwd)"

SRC="$WORK/targets/libical-src"
OUT="$WORK/targets/libical-build"
HARNESS="$HERE/harnesses/libical_fuzzer.cc"

# Recovered from the surviving CMakeCache.txt of the campaign builds. Static
# only, no bindings, no tests/examples/docs, no builtin tzdata - the harness
# only needs the parser.
CMAKE_FLAGS=(
  -DCMAKE_BUILD_TYPE=Release
  -DCMAKE_C_COMPILER=afl-clang-fast
  -DCMAKE_CXX_COMPILER=afl-clang-fast++
  -DLIBICAL_STATIC=True
  -DLIBICAL_BUILD_TESTING=False
  -DLIBICAL_BUILD_EXAMPLES=False
  -DLIBICAL_BUILD_DOCS=False
  -DLIBICAL_BUILD_VZIC=OFF
  -DLIBICAL_BUILD_TESTING_BIGFUZZ=OFF
  -DLIBICAL_GLIB=False
  -DLIBICAL_GOBJECT_INTROSPECTION=False
  -DLIBICAL_JAVA_BINDINGS=False
  -DLIBICAL_CXX_BINDINGS=ON
  -DLIBICAL_ENABLE_BUILTIN_TZDATA=OFF
  -DLIBICAL_DEVMODE=OFF
  -DLIBICAL_DEVMODE_ADDRESS_SANITIZER=OFF
)

command -v afl-clang-fast >/dev/null || { echo "afl-clang-fast not found - run setup_machine.sh first" >&2; exit 2; }
[[ -f "$HARNESS" ]] || { echo "harness missing: $HARNESS" >&2; exit 2; }
DRIVER="$(ls /usr/local/lib/afl/libAFLDriver.a 2>/dev/null || true)"
[[ -n "$DRIVER" ]] || { echo "libAFLDriver.a not found in /usr/local/lib/afl - re-run 'make install' in AFLplusplus" >&2; exit 2; }

# ---------------------------------------------------------------- source
if [[ ! -d "$SRC/.git" ]]; then
  echo "[*] Cloning libical branch $LIBICAL_BRANCH..."
  # Not --depth 1: we check out a specific commit that is not the branch tip.
  git clone --branch "$LIBICAL_BRANCH" https://github.com/libical/libical.git "$SRC"
fi
git -C "$SRC" checkout -q "$LIBICAL_REF"
COMMIT="$(git -C "$SRC" rev-parse HEAD)"
mkdir -p "$OUT"

# ---------------------------------------------------------------- build
# $1 = variant name, rest = env for both cmake and the harness compile
build() {
  local name="$1"; shift
  local dir="$WORK/targets/libical-$name"
  echo "[*] Building libical ($name) in $dir ..."
  rm -rf "$dir"; mkdir -p "$dir"
  (
    cd "$dir"
    env "$@" cmake "${CMAKE_FLAGS[@]}" -DCMAKE_INSTALL_PREFIX="$dir" "$SRC" > cmake.log 2>&1
    env "$@" make -j"$(nproc)" > make.log 2>&1
    env "$@" make install > install.log 2>&1
  ) || { echo "[-] libical '$name' build failed - see $dir/{cmake,make,install}.log" >&2; exit 1; }

  # The harness is not a CMake target, so compile and link it by hand against
  # the static libs, with libAFLDriver.a as the fuzzing engine. Link with the
  # C++ driver because the harness is .cc and libical_cxx is in the link line.
  # libical pulls in ICU (libicuuc/libicui18n/libicudata) and libresolv; those
  # are real dependencies of the campaign binary, confirmed with ldd against it.
  echo "[*] Linking harness ($name) ..."
  env "$@" afl-clang-fast++ -O2 -std=c++17 \
      -I"$dir/include" \
      "$HARNESS" \
      "$dir/lib/libical.a" "$dir/lib/libical_cxx.a" \
      "$dir/lib/libicalss.a" "$dir/lib/libicalvcal.a" "$dir/lib/libicalvcard.a" \
      "$DRIVER" \
      -licui18n -licuuc -licudata -lresolv -lpthread -ldl -lm \
      -o "$OUT/libical_fuzzer-$name" \
    || { echo "[-] harness link '$name' failed (ICU dev headers missing? apt-get install libicu-dev)" >&2; exit 1; }
}

if [[ "$USE_ASAN" == "1" ]]; then
  build afl AFL_USE_ASAN=1
else
  build afl AFL_QUIET=1
fi
build cmplog AFL_LLVM_CMPLOG=1

# ---------------------------------------------------------------- record + check
{
  echo "libical_ref=$LIBICAL_REF"
  echo "libical_branch=$LIBICAL_BRANCH"
  echo "libical_commit=$COMMIT"
  echo "use_asan=$USE_ASAN"
  echo "cmake_flags=${CMAKE_FLAGS[*]}"
  echo "harness=harnesses/libical_fuzzer.cc"
  echo "built_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "$OUT/build_info.txt"

SANITY="$HERE/sanity/sanity.ics"
"$OUT/libical_fuzzer-afl" "$SANITY" >/dev/null 2>&1 && echo "[+] libical_fuzzer-afl runs"
if [[ "$USE_ASAN" == "1" ]]; then
  if [[ "$(nm "$OUT/libical_fuzzer-afl" | grep -c __asan_init)" -gt 0 ]]; then
    echo "[+] ASan is compiled in"
  else
    echo "[-] WARNING: ASan symbols not found in libical_fuzzer-afl" >&2
  fi
fi

# The harness is a persistent libAFLDriver binary: under afl-fuzz it takes input
# over shared memory with NO file argument, but afl-showmap must pass @@. Check
# the showmap path here, because that is the one AgentAFL's seed scoring uses and
# the one whose failure is silent.
edges="$(afl-showmap -e -q -o - -- "$OUT/libical_fuzzer-afl" "$SANITY" 2>/dev/null | grep -c ':' || true)"
if [[ "${edges:-0}" -gt 0 ]]; then
  echo "[+] AFL++ instrumentation detected ($edges edges on sanity.ics via @@)"
else
  echo "[-] WARNING: afl-showmap saw no coverage - seed scoring would be dead" >&2
fi

echo
cat "$OUT/build_info.txt"
echo
echo "Next: ./run_campaign.sh libical baseline 1 3 43200"
