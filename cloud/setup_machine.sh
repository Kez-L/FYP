#!/bin/bash
# setup_machine.sh - one-time setup of a fresh Ubuntu 24.04 EC2 instance.
#
# Installs build dependencies, builds AFL++ from source at a pinned version,
# and clones the FYP repo. Safe to re-run (skips steps already done).
#
#   ./setup_machine.sh
#
# Override defaults with env vars, e.g.:
#   AFLPP_REF=v5.02c ./setup_machine.sh
#
# IMPORTANT: AFLPP_REF should match the AFL++ version you used locally
# (run `afl-fuzz -h 2>&1 | head -1` on your own machine to check), and must be
# the SAME for every run in the experiment.
set -euo pipefail

AFLPP_REF="${AFLPP_REF:-v5.03c}"
REPO_URL="${REPO_URL:-https://github.com/Kez-L/FYP.git}"
REPO_REF="${REPO_REF:-master}"
WORK="${WORK:-$HOME/fyp}"
LLVM="${LLVM:-18}"                       # Ubuntu 24.04's default LLVM

SUDO=""; [[ $EUID -ne 0 ]] && SUDO="sudo"
GCCV="$(gcc -dumpversion 2>/dev/null | cut -d. -f1 || echo 13)"

echo "[*] Installing packages (LLVM $LLVM, gcc $GCCV plugin)..."
export DEBIAN_FRONTEND=noninteractive
$SUDO apt-get update -y
$SUDO apt-get install -y \
  build-essential git curl jq tmux htop \
  python3 python3-dev python3-pip python3-psutil \
  autoconf automake libtool pkg-config cmake flex bison libglib2.0-dev \
  "clang-$LLVM" "llvm-$LLVM" "llvm-$LLVM-dev" "lld-$LLVM" \
  "gcc-$GCCV-plugin-dev"

mkdir -p "$WORK"

# ---------------------------------------------------------------- AFL++
if command -v afl-fuzz >/dev/null && [[ -f "$WORK/.aflpp_ref" ]] \
   && [[ "$(cat "$WORK/.aflpp_ref")" == "$AFLPP_REF" ]]; then
  echo "[=] AFL++ $AFLPP_REF already installed, skipping."
else
  echo "[*] Building AFL++ $AFLPP_REF (takes ~5-15 min)..."
  rm -rf "$WORK/AFLplusplus"
  git clone --depth 1 --branch "$AFLPP_REF" \
    https://github.com/AFLplusplus/AFLplusplus.git "$WORK/AFLplusplus"
  cd "$WORK/AFLplusplus"
  # source-only = LLVM + gcc-plugin modes, no QEMU/Frida/Nyx (not needed: we
  # always have target source). python3-dev above gives Python mutator support,
  # which Min Yee's adaptive_mutator.py will need later.
  make -j"$(nproc)" source-only LLVM_CONFIG="llvm-config-$LLVM"
  $SUDO make install
  echo "$AFLPP_REF" > "$WORK/.aflpp_ref"
fi

# ---------------------------------------------------------------- FYP repo
if [[ -d "$WORK/FYP/.git" ]]; then
  echo "[=] FYP repo present; pulling $REPO_REF..."
  git -C "$WORK/FYP" fetch --depth 1 origin "$REPO_REF"
  git -C "$WORK/FYP" checkout -q FETCH_HEAD
else
  echo "[*] Cloning FYP repo..."
  # If the repo is ever made private, use a GitHub personal access token:
  #   REPO_URL=https://<token>@github.com/Kez-L/FYP.git ./setup_machine.sh
  git clone --depth 1 --branch "$REPO_REF" "$REPO_URL" "$WORK/FYP"
fi

# ---------------------------------------------------------------- record versions
{
  echo "setup_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "aflpp_ref=$AFLPP_REF"
  echo "afl_fuzz_banner=$(afl-fuzz -h 2>&1 | head -1 | sed 's/\x1b\[[0-9;]*m//g')"
  echo "clang=$(clang-$LLVM --version | head -1)"
  echo "fyp_commit=$(git -C "$WORK/FYP" rev-parse HEAD)"
  echo "kernel=$(uname -r)"
} > "$WORK/versions.txt"

echo
echo "[+] Setup done. Versions recorded in $WORK/versions.txt:"
cat "$WORK/versions.txt"
echo
echo "Next: ./build_libxml2.sh"
