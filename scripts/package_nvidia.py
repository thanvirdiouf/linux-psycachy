#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Build the optional NVIDIA DKMS integration package without kernel compilation."""

import argparse
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def build(output, llvm_version, root=ROOT):
    if not re.fullmatch(r"[1-9][0-9]*", str(llvm_version)):
        raise ValueError("LLVM version must be a positive major version")
    source = root / "packaging/nvidia"
    version = (source / "version").read_text().strip()
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", version):
        raise ValueError("Invalid NVIDIA support package version")
    majors = {int(llvm_version)}
    for line in (source / "llvm-versions").read_text().splitlines():
        if line and not line.startswith("#"):
            if not re.fullmatch(r"[1-9][0-9]*", line):
                raise ValueError("Invalid retained LLVM version")
            majors.add(int(line))
    depends = ["dkms (>= 3.0)", "make", "kmod", "libc6-dev"]
    for major in sorted(majors):
        depends.extend([f"clang-{major}", f"lld-{major}", f"llvm-{major}"])
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    package_version = f"{version}+llvm{llvm_version}"
    package = output / f"psycachy-nvidia-support_{package_version}_amd64.deb"
    with tempfile.TemporaryDirectory(prefix="psycachy-nvidia-package-") as work:
        stage = Path(work)
        control = stage / "DEBIAN"
        control.mkdir()
        (control / "control").write_text(
            "Package: psycachy-nvidia-support\n"
            f"Version: {package_version}\n"
            "Architecture: amd64\n"
            "Maintainer: PsyCachy maintainers <thanvirdiouf@users.noreply.github.com>\n"
            "Section: kernel\nPriority: optional\n"
            f"Depends: {', '.join(depends)}\n"
            "Homepage: https://github.com/thanvirdiouf/linux-psycachy\n"
            "Description: NVIDIA DKMS toolchain integration for PsyCachy LLVM kernels\n"
            " Selects the kernel's LLVM compiler and linker for NVIDIA DKMS builds.\n"
            " Uses the distribution's installed NVIDIA driver; no driver is bundled.\n"
        )
        (control / "conffiles").write_text("/etc/dkms/nvidia.conf\n")
        for name in ("preinst", "postinst"):
            shutil.copyfile(source / name, control / name)
            (control / name).chmod(0o755)
        for src, dest in (
            (source / "nvidia.conf", "etc/dkms/nvidia.conf"),
            (source / "override.sh", "usr/share/psycachy-nvidia-support/override.sh"),
            (root / "LICENSE", "usr/share/doc/psycachy-nvidia-support/copyright"),
        ):
            target = stage / dest
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, target)
        wrapper = stage / "usr/lib/psycachy-nvidia-support/build"
        wrapper.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / "build", wrapper)
        wrapper.chmod(0o755)
        subprocess.run(["dpkg-deb", "--root-owner-group", "--build", str(stage), str(package)], check=True)
    return package


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--llvm-version", default=(ROOT / "src/llvm-version").read_text().strip())
    parser.add_argument("--output", type=Path, default=ROOT / "src")
    args = parser.parse_args()
    print(build(args.output, args.llvm_version))


if __name__ == "__main__":
    main()
