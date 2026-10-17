#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-only
set -euo pipefail
kernel=$1
headers="/lib/modules/$kernel/build"
arguments=()
if grep -Fxq CONFIG_CC_IS_CLANG=y "$headers/.config"; then
    version=$(sed -n 's/^CONFIG_CLANG_VERSION=\([0-9]*\)$/\1/p' "$headers/.config")
    major=$((version / 10000))
    arguments+=("LLVM=-$major" LLVM_IAS=1 "CC=clang-$major" "LD=ld.lld-$major")
fi
# Ignore DKMS's appended compiler defaults and build against the selected headers.
exec make -C "$headers" M="$PWD" "${arguments[@]}" modules
