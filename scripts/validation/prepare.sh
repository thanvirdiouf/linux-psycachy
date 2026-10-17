#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-only
# Runs only in the disposable debootstrap chroot, before the first guest boot.
set -euo pipefail
kernel=$1
compiler=$2
apt-get update
packages=(sudo initramfs-tools dkms build-essential kmod libelf-dev libssl-dev
          bc flex bison dwarves busybox-static grub-common grub2-common udev)
if [[ $compiler != gcc ]]; then
    packages+=("clang-$compiler" "llvm-$compiler" "lld-$compiler")
fi
apt-get install -y --no-install-recommends "${packages[@]}"
printf '%s\n' 'RESUME=none' > /etc/initramfs-tools/conf.d/resume
printf '%s\n' virtio_pci virtio_blk ext4 >> /etc/initramfs-tools/modules
mkdir -p /etc/default/grub.d
printf '%s\n' 'GRUB_DISABLE_OS_PROBER=true' > /etc/default/grub.d/99-validation.cfg
dpkg-query -W -f '${Version}' linux-libc-dev > /etc/psycachy-libc-version
# Register before the kernel transaction: its actual postinst/header hooks must
# build and install this module. No explicit dkms autoinstall masks hook failures.
dkms add -m psycachy-ci -v 1.0
# The distro GRUB kernel hook skips generation until a config exists. Generate
# that config only after boot, when /dev/vda is the real guest root device.
test ! -e /boot/grub/grub.cfg
bash /opt/psycachy-packages/install-packages.sh /opt/psycachy-packages
test "$(dpkg-query -W -f '${Version}' linux-libc-dev)" = "$(cat /etc/psycachy-libc-version)"
dkms status -m psycachy-ci -v 1.0 -k "$kernel" | grep -F ': installed'
test "$(modinfo -k "$kernel" -F vermagic psycachy_ci | cut -d' ' -f1)" = "$kernel"
test -s "/boot/initrd.img-$kernel"
test -z "$(dpkg --audit)"
