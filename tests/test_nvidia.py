# SPDX-License-Identifier: GPL-2.0-only
"""Test optional packaging, kernel scoping, and install order without root."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts import package_nvidia

ROOT = Path(__file__).resolve().parents[1]


class NvidiaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="psycachy-nvidia-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.headers = self.root / "headers with spaces"
        self.headers.mkdir()
        (self.headers / ".config").write_text(
            "CONFIG_CC_IS_CLANG=y\nCONFIG_LD_IS_LLD=y\nCONFIG_CLANG_VERSION=180103\n"
        )

    def override(self, kernel, major="180103"):
        (self.headers / ".config").write_text(
            f"CONFIG_CC_IS_CLANG=y\nCONFIG_LD_IS_LLD=y\nCONFIG_CLANG_VERSION={major}\n"
        )
        result = subprocess.run([
            "bash", "-c",
            'kernelver=$1; kernel_source_dir=$2; MAKE[0]="original-gcc"; MAKE[1]="other-command"; '
            'MAKE_MATCH[1]=".*"; . "$3"; declare -p MAKE MAKE_MATCH 2>/dev/null || true',
            "test", kernel, str(self.headers), str(ROOT / "packaging/nvidia/override.sh")
        ], capture_output=True, text=True, check=True)
        return result.stdout

    def test_only_psycachy_llvm_kernels_are_overridden(self):
        for kernel in ("7.2.9-psycachy", "7.2.9-nitro-thinlto", "7.0.0-38-generic"):
            with self.subTest(kernel=kernel):
                output = self.override(kernel)
                self.assertIn("original-gcc", output)
                self.assertIn("other-command", output)
                self.assertNotIn("psycachy-nvidia-support/build", output)
        output = self.override("7.3.1-psycachy-llvm", "190107")
        self.assertIn("build 19", output)
        self.assertNotIn("original-gcc", output)
        self.assertNotIn("other-command", output)

    def test_invalid_toolchain_metadata_fails_build(self):
        for value in ("", "no-version", "180103; touch /tmp/never"):
            self.assertIn('"false"', self.override("7.2.9-psycachy-llvm", value))

    def test_wrapper_pins_tools_despite_dkms_appended_defaults(self):
        bins = self.root / "bin"
        bins.mkdir()
        for name in ("clang", "ld.lld", "llvm-ar", "llvm-nm", "llvm-objcopy",
                     "llvm-objdump", "llvm-readelf", "llvm-strip"):
            path = bins / (name + "-18")
            path.write_text("#!/bin/sh\nexit 0\n")
            path.chmod(0o755)
        path = bins / "make"
        path.write_text("#!/usr/bin/env python3\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n")
        path.chmod(0o755)
        result = subprocess.run([
            "bash", str(ROOT / "packaging/nvidia/build"), "18", "7.2.9-psycachy-llvm",
            str(self.headers), "CC=clang", "LD=ld.lld", "KERNELRELEASE=7.2.9-psycachy-llvm"
        ], env=dict(os.environ, PATH=f"{bins}:{os.environ['PATH']}"), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = json.loads(result.stdout)
        self.assertIn("CC=clang-18", arguments)
        self.assertIn("LD=ld.lld-18", arguments)
        self.assertNotIn("CC=clang", arguments)
        self.assertIn(f"SYSSRC={self.headers}", arguments)

    def test_package_contents_dependencies_and_configuration_conflict(self):
        package = package_nvidia.build(self.root, "19")
        depends = subprocess.check_output(["dpkg-deb", "-f", str(package), "Depends"], text=True)
        for dependency in ("dkms", "clang-18", "llvm-18", "lld-18", "clang-19", "llvm-19", "lld-19"):
            self.assertIn(dependency, depends)
        self.assertNotIn("nvidia-dkms-", depends)
        stage = self.root / "extracted"
        subprocess.run(["dpkg-deb", "-R", str(package), str(stage)], check=True)
        self.assertEqual((stage / "DEBIAN/conffiles").read_text(), "/etc/dkms/nvidia.conf\n")
        self.assertTrue(os.access(stage / "usr/lib/psycachy-nvidia-support/build", os.X_OK))
        # Exercise preinst in an isolated filesystem fixture.
        preinst = (stage / "DEBIAN/preinst").read_text().replace("/etc/dkms", str(self.root / "etc/dkms"))
        script = self.root / "preinst"
        script.write_text(preinst)
        conf = self.root / "etc/dkms/nvidia.conf"
        conf.parent.mkdir(parents=True)
        original = "MAKE[0]='my custom command'\n"
        conf.write_text(original)
        result = subprocess.run(["sh", str(script), "install"], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(conf.read_text(), original)
        conf.write_text((stage / "etc/dkms/nvidia.conf").read_text())
        subprocess.run(["sh", str(script), "install"], check=True)
        # A residual conffile after removal must remain harmless to DKMS.
        residual = conf.read_text().replace("/usr/share/psycachy-nvidia-support", str(self.root / "removed"))
        subprocess.run(["bash", "-c", "MAKE[0]=original; " + residual + '\n[[ ${MAKE[0]} == original ]]'], check=True)

    def test_postinst_selects_latest_nvidia_and_only_installed_llvm_kernels(self):
        modules = self.root / "modules"
        for kernel in ("7.2.9-psycachy-llvm", "7.2.9-psycachy", "7.2.9-nitro-thinlto"):
            path = modules / kernel / "build"
            path.mkdir(parents=True)
            (path / ".config").write_text("CONFIG_CC_IS_CLANG=y\n")
        bins = self.root / "bin"
        bins.mkdir()
        log = self.root / "dkms.log"
        dkms = bins / "dkms"
        dkms.write_text('#!/bin/sh\nif [ "$1" = status ]; then\n'
                        'echo "nvidia/575.1.2, 7.0.0-generic, x86_64: installed"\n'
                        'echo "nvidia/595.99.02, 7.0.0-generic, x86_64: installed"\n'
                        'else printf "%s\\n" "$*" >> "$TEST_LOG"; fi\n')
        dkms.chmod(0o755)
        postinst = self.root / "postinst"
        postinst.write_text((ROOT / "packaging/nvidia/postinst").read_text()
                            .replace("/lib/modules", str(modules)).replace("/boot", str(self.root / "boot")))
        env = dict(os.environ, PATH=f"{bins}:{os.environ['PATH']}", TEST_LOG=str(log))
        subprocess.run(["sh", str(postinst), "configure"], env=env, check=True)
        self.assertEqual(log.read_text(), "install -m nvidia -v 595.99.02 -k 7.2.9-psycachy-llvm\n")


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="psycachy-install-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ("linux-image-7.2.9-psycachy-llvm_7.2.9-2_amd64.deb",
                     "linux-headers-7.2.9-psycachy-llvm_7.2.9-2_amd64.deb",
                     "psycachy-nvidia-support_1.0+llvm18_amd64.deb"):
            (self.root / name).write_text(name)
        self.bins = self.root / "bin"
        self.bins.mkdir()
        scripts = {
            "sudo": 'printf "%s\\n" "$*" >> "$INSTALL_LOG"',
            "dpkg-deb": 'if [[ $3 == Version ]]; then echo 7.2.9-2; else p=${2##*/}; echo "${p%%_*}"; fi',
            "dkms": 'echo "nvidia/595.99.02, 7.2.9-psycachy-llvm, x86_64: installed"',
            "modinfo": 'echo "7.2.9-psycachy-llvm SMP modversions"',
        }
        for name, body in scripts.items():
            path = self.bins / name
            path.write_text("#!/bin/bash\n" + body + "\n")
            path.chmod(0o755)
        self.log = self.root / "install.log"
        self.env = dict(os.environ, PATH=f"{self.bins}:{os.environ['PATH']}", INSTALL_LOG=str(self.log))
        self.checksums()

    def checksums(self):
        import hashlib
        (self.root / "SHA256SUMS").write_text("".join(
            f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n" for p in self.root.glob("*.deb")))

    def run_installer(self, *args):
        return subprocess.run(["bash", str(ROOT / "scripts/install-packages.sh"), *args, str(self.root)],
                              env=self.env, text=True, capture_output=True)

    def test_nvidia_support_installs_before_kernel(self):
        result = self.run_installer("--nvidia")
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = self.log.read_text().splitlines()
        self.assertEqual(len(commands), 2)
        self.assertIn("psycachy-nvidia-support", commands[0])
        self.assertNotIn("linux-image", commands[0])
        self.assertIn("linux-image", commands[1])
        self.assertIn("linux-headers", commands[1])

    def test_normal_install_excludes_optional_package(self):
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("nvidia-support", self.log.read_text())

    def test_corrupt_package_stops_before_privileged_action(self):
        next(self.root.glob("linux-image-*.deb")).write_text("corrupt")
        result = self.run_installer("--nvidia")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.log.exists())

    def test_mismatched_headers_stop_before_privileged_action(self):
        next(self.root.glob("linux-headers-*.deb")).rename(self.root / "linux-headers-7.2.10-psycachy-llvm_7.2.9-2_amd64.deb")
        self.checksums()
        result = self.run_installer("--nvidia")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.log.exists())

    def test_missing_distribution_driver_stops_before_privileged_action(self):
        (self.bins / "dkms").write_text("#!/bin/sh\nexit 0\n")
        result = self.run_installer("--nvidia")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("distribution first", result.stderr)
        self.assertFalse(self.log.exists())


if __name__ == "__main__":
    unittest.main()
