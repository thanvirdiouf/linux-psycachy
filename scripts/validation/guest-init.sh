#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-only
# PID 1 in the disposable QEMU guest. No network services are started.
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
. /etc/psycachy-validation.env
finish() {
    status=$?
    trap - EXIT
    if (( status == 0 )); then
        echo "PSYCACHY_VALIDATION_OK:$VALIDATION_NONCE"
    else
        echo "PSYCACHY_VALIDATION_FAILED:$VALIDATION_NONCE:exit=$status"
    fi
    sync
    /bin/busybox poweroff -f
    while :; do sleep 1; done
}
trap finish EXIT
check() { echo "PSYCACHY_CHECK:$1:$VALIDATION_NONCE"; }
set -x
mountpoint -q /proc || mount -t proc proc /proc
mountpoint -q /sys || mount -t sysfs sysfs /sys
mountpoint -q /dev || mount -t devtmpfs devtmpfs /dev
mountpoint -q /run || mount -t tmpfs tmpfs /run
test "$(uname -r)" = "$KERNEL_RELEASE"
check running_kernel
for package in "linux-image-$KERNEL_RELEASE" "linux-headers-$KERNEL_RELEASE" "$IMAGE_META" "$HEADERS_META"; do
    test "$(dpkg-query -W -f '${db:Status-Status}' "$package")" = installed
done
test -z "$(dpkg --audit)"
check package_install
test "$(dpkg-query -W -f '${Version}' linux-libc-dev)" = "$(cat /etc/psycachy-libc-version)"
check libc_headers_retained
dkms status -m psycachy-ci -v 1.0 -k "$KERNEL_RELEASE" | grep -F ': installed'
check dkms_autoinstall
modprobe psycachy_ci
grep '^psycachy_ci ' /proc/modules
test "$(modinfo -F vermagic psycachy_ci | cut -d' ' -f1)" = "$KERNEL_RELEASE"
check module_load
zcat /proc/config.gz > /run/running.config
cmp /run/running.config "/boot/config-$KERNEL_RELEASE"
check config_matches
if (( EXPECT_BTF )); then
    grep -Fx 'CONFIG_DEBUG_INFO_BTF=y' /run/running.config
    test -s /sys/kernel/btf/vmlinux
else
    # Kconfig can omit symbols entirely when their dependencies are disabled.
    if grep -Eq '^CONFIG_DEBUG_INFO_BTF=[ym]$' /run/running.config; then exit 1; fi
    test ! -e /sys/kernel/btf/vmlinux
fi
check btf
if (( EXPECT_SCX )); then
    grep -Fx 'CONFIG_SCHED_CLASS_EXT=y' /run/running.config
    test -d /sys/kernel/sched_ext
else
    if grep -Eq '^CONFIG_SCHED_CLASS_EXT=[ym]$' /run/running.config; then exit 1; fi
    test ! -d /sys/kernel/sched_ext
fi
check sched_ext
update-initramfs -u -k "$KERNEL_RELEASE"
test -s "/boot/initrd.img-$KERNEL_RELEASE"
check initramfs
mkdir -p /boot/grub
update-grub
grep -F "$KERNEL_RELEASE" /boot/grub/grub.cfg
check grub_config
