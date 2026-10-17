# SPDX-License-Identifier: GPL-2.0-only
"""Check real Debian metadata, safe local installation, and signed APT consumption."""

import getpass
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from scripts import apt_repository, package_meta

ROOT = Path(__file__).resolve().parents[1]
DEB_TOOLS = all(shutil.which(tool) for tool in ("dpkg", "dpkg-deb", "sha256sum"))
APT_TOOLS = DEB_TOOLS and all(shutil.which(tool) for tool in ("gpg", "dpkg-scanpackages", "apt-get", "apt-cache"))


@unittest.skipUnless(DEB_TOOLS, "Debian package tools are required")
class PackageFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="psycachy-delivery-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bundle = self.root / "build"
        self.bundle.mkdir()

    def package(self, name, version="7.2.9-3", architecture="amd64", depends=None, folder=None, payload="fixture"):
        folder = folder or self.bundle
        with tempfile.TemporaryDirectory(dir=self.root) as temporary:
            stage = Path(temporary)
            (stage / "DEBIAN").mkdir()
            control = (f"Package: {name}\nVersion: {version}\nArchitecture: {architecture}\n"
                       "Maintainer: Test <test@example.invalid>\nDescription: Package fixture\n")
            if depends:
                control += f"Depends: {depends}\n"
            (stage / "DEBIAN/control").write_text(control)
            (stage / "fixture").write_text(payload)
            result = folder / f"{name}_{version}_{architecture}.deb"
            subprocess.run(["dpkg-deb", "--root-owner-group", "--build", str(stage), str(result)],
                           check=True, capture_output=True)
        return result

    def kernel(self, release="7.2.9-psycachy-llvm", version="7.2.9-3", folder=None, tracking=True):
        folder = folder or self.bundle
        for kind in ("image", "headers"):
            self.package(f"linux-{kind}-{release}", version, folder=folder)
        if tracking:
            package_meta.build(folder, release, version, "clang-thinlto" if "-llvm" in release else "gcc")
        self.checksums(folder)

    def checksums(self, folder=None):
        folder = folder or self.bundle
        (folder / "SHA256SUMS").write_text("".join(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  ./{path.name}\n"
            for path in sorted(folder.glob("*.deb"))))


class TrackingPackageTests(PackageFixture):
    def test_tracking_packages_have_exact_dependencies_and_native_architecture(self):
        for release, toolchain, suffix in (("7.2.9-psycachy-llvm", "clang-thinlto", "-llvm"),
                                           ("7.2.9-psycachy", "gcc", ""),
                                           ("7.2.9-psycachy-llvm-minimal", "clang-thinlto", "-llvm-minimal"),
                                           ("7.2.9-psycachy-minimal", "gcc", "-minimal"),
                                           ("7.2.9-psycachy-llvm-r3", "clang-thinlto", "-llvm"),
                                           ("7.2.9-psycachy-llvm-minimal-r3", "clang-thinlto", "-llvm-minimal")):
            with self.subTest(toolchain=toolchain):
                self.kernel(release=release, tracking=False)
                result = package_meta.build(self.bundle, release, "7.2.9-3", toolchain)
                self.assertEqual(len(result), 2)
                for path, kind in zip(result, ("image", "headers")):
                    self.assertEqual(package_meta.field(path, "Package"), f"linux-{kind}-psycachy{suffix}")
                    self.assertEqual(package_meta.field(path, "Architecture"), "amd64")
                    self.assertEqual(package_meta.field(path, "Depends"), f"linux-{kind}-{release} (= 7.2.9-3)")

    def test_inconsistent_kernel_version_architecture_or_toolchain_is_rejected(self):
        self.kernel(tracking=False)
        with self.assertRaises(ValueError):
            package_meta.build(self.bundle, "7.2.9-psycachy-llvm", "7.2.9-4", "clang-thinlto")
        with self.assertRaises(ValueError):
            package_meta.build(self.bundle, "7.2.9-psycachy-llvm", "7.2.9-3", "gcc")
        next(self.bundle.glob("linux-headers-*.deb")).unlink()
        self.package("linux-headers-7.2.9-psycachy-llvm", architecture="arm64")
        with self.assertRaises(ValueError):
            package_meta.build(self.bundle, "7.2.9-psycachy-llvm", "7.2.9-3", "clang-thinlto")
        self.assertFalse(list(self.bundle.glob("linux-image-psycachy*.deb")))


