#!/bin/bash
set -Eeuo pipefail

usage() {
    echo "Usage: $0 <version> [--prepare-only]"
    echo "Example: $0 7.2.9"
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

archive="$repo_dir/src/$source_release.tar.gz"
build_dir="$repo_dir/src/build-$version"
staging_dir=
trap 'echo "Build failed at line $LINENO." >&2' ERR
trap 'if [[ -n $staging_dir ]]; then rm -rf -- "$staging_dir"; fi' EXIT

# Reject unsupported versions before downloading or installing dependencies.
dependencies=(build-essential bc bison flex libssl-dev libelf-dev libdw-dev libncurses-dev
    pkg-config python3 perl openssl curl zstd xz-utils)
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
cp -- "$repo_dir/src/config" .config
if [[ $locked_patches == 1 ]]; then
    python3 "$repo_dir/scripts/patchsets.py" configure "$repo_dir" "$version" "$build_dir"
fi

# Ubuntu LTS ships older pahole; BORE itself does not require BTF.
if [[ $(scripts/pahole-version.sh "${PAHOLE:-pahole}") -lt 126 ]]; then
    echo "pahole >= 1.26 unavailable; disabling BTF and sched_ext for this build."
    scripts/config --disable DEBUG_INFO_BTF --disable DEBUG_INFO_BTF_MODULES \
        --disable SCHED_CLASS_EXT
fi
make CC=gcc olddefconfig
if [[ $locked_patches == 1 ]]; then
    python3 "$repo_dir/scripts/patchsets.py" verify-config "$repo_dir" "$version" "$build_dir"
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
if [[ $(make -s CC=gcc kernelversion) != "$version" ]]; then
    echo "Source version does not match $version." >&2
    exit 1
fi

if [[ ${2:-} == --prepare-only ]]; then
    make CC=gcc prepare
    echo "Kernel source and configuration prepared: $build_dir"
    exit 0
fi

make CC=gcc bindeb-pkg -j"$jobs" LOCALVERSION=-psycachy KDEB_PKGVERSION="$version-1"
echo "Kernel build complete. Debian packages are in $repo_dir/src."
