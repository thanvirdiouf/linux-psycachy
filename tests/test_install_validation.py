# SPDX-License-Identifier: GPL-2.0-only
"""Verify install-validation plans and failure handling without root or a VM."""

import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from scripts import validate_install


@unittest.skipUnless(shutil.which("dpkg-deb"), "requires Debian package archive tools")
class InstallValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="psycachy-install-plan-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.packages = self.root / "bundle"
        self.packages.mkdir()
        self.kernel = "7.2.9-psycachy-llvm-minimal-r3"
        self.version = "7.2.9-3"
        self.config = ("CONFIG_CC_IS_CLANG=y\nCONFIG_CLANG_VERSION=180108\n"
                       "CONFIG_DEBUG_INFO_BTF=y\nCONFIG_SCHED_CLASS_EXT=y\n")
        self.make_package(f"linux-image-{self.kernel}", contents={
            f"boot/config-{self.kernel}": self.config, f"boot/vmlinuz-{self.kernel}": "dummy image"})
        self.make_package(f"linux-headers-{self.kernel}", contents={
            f"usr/src/linux-headers-{self.kernel}/.config": self.config})
        for kind in ("image", "headers"):
            self.make_package(f"linux-{kind}-psycachy-llvm-minimal",
                              depends=f"linux-{kind}-{self.kernel} (= {self.version})")
        (self.packages / "install-packages.sh").write_text("#!/bin/bash\nexit 99\n")
        self.write_sums()

    def make_package(self, name, version=None, contents=None, depends=None):
        stage = self.root / name
        if stage.exists():
            shutil.rmtree(stage)
        (stage / "DEBIAN").mkdir(parents=True)
        control = (f"Package: {name}\nVersion: {version or self.version}\nArchitecture: amd64\n"
                   "Maintainer: CI <ci@example.invalid>\nDescription: Offline validation fixture\n")
        if depends:
            control += f"Depends: {depends}\n"
        (stage / "DEBIAN/control").write_text(control)
        for target, content in (contents or {}).items():
            path = stage / target
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        subprocess.run(["dpkg-deb", "--build", "--root-owner-group", str(stage),
                        str(self.packages / f"{name}.deb")], check=True, capture_output=True)

    def write_sums(self):
        files = sorted(self.packages.glob("*.deb")) + [self.packages / "install-packages.sh"]
        (self.packages / "SHA256SUMS").write_text("".join(
            f"{validate_install.checksum(path)}  {path.name}\n" for path in files))

    def test_reads_real_deb_headers_and_preserves_exact_abi_revision(self):
        plan = validate_install.inspect_bundle(self.packages)
        self.assertEqual(plan["kernel_release"], self.kernel)
        self.assertEqual(plan["version"], self.version)
        self.assertEqual(plan["llvm_major"], 18)
        self.assertTrue(plan["btf"])
        self.assertTrue(plan["sched_ext"])
        self.assertEqual(plan["image_meta"], "linux-image-psycachy-llvm-minimal")

    def test_dry_run_validates_without_root_or_executing_installer_or_creating_paths(self):
        output, work = self.root / "output", self.root / "work"
        stream = io.StringIO()
        with mock.patch.object(validate_install.os, "geteuid", return_value=1000), contextlib.redirect_stdout(stream):
            result = validate_install.main(["--packages", str(self.packages), "--distro", "debian",
                                            "--output", str(output), "--work-dir", str(work), "--dry-run"])
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(stream.getvalue())["suite"], "trixie")
        self.assertFalse(output.exists())
        self.assertFalse(work.exists())

    def test_checksums_reject_changed_missing_or_unsafe_targets(self):
        installer = self.packages / "install-packages.sh"
        installer.write_text("changed")
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            validate_install.verify_bundle(self.packages)
        self.write_sums()
        original = (self.packages / "SHA256SUMS").read_text()
        (self.packages / "SHA256SUMS").write_text(original.replace("  install-packages.sh", "  ../install-packages.sh"))
        with self.assertRaisesRegex(ValueError, "local basenames"):
            validate_install.verify_bundle(self.packages)
        (self.packages / "SHA256SUMS").write_text("\n".join(original.splitlines()[:-1]) + "\n")
        with self.assertRaisesRegex(ValueError, "must be covered"):
            validate_install.verify_bundle(self.packages)

    def test_checksum_symlinks_are_rejected(self):
        installer = self.packages / "install-packages.sh"
        target = self.root / "installer"
        installer.rename(target)
        installer.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "nonregular"):
            validate_install.verify_bundle(self.packages)

    def test_wrong_headers_version_and_feature_expectations_fail_before_privileged_work(self):
        self.make_package(f"linux-headers-{self.kernel}", version="7.2.9-1", contents={
            f"usr/src/linux-headers-{self.kernel}/.config": self.config})
        self.write_sums()
        with self.assertRaisesRegex(ValueError, "must match"):
            validate_install.inspect_bundle(self.packages)
        self.make_package(f"linux-headers-{self.kernel}", contents={
            f"usr/src/linux-headers-{self.kernel}/.config": self.config})
        self.write_sums()
        with self.assertRaisesRegex(ValueError, "BTF configuration"):
            validate_install.inspect_bundle(self.packages, expected_btf=False)
        with self.assertRaisesRegex(ValueError, "sched_ext configuration"):
            validate_install.inspect_bundle(self.packages, expected_scx=False)

    def test_minimal_config_can_omit_disabled_dependent_symbols(self):
        config = "CONFIG_CC_IS_CLANG=y\nCONFIG_CLANG_VERSION=180108\n"
        self.make_package(f"linux-headers-{self.kernel}", contents={
            f"usr/src/linux-headers-{self.kernel}/.config": config})
        self.write_sums()
        plan = validate_install.inspect_bundle(self.packages, expected_btf=False, expected_scx=False)
        self.assertFalse(plan["btf"])
        self.assertFalse(plan["sched_ext"])

    def test_guest_requires_matching_nonce_all_checks_and_success(self):
        log = self.root / "guest.log"
        nonce = "ephemeral-ci-token"
        markers = "".join(f"PSYCACHY_CHECK:{name}:{nonce}\r\n" for name in validate_install.CHECKS)
        log.write_text(markers + f"PSYCACHY_VALIDATION_OK:{nonce}\n")
        self.assertEqual(validate_install.check_guest(log, nonce), sorted(validate_install.CHECKS))
        with self.assertRaisesRegex(RuntimeError, "missing checks"):
            validate_install.check_guest(log, "different-token")
        log.write_text(markers)
        with self.assertRaisesRegex(RuntimeError, "failed or stopped"):
            validate_install.check_guest(log, nonce)
        log.write_text(markers + f"PSYCACHY_VALIDATION_OK:{nonce}\nPSYCACHY_VALIDATION_FAILED:{nonce}\n")
        with self.assertRaisesRegex(RuntimeError, "reported failure"):
            validate_install.check_guest(log, nonce)

    def test_qemu_direct_boot_has_no_network_and_disk_filename_is_structured(self):
        work = self.root / "spaces and,commas"
        command = validate_install.qemu_command(work, "tcg")
        self.assertEqual(command[command.index("-nic") + 1], "none")
        drive = json.loads(command[command.index("-blockdev") + 1])
        self.assertEqual(drive["filename"], str(work / "rootfs.ext4"))
        self.assertIn("root=/dev/vda", command[-1])
        self.assertIn("init=/usr/lib/psycachy-validation/guest-init.sh", command[-1])

    def test_command_timeout_terminates_child_and_retains_log(self):
        log = self.root / "timeout.log"
        with self.assertRaises(subprocess.TimeoutExpired):
            validate_install.run_logged([sys.executable, "-c", "import time; print('started', flush=True); time.sleep(60)"],
                                        log, timeout=0.2)
        self.assertIn("started", log.read_text())

    def test_only_explicitly_supported_distro_suites_are_used(self):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            validate_install.main(["--packages", str(self.packages), "--distro", "ubuntu",
                                   "--output", str(self.root / "output"), "--dry-run"])
        self.assertEqual(json.loads(stream.getvalue())["suite"], "noble")
        sources = validate_install.apt_sources("ubuntu", "noble")
        self.assertIn("noble-security main universe", sources)
        with self.assertRaisesRegex(ValueError, "Supported validation targets"):
            validate_install.main(["--packages", str(self.packages), "--distro", "ubuntu", "--suite", "arbitrary",
                                   "--output", str(self.root / "output"), "--dry-run"])


if __name__ == "__main__":
    unittest.main()