class InstallerTests(PackageFixture):
    def setUp(self):
        super().setUp()
        self.kernel()
        self.bins = self.root / "bin"
        self.bins.mkdir()
        self.log = self.root / "installed"
        sudo = self.bins / "sudo"
        sudo.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$INSTALL_LOG"\n')
        sudo.chmod(0o755)
        self.env = dict(os.environ, PATH=f"{self.bins}:{os.environ['PATH']}", INSTALL_LOG=str(self.log))

    def install(self, *arguments):
        return subprocess.run(["bash", str(ROOT / "scripts/install-packages.sh"), *arguments, str(self.bundle)],
                              env=self.env, text=True, capture_output=True)

    def test_installs_tracking_packages_and_excludes_libc_and_debug_by_default(self):
        self.package("linux-libc-dev")
        self.package("linux-image-7.2.9-psycachy-llvm-dbg")
        self.checksums()
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        installed = self.log.read_text()
        self.assertIn("linux-image-psycachy-llvm_", installed)
        self.assertIn("linux-headers-psycachy-llvm_", installed)
        self.assertNotIn("linux-libc-dev", installed)
        self.assertNotIn("-dbg", installed)
        self.log.unlink()
        result = self.install("--with-libc-dev")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("linux-libc-dev", self.log.read_text())

    def test_unchecksummed_package_duplicate_or_traversal_manifest_fails_before_sudo(self):
        manifest = self.bundle / "SHA256SUMS"
        original = manifest.read_text()
        injected = self.package("linux-libc-dev")
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        injected.unlink()
        manifest.write_text(original + original.splitlines()[0] + "\n")
        self.assertNotEqual(self.install().returncode, 0)
        manifest.write_text("0" * 64 + "  ../outside\n")
        self.assertNotEqual(self.install().returncode, 0)
        self.assertFalse(self.log.exists())

    def test_names_come_from_package_metadata(self):
        path = next(self.bundle.glob("linux-image-7*.deb"))
        path.rename(self.bundle / "renamed.deb")
        self.checksums()
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("./renamed.deb", self.log.read_text())

    def test_wrong_tracking_dependency_fails_before_sudo(self):
        self.package("linux-image-psycachy-llvm", depends="linux-image-7.2.8-psycachy-llvm (= 7.2.9-3)")
        self.checksums()
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.log.exists())

    def test_revisioned_minimal_kernel_uses_stable_tracking_package_names(self):
        for package in self.bundle.glob("*.deb"):
            package.unlink()
        self.kernel(release="7.2.9-psycachy-llvm-minimal-r3")
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        installed = self.log.read_text()
        self.assertIn("linux-image-7.2.9-psycachy-llvm-minimal-r3_", installed)
        self.assertIn("linux-image-psycachy-llvm-minimal_", installed)


