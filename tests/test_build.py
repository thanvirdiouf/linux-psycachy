# SPDX-License-Identifier: GPL-2.0-only
"""Exercise builder control flow with real tar/checksum/patch tools, offline."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

from scripts import patchsets


BUILDER = Path(__file__).resolve().parents[1] / "build.sh"


class BuilderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="psycachy-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "fork with spaces"
        src = self.repo / "src"
        self.patch_dir = src / "patches" / "7.2"
        self.patch_dir.mkdir(parents=True)
        self.common_dir = src / "patches" / "common"
        self.common_dir.mkdir()
        self.manifest = src / "releases.tsv"
        (src / "llvm-version").write_text("18\n")
        (src / "config").write_text(
            "CONFIG_CACHY=y\nCONFIG_SCHED_BORE=y\n"
            "CONFIG_CC_OPTIMIZE_FOR_PERFORMANCE_O3=y\n"
            "CONFIG_TCP_CONG_BBR3=m\nCONFIG_MQ_IOSCHED_ADIOS=m\n"
            "CONFIG_CC_IS_GCC=y\nCONFIG_LTO_NONE=y\n"
        )

        tree = self.root / "source"
        (tree / "scripts").mkdir(parents=True)
        (tree / "sample.txt").write_text("old\n")
        (tree / "headers.txt").write_text("old\n")
        self.executable(tree / "scripts" / "config", 'echo "$*" >> "$CONFIG_LOG"')
        self.executable(tree / "scripts" / "pahole-version.sh", 'echo "${TEST_PAHOLE:-126}"')
        archive = src / "cachyos-7.2.9-2.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(tree, arcname="cachyos-7.2.9-2")
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        self.manifest.write_text(f"# Pinned releases\n7.2.9\tcachyos-7.2.9-2\t{digest}\n")
        (self.repo / "build.sh").write_text(BUILDER.read_text())
        (self.repo / "scripts").mkdir()
        (self.repo / "scripts/patchsets.py").write_text(Path(patchsets.__file__).read_text())

        for directory, name, target in (
            (self.patch_dir, "0001-bore-cachy.patch", "sample.txt"),
            (self.common_dir, "0002-debian-headers-config.patch", "headers.txt"),
        ):
            (directory / name).write_text(
                f"--- a/{target}\n+++ b/{target}\n@@ -1 +1 @@\n-old\n+new\n"
            )

        binaries = self.root / "bin"
        binaries.mkdir()
        for name in ("clang", "clang++", "ld.lld", "llvm-ar", "llvm-nm", "llvm-strip",
                     "llvm-objcopy", "llvm-objdump", "llvm-readelf"):
            output = "Ubuntu LLD 18.1.3" if name == "ld.lld" else "Ubuntu clang version 18.1.3"
            self.executable(binaries / (name + "-18"), f'echo "{output}"')
        self.executable(binaries / "dpkg-query", "printf installed")
        self.executable(binaries / "uname", "echo x86_64")
        self.executable(binaries / "nproc", 'echo "${TEST_CPUS:-8}"')
        self.executable(binaries / "sudo", "echo 'Unexpected dependency installation' >&2; exit 99")
        self.executable(
            binaries / "make",
            'echo "$*" >> "$BUILD_LOG"\n'
            'if [[ -n ${FAIL_TARGET:-} && " $* " == *" $FAIL_TARGET "* ]]; then exit 2; fi\n'
            'if [[ " $* " == *" olddefconfig "* && " $* " == *" LLVM=-18 "* ]]; then\n'
            '  sed -i "/^CONFIG_CC_IS_GCC=/d; /^CONFIG_LTO_NONE=/d" .config\n'
            '  printf "CONFIG_CC_IS_CLANG=y\\nCONFIG_LD_IS_LLD=y\\nCONFIG_AS_IS_LLVM=y\\n" >> .config\n'
            '  if [[ ${DROP_THINLTO:-0} != 1 ]]; then echo CONFIG_LTO_CLANG_THIN=y >> .config; fi\n'
            'fi\n'
            'if [[ " $* " == *" kernelversion "* ]]; then echo "${TEST_KERNEL_VERSION:-7.2.9}"; fi',
        )
        self.log = self.root / "make.log"
        self.config_log = self.root / "config.log"
        self.env = dict(os.environ, PATH=f"{binaries}:{os.environ['PATH']}",
                        BUILD_LOG=str(self.log), CONFIG_LOG=str(self.config_log))
        self.env["TOOLCHAIN"] = "gcc"
        for name in ("LLVM_VERSION", "LLVM", "LLVM_IAS"):
            self.env.pop(name, None)
        self.env.pop("JOBS", None)
        self.env.pop("PAHOLE", None)
        self.env.pop("INSTALL_DEPS", None)

    @staticmethod
    def executable(path, body):
        path.write_text("#!/bin/bash\n" + body + "\n")
        path.chmod(0o755)

    def run_builder(self, *args, **env):
        return subprocess.run(
            ["bash", str(self.repo / "build.sh"), *args], cwd=self.root,
            env=dict(self.env, **env), text=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, timeout=15,
        )

    def test_default_uses_llvm_for_every_stage_and_distinct_kernel_identity(self):
        result = self.run_builder("7.2.9", TOOLCHAIN="")
        self.assertEqual(result.returncode, 0, result.stdout)
        calls = self.log.read_text().splitlines()
        self.assertTrue(all("LLVM=-18 LLVM_IAS=1" in call for call in calls))
        self.assertTrue(all("CC=gcc" not in call for call in calls))
        self.assertIn("LOCALVERSION=-psycachy-llvm KDEB_PKGVERSION=7.2.9-2", calls[-1])
        tree = self.repo / "src/build-7.2.9-clang-thinlto"
        self.assertIn("clang version 18.1.3", (tree / "TOOLCHAIN.txt").read_text())
        self.assertIn("CONFIG_LTO_CLANG_THIN=y", (tree / ".config").read_text())

    def test_switch_to_clang_preserves_existing_gcc_objects(self):
        gcc = self.run_builder("7.2.9", "--prepare-only")
        self.assertEqual(gcc.returncode, 0, gcc.stdout)
        old_tree = self.repo / "src/build-7.2.9"
        before = (old_tree / ".config").read_bytes()
        (old_tree / "existing.o").write_text("GCC object")
        clang = self.run_builder("7.2.9", "--prepare-only", TOOLCHAIN="")
        self.assertEqual(clang.returncode, 0, clang.stdout)
        self.assertEqual((old_tree / ".config").read_bytes(), before)
        self.assertEqual((old_tree / "existing.o").read_text(), "GCC object")

    def test_dropped_thinlto_stops_before_packaging(self):
        result = self.run_builder("7.2.9", TOOLCHAIN="", DROP_THINLTO="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CONFIG_LTO_CLANG_THIN", result.stdout)
        self.assertNotIn("bindeb-pkg", self.log.read_text())

    def test_compiler_change_requires_fresh_build_tree(self):
        result = self.run_builder("7.2.9", "--prepare-only", TOOLCHAIN="")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.executable(self.root / "bin/clang-18", 'echo "Ubuntu clang version 18.1.4"')
        result = self.run_builder("7.2.9", TOOLCHAIN="")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("different compiler or linker", result.stdout)
        self.assertNotIn("bindeb-pkg", self.log.read_text())

    def test_wrong_llvm_major_and_missing_toolchain_stop_before_preparation(self):
        self.executable(self.root / "bin/clang-18", 'echo "Ubuntu clang version 19.1.0"')
        result = self.run_builder("7.2.9", TOOLCHAIN="")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match LLVM_VERSION=18", result.stdout)
        self.assertFalse(self.log.exists())
        result = self.run_builder("7.2.9", TOOLCHAIN="", LLVM_VERSION="987")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Required tool is missing: clang-987", result.stdout)
        self.assertFalse(self.log.exists())

    def test_invalid_toolchain_inputs_stop_before_preparation(self):
        for env in ({"TOOLCHAIN": "unknown"}, {"TOOLCHAIN": "", "LLVM_VERSION": "../18"}):
            with self.subTest(env=env):
                result = self.run_builder("7.2.9", **env)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.log.exists())

    def test_rejects_unsupported_version_before_any_work(self):
        result = self.run_builder("7.3.0")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unsupported kernel version", result.stdout)
        self.assertFalse(self.log.exists())
        self.assertFalse((self.repo / "src" / "build-7.3.0").exists())

    def test_rejects_invalid_jobs_before_preparation(self):
        result = self.run_builder("7.2.9", JOBS="0")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("JOBS must be a positive integer", result.stdout)
        self.assertFalse(self.log.exists())
        self.assertFalse((self.repo / "src" / "build-7.2.9").exists())

    def test_rejects_corrupt_archive(self):
        archive = self.repo / "src" / "cachyos-7.2.9-2.tar.gz"
        with archive.open("ab") as stream:
            stream.write(b"corrupt")
        result = self.run_builder("7.2.9")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FAILED", result.stdout)
        self.assertFalse(self.log.exists())

    def test_missing_bbr3_stops_before_compilation(self):
        config = self.repo / "src" / "config"
        config.write_text(config.read_text().replace("CONFIG_TCP_CONG_BBR3=m\n", ""))
        result = self.run_builder("7.2.9")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Required configuration option is missing: CONFIG_TCP_CONG_BBR3", result.stdout)
        self.assertNotIn("bindeb-pkg", self.log.read_text())

    def test_patch_failure_stops_and_cleans_staging(self):
        patch = self.common_dir / "0002-debian-headers-config.patch"
        patch.write_text(patch.read_text().replace("-old", "-missing"))
        result = self.run_builder("7.2.9")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("File to patch:", result.stdout)
        self.assertNotIn("Kernel build complete", result.stdout)
        self.assertFalse(self.log.exists())
        self.assertFalse((self.repo / "src" / "build-7.2.9").exists())
        self.assertEqual(list((self.repo / "src").glob(".prepare-*")), [])

    def test_prepare_only_and_reuse(self):
        first = self.run_builder("7.2.9", "--prepare-only")
        self.assertEqual(first.returncode, 0, first.stdout)
        tree = self.repo / "src" / "build-7.2.9"
        self.assertEqual((tree / "sample.txt").read_text(), "new\n")
        second = self.run_builder("7.2.9", "--prepare-only")
        self.assertEqual(second.returncode, 0, second.stdout)
        self.assertIn("Reusing prepared source", second.stdout)
        self.assertNotIn("bindeb-pkg", self.log.read_text())

    def test_changed_patches_require_fresh_source(self):
        first = self.run_builder("7.2.9", "--prepare-only")
        self.assertEqual(first.returncode, 0, first.stdout)
        patch = self.patch_dir / "0001-bore-cachy.patch"
        patch.write_text(patch.read_text().replace("+new", "+different"))
        result = self.run_builder("7.2.9")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("different or incomplete patches", result.stdout)
        self.assertNotIn("bindeb-pkg", self.log.read_text())

    def pin_snapshot(self, config=None):
        files = {name: (directory / name).read_bytes() for directory, name in (
            (self.patch_dir, "0001-bore-cachy.patch"),
            (self.common_dir, "0002-debian-headers-config.patch"))}
        metadata = {"upstream_commit": "a" * 40, "features": [], "config": config or {},
                    "upstream_paths": [], "patches": [
                        {"name": name, "sha256": hashlib.sha256(content).hexdigest()}
                        for name, content in files.items()]}
        identity = patchsets.store_snapshot(self.repo, metadata, files)
        digest = self.manifest.read_text().splitlines()[-1].split()[2]
        (self.repo / "src/patches/releases.json").write_text(json.dumps({
            "7.2.9": {"source_sha256": digest, "snapshot": identity}}))
        return self.repo / "src/patches/snapshots" / identity

    def test_locked_release_survives_changes_to_shared_series_patch(self):
        self.pin_snapshot()
        first = self.run_builder("7.2.9", "--prepare-only")
        self.assertEqual(first.returncode, 0, first.stdout)
        bore = self.patch_dir / "0001-bore-cachy.patch"
        bore.write_text(bore.read_text().replace("+new", "+different"))
        second = self.run_builder("7.2.9", "--prepare-only")
        self.assertEqual(second.returncode, 0, second.stdout)
        self.assertIn("Reusing prepared source", second.stdout)
        self.assertEqual((self.repo / "src/build-7.2.9/sample.txt").read_text(), "new\n")

    def test_locked_patch_corruption_stops_before_preparation(self):
        folder = self.pin_snapshot()
        (folder / "0001-bore-cachy.patch").write_text("corrupt\n")
        result = self.run_builder("7.2.9")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("checksum mismatch", result.stdout)
        self.assertFalse(self.log.exists())

    def test_missing_selected_feature_stops_before_packaging(self):
        self.pin_snapshot({"ACPI_CALL": "m"})
        result = self.run_builder("7.2.9")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CONFIG_ACPI_CALL=m", result.stdout)
        self.assertNotIn("bindeb-pkg", self.log.read_text())

    def test_snapshot_configuration_change_invalidates_prepared_tree(self):
        config = self.repo / "src/config"
        config.write_text(config.read_text() + "CONFIG_ACPI_CALL=m\n")
        self.pin_snapshot({"ACPI_CALL": "m"})
        first = self.run_builder("7.2.9", "--prepare-only")
        self.assertEqual(first.returncode, 0, first.stdout)
        self.pin_snapshot({"ACPI_CALL": "y"})
        second = self.run_builder("7.2.9")
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("different or incomplete patches", second.stdout)

    def test_single_cpu_build_uses_one_job(self):
        result = self.run_builder("7.2.9", TEST_CPUS="1")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("bindeb-pkg -j1", self.log.read_text())
        self.assertIn("LOCALVERSION=-psycachy", self.log.read_text())

    def test_make_failure_does_not_report_success(self):
        for target in ("olddefconfig", "bindeb-pkg"):
            with self.subTest(target=target):
                result = self.run_builder("7.2.9", FAIL_TARGET=target)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("Kernel build complete", result.stdout)

    def test_old_pahole_disables_btf_and_sched_ext(self):
        result = self.run_builder("7.2.9", "--prepare-only", TEST_PAHOLE="125")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("disabling BTF and sched_ext", result.stdout)
        self.assertIn("--disable SCHED_CLASS_EXT", self.config_log.read_text())

    def test_supplied_toolchain_can_skip_package_installation(self):
        binary = self.root / "bin" / "dpkg-query"
        self.executable(binary, "exit 1")
        result = self.run_builder("7.2.9", "--prepare-only", INSTALL_DEPS="0")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("using supplied tools", result.stdout)

    def test_another_stable_release_reuses_series_and_common_patches(self):
        src = self.repo / "src"
        release = "cachyos-7.2.10-1"
        archive = src / f"{release}.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(self.root / "source", arcname=release)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        with self.manifest.open("a") as stream:
            stream.write(f"7.2.10\t{release}\t{digest}\n")
        result = self.run_builder("7.2.10", "--prepare-only", TEST_KERNEL_VERSION="7.2.10")
        self.assertEqual(result.returncode, 0, result.stdout)
        tree = src / "build-7.2.10"
        self.assertEqual((tree / "sample.txt").read_text(), "new\n")
        self.assertEqual((tree / "headers.txt").read_text(), "new\n")
        self.assertFalse((src / "patches" / "7.2.10").exists())

    def test_pinned_series_without_bore_patch_is_rejected(self):
        digest = "a" * 64
        self.manifest.write_text(f"7.3.0\tcachyos-7.3.0-1\t{digest}\n")
        result = self.run_builder("7.3.0")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Missing patch for kernel series 7.3", result.stdout)
        self.assertFalse(self.log.exists())

    def test_invalid_release_metadata_is_rejected(self):
        for release, digest in (("cachyos-7.2.10-1", "a" * 64),
                                ("cachyos-7x2x9-2", "a" * 64),
                                ("cachyos-7.2.9-2", "bad-checksum")):
            with self.subTest(release=release, digest=digest):
                self.manifest.write_text(f"7.2.9\t{release}\t{digest}\n")
                result = self.run_builder("7.2.9")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Invalid source release entry", result.stdout)
                self.assertFalse(self.log.exists())

    def test_duplicate_release_is_rejected(self):
        self.manifest.write_text(self.manifest.read_text() * 2)
        result = self.run_builder("7.2.9")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Duplicate kernel version", result.stdout)
        self.assertFalse(self.log.exists())

    def test_legacy_prepared_tree_survives_patch_move(self):
        result = self.run_builder("7.2.9", "--prepare-only")
        self.assertEqual(result.returncode, 0, result.stdout)
        tree = self.repo / "src" / "build-7.2.9"
        stamp = tree / ".psycachy-prepared"
        source_digest = stamp.read_text().split(":")[0]
        legacy_lines = "".join(
            f"{hashlib.sha256(patch.read_bytes()).hexdigest()}  "
            f"{self.repo}/src/patches/7.2.9/{patch.name}\n"
            for patch in (self.patch_dir / "0001-bore-cachy.patch",
                          self.common_dir / "0002-debian-headers-config.patch")
        )
        legacy_hash = hashlib.sha256(legacy_lines.encode()).hexdigest()
        stamp.write_text(f"{source_digest}:{legacy_hash}\n")
        marker = tree / "existing-object.o"
        marker.write_text("Preserve compiled objects")
        result = self.run_builder("7.2.9", "--prepare-only")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("Updated prepared-source fingerprint", result.stdout)
        self.assertEqual(marker.read_text(), "Preserve compiled objects")
        self.assertNotIn(legacy_hash, stamp.read_text())

    def test_prepared_tree_survives_repository_move(self):
        result = self.run_builder("7.2.9", "--prepare-only")
        self.assertEqual(result.returncode, 0, result.stdout)
        relocated = self.root / "relocated fork"
        self.repo.rename(relocated)
        self.repo = relocated
        result = self.run_builder("7.2.9", "--prepare-only")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("Reusing prepared source", result.stdout)

    def test_manifest_without_final_newline_is_accepted(self):
        self.manifest.write_text(self.manifest.read_text().rstrip("\n"))
        result = self.run_builder("7.2.9", "--prepare-only")
        self.assertEqual(result.returncode, 0, result.stdout)


if __name__ == "__main__":
    unittest.main()
