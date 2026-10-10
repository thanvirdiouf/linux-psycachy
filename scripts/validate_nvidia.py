#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Compile pinned NVIDIA open modules against the headers we actually ship."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
MODULES = ("nvidia", "nvidia-modeset", "nvidia-drm", "nvidia-uvm", "nvidia-peermem")


def validate(packages, work, archive=None):
    pin = json.loads((ROOT / "src/nvidia-validation.json").read_text())
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", pin["version"]) or not re.fullmatch(r"[a-f0-9]{64}", pin["sha256"]):
        raise ValueError("Invalid NVIDIA validation pin")
    packages = Path(packages).resolve()
    headers = list(packages.glob("linux-headers-*-psycachy-llvm_*.deb"))
    if len(headers) != 1:
        raise ValueError("Expected exactly one PsyCachy LLVM headers package")
    name = subprocess.check_output(["dpkg-deb", "--field", str(headers[0]), "Package"], text=True).strip()
    kernel = name.removeprefix("linux-headers-")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+-psycachy-llvm", kernel):
        raise ValueError("Invalid headers kernel release")
    work = Path(work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    header_root = work / "headers"
    subprocess.run(["dpkg-deb", "--extract", str(headers[0]), str(header_root)], check=True)
    tree = header_root / "usr/src" / name
    config = (tree / ".config").read_text()
    match = re.search(r"^CONFIG_CLANG_VERSION=([0-9]+)$", config, re.M)
    if not match or "CONFIG_LTO_CLANG_THIN=y\n" not in config:
        raise ValueError("Headers must describe a Clang/ThinLTO kernel")
    major = int(match[1]) // 10000
    if archive is None:
        archive = work / "nvidia.tar.gz"
        with urllib.request.urlopen(pin["url"], timeout=120) as response, archive.open("wb") as dest:
            while chunk := response.read(1024 * 1024):
                dest.write(chunk)
    archive = Path(archive)
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != pin["sha256"]:
        raise ValueError("NVIDIA source SHA-256 mismatch")
    source_root = work / "source"
    with tarfile.open(archive) as tar:
        tar.extractall(source_root, filter="data")
    driver = source_root / f"open-gpu-kernel-modules-{pin['version']}"
    command = ["make", "-j4", f"LLVM=-{major}", "LLVM_IAS=1", f"CC=clang-{major}",
               f"CXX=clang++-{major}", f"LD=ld.lld-{major}", f"AR=llvm-ar-{major}",
               f"OBJDUMP=llvm-objdump-{major}", f"OBJCOPY=llvm-objcopy-{major}",
               f"KERNEL_UNAME={kernel}", f"SYSSRC={tree}", "modules"]
    log = packages / "nvidia-build.log"
    with log.open("w") as stream:
        subprocess.run(command, cwd=driver, stdout=stream, stderr=subprocess.STDOUT, check=True)
    report = [f"NVIDIA open modules: {pin['version']}", f"Kernel: {kernel}",
              f"LLVM family: {major}", f"NVIDIA source SHA-256: {digest}"]
    for module in MODULES:
        path = driver / "kernel-open" / f"{module}.ko"
        version = subprocess.check_output(["modinfo", "-F", "version", str(path)], text=True).strip()
        vermagic = subprocess.check_output(["modinfo", "-F", "vermagic", str(path)], text=True).strip()
        if version != pin["version"] or vermagic.split()[0] != kernel:
            raise ValueError(f"Wrong driver version or kernel vermagic: {module}")
        report.append(f"{module}: {vermagic}")
    report.append("Compilation and module metadata passed. No GPU boot/load test performed.")
    (packages / "NVIDIA-VALIDATION.txt").write_text("\n".join(report) + "\n")
    print("\n".join(report))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packages", type=Path, required=True)
    parser.add_argument("--archive", type=Path, help="Use an already downloaded pinned archive")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="psycachy-nvidia-ci-") as work:
        validate(args.packages, work, args.archive)


if __name__ == "__main__":
    main()