@unittest.skipUnless(APT_TOOLS, "APT/GnuPG tools are required")
class AptRepositoryTests(PackageFixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.key_temp = tempfile.TemporaryDirectory(prefix="psycachy-test-key-")
        cls.keyhome = Path(cls.key_temp.name)
        cls.keyhome.chmod(0o700)
        subprocess.run(["gpg", "--homedir", str(cls.keyhome), "--batch", "--pinentry-mode", "loopback",
                        "--passphrase", "", "--quick-generate-key", "PsyCachy test <test@example.invalid>",
                        "ed25519", "sign", "0"], check=True, capture_output=True)
        keys = subprocess.check_output(["gpg", "--homedir", str(cls.keyhome), "--with-colons", "--list-secret-keys"], text=True)
        cls.fingerprint = next(line.split(":")[9] for line in keys.splitlines() if line.startswith("fpr:"))

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["gpgconf", "--homedir", str(cls.keyhome), "--kill", "gpg-agent"], check=True)
        cls.key_temp.cleanup()
        super().tearDownClass()

    def prepare(self, directories=None, **kwargs):
        return apt_repository.build(directories or [self.bundle], self.root / "repository", self.fingerprint,
                                    gnupghome=self.keyhome, **kwargs)

    def test_signed_repository_is_accepted_by_apt_and_tracks_newest_kernel(self):
        self.kernel(release="7.2.8-psycachy-llvm", version="7.2.8-3")
        self.package("linux-libc-dev", version="7.2.8-3")
        self.package("linux-image-7.2.8-psycachy-llvm-dbg", version="7.2.8-3")
        self.checksums()
        newer = self.root / "newer"
        newer.mkdir()
        self.kernel(release="7.2.9-psycachy-llvm-r5", folder=newer, version="7.2.9-5")
        repository = self.prepare([self.bundle, newer])
        binary = repository / "dists/stable/main/binary-amd64"
        index = (binary / "Packages").read_text()
        self.assertNotIn("Package: linux-libc-dev", index)
        self.assertNotIn("-dbg", index)
        for version in ("7.2.8-3", "7.2.9-5"):
            self.assertIn(f"Version: {version}", index)
        for filename in ("Packages", "Packages.gz"):
            path = binary / filename
            self.assertEqual(path.read_bytes(), (binary / "by-hash/SHA256" / apt_repository.digest(path)).read_bytes())
        lists = self.root / "lists"
        lists.mkdir()
        sources = self.root / "sources.list"
        sources.write_text(f"deb [arch=amd64 signed-by={repository / 'psycachy-archive-keyring.gpg'}] file:{repository} stable main\n")
        options = ["-o", f"Dir::State::lists={lists}", "-o", f"Dir::Etc::sourcelist={sources}",
                   "-o", "Dir::Etc::sourceparts=-", "-o", "Debug::NoLocking=true",
                   "-o", f"APT::Sandbox::User={getpass.getuser()}", "-o", f"Dir::Cache={self.root / 'cache'}"]
        update = subprocess.run(["apt-get", *options, "update"], text=True, capture_output=True)
        self.assertEqual(update.returncode, 0, update.stdout + update.stderr)
        self.assertNotIn("NO_PUBKEY", update.stderr)
        policy = subprocess.check_output(["apt-cache", *options, "policy", "linux-image-psycachy-llvm"], text=True)
        self.assertIn("Candidate: 7.2.9-5", policy)

    def test_corruption_fails_without_creating_output(self):
        self.kernel()
        path = next(self.bundle.glob("*.deb"))
        path.write_bytes(path.read_bytes() + b"corruption")
        with self.assertRaisesRegex(ValueError, "Checksum mismatch"):
            self.prepare()
        self.assertFalse((self.root / "repository").exists())

    def test_missing_tracking_packages_and_existing_output_are_rejected(self):
        self.kernel(tracking=False)
        with self.assertRaisesRegex(ValueError, "tracking packages"):
            self.prepare()
        self.kernel()
        output = self.root / "repository"
        output.mkdir()
        with self.assertRaisesRegex(ValueError, "must not exist"):
            self.prepare()

    def test_conflicting_package_bytes_across_bundles_are_rejected(self):
        self.kernel()
        other = self.root / "other"
        other.mkdir()
        for path in self.bundle.glob("*.deb"):
            shutil.copyfile(path, other / path.name)
        self.package("linux-image-7.2.9-psycachy-llvm", folder=other, payload="different build")
        self.checksums(other)
        with self.assertRaisesRegex(ValueError, "Conflicting package bytes"):
            self.prepare([self.bundle, other])


if __name__ == "__main__":
    unittest.main()
