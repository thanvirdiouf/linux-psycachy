#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Prepare a signed, static APT repository locally; never upload or publish it."""

import argparse
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import gzip
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

try:
    from .package_meta import field
except ImportError:
    from package_meta import field

CHECKSUM = re.compile(r"([0-9a-fA-F]{64}) [ *](?:\./)?([A-Za-z0-9][A-Za-z0-9.+_:~\-]*)")
KERNEL_PACKAGE = re.compile(
    r"linux-(?:image|headers)-(?:psycachy(?:-llvm)?(?:-minimal)?|"
    r"[0-9][a-z0-9.+-]*-psycachy(?:-llvm)?(?:-minimal)?(?:-r[1-9][0-9]*)?)"
)


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def verified_packages(directory):
    directory = Path(directory).resolve()
    sums = directory / "SHA256SUMS"
    if not sums.is_file() or sums.is_symlink():
        raise ValueError(f"Missing regular checksum manifest: {sums}")
    covered = set()
    for line in sums.read_text().splitlines():
        match = CHECKSUM.fullmatch(line)
        if not match:
            raise ValueError(f"Invalid checksum entry in {sums}")
        checksum, name = match.groups()
        path = directory / name
        if name in covered or path.is_symlink() or not path.is_file():
            raise ValueError(f"Duplicate or nonregular checksum target: {name}")
        if digest(path) != checksum.lower():
            raise ValueError(f"Checksum mismatch: {name}")
        covered.add(name)
    packages = sorted(directory.glob("*.deb"))
    if not packages or any(path.name not in covered for path in packages):
        raise ValueError(f"Every package must be covered by SHA256SUMS: {directory}")
    return packages


def collect(directories):
    selected = {}
    for directory in directories:
        for package in verified_packages(directory):
            name = field(package, "Package")
            # Distribution UAPI headers and optional debug symbols do not belong in
            # the normal kernel upgrade channel.
            if name == "linux-libc-dev" or name.endswith(("-dbg", "-dbgsym")):
                continue
            if not KERNEL_PACKAGE.fullmatch(name) and name != "psycachy-nvidia-support":
                raise ValueError(f"Unexpected package in kernel repository: {name}")
            version = field(package, "Version")
            architecture = field(package, "Architecture")
            if architecture != "amd64":
                raise ValueError(f"Only amd64 builds are supported: {package.name}")
            subprocess.run(["dpkg", "--validate-version", version], check=True)
            identity = name, version, architecture
            if identity in selected and digest(selected[identity]) != digest(package):
                raise ValueError(f"Conflicting package bytes for {identity}")
            selected[identity] = package
    for (name, version, architecture), package in selected.items():
        if name.startswith(("linux-image-", "linux-headers-")):
            if name.startswith("linux-image-"):
                peer = "linux-headers-" + name.removeprefix("linux-image-")
            else:
                peer = "linux-image-" + name.removeprefix("linux-headers-")
            if (peer, version, architecture) not in selected:
                raise ValueError(f"Image and headers must match: {name} {version}")
        if re.fullmatch(r"linux-(?:image|headers)-psycachy(?:-llvm)?(?:-minimal)?", name):
            dependency = field(package, "Depends")
            match = re.fullmatch(r"(linux-(?:image|headers)-[0-9][a-z0-9.+-]*-psycachy(?:-llvm)?(?:-minimal)?(?:-r[1-9][0-9]*)?) \(= ([^)]+)\)", dependency)
            if not match or match.group(2) != version or (match.group(1), version, architecture) not in selected:
                raise ValueError(f"Tracking package must depend on its included versioned kernel: {name}")
            expected = "linux-" + ("headers" if name.startswith("linux-headers") else "image") + "-"
            dependency_suffix = re.sub(r"-r[1-9][0-9]*$", "", match.group(1).split("psycachy", 1)[1])
            if not match.group(1).startswith(expected) or name.split("psycachy", 1)[1] != dependency_suffix:
                raise ValueError(f"Tracking package toolchain or kind differs: {name}")
    if not any(re.fullmatch(r"linux-image-psycachy(?:-llvm)?(?:-minimal)?", name) for name, _, _ in selected):
        raise ValueError("Repository requires kernel tracking packages to provide upgrades")
    return selected


