"""Exercise builder control flow with real tar/checksum/patch tools, offline."""

import hashlib
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest


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
        (src / "config").write_text(
            "CONFIG_CACHY=y\nCONFIG_SCHED_BORE=y\n"
            "CONFIG_CC_OPTIMIZE_FOR_PERFORMANCE_O3=y\n"
            "CONFIG_TCP_CONG_BBR3=m\nCONFIG_MQ_IOSCHED_ADIOS=m\n"
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

        for directory, name, target in (
            (self.patch_dir, "0001-bore-cachy.patch", "sample.txt"),
            (self.common_dir, "0002-debian-headers-config.patch", "headers.txt"),
        ):
            (directory / name).write_text(
                f"--- a/{target}\n+++ b/{target}\n@@ -1 +1 @@\n-old\n+new\n"
            )

        binaries = self.root / "bin"
        binaries.mkdir()
        self.executable(binaries / "dpkg-query", "printf installed")
        self.executable(binaries / "uname", "echo x86_64")
        self.executable(binaries / "nproc", 'echo "${TEST_CPUS:-8}"')
        self.executable(binaries / "sudo", "echo 'Unexpected dependency installation' >&2; exit 99")
        self.executable(
            binaries / "make",
            'echo "$*" >> "$BUILD_LOG"\n'
            'if [[ -n ${FAIL_TARGET:-} && " $* " == *" $FAIL_TARGET "* ]]; then exit 2; fi\n'
            'if [[ " $* " == *" kernelversion "* ]]; then echo "${TEST_KERNEL_VERSION:-7.2.9}"; fi',
        )
        self.log = self.root / "make.log"
        self.config_log = self.root / "config.log"
        self.env = dict(os.environ, PATH=f"{binaries}:{os.environ['PATH']}",
                        BUILD_LOG=str(self.log), CONFIG_LOG=str(self.config_log))
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
