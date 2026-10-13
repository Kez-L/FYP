#!/bin/bash
# build_libxml2.sh - build instrumented xmllint binaries for fuzzing.
#
# Produces, in $WORK/targets/libxml2-build/:
#   xmllint-afl     main fuzzing binary (AFL++ instrumentation, ASan by default)
#   xmllint-cmplog  CMPLOG binary for the main instance's -c flag (no ASan)
#
#   ./build_libxml2.sh
#
# IMPORTANT: make these match how you built xmllint locally, and keep them
# identical for every run:
#   LIBXML2_REF  libxml2 git tag/commit   (check: xmllint --version locally)
#   USE_ASAN     1 = AddressSanitizer on, 0 = off
set -euo pipefail

LIBXML2_REF="${LIBXML2_REF:-v2.15.4}"
USE_ASAN="${USE_ASAN:-1}"
WORK="${WORK:-$HOME/fyp}"

SRC="$WORK/targets/libxml2-src"
OUT="$WORK/targets/libxml2-build"
CONF_FLAGS=(--disable-shared --without-python --without-zlib --without-lzma)

command -v afl-clang-fast >/dev/null || { echo "afl-clang-fast not found - run setup_machine.sh first" >&2; exit 2; }

# ---------------------------------------------------------------- source
if [[ ! -d "$SRC/.git" ]]; then
  echo "[*] Cloning libxml2 $LIBXML2_REF..."
  git clone --depth 1 --branch "$LIBXML2_REF" https://github.com/GNOME/libxml2.git "$SRC"
  (cd "$SRC" && autoreconf -fi >/dev/null)
fi
COMMIT="$(git -C "$SRC" rev-parse HEAD)"
mkdir -p "$OUT"

build() {  # name  extra-env...
  local name="$1"; shift
  local dir="$WORK/targets/libxml2-$name"
  echo "[*] Building $name in $dir ..."
  rm -rf "$dir"; mkdir -p "$dir"
  (
    cd "$dir"
    env "$@" CC=afl-clang-fast "$SRC/configure" "${CONF_FLAGS[@]}" > configure.log 2>&1
    env "$@" make -j"$(nproc)" xmllint > make.log 2>&1
  ) || { echo "[-] build '$name' failed - see $dir/configure.log and $dir/make.log" >&2; exit 1; }
}

if [[ "$USE_ASAN" == "1" ]]; then
  build afl AFL_USE_ASAN=1
else
  build afl AFL_QUIET=1
fi
cp "$WORK/targets/libxml2-afl/xmllint" "$OUT/xmllint-afl"

build cmplog AFL_LLVM_CMPLOG=1
cp "$WORK/targets/libxml2-cmplog/xmllint" "$OUT/xmllint-cmplog"

# ---------------------------------------------------------------- record + sanity check
{
  echo "libxml2_ref=$LIBXML2_REF"
  echo "libxml2_commit=$COMMIT"
  echo "use_asan=$USE_ASAN"
  echo "configure_flags=${CONF_FLAGS[*]}"
  echo "built_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "$OUT/build_info.txt"

echo '<?xml version="1.0"?><a b="c">ok</a>' > "$OUT/.sanity.xml"
"$OUT/xmllint-afl" --noout "$OUT/.sanity.xml" && echo "[+] xmllint-afl runs"
[[ "$USE_ASAN" == "1" ]] && [[ "$(nm "$OUT/xmllint-afl" | grep -c __asan_init)" -gt 0 ]] && echo "[+] ASan is compiled in"
if [[ "$USE_ASAN" == "1" ]] && [[ "$(nm "$OUT/xmllint-afl" | grep -c __asan_init)" -eq 0 ]]; then
  echo "[-] WARNING: ASan symbols not found in xmllint-afl" >&2
fi
afl-showmap -q -o /dev/null -- "$OUT/xmllint-afl" --noout "$OUT/.sanity.xml" \
  && echo "[+] AFL++ instrumentation detected" \
  || echo "[-] WARNING: afl-showmap could not see instrumentation" >&2

echo
cat "$OUT/build_info.txt"
echo
echo "Next: ./run_campaign.sh libxml2 baseline 1"
