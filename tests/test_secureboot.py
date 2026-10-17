# SPDX-License-Identifier: GPL-2.0-only
"""Check safe signing/recovery offline, using temporary images and ephemeral keys."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from secureboot import sign_kernel


ENTRYPOINT = Path(__file__).resolve().parents[1] / "secureboot/create-key.sh"


class SecureBootTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="psycachy-secureboot-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.image = self.root / "vmlinuz-7.2.9-psycachy-llvm"
        self.original = b"test unsigned kernel image\n"
        self.image.write_bytes(self.original)
        self.image.chmod(0o640)
        self.backup = self.image.with_name(self.image.name + ".unsigned")
        self.key = self.root / "MOK.priv"
        self.certificate = self.root / "MOK.pem"
        self.key.write_text("test private key")
        self.key.chmod(0o600)
        self.certificate.write_text("test certificate")

    def fake_signing(self, *arguments, check=True):
        if arguments[0] == "sbverify":
            valid = Path(arguments[-1]).read_bytes().endswith(b"\nVERIFIED SIGNATURE")
            return subprocess.CompletedProcess(arguments, 0 if valid else 1, "", "")
        if arguments[0] == "sbsign":
            output = Path(arguments[arguments.index("--output") + 1])
            output.write_bytes(Path(arguments[-1]).read_bytes() + b"\nVERIFIED SIGNATURE")
            return subprocess.CompletedProcess(arguments, 0, "", "")
        self.fail(f"Unexpected external tool: {arguments}")

    def sign(self):
        sign_kernel.sign_image(self.image, self.key, self.certificate, self.backup)

    def test_sign_verifies_before_replacement_retains_original_and_is_idempotent(self):
        with mock.patch.object(sign_kernel, "command", side_effect=self.fake_signing) as run:
            self.sign()
            signed = self.image.read_bytes()
            self.assertEqual(self.backup.read_bytes(), self.original)
            self.assertTrue(signed.endswith(b"VERIFIED SIGNATURE"))
            self.assertEqual(self.image.stat().st_mode & 0o777, 0o640)
            self.assertEqual(self.backup.stat().st_mode & 0o777, 0o640)
            self.sign()
            self.assertEqual(self.image.read_bytes(), signed)
            self.assertEqual(sum(call.args[0] == "sbsign" for call in run.call_args_list), 1)
        self.assertEqual(list(self.root.glob(".*.secureboot-*")), [])

    def test_verification_failure_retains_original_without_partial_backup(self):
        def bad_signing(*arguments, check=True):
            result = self.fake_signing(*arguments, check=check)
            if arguments[0] == "sbverify":
                result.returncode = 1
            return result

        with mock.patch.object(sign_kernel, "command", side_effect=bad_signing):
            with self.assertRaisesRegex(sign_kernel.SigningError, "failed signature verification"):
                self.sign()
        self.assertEqual(self.image.read_bytes(), self.original)
        self.assertFalse(self.backup.exists())
        self.assertEqual(list(self.root.glob(".*.secureboot-*")), [])

    def test_signer_failure_retains_original(self):
        def fail_signing(*arguments, check=True):
            if arguments[0] == "sbsign":
                raise sign_kernel.SigningError("signer failed")
            return self.fake_signing(*arguments, check=check)

        with mock.patch.object(sign_kernel, "command", side_effect=fail_signing):
            with self.assertRaisesRegex(sign_kernel.SigningError, "signer failed"):
                self.sign()
        self.assertEqual(self.image.read_bytes(), self.original)
        self.assertFalse(self.backup.exists())

    def test_stale_backup_is_never_overwritten(self):
        self.backup.write_bytes(b"earlier kernel package")
        with mock.patch.object(sign_kernel, "command", side_effect=self.fake_signing):
            with self.assertRaisesRegex(sign_kernel.SigningError, "different image"):
                self.sign()
        self.assertEqual(self.image.read_bytes(), self.original)
        self.assertEqual(self.backup.read_bytes(), b"earlier kernel package")

    def test_changed_kernel_during_signing_is_not_replaced(self):
        def racing_signing(*arguments, check=True):
            result = self.fake_signing(*arguments, check=check)
            if arguments[0] == "sbsign":
                self.image.write_bytes(b"new package installed meanwhile")
            return result

        with mock.patch.object(sign_kernel, "command", side_effect=racing_signing):
            with self.assertRaisesRegex(sign_kernel.SigningError, "changed while signing"):
                self.sign()
        self.assertEqual(self.image.read_bytes(), b"new package installed meanwhile")
        self.assertFalse(self.backup.exists())

    def test_restore_is_atomic_retains_signed_image_and_is_idempotent(self):
        with mock.patch.object(sign_kernel, "command", side_effect=self.fake_signing):
            self.sign()
        signed = self.image.read_bytes()
        sign_kernel.restore_image(self.image, self.backup)
        self.assertEqual(self.image.read_bytes(), self.original)
        self.assertEqual(self.image.with_name(self.image.name + ".before-restore").read_bytes(), signed)
        sign_kernel.restore_image(self.image, self.backup)
        self.assertEqual(self.image.read_bytes(), self.original)

    def test_backup_symlink_does_not_modify_its_target(self):
        victim = self.root / "unrelated-file"
        victim.write_bytes(self.original)
        self.backup.symlink_to(victim)
        with mock.patch.object(sign_kernel, "command", side_effect=self.fake_signing):
            with self.assertRaisesRegex(sign_kernel.SigningError, "regular file"):
                self.sign()
        self.assertEqual(victim.read_bytes(), self.original)
        self.assertEqual(self.image.read_bytes(), self.original)

    def test_private_key_permissions_and_mismatch_are_rejected(self):
        self.key.chmod(0o644)
        with self.assertRaisesRegex(sign_kernel.SigningError, "0600"):
            sign_kernel.validate_key(self.key, self.certificate)
        self.key.chmod(0o600)
        results = [subprocess.CompletedProcess([], 0, "key A", ""),
                   subprocess.CompletedProcess([], 0, "key B", "")]
        with mock.patch.object(sign_kernel, "command", side_effect=results):
            with self.assertRaisesRegex(sign_kernel.SigningError, "do not match"):
                sign_kernel.validate_key(self.key, self.certificate)

    def test_kernel_release_supports_llvm_name_and_rejects_directory_components(self):
        with mock.patch.object(sign_kernel, "regular_file"), \
                mock.patch.object(sign_kernel, "require_tools"), \
                mock.patch.object(sign_kernel, "validate_certificate"), \
                mock.patch.object(sign_kernel, "validate_key"), \
                mock.patch.object(sign_kernel, "sign_image") as signing:
            sign_kernel.main(["--kernel-release", "7.2.9-psycachy-llvm", "--key-dir", str(self.root)])
            self.assertEqual(signing.call_args.args[0], Path("/boot/vmlinuz-7.2.9-psycachy-llvm"))
        with self.assertRaisesRegex(sign_kernel.SigningError, "Invalid --kernel-release"):
            sign_kernel.main(["--kernel-release", "../../another-file"])

    def test_verify_only_needs_no_private_key_and_cannot_enroll(self):
        self.key.unlink()
        with mock.patch.object(sign_kernel, "require_tools"), \
                mock.patch.object(sign_kernel, "validate_certificate"), \
                mock.patch.object(sign_kernel, "verified", return_value=True):
            self.assertEqual(sign_kernel.main(["--image", str(self.image), "--key-dir", str(self.root),
                                               "--verify-only"]), 0)
        with self.assertRaisesRegex(sign_kernel.SigningError, "cannot be combined"):
            sign_kernel.main(["--image", str(self.image), "--verify-only", "--enroll"])

    def test_enrollment_is_only_requested_explicitly_and_skips_enrolled_key(self):
        with mock.patch.object(sign_kernel, "require_tools"), \
                mock.patch.object(sign_kernel, "validate_certificate"), \
                mock.patch.object(sign_kernel, "validate_key"), \
                mock.patch.object(sign_kernel, "sign_image"), \
                mock.patch.object(sign_kernel, "enroll") as enrollment:
            arguments = ["--image", str(self.image), "--key-dir", str(self.root)]
            sign_kernel.main(arguments)
            enrollment.assert_not_called()
            sign_kernel.main([*arguments, "--enroll"])
            enrollment.assert_called_once()
        der = self.root / "MOK.der"
        der.write_bytes(b"test der certificate")
        with mock.patch.object(sign_kernel, "command", return_value=
                               subprocess.CompletedProcess([], 0, "same certificate", "")), \
                mock.patch.object(sign_kernel.subprocess, "run") as importing:
            sign_kernel.enroll(self.certificate, der)
            importing.assert_not_called()

    def test_partial_keys_are_never_regenerated(self):
        (self.root / "MOK.pem").unlink()
        with self.assertRaisesRegex(sign_kernel.SigningError, "Incomplete key set"):
            sign_kernel.create_key(self.root)
        self.assertEqual(self.key.read_text(), "test private key")

    def test_cli_requires_explicit_selection_and_help_uses_only_local_code(self):
        result = subprocess.run(["bash", str(ENTRYPOINT)], text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Select --kernel-release or --image", result.stdout)
        result = subprocess.run(["bash", str(ENTRYPOINT), "--help"], text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("--kernel-release", result.stdout)
        self.assertIn("does not sign kernel modules", result.stdout)


@unittest.skipUnless(all(shutil.which(name) for name in ("openssl", "sbsign", "sbverify")),
                     "real signing integration needs openssl and sbsigntool")
class RealSecureBootTests(unittest.TestCase):
    def test_ephemeral_key_signs_a_temporary_efi_copy_and_verifies(self):
        # Read a public system binary, copy it to /tmp, and never touch /boot or user keys.
        candidates = (Path("/usr/lib/shim/mmx64.efi"),
                      Path("/usr/lib/grub/x86_64-efi/monolithic/grubx64.efi"))
        source = next((path for path in candidates if path.is_file()), None)
        if source is None:
            self.skipTest("no public local EFI binary available for the integration fixture")
        with tempfile.TemporaryDirectory(prefix="psycachy-efi-test-") as directory:
            root = Path(directory)
            image, keys = root / "test-image.efi", root / "keys"
            shutil.copyfile(source, image)
            original = image.read_bytes()
            arguments = ["bash", str(ENTRYPOINT), "--image", str(image), "--key-dir", str(keys)]
            result = subprocess.run([*arguments, "--create-key"], text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertEqual(image.with_name(image.name + ".unsigned").read_bytes(), original)
            self.assertEqual((keys / "MOK.priv").stat().st_mode & 0o777, 0o600)
            fingerprint = sign_kernel.digest(keys / "MOK.priv")
            signed_digest = sign_kernel.digest(image)
            result = subprocess.run([*arguments, "--create-key"], text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertEqual(sign_kernel.digest(keys / "MOK.priv"), fingerprint)
            self.assertEqual(sign_kernel.digest(image), signed_digest)
            result = subprocess.run([*arguments, "--verify-only"], text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout)
            result = subprocess.run([*arguments, "--restore"], text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertEqual(image.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
