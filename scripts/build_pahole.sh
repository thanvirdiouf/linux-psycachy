#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-only
# Build a pinned pahole for hosts whose distribution version predates BTF needs.
set -euo pipefail
if (( $# != 1 )); then echo "Usage: $0 <installation-prefix>" >&2; exit 2; fi
prefix=$(realpath -m -- "$1")
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
# Upstream dwarves v1.30, peeled commit from its signed tag.
commit=52e071dbb1b6886471bf2c90eab95de89c5b3e0d
git -C "$work" init --quiet
git -C "$work" remote add origin https://github.com/acmel/dwarves.git
git -C "$work" fetch --depth=1 origin "$commit"
git -C "$work" checkout --detach --quiet FETCH_HEAD
test "$(git -C "$work" rev-parse HEAD)" = "$commit"
cmake -S "$work" -B "$work/build" -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="$prefix" -DLIBBPF_EMBEDDED=OFF -DBUILD_SHARED_LIBS=OFF
cmake --build "$work/build" --parallel 2
cmake --install "$work/build"
"$prefix/bin/pahole" --version
