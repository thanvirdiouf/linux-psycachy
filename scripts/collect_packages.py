#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Collect one exact build into a self-contained installation directory."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

try:
    from . import package_meta
except ImportError:
    import package_meta

ROOT = Path(__file__).resolve().parents[1]


def collect(source, output, tree, release, version, toolchain, root=ROOT, nvidia_package=None):
    source, output, tree = Path(source), Path(output), Path(tree)
    expected = {f"linux-image-{release}", f"linux-headers-{release}"}
    optional = {"linux-libc-dev", f"linux-image-{release}-dbg"}
    if toolchain == 'clang-thinlto':
        if nvidia_package is None:
            raise ValueError('Clang bundles require the exact newly built NVIDIA support package')
        optional.add('psycachy-nvidia-support')
    selected = {}
    for path in source.glob("*.deb"):
        if path.is_symlink() or not path.is_file():
            raise ValueError(f'Package must be a regular file: {path.name}')
        name = subprocess.check_output(["dpkg-deb", "-f", str(path), "Package"], text=True).strip()
        if name not in expected | optional:
            continue
        if name == 'psycachy-nvidia-support' and path.resolve() != Path(nvidia_package).resolve():
            continue
        actual = subprocess.check_output(["dpkg-deb", "-f", str(path), "Version"], text=True).strip()
        if name != "psycachy-nvidia-support" and actual != version:
            continue
        if name in selected:
            raise ValueError(f"Ambiguous package for this build: {name}")
        selected[name] = path
    if not expected <= selected.keys():
        raise ValueError("Build did not produce the expected image and matching headers")
    if toolchain == 'clang-thinlto' and 'psycachy-nvidia-support' not in selected:
        raise ValueError('Missing newly built NVIDIA support package')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".packages-", dir=output.parent) as temporary:
        stage = Path(temporary) / "bundle"
        stage.mkdir()
        for path in selected.values():
            shutil.copy2(path, stage / path.name)
        package_meta.build(stage, release, version, toolchain)
        shutil.copy2(root / "scripts/install-packages.sh", stage / "install-packages.sh")
        for name, target in ((".config", "kernel.config"), ("TOOLCHAIN.txt", "TOOLCHAIN.txt"),
                             ("BUILD-TUNING.txt", "BUILD-TUNING.txt"),
                             ("OPTIMIZATIONS.json", "OPTIMIZATIONS.json")):
            if (tree / name).exists():
                shutil.copy2(tree / name, stage / target)
        for name in ("PATCHES.json",):
            if (tree / name).exists():
                shutil.copy2(tree / name, stage / name)
        (stage / "PACKAGE-BUILD.json").write_text(json.dumps({
            "kernel_release": release, "package_version": version, "toolchain": toolchain,
        }, indent=2) + "\n")
        write_checksums(stage)
        if output.exists():
            raise ValueError(f"Package output already exists; move it aside before rebuilding: {output}")
        stage.rename(output)
    return output


def write_checksums(folder):
    paths = sorted(path for path in folder.iterdir() if path.is_file() and path.name != "SHA256SUMS")
    with (folder / "SHA256SUMS").open("w") as stream:
        for path in paths:
            with path.open("rb") as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
            stream.write(f"{digest}  {path.name}\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "output", "tree"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--kernel-release", required=True)
    parser.add_argument("--package-version", required=True)
    parser.add_argument("--toolchain", choices=("clang-thinlto", "gcc"), required=True)
    parser.add_argument("--nvidia-package", type=Path)
    args = parser.parse_args()
    print(collect(args.source, args.output, args.tree, args.kernel_release,
                  args.package_version, args.toolchain, nvidia_package=args.nvidia_package))


if __name__ == "__main__":
    main()
