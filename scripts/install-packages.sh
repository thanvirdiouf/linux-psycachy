#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-only
set -euo pipefail

nvidia=0
if [[ ${1:-} == --nvidia ]]; then
    nvidia=1
    shift
fi
if (( $# > 1 )); then
    echo "Usage: bash $0 [--nvidia] [package-directory]" >&2
    exit 1
fi
cd -- "${1:-$(dirname -- "${BASH_SOURCE[0]}")}"
sha256sum --check SHA256SUMS
shopt -s nullglob
images=(linux-image-*.deb)
headers=(linux-headers-*.deb)
libc=(linux-libc-dev_*.deb)
support=(psycachy-nvidia-support_*.deb)
if (( ${#images[@]} != 1 || ${#headers[@]} != 1 )); then
    echo 'Use a directory containing exactly one build, with its image and headers.' >&2
    exit 1
fi
image=$(dpkg-deb --field "${images[0]}" Package)
header=$(dpkg-deb --field "${headers[0]}" Package)
kernel=${image#linux-image-}
if [[ $header != "linux-headers-$kernel" ]] ||
   [[ $(dpkg-deb --field "${images[0]}" Version) != "$(dpkg-deb --field "${headers[0]}" Version)" ]]; then
    echo 'The kernel image and headers must match.' >&2
    exit 1
fi
if (( nvidia )); then
    if [[ $kernel != *-psycachy-llvm ]] || (( ${#support[@]} != 1 )); then
        echo 'NVIDIA support requires a PsyCachy LLVM build and its support package.' >&2
        exit 1
    fi
    if ! command -v dkms >/dev/null || ! dkms status -m nvidia | grep -q '^nvidia/'; then
        echo 'Install the NVIDIA DKMS driver appropriate for your GPU from your distribution first.' >&2
        exit 1
    fi
    # Two APT transactions guarantee tools/configuration precede kernel hooks.
    sudo apt install "./${support[0]}"
fi
packages=("./${images[0]}" "./${headers[0]}")
for package in "${libc[@]}"; do packages+=("./$package"); done
sudo apt install "${packages[@]}"
if (( nvidia )); then
    if ! dkms status -m nvidia -k "$kernel" | grep -Fq ': installed'; then
        echo "NVIDIA is not installed for $kernel. Check /var/lib/dkms/nvidia/<driver-version>/build/make.log for the build failure." >&2
        exit 1
    fi
    modinfo -k "$kernel" -F vermagic nvidia
fi
echo "Installed $kernel. Restart and select it in your boot menu."
