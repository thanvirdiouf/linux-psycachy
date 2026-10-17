#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Sign an explicitly selected local kernel image; never download or run code."""

import argparse
import contextlib
import fcntl
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile


class SigningError(Exception):
    pass


def command(*args, check=True):
    result = subprocess.run(args, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, check=False)
    if check and result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise SigningError(f"{args[0]} failed: {detail}")
    return result


def require_tools(*names):
    missing = [name for name in names if shutil.which(name) is None]
    if missing:
        raise SigningError("Missing required tools: " + ", ".join(missing)
                           + ". Install openssl, sbsigntool, and (for enrollment) mokutil "
                           "using your distribution's package manager.")


def regular_file(path):
    try:
        info = path.lstat()
    except FileNotFoundError as error:
        raise SigningError(f"File not found: {path}") from error
    if not stat.S_ISREG(info.st_mode):
        raise SigningError(f"Expected a regular file, without a symbolic link: {path}")
    return info


def digest(path):
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


@contextlib.contextmanager
def lock(path):
    # Retain the lock inode: unlinking it could let two processes lock different files.
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise SigningError(f"Lock is not a regular file: {path}")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def copy_metadata(source, target):
    info = source.stat()
    target_info = target.stat()
    if (target_info.st_uid, target_info.st_gid) != (info.st_uid, info.st_gid):
        os.chown(target, info.st_uid, info.st_gid)
    shutil.copystat(source, target)


def preserve(source, backup):
    """Create one complete backup without replacing an earlier original."""
    if backup.exists() or backup.is_symlink():
        regular_file(backup)
        if digest(backup) != digest(source):
            raise SigningError(f"Backup already contains a different image: {backup}. "
                               "Keep it and select a new --backup path for this image.")
        return
    # Publish the complete backup atomically: an interrupted copy must not leave
    # a partial .unsigned file that appears to be a recoverable original.
    with sibling_temp(backup) as prepared:
        shutil.copyfile(source, prepared)
        copy_metadata(source, prepared)
        with prepared.open("rb") as stream:
            os.fsync(stream.fileno())
        os.link(prepared, backup)
        sync_directory(backup.parent)


@contextlib.contextmanager
def sibling_temp(image):
    descriptor, name = tempfile.mkstemp(prefix=f".{image.name}.secureboot-", dir=image.parent)
    os.close(descriptor)
    path = Path(name)
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


def atomic_replace(prepared, image, metadata_source):
    copy_metadata(metadata_source, prepared)
    with prepared.open("rb") as stream:
        os.fsync(stream.fileno())
    os.replace(prepared, image)
    sync_directory(image.parent)


def create_key(key_dir):
    key_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = key_dir.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o022:
        raise SigningError(f"Key directory must be a directory without group/other write access: {key_dir}")
    paths = [key_dir / name for name in ("MOK.priv", "MOK.pem", "MOK.der")]
    with lock(key_dir / ".create.lock"):
        if any(path.exists() or path.is_symlink() for path in paths):
            if not all(path.exists() for path in paths):
                raise SigningError("Incomplete key set; refusing to overwrite existing key material. "
                                   "Recover the matching files or choose a new --key-dir.")
            print(f"Keeping existing key material in {key_dir}")
            return
        with tempfile.TemporaryDirectory(prefix=".create-", dir=key_dir) as directory:
            staging = Path(directory)
            command("openssl", "req", "-config", str(Path(__file__).with_name("mokconfig.cnf")),
                    "-new", "-x509", "-newkey", "rsa:3072", "-nodes", "-days", "3650",
                    "-keyout", str(staging / "MOK.priv"), "-out", str(staging / "MOK.pem"))
            command("openssl", "x509", "-in", str(staging / "MOK.pem"), "-outform", "DER",
                    "-out", str(staging / "MOK.der"))
            created = []
            try:
                for path in paths:
                    source = staging / path.name
                    source.chmod(0o600 if path.name == "MOK.priv" else 0o644)
                    # Hard links fail if a destination appeared during generation.
                    os.link(source, path)
                    created.append(path)
                sync_directory(key_dir)
            except BaseException:
                for path in created:
                    path.unlink()
                raise
        print(f"Created local signing key in {key_dir}; keep MOK.priv private and backed up.")


def validate_certificate(certificate):
    regular_file(certificate)
    command("openssl", "x509", "-in", str(certificate), "-noout", "-checkend", "0")


def validate_key(key, certificate):
    info = regular_file(key)
    if info.st_mode & 0o077:
        raise SigningError(f"Private key must have mode 0600 or stricter: {key}")
    public_key = command("openssl", "pkey", "-in", str(key), "-pubout").stdout.strip()
    public_certificate = command("openssl", "x509", "-in", str(certificate),
                                 "-pubkey", "-noout").stdout.strip()
    if public_key != public_certificate:
        raise SigningError("Private key and certificate do not match.")


def verified(image, certificate):
    return command("sbverify", "--cert", str(certificate), str(image), check=False).returncode == 0


def sign_image(image, key, certificate, backup):
    with lock(image.with_name(image.name + ".secureboot.lock")):
        regular_file(image)
        if verified(image, certificate):
            print(f"Signature already verifies with this certificate: {image}")
            return
        original_digest = digest(image)
        with sibling_temp(image) as prepared:
            command("sbsign", "--key", str(key), "--cert", str(certificate),
                    "--output", str(prepared), str(image))
            regular_file(prepared)
            if not verified(prepared, certificate):
                raise SigningError("Signed temporary image failed signature verification; original retained.")
            if digest(image) != original_digest:
                raise SigningError("Kernel changed while signing; original was not replaced. Retry after package updates finish.")
            preserve(image, backup)
            if digest(backup) != original_digest or digest(image) != original_digest:
                raise SigningError("Kernel changed while backing up; original was not replaced.")
            atomic_replace(prepared, image, image)
        print(f"Signed and verified: {image}\nOriginal retained: {backup}")


