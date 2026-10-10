# SPDX-License-Identifier: GPL-2.0-only
# Sourced by DKMS after the distribution's NVIDIA dkms.conf. More-specific
# administrator overrides are still read afterwards by DKMS.
if [[ ${kernelver:-} =~ ^[0-9]+\.[0-9]+\.[0-9]+-psycachy-llvm$ &&
      -r ${kernel_source_dir:-}/.config ]] &&
   grep -qx 'CONFIG_CC_IS_CLANG=y' "$kernel_source_dir/.config" &&
   grep -qx 'CONFIG_LD_IS_LLD=y' "$kernel_source_dir/.config"; then
    _psycachy_clang_version=$(sed -n 's/^CONFIG_CLANG_VERSION=\([0-9][0-9]*\)$/\1/p' "$kernel_source_dir/.config")
    unset MAKE MAKE_MATCH
    if [[ $_psycachy_clang_version =~ ^[1-9][0-9]{5,}$ ]]; then
        _psycachy_llvm=$((_psycachy_clang_version / 10000))
        MAKE[0]="/usr/lib/psycachy-nvidia-support/build $_psycachy_llvm '$kernelver' '$kernel_source_dir'"
    else
        echo "PsyCachy: missing or invalid CONFIG_CLANG_VERSION in $kernel_source_dir/.config" >&2
        MAKE[0]="false"
    fi
    unset _psycachy_clang_version _psycachy_llvm
fi
