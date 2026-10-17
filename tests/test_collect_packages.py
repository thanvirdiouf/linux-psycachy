# SPDX-License-Identifier: GPL-2.0-only
"""Verify installable bundles using real Debian packages and checksum tools."""

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts import collect_packages, kernel_config


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'sources'
        self.source.mkdir()
        self.tree = self.root / 'tree'
        self.tree.mkdir()
        (self.tree / '.config').write_text('CONFIG_CC_IS_GCC=y\n')
        self.release = '7.2.10-psycachy-r3'
        self.version = '7.2.10-5'

    def package(self, name, version=None):
        stage = self.root / ('stage-' + name + '-' + (version or self.version))
        (stage / 'DEBIAN').mkdir(parents=True)
        (stage / 'DEBIAN/control').write_text(
            f'Package: {name}\nVersion: {version or self.version}\nArchitecture: amd64\n'
            'Maintainer: Test <test@example.invalid>\nDescription: Package fixture\n')
        path = self.source / f'{name}_{version or self.version}_amd64.deb'
        subprocess.run(['dpkg-deb', '--build', '--root-owner-group', str(stage), str(path)],
                       check=True, stdout=subprocess.DEVNULL)
        return path

    def kernel(self):
        for kind in ('image', 'headers'):
            self.package(f'linux-{kind}-{self.release}')

    def test_collects_only_exact_build_and_emits_usable_installation_bundle(self):
        self.kernel()
        self.package('linux-image-7.1.1-psycachy', '7.1.1-1')
        self.package('linux-libc-dev', '7.1.1-1')
        self.package(f'linux-image-{self.release}-dbg')
        bundle = collect_packages.collect(self.source, self.root / 'bundle', self.tree,
                                          self.release, self.version, 'gcc')
        self.assertFalse(list(bundle.glob('*7.1.1*')))
        self.assertTrue(list(bundle.glob('linux-image-psycachy_*.deb')))
        self.assertTrue(list(bundle.glob('*-dbg_*.deb')))
        subprocess.run(['sha256sum', '--check', 'SHA256SUMS'], cwd=bundle,
                       check=True, stdout=subprocess.DEVNULL)
        self.assertEqual(json.loads((bundle / 'PACKAGE-BUILD.json').read_text())['kernel_release'], self.release)

    def test_missing_headers_or_existing_output_does_not_publish_partial_bundle(self):
        self.package(f'linux-image-{self.release}')
        output = self.root / 'bundle'
        with self.assertRaisesRegex(ValueError, 'matching headers'):
            collect_packages.collect(self.source, output, self.tree, self.release, self.version, 'gcc')
        self.assertFalse(output.exists())
        self.package(f'linux-headers-{self.release}')
        output.mkdir()
        (output / 'preserved').write_text('old bundle')
        with self.assertRaisesRegex(ValueError, 'already exists'):
            collect_packages.collect(self.source, output, self.tree, self.release, self.version, 'gcc')
        self.assertEqual((output / 'preserved').read_text(), 'old bundle')

    def test_clang_bundle_uses_exact_new_helper_instead_of_stale_higher_llvm(self):
        self.release = '7.2.10-psycachy-llvm-r3'
        self.version = '7.2.10-6'
        self.kernel()
        chosen = self.package('psycachy-nvidia-support', '1.1+llvm18')
        self.package('psycachy-nvidia-support', '1.1+llvm20')
        bundle = collect_packages.collect(self.source, self.root / 'bundle', self.tree,
                                          self.release, self.version, 'clang-thinlto', nvidia_package=chosen)
        self.assertEqual([path.name for path in bundle.glob('psycachy-nvidia-support*.deb')], [chosen.name])


class FragmentTests(unittest.TestCase):
    def test_overrides_distro_certificates_and_preserves_other_kernel_options(self):
        with tempfile.TemporaryDirectory() as work:
            config, fragment = Path(work) / '.config', Path(work) / 'debian.config'
            config.write_text('CONFIG_SYSTEM_TRUSTED_KEYS="debian/certs.pem"\nCONFIG_EXT4_FS=y\n# CONFIG_SECURITY_APPARMOR is not set\n')
            fragment.write_text('CONFIG_SYSTEM_TRUSTED_KEYS=""\nCONFIG_SECURITY_APPARMOR=y\n')
            kernel_config.merge(config, fragment)
            self.assertIn('CONFIG_EXT4_FS=y\n', config.read_text())
            self.assertNotIn('debian/certs.pem', config.read_text())
            self.assertNotIn('# CONFIG_SECURITY_APPARMOR is not set', config.read_text())
            self.assertEqual(config.read_text().count('CONFIG_SECURITY_APPARMOR'), 1)