def restore_image(image, backup):
    with lock(image.with_name(image.name + ".secureboot.lock")):
        regular_file(image)
        regular_file(backup)
        if digest(image) == digest(backup):
            print(f"Original image is already restored: {image}")
            return
        current_digest = digest(image)
        signed_backup = image.with_name(image.name + ".before-restore")
        preserve(image, signed_backup)
        with sibling_temp(image) as prepared:
            shutil.copyfile(backup, prepared)
            if digest(prepared) != digest(backup):
                raise SigningError("Backup copy failed verification; current image retained.")
            if digest(image) != current_digest:
                raise SigningError("Kernel changed during restoration; current image retained.")
            atomic_replace(prepared, image, backup)
        print(f"Restored original: {image}\nPrevious image retained: {signed_backup}")


def enroll(certificate, der):
    regular_file(der)
    converted = command("openssl", "x509", "-in", str(der), "-inform", "DER",
                        "-outform", "PEM").stdout.strip()
    canonical = command("openssl", "x509", "-in", str(certificate),
                        "-outform", "PEM").stdout.strip()
    if converted != canonical:
        raise SigningError("MOK.der and MOK.pem do not contain the same certificate.")
    if command("mokutil", "--test-key", str(der), check=False).returncode == 0:
        print("MOK certificate is already enrolled.")
        return
    # Leave the password prompt attached to the user's terminal.
    result = subprocess.run(["mokutil", "--import", str(der)], check=False)
    if result.returncode:
        raise SigningError("MOK import failed. Any signed kernel image and original backup were retained.")
    print("MOK enrollment requested. Reboot and confirm enrollment in shim's MOK Manager.")


def parser():
    result = argparse.ArgumentParser(description=__doc__, epilog=(
        "Use sudo for /boot and MOK enrollment. MOK enrollment requires a shim-based "
        "boot chain and confirmation after reboot. A valid signature alone does not "
        "prove firmware/shim trust. This helper does not sign kernel modules or "
        "configure DKMS: external modules need separate signing and a certificate "
        "trusted by the running kernel. In-place signing needs no initramfs or GRUB rebuild."))
    selection = result.add_mutually_exclusive_group()
    selection.add_argument("--kernel-release", help="exact installed release, e.g. 7.2.9-psycachy-llvm")
    selection.add_argument("--image", type=Path, help="explicit local kernel/EFI image path")
    result.add_argument("--key-dir", type=Path, default=Path.home() / ".local/share/psycachy/secureboot",
                        help="local MOK.priv, MOK.pem and MOK.der directory (default: %(default)s)")
    result.add_argument("--create-key", action="store_true", help="create local key material only if none exists")
    result.add_argument("--enroll", action="store_true", help="explicitly request MOK enrollment; never automatic")
    action = result.add_mutually_exclusive_group()
    action.add_argument("--verify-only", action="store_true", help="verify the selected image with MOK.pem")
    action.add_argument("--restore", action="store_true", help="atomically restore the saved original image")
    result.add_argument("--backup", type=Path, help="original backup path (default: IMAGE.unsigned); never overwritten")
    return result


def main(argv=None):
    arguments = parser().parse_args(argv)
    if arguments.kernel_release:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+_~\-]*", arguments.kernel_release):
            raise SigningError("Invalid --kernel-release; use the exact release without directory components.")
        arguments.image = Path("/boot") / ("vmlinuz-" + arguments.kernel_release)
    if not arguments.image and (arguments.verify_only or arguments.restore or arguments.backup):
        raise SigningError("--verify-only, --restore and --backup require --image or --kernel-release.")
    if not arguments.image and not (arguments.create_key or arguments.enroll):
        raise SigningError("Select --kernel-release or --image, or request --create-key/--enroll.")
    if (arguments.verify_only or arguments.restore) and (arguments.create_key or arguments.enroll):
        raise SigningError("--verify-only/--restore cannot be combined with --create-key/--enroll.")
    image = arguments.image.expanduser().absolute() if arguments.image else None
    backup = ((arguments.backup.expanduser().absolute() if arguments.backup else
               image.with_name(image.name + ".unsigned")) if image else None)
    if image:
        regular_file(image)
        if image.resolve() == backup.resolve():
            raise SigningError("Image and --backup must be different files.")
    if arguments.restore:
        restore_image(image, backup)
        return 0
    signing_tools = ["sbverify"] if arguments.verify_only else ["sbsign", "sbverify"] if image else []
    require_tools("openssl", *signing_tools, *(["mokutil"] if arguments.enroll else []))
    key_dir = arguments.key_dir.expanduser().absolute()
    if arguments.create_key:
        create_key(key_dir)
    key, certificate, der = [key_dir / name for name in ("MOK.priv", "MOK.pem", "MOK.der")]
    validate_certificate(certificate)
    if arguments.verify_only:
        if not verified(image, certificate):
            raise SigningError(f"Signature does not verify with {certificate}: {image}")
        print(f"Signature verified: {image}")
        return 0
    if image or arguments.create_key:
        validate_key(key, certificate)
    if image:
        sign_image(image, key, certificate, backup)
    if arguments.enroll:
        enroll(certificate, der)
    if image:
        print("Boot trust requires an enrolled certificate and a compatible shim/kernel. "
              "Kernel modules and DKMS modules require separate signing and trust configuration.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (SigningError, OSError) as error:
        print(f"Secure Boot: {error}", file=sys.stderr)
        sys.exit(1)
