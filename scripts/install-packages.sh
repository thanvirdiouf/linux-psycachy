#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-only
set -euo pipefail

usage() {
    echo "Usage: bash $0 [--nvidia] [--with-libc-dev] [package-directory]" >&2
}
fail() { echo "$*" >&2; exit 1; }
nvidia=0
with_libc=0
directory=
while (( $# )); do
    case $1 in
        --nvidia) nvidia=1 ;;
        --with-libc-dev) with_libc=1 ;;
        -h|--help) usage; exit 0 ;;
        --*) usage; exit 1 ;;
        *) [[ -z $directory ]] || { usage; exit 1; }; directory=$1 ;;
    esac
    shift
done
cd -- "${directory:-$(dirname -- "${BASH_SOURCE[0]}")}"
[[ -f SHA256SUMS && ! -L SHA256SUMS ]] || fail 'Missing regular SHA256SUMS file.'

# Validate names before sha256sum reads them, and require every .deb to be covered.
# Checksums detect corruption; authenticate their source before running this script.
declare -A checked=()
checksum_line='^([[:xdigit:]]{64})[[:space:]]([ *])(\./)?([A-Za-z0-9][A-Za-z0-9.+_:~-]*)$'
while IFS= read -r line || [[ -n $line ]]; do
    [[ $line =~ $checksum_line ]] || fail 'Invalid SHA256SUMS entry: use checksums of local basenames only.'
    name=${BASH_REMATCH[4]}
    [[ -z ${checked[$name]:-} ]] || fail "Duplicate checksum entry: $name"
    [[ -f $name && ! -L $name ]] || fail "Checksum target must be a regular file: $name"
    checked[$name]=1
done < SHA256SUMS
(( ${#checked[@]} )) || fail 'SHA256SUMS is empty.'
shopt -s nullglob
all_packages=(./*.deb)
(( ${#all_packages[@]} )) || fail 'No Debian packages found.'
for package in "${all_packages[@]}"; do
    [[ -n ${checked[${package#./}]:-} ]] || fail "Package is absent from SHA256SUMS: $package"
done
sha256sum --strict --check SHA256SUMS

images=() headers=() libc=() support=() image_meta=() header_meta=()
declare -A names=() versions=() architectures=()
for package in "${all_packages[@]}"; do
    names[$package]=$(dpkg-deb --field "$package" Package)
    versions[$package]=$(dpkg-deb --field "$package" Version)
    architectures[$package]=$(dpkg-deb --field "$package" Architecture)
    case ${names[$package]} in
        linux-image-psycachy|linux-image-psycachy-llvm|linux-image-psycachy-minimal|linux-image-psycachy-llvm-minimal) image_meta+=("$package") ;;
        linux-headers-psycachy|linux-headers-psycachy-llvm|linux-headers-psycachy-minimal|linux-headers-psycachy-llvm-minimal) header_meta+=("$package") ;;
        linux-image-*-dbg|linux-image-*-dbgsym) ;; # Optional debug symbols are never needed to boot.
        linux-image-*) images+=("$package") ;;
        linux-headers-*) headers+=("$package") ;;
        linux-libc-dev) libc+=("$package") ;;
        psycachy-nvidia-support) support+=("$package") ;;
    esac
done
if (( ${#images[@]} != 1 || ${#headers[@]} != 1 )); then
    fail 'Use a directory containing exactly one build, with its image and headers.'
fi
image=${images[0]}
header=${headers[0]}
kernel=${names[$image]#linux-image-}
version=${versions[$image]}
architecture=$(dpkg --print-architecture)
if [[ ! $kernel =~ ^[0-9][a-z0-9.+-]*-psycachy(-llvm)?(-minimal)?(-r[1-9][0-9]*)?$ ]] ||
   [[ ${names[$header]} != "linux-headers-$kernel" || -z $version || ${versions[$header]} != "$version" ]] ||
   [[ ${architectures[$image]} != "$architecture" || ${architectures[$header]} != "$architecture" ]]; then
    fail 'The PsyCachy image and headers must match in kernel release, version, and native architecture.'
fi
packages=("$image" "$header")
if (( ${#image_meta[@]} || ${#header_meta[@]} )); then
    (( ${#image_meta[@]} == 1 && ${#header_meta[@]} == 1 )) || fail 'Both matching kernel tracking packages are required.'
    suffix=${kernel##*-psycachy}
    suffix=${suffix%-r*}
    for kind in image headers; do
        if [[ $kind == image ]]; then package=${image_meta[0]}; else package=${header_meta[0]}; fi
        if [[ ${names[$package]} != "linux-$kind-psycachy$suffix" || ${versions[$package]} != "$version" ||
              ${architectures[$package]} != "$architecture" ||
              $(dpkg-deb --field "$package" Depends) != "linux-$kind-$kernel (= $version)" ]]; then
            fail 'Kernel tracking packages must depend on this exact image and headers build.'
        fi
        packages+=("$package")
    done
fi
if (( with_libc )); then
    (( ${#libc[@]} == 1 )) || fail '--with-libc-dev requires exactly one linux-libc-dev package.'
    [[ ${versions[${libc[0]}]} == "$version" && ${architectures[${libc[0]}]} == "$architecture" ]] ||
        fail 'linux-libc-dev must match this build and native architecture.'
    packages+=("${libc[0]}")
fi
if (( nvidia )); then
    if [[ ! $kernel =~ -psycachy-llvm(-minimal)?(-r[1-9][0-9]*)?$ ]] || (( ${#support[@]} != 1 )); then
        fail 'NVIDIA support requires a PsyCachy LLVM build and its support package.'
    fi
    [[ ${architectures[${support[0]}]} == "$architecture" ]] || fail 'NVIDIA support package architecture must match.'
    if ! command -v dkms >/dev/null || ! dkms status -m nvidia | grep -q '^nvidia/'; then
        fail 'Install the NVIDIA DKMS driver appropriate for your GPU from your distribution first.'
    fi
    # Two APT transactions guarantee tools/configuration precede kernel hooks.
    sudo apt install "${support[0]}"
fi
sudo apt install "${packages[@]}"
if (( nvidia )); then
    if ! dkms status -m nvidia -k "$kernel" | grep -Fq ': installed'; then
        fail "NVIDIA is not installed for $kernel. Check /var/lib/dkms/nvidia/<driver-version>/build/make.log for the build failure."
    fi
    modinfo -k "$kernel" -F vermagic nvidia
fi
echo "Installed $kernel. Restart and select it in your boot menu."
