#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Build APT tracking packages for one verified, versioned PsyCachy kernel build."""

import argparse
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
TOOLCHAINS = {"clang-thinlto": "-llvm", "gcc": ""}


def field(package, name):
    return subprocess.check_output(
        ["dpkg-deb", "--field", str(package), name], text=True,
    ).strip()


def build(packages, kernel_release, package_version, toolchain):
    packages = Path(packages).resolve()
    if toolchain not in TOOLCHAINS:
        raise ValueError("Unsupported toolchain")
    minimal = bool(re.search(r"-minimal(?:-r[1-9][0-9]*)?$", kernel_release))
    suffix = TOOLCHAINS[toolchain] + ("-minimal" if minimal else "")
    if not re.fullmatch(r"[0-9][a-z0-9.+-]*-psycachy" + re.escape(suffix) + r"(?:-r[1-9][0-9]*)?", kernel_release):
        raise ValueError("Kernel release does not match the selected PsyCachy toolchain")
    if not re.fullmatch(r"[0-9][a-zA-Z0-9.+:~\-]*", package_version):
        raise ValueError("Invalid Debian package version")
    subprocess.run(["dpkg", "--validate-version", package_version], check=True)
    if not packages.is_dir():
        raise ValueError("Package directory does not exist")
    expected = {f"linux-{kind}-{kernel_release}" for kind in ("image", "headers")}
    found = {}
    for path in sorted(packages.glob("*.deb")):
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Package must be a regular file: {path.name}")
        name = field(path, "Package")
        if name not in expected:
            continue
        if name in found:
            raise ValueError(f"Multiple packages for {name}")
        if field(path, "Version") != package_version or field(path, "Architecture") != "amd64":
            raise ValueError(f"Kernel package version or architecture differs: {path.name}")
        found[name] = path
    if found.keys() != expected:
        raise ValueError("Exactly matching amd64 image and headers are required before tracking packages")
    result = []
    for kind in ("image", "headers"):
        name = f"linux-{kind}-psycachy{suffix}"
        target = packages / f"{name}_{package_version}_amd64.deb"
        with tempfile.TemporaryDirectory(prefix="psycachy-meta-") as work:
            stage = Path(work)
            (stage / "DEBIAN").mkdir()
            (stage / "DEBIAN/control").write_text(
                f"Package: {name}\nVersion: {package_version}\nArchitecture: amd64\n"
                "Maintainer: PsyCachy maintainers <thanvirdiouf@users.noreply.github.com>\n"
                "Section: kernel\nPriority: optional\nInstalled-Size: 1\n"
                f"Depends: linux-{kind}-{kernel_release} (= {package_version})\n"
                "Homepage: https://github.com/thanvirdiouf/linux-psycachy\n"
                f"Description: Track PsyCachy {toolchain} kernel {kind} updates\n"
                f" This package depends on the current versioned PsyCachy kernel {kind}.\n"
                " Keep it installed to receive new kernels from a configured PsyCachy APT repository.\n"
            )
            doc = stage / "usr/share/doc" / name
            doc.mkdir(parents=True)
            shutil.copyfile(ROOT / "LICENSE", doc / "copyright")
            subprocess.run(["dpkg-deb", "--root-owner-group", "--build", str(stage), str(target)], check=True)
        result.append(target)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packages", required=True, type=Path)
    parser.add_argument("--kernel-release", required=True)
    parser.add_argument("--package-version", required=True)
    parser.add_argument("--toolchain", choices=TOOLCHAINS, required=True)
    args = parser.parse_args()
    try:
        for package in build(args.packages, args.kernel_release, args.package_version, args.toolchain):
            print(package)
    except (ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Tracking package creation failed: {error}\n")


if __name__ == "__main__":
    main()