def build(directories, output, signing_key, suite="stable", gnupghome=None, valid_days=14):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", suite):
        raise ValueError("Suite must be a simple lowercase name")
    if not re.fullmatch(r"(?:[0-9A-Fa-f]{40}|[0-9A-Fa-f]{64})", signing_key):
        raise ValueError("Use the full signing key fingerprint")
    if not 1 <= valid_days <= 90:
        raise ValueError("Release validity must be between 1 and 90 days")
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("Output must not exist; prepare a fresh repository before replacing hosting files")
    selected = collect(directories)
    output.parent.mkdir(parents=True, exist_ok=True)
    gpg = ["gpg", "--batch", "--no-tty"]
    if gnupghome is not None:
        gpg += ["--homedir", str(gnupghome)]
    # Confirm the specified fingerprint identifies an available secret key.
    keys = subprocess.check_output(gpg + ["--with-colons", "--list-secret-keys", signing_key], text=True)
    if signing_key.upper() not in {line.split(":")[9].upper() for line in keys.splitlines() if line.startswith("fpr:")}:
        raise ValueError("Signing key fingerprint is unavailable")
    with tempfile.TemporaryDirectory(prefix=".psycachy-apt-", dir=output.parent) as temporary:
        stage = Path(temporary) / "repository"
        pool = stage / "pool/main"
        pool.mkdir(parents=True)
        for (name, version, architecture), package in sorted(selected.items()):
            # Canonical names prevent filename collisions or misleading uploaded names.
            filename = f"{name}_{version}_{architecture}.deb"
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+_:~\-]*", filename):
                raise ValueError(f"Unsafe package filename: {filename}")
            shutil.copyfile(package, pool / filename)
        binary = stage / "dists" / suite / "main/binary-amd64"
        binary.mkdir(parents=True)
        index = subprocess.check_output(
            ["dpkg-scanpackages", "--multiversion", "pool", "/dev/null"], cwd=stage,
        )
        (binary / "Packages").write_bytes(index)
        (binary / "Packages.gz").write_bytes(gzip.compress(index, mtime=0))
        hashes = binary / "by-hash/SHA256"
        hashes.mkdir(parents=True)
        for path in (binary / "Packages", binary / "Packages.gz"):
            shutil.copyfile(path, hashes / digest(path))
        now = datetime.now(timezone.utc).replace(microsecond=0)
        release_text = (
            "Origin: PsyCachy\nLabel: PsyCachy\n"
            f"Suite: {suite}\nCodename: {suite}\n"
            f"Date: {format_datetime(now, usegmt=True)}\n"
            f"Valid-Until: {format_datetime(now + timedelta(days=valid_days), usegmt=True)}\n"
            "Architectures: amd64\nComponents: main\nAcquire-By-Hash: yes\n"
            "Description: PsyCachy kernels for Debian based distributions\nSHA256:\n"
        )
        dist = stage / "dists" / suite
        for path in (binary / "Packages", binary / "Packages.gz"):
            release_text += f" {digest(path)} {path.stat().st_size:16d} {path.relative_to(dist).as_posix()}\n"
        release = dist / "Release"
        release.write_text(release_text)
        signing = gpg + ["--yes", "--pinentry-mode", "loopback", "--passphrase-fd", "0", "--local-user", signing_key]
        passphrase = os.environ.get("PSYCACHY_APT_KEY_PASSPHRASE", "") + "\n"
        for arguments in (["--clearsign", "--output", str(dist / "InRelease")],
                          ["--detach-sign", "--output", str(dist / "Release.gpg")]):
            subprocess.run(signing + arguments + [str(release)], input=passphrase, text=True, check=True)
        public_key = stage / "psycachy-archive-keyring.gpg"
        public_key.write_bytes(subprocess.check_output(gpg + ["--export-options", "export-minimal", "--export", signing_key]))
        if not public_key.stat().st_size:
            raise ValueError("No public signing key was exported")
        (stage / "SIGNING-FINGERPRINT.txt").write_text(signing_key.upper() + "\n")
        (stage / ".nojekyll").touch()
        for signed in ("InRelease", "Release.gpg"):
            command = gpg + ["--verify", str(dist / signed)]
            if signed == "Release.gpg":
                command.append(str(release))
            subprocess.run(command, check=True)
        stage.replace(output)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packages", required=True, action="append", type=Path,
                        help="Verified build bundle directory; repeat to retain old kernels")
    parser.add_argument("--output", required=True, type=Path, help="Fresh output directory")
    parser.add_argument("--signing-key", required=True, help="Full fingerprint of an existing secret signing key")
    parser.add_argument("--gnupghome", type=Path)
    parser.add_argument("--suite", default="stable")
    parser.add_argument("--valid-days", type=int, default=14)
    args = parser.parse_args()
    try:
        print(build(args.packages, args.output, args.signing_key, args.suite, args.gnupghome, args.valid_days))
    except (ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"APT repository preparation failed: {error}\n")


if __name__ == "__main__":
    main()
