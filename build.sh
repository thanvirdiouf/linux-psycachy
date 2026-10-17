#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-only
set -Eeuo pipefail

usage() {
    echo "Usage: $0 <version> [--prepare-only]"
    echo "Example: $0 7.2.9"
    echo "Default: Clang/LLVM with ThinLTO. Set TOOLCHAIN=gcc for a GCC build."
    echo "BUILD_PROFILE=full (default) retains BTF/sched_ext; minimal explicitly omits them."
}

if [[ ${1:-} == --help ]]; then
    usage
    exit 0
fi
if [[ $# -lt 1 || $# -gt 2 || ( $# -eq 2 && $2 != --prepare-only ) ]]; then
    usage >&2
    exit 1
fi

version=$1
if [[ ! $version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "Invalid kernel version: $version. Use a stable version such as 7.2.9." >&2
    exit 1
fi
repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
kernel_series=${version%.*}
release_manifest="$repo_dir/src/releases.tsv"
source_release=
source_sha256=
while read -r release_version release_name release_hash extra || [[ -n $release_version ]]; do
    [[ $release_version == "$version" ]] || continue
    if [[ -n $source_release ]]; then
        echo "Duplicate kernel version in $release_manifest: $version" >&2
        exit 1
    fi
    source_prefix="cachyos-$version-"
    if [[ $release_name != "$source_prefix"* || ! ${release_name#"$source_prefix"} =~ ^[0-9]+$ || ! $release_hash =~ ^[a-f0-9]{64}$ || -n $extra ]]; then
        echo "Invalid source release entry for $version in $release_manifest." >&2
        exit 1
    fi
    source_release=$release_name
    source_sha256=$release_hash
done < "$release_manifest"
if [[ -z $source_release ]]; then
    echo "Unsupported kernel version: $version. No pinned source release in $release_manifest." >&2
    exit 1
fi
patch_dir="$repo_dir/src/patches/$kernel_series"
patches=("$patch_dir/0001-bore-cachy.patch"
    "$repo_dir/src/patches/common/0002-debian-headers-config.patch")
locked_patches=0
if [[ -f $repo_dir/src/patches/releases.json ]]; then
    patch_list=$(python3 "$repo_dir/scripts/patchsets.py" list "$repo_dir" "$version")
    mapfile -t patches <<< "$patch_list"
    locked_patches=1
fi
for patch_file in "${patches[@]}"; do
    if [[ ! -f $patch_file ]]; then
        echo "Missing patch for kernel series $kernel_series: $patch_file" >&2
        exit 1
    fi
done
if [[ $(uname -m) != x86_64 ]]; then
    echo "The bundled configuration requires an x86_64 build host." >&2
    exit 1
fi
cpu_count=$(nproc)
jobs=${JOBS:-$((cpu_count > 2 ? cpu_count - 2 : 1))}
if [[ ! $jobs =~ ^[1-9][0-9]*$ ]]; then
    echo "JOBS must be a positive integer." >&2
    exit 1
fi
build_cache=${BUILD_CACHE:-0}
thinlto_tuning=${THINLTO_TUNING:-1}
for setting in build_cache thinlto_tuning; do
    if [[ ${!setting} != 0 && ${!setting} != 1 ]]; then
        echo "BUILD_CACHE and THINLTO_TUNING must be 0 or 1." >&2
        exit 1
    fi
done
module_link_jobs=${MODULE_LINK_JOBS:-1}
kernel_link_jobs=${KERNEL_LINK_JOBS:-$jobs}
for setting in module_link_jobs kernel_link_jobs; do
    if [[ ! ${!setting} =~ ^[1-9][0-9]*$ ]]; then
        echo "MODULE_LINK_JOBS and KERNEL_LINK_JOBS must be positive integers." >&2
        exit 1
    fi
done

archive="$repo_dir/src/$source_release.tar.gz"
toolchain=${TOOLCHAIN:-clang-thinlto}
build_profile=${BUILD_PROFILE:-full}
if [[ $build_profile != full && $build_profile != minimal ]]; then
    echo "BUILD_PROFILE must be full or minimal." >&2
    exit 1
fi
if [[ -n ${CLANG_AUTOFDO_PROFILE:-} ]]; then
    CLANG_AUTOFDO_PROFILE=$(realpath -- "$CLANG_AUTOFDO_PROFILE")
    export CLANG_AUTOFDO_PROFILE
fi
if [[ -n ${CLANG_PROPELLER_PROFILE_PREFIX:-} ]]; then
    CLANG_PROPELLER_PROFILE_PREFIX=$(realpath -m -- "$CLANG_PROPELLER_PROFILE_PREFIX")
    export CLANG_PROPELLER_PROFILE_PREFIX
fi
release_revision=1
if [[ $locked_patches == 1 ]]; then
    release_revision=$(python3 "$repo_dir/scripts/patchsets.py" debian-revision "$repo_dir" "$version")
fi
builder_revision=1
if [[ -f $repo_dir/src/package-revision ]]; then builder_revision=$(<"$repo_dir/src/package-revision"); fi
builder_revision=${PACKAGE_REVISION:-$builder_revision}
if [[ ! $release_revision =~ ^[1-9][0-9]*$ || ! $builder_revision =~ ^[1-9][0-9]*$ ]]; then
    echo "Package revisions must be positive integers." >&2
    exit 1
fi
revision=$((builder_revision + release_revision - 1))
make_args=()
case "$toolchain" in
    clang-thinlto)
        llvm_version=${LLVM_VERSION:-$(<"$repo_dir/src/llvm-version")}
        if [[ ! $llvm_version =~ ^[1-9][0-9]*$ ]]; then
            echo "LLVM_VERSION must be a positive major version." >&2
            exit 1
        fi
        export LLVM="-$llvm_version" LLVM_IAS=1
        make_args=("LLVM=$LLVM" LLVM_IAS=1)
        build_dir="$repo_dir/src/build-$version-clang-thinlto"
        local_version=-psycachy-llvm
        package_version="$version-$((2 * revision))"
        tools=(clang clang++ ld.lld llvm-ar llvm-nm llvm-strip llvm-objcopy llvm-objdump llvm-readelf)
        for index in "${!tools[@]}"; do tools[$index]="${tools[$index]}-$llvm_version"; done
        ;;
    gcc)
        unset LLVM LLVM_IAS
        make_args=(CC=gcc HOSTCC=gcc HOSTCXX=g++)
        build_dir="$repo_dir/src/build-$version"
        local_version=-psycachy
        package_version="$version-$((2 * revision - 1))"
        tools=(gcc g++ ld ar nm strip objcopy objdump readelf)
        ;;
    *)
        echo "Unsupported TOOLCHAIN: $toolchain. Use clang-thinlto or gcc." >&2
        exit 1
        ;;
esac
if [[ $build_profile == minimal ]]; then
    build_dir+=-minimal
    local_version+=-minimal
fi
if (( revision > 1 )); then
    local_version+="-r$revision"
    build_dir+="-r$revision"
fi
package_output="$repo_dir/src/packages/$version-$toolchain-r$revision-$build_profile"
if [[ ${2:-} != --prepare-only && -e $package_output ]]; then
    echo "Package output already exists: $package_output" >&2
    echo "Move it aside, or increment src/package-revision for a changed build." >&2
    exit 1
fi
staging_dir=
trap 'echo "Build failed at line $LINENO." >&2' ERR
trap 'if [[ -n $staging_dir ]]; then rm -rf -- "$staging_dir"; fi' EXIT

# Reject unsupported versions before downloading or installing dependencies.
dependencies=(build-essential bc bison flex libssl-dev libelf-dev libdw-dev libncurses-dev
    pkg-config python3 perl openssl curl zstd xz-utils pahole)
if [[ $toolchain == clang-thinlto ]]; then
    dependencies+=("clang-$llvm_version" "lld-$llvm_version" "llvm-$llvm_version")
fi
if [[ $build_cache == 1 ]]; then
    dependencies+=(ccache git)
fi
if [[ ${2:-} != --prepare-only ]]; then
    dependencies+=(git cpio kmod rsync fakeroot debhelper)
fi
missing=()
for dep in "${dependencies[@]}"; do
    if [[ $(dpkg-query -W -f='${db:Status-Status}' "$dep" 2>/dev/null || true) != installed ]]; then
        missing+=("$dep")
    fi
done
if [[ ${#missing[@]} -gt 0 ]]; then
    if [[ ${INSTALL_DEPS:-1} == 0 ]]; then
        echo "Missing Debian packages: ${missing[*]}; using supplied tools (INSTALL_DEPS=0)."
    else
        echo "Installing missing dependencies: ${missing[*]}"
        sudo apt-get update
        sudo apt-get install -y "${missing[@]}"
    fi
fi

if [[ $build_profile == full ]]; then
    pahole_bin=$(command -v "${PAHOLE:-pahole}") || {
        echo "Full builds require pahole >= 1.26; install it or select BUILD_PROFILE=minimal." >&2
        exit 1
    }
    make_args+=("PAHOLE=$pahole_bin")
    tools+=("$pahole_bin")
fi
# Record the toolchain and reject reuse after a compiler or linker change.
toolchain_info=$(printf 'Toolchain: %s\n' "$toolchain"
    for tool in "${tools[@]}"; do
        if ! command -v "$tool" >/dev/null; then
            echo "Required tool is missing: $tool" >&2
            exit 1
        fi
        output=$("$tool" --version)
        if [[ $toolchain == clang-thinlto && $tool == "clang-$llvm_version" && ! $output =~ clang\ version\ $llvm_version\. ]]; then
            echo "Compiler does not match LLVM_VERSION=$llvm_version: $tool" >&2
            exit 1
        fi
        if [[ $toolchain == clang-thinlto && $tool == "ld.lld-$llvm_version" && ! $output =~ LLD\ $llvm_version\. ]]; then
            echo "Linker does not match LLVM_VERSION=$llvm_version: $tool" >&2
            exit 1
        fi
        printf '%s: %s\n' "$tool" "${output%%$'\n'*}"
    done)
toolchain_id=$(printf '%s\n' "$toolchain_info" | sha256sum | cut -d ' ' -f1)
if [[ -f $build_dir/.psycachy-toolchain && $(<"$build_dir/.psycachy-toolchain") != "$toolchain_id" ]]; then
    echo "Existing build tree uses a different compiler or linker: $build_dir" >&2
    echo "Move it aside before changing toolchains." >&2
    exit 1
fi
printf '%s\n' "$toolchain_info"

# Keep upstream Kbuild linker flags intact: the wrapper adds only resource/cache
# options, including for intermediate module links where ThinLTO also runs.
if [[ $toolchain == clang-thinlto && ( $thinlto_tuning == 1 || $build_cache == 1 ) ]]; then
    export PATH="$repo_dir/scripts:$PATH"
    export PSYCACHY_REAL_LD="$(command -v "ld.lld-$llvm_version")"
    export PSYCACHY_MODULE_LINK_JOBS="$module_link_jobs"
    export PSYCACHY_KERNEL_LINK_JOBS="$kernel_link_jobs"
    export PSYCACHY_THINLTO_TUNING="$thinlto_tuning"
    if [[ $build_cache == 1 ]]; then
        export PSYCACHY_THINLTO_CACHE="$repo_dir/.cache/thinlto"
        mkdir -p "$PSYCACHY_THINLTO_CACHE"
    else
        unset PSYCACHY_THINLTO_CACHE
    fi
    make_args+=(LD=psycachy-ld)
fi
if [[ $build_cache == 1 ]]; then
    command -v ccache >/dev/null || { echo "Required tool is missing: ccache" >&2; exit 1; }
    export CCACHE_DIR="$repo_dir/.cache/ccache" CCACHE_BASEDIR="$repo_dir"
    export CCACHE_COMPILERCHECK=content CCACHE_MAXSIZE=2Gi
    mkdir -p "$CCACHE_DIR"
    if [[ $toolchain == clang-thinlto ]]; then
        make_args+=("CC=ccache clang-$llvm_version")
    else
        make_args+=("CC=ccache gcc")
    fi
    # Avoid wall-clock build timestamps defeating cache reuse. Respect explicit
    # timestamps supplied by the caller; use the source commit otherwise.
    if [[ ! ${KBUILD_BUILD_TIMESTAMP+x} ]]; then
        KBUILD_BUILD_TIMESTAMP=$(git -C "$repo_dir" show -s --format=%cD HEAD)
        export KBUILD_BUILD_TIMESTAMP
    fi
fi

# A completed preparation can be reused after an interrupted compilation.
patch_fingerprint=$(
    for patch_file in "${patches[@]}"; do
        sha256sum < "$patch_file" | cut -d ' ' -f1
    done | sha256sum | cut -d ' ' -f1
)
if [[ $locked_patches == 1 ]]; then
    config_id=$(python3 "$repo_dir/scripts/patchsets.py" config-id "$repo_dir" "$version")
    if [[ -n $config_id ]]; then
        patch_fingerprint=$(printf '%s\n%s\n' "$patch_fingerprint" "$config_id" | sha256sum | cut -d ' ' -f1)
    fi
fi
preparation_id="$source_sha256:$patch_fingerprint"
if [[ -e $build_dir ]]; then
    # Upgrade the old stamp only when the source and ordered patch contents match.
    legacy_fingerprint=$(
        for patch_file in "${patches[@]}"; do
            digest=$(sha256sum "$patch_file")
            printf '%s  %s/src/patches/%s/%s\n' "${digest%% *}" "$repo_dir" "$version" "${patch_file##*/}"
        done | sha256sum | cut -d ' ' -f1
    )
    if [[ -f $build_dir/.psycachy-prepared && $(<"$build_dir/.psycachy-prepared") == "$source_sha256:$legacy_fingerprint" ]]; then
        printf '%s\n' "$preparation_id" > "$build_dir/.psycachy-prepared"
        echo "Updated prepared-source fingerprint for the shared patch layout."
    fi
    if [[ ! -f $build_dir/.psycachy-prepared || $(<"$build_dir/.psycachy-prepared") != "$preparation_id" ]]; then
        echo "Existing build tree has different or incomplete patches: $build_dir" >&2
        echo "Move it aside and rerun the builder to prepare a fresh tree." >&2
        exit 1
    fi
    echo "Reusing prepared source: $build_dir"
else
    if [[ ! -f $archive ]]; then
        echo "Downloading $source_release..."
        curl --fail --location --retry 3 --output "$archive.part" \
            "https://github.com/CachyOS/linux/releases/download/$source_release/$source_release.tar.gz"
        echo "$source_sha256  $archive.part" | sha256sum --check --status
        mv -- "$archive.part" "$archive"
    fi
    echo "$source_sha256  $archive" | sha256sum --check
    staging_dir=$(mktemp -d "$repo_dir/src/.prepare-$version.XXXXXX")
    echo "Extracting $source_release..."
    tar -xzf "$archive" -C "$staging_dir" --strip-components=1
    for patch_file in "${patches[@]}"; do
        echo "Checking patch: ${patch_file##*/}"
        patch --batch --forward --fuzz=0 -p1 --dry-run -d "$staging_dir" -i "$patch_file"
        patch --batch --forward --fuzz=0 -p1 -d "$staging_dir" -i "$patch_file"
    done
    printf '%s\n' "$preparation_id" > "$staging_dir/.psycachy-prepared"
    mv -- "$staging_dir" "$build_dir"
    staging_dir=
fi

cd -- "$build_dir"
printf '%s\n' "$toolchain_id" > .psycachy-toolchain
printf '%s\n' "$toolchain_info" > TOOLCHAIN.txt
effective_thinlto_tuning=0
if [[ $toolchain == clang-thinlto ]]; then effective_thinlto_tuning=$thinlto_tuning; fi
printf 'Compiler cache: %s\nThinLTO tuning: %s\nMake jobs: %s\nModule link jobs: %s\nKernel link jobs: %s\nBuild timestamp: %s\n' \
    "$build_cache" "$effective_thinlto_tuning" "$jobs" "$module_link_jobs" "$kernel_link_jobs" \
    "${KBUILD_BUILD_TIMESTAMP-automatic}" > BUILD-TUNING.txt
base_config="$repo_dir/src/config"
if [[ $locked_patches == 1 ]]; then
    base_config=$(python3 "$repo_dir/scripts/patchsets.py" base-config "$repo_dir" "$version")
fi
cp -- "$base_config" .config
if [[ -f $repo_dir/src/debian.config ]]; then
    python3 "$repo_dir/scripts/kernel_config.py" .config "$repo_dir/src/debian.config"
fi
if [[ $locked_patches == 1 ]]; then
    python3 "$repo_dir/scripts/patchsets.py" configure "$repo_dir" "$version" "$build_dir"
fi

if [[ $toolchain == clang-thinlto ]]; then
    scripts/config --disable LTO_NONE --disable LTO_CLANG_FULL \
        --disable LTO_CLANG_THIN_DIST --enable LTO_CLANG_THIN
else
    scripts/config --disable LTO_CLANG_FULL --disable LTO_CLANG_THIN \
        --disable LTO_CLANG_THIN_DIST --enable LTO_NONE
fi

if [[ $build_profile == full ]]; then
    if [[ $(scripts/pahole-version.sh "${PAHOLE:-pahole}") -lt 126 ]]; then
        echo "Full builds require pahole >= 1.26 for BTF/sched_ext. Install it or explicitly set BUILD_PROFILE=minimal." >&2
        exit 1
    fi
    scripts/config --disable DEBUG_INFO_NONE --disable DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT \
        --disable DEBUG_INFO_DWARF4 --enable DEBUG_INFO_DWARF5 \
        --enable DEBUG_INFO_BTF --enable DEBUG_INFO_BTF_MODULES --enable SCHED_CLASS_EXT
else
    echo "Minimal profile selected: omitting debug information, BTF and sched_ext."
    scripts/config --disable DEBUG_INFO_BTF --disable DEBUG_INFO_BTF_MODULES \
        --disable SCHED_CLASS_EXT --disable DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT \
        --disable DEBUG_INFO_DWARF4 --disable DEBUG_INFO_DWARF5 --enable DEBUG_INFO_NONE \
        --disable GENDWARFKSYMS --enable GENKSYMS
fi
# Profiling is explicit: never silently claim a profile-guided build.
scripts/config --disable AUTOFDO_CLANG --disable PROPELLER_CLANG
if [[ -n ${CLANG_AUTOFDO_PROFILE:-}${CLANG_PROPELLER_PROFILE_PREFIX:-} && $toolchain != clang-thinlto ]]; then
    echo "AutoFDO/Propeller profiles require the Clang toolchain." >&2
    exit 1
fi
if [[ -n ${CLANG_AUTOFDO_PROFILE:-} ]]; then
    CLANG_AUTOFDO_PROFILE=$(realpath -- "$CLANG_AUTOFDO_PROFILE")
    test -s "$CLANG_AUTOFDO_PROFILE"
    export CLANG_AUTOFDO_PROFILE
    scripts/config --enable AUTOFDO_CLANG
    make_args+=("CLANG_AUTOFDO_PROFILE=$CLANG_AUTOFDO_PROFILE")
fi
if [[ -n ${CLANG_PROPELLER_PROFILE_PREFIX:-} ]]; then
    CLANG_PROPELLER_PROFILE_PREFIX=$(realpath -m -- "$CLANG_PROPELLER_PROFILE_PREFIX")
    test -s "${CLANG_PROPELLER_PROFILE_PREFIX}_cc_profile.txt"
    test -s "${CLANG_PROPELLER_PROFILE_PREFIX}_ld_profile.txt"
    export CLANG_PROPELLER_PROFILE_PREFIX
    scripts/config --enable PROPELLER_CLANG
    make_args+=("CLANG_PROPELLER_PROFILE_PREFIX=$CLANG_PROPELLER_PROFILE_PREFIX")
fi
make "${make_args[@]}" olddefconfig
if [[ $locked_patches == 1 ]]; then
    python3 "$repo_dir/scripts/patchsets.py" verify-config "$repo_dir" "$version" "$build_dir"
fi
if [[ $build_profile == full ]]; then
    for symbol in DEBUG_INFO_BTF DEBUG_INFO_BTF_MODULES SCHED_CLASS_EXT; do
        if ! grep -qx "CONFIG_$symbol=y" .config; then
            echo "Required full-profile configuration is missing: CONFIG_$symbol" >&2
            exit 1
        fi
    done
fi
for setting in CLANG_AUTOFDO_PROFILE CLANG_PROPELLER_PROFILE_PREFIX; do
    if [[ -n ${!setting:-} ]]; then
        symbol=AUTOFDO_CLANG
        if [[ $setting == CLANG_PROPELLER_PROFILE_PREFIX ]]; then symbol=PROPELLER_CLANG; fi
        grep -qx "CONFIG_$symbol=y" .config || { echo "Unsupported profiling option: CONFIG_$symbol" >&2; exit 1; }
    fi
done
if [[ $toolchain == clang-thinlto ]]; then
    for symbol in CC_IS_CLANG LD_IS_LLD AS_IS_LLVM LTO_CLANG_THIN; do
        if ! grep -qx "CONFIG_$symbol=y" .config; then
            echo "Required Clang/ThinLTO option is missing: CONFIG_$symbol" >&2
            exit 1
        fi
    done
elif ! grep -qx 'CONFIG_CC_IS_GCC=y' .config || ! grep -qx 'CONFIG_LTO_NONE=y' .config; then
    echo "Required GCC configuration is missing." >&2
    exit 1
fi
for symbol in CACHY SCHED_BORE CC_OPTIMIZE_FOR_PERFORMANCE_O3; do
    if ! grep -qx "CONFIG_$symbol=y" .config; then
        echo "Required configuration option is missing: CONFIG_$symbol" >&2
        exit 1
    fi
done
for symbol in TCP_CONG_BBR3 MQ_IOSCHED_ADIOS; do
    if ! grep -Eq "^CONFIG_$symbol=[ym]$" .config; then
        echo "Required configuration option is missing: CONFIG_$symbol" >&2
        exit 1
    fi
done
if [[ $(make -s "${make_args[@]}" kernelversion) != "$version" ]]; then
    echo "Source version does not match $version." >&2
    exit 1
fi

if [[ ${2:-} == --prepare-only ]]; then
    make "${make_args[@]}" prepare
    echo "Kernel source and configuration prepared: $build_dir"
    exit 0
fi

export PSYCACHY_BUILD_PROFILE="$build_profile" PSYCACHY_BASE_CONFIG="$base_config"
export PSYCACHY_DEBIAN_CONFIG="$repo_dir/src/debian.config"
python3 - <<'PY'
import hashlib, json, os
from pathlib import Path
config = Path('.config').read_text().splitlines()
cpu_target = 'custom'
if 'CONFIG_GENERIC_CPU=y' in config:
    cpu_target = 'generic-x86-64-v' + next((line.split('=', 1)[1] for line in config if line.startswith('CONFIG_X86_64_VERSION=')), '1')
elif 'CONFIG_MZEN4=y' in config:
    cpu_target = 'zen4'
elif 'CONFIG_X86_NATIVE_CPU=y' in config:
    cpu_target = 'native'
data = {"profile": os.environ['PSYCACHY_BUILD_PROFILE'], "cpu_target": cpu_target,
        "base_config_sha256": hashlib.sha256(Path(os.environ['PSYCACHY_BASE_CONFIG']).read_bytes()).hexdigest(),
        "autofdo": None, "propeller": None}
def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
if Path(os.environ['PSYCACHY_DEBIAN_CONFIG']).is_file():
    data['debian_config_sha256'] = digest(os.environ['PSYCACHY_DEBIAN_CONFIG'])
if path := os.environ.get('CLANG_AUTOFDO_PROFILE'):
    data['autofdo'] = {"sha256": digest(path)}
if prefix := os.environ.get('CLANG_PROPELLER_PROFILE_PREFIX'):
    data['propeller'] = {name: digest(prefix + '_' + name + '_profile.txt') for name in ('cc', 'ld')}
Path('OPTIMIZATIONS.json').write_text(json.dumps(data, indent=2) + '\n')
PY
if [[ ${BUILD_DEBUG_PACKAGE:-0} != 1 ]]; then
    export DEB_BUILD_PROFILES="${DEB_BUILD_PROFILES:+$DEB_BUILD_PROFILES }pkg.linux-upstream.nokerneldbg"
fi
make "${make_args[@]}" bindeb-pkg -j"$jobs" "LOCALVERSION=$local_version" "KDEB_PKGVERSION=$package_version"
collector_args=()
if [[ $toolchain == clang-thinlto ]]; then
    nvidia_package=$(python3 "$repo_dir/scripts/package_nvidia.py" --llvm-version "$llvm_version" --output "$repo_dir/src" | tail -n 1)
    collector_args+=(--nvidia-package "$nvidia_package")
fi
cp -- "$repo_dir/scripts/install-packages.sh" "$repo_dir/src/install-packages.sh"
if [[ $locked_patches == 1 ]]; then
    python3 "$repo_dir/scripts/patchsets.py" metadata "$repo_dir" "$version" > "$build_dir/PATCHES.json"
fi
python3 "$repo_dir/scripts/collect_packages.py" --source "$repo_dir/src" --output "$package_output" \
    --tree "$build_dir" --kernel-release "$version$local_version" \
    --package-version "$package_version" --toolchain "$toolchain" "${collector_args[@]}"
printf '%s\n' "$package_output" > "$repo_dir/src/last-package-output"
echo "Kernel build complete. Installable package bundle: $package_output"
