#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Install a verified bundle in a fresh distro root, then boot it in QEMU.

Requires root, debootstrap, QEMU, e2fsprogs and the distro archive keyrings.
All mounts live in a private mount namespace. No host kernel is installed.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "scripts/validation"
DISTROS = {
    "debian": {"suite": "trixie", "mirror": "https://deb.debian.org/debian",
               "security": "https://security.debian.org/debian-security",
               "components": "main", "keyring": "/usr/share/keyrings/debian-archive-keyring.gpg"},
    "ubuntu": {"suite": "noble", "mirror": "https://archive.ubuntu.com/ubuntu",
               "security": "https://security.ubuntu.com/ubuntu",
               "components": "main,universe", "keyring": "/usr/share/keyrings/ubuntu-archive-keyring.gpg"},
}
CHECKS = ["running_kernel", "package_install", "libc_headers_retained", "dkms_autoinstall",
          "module_load", "config_matches", "btf", "sched_ext", "initramfs", "grub_config"]


def checksum(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def verify_bundle(packages):
    sums = packages / "SHA256SUMS"
    if not sums.is_file() or sums.is_symlink():
        raise ValueError("Package bundle needs a regular SHA256SUMS file")
    verified = {}
    for line in sums.read_text().splitlines():
        match = re.fullmatch(r"([a-fA-F0-9]{64}) [ *](?:\./)?([A-Za-z0-9][A-Za-z0-9.+_:~-]*)", line)
        if not match:
            raise ValueError("SHA256SUMS must contain hashes of local basenames only")
        digest, name = match.groups()
        target = packages / name
        if name in verified or target.is_symlink() or not target.is_file():
            raise ValueError(f"Duplicate, missing or nonregular checksum target: {name}")
        if checksum(target) != digest.lower():
            raise ValueError(f"SHA-256 mismatch: {name}")
        verified[name] = digest.lower()
    required = {path.name for path in packages.glob("*.deb")} | {"install-packages.sh"}
    if not required.issubset(verified) or not any(name.endswith(".deb") for name in verified):
        raise ValueError("Every .deb and install-packages.sh must be covered by SHA256SUMS")
    return verified


def field(package, name):
    return subprocess.check_output(["dpkg-deb", "--field", str(package), name], text=True).strip()


def headers_config(package, name):
    # Read only the named regular member; do not extract arbitrary archive paths.
    process = subprocess.Popen(["dpkg-deb", "--fsys-tarfile", str(package)], stdout=subprocess.PIPE)
    config = None
    try:
        with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
            for member in archive:
                if member.name.removeprefix("./") == f"usr/src/{name}/.config" and member.isfile():
                    config = archive.extractfile(member).read().decode("utf-8")
                # Consume the whole stream, so dpkg-deb never fails with SIGPIPE.
        if process.wait() != 0:
            raise ValueError("Unable to read packaged kernel headers")
    finally:
        process.stdout.close()
        if process.poll() is None:
            process.kill()
            process.wait()
    if config is None:
        raise ValueError("Headers package is missing its .config")
    return config


def inspect_bundle(packages, expected_btf=None, expected_scx=None):
    verified = verify_bundle(packages)
    metadata = []
    for name in sorted(verified):
        if name.endswith(".deb"):
            package = packages / name
            metadata.append({"file": name, "name": field(package, "Package"),
                             "version": field(package, "Version"), "arch": field(package, "Architecture")})
    images = [item for item in metadata if re.fullmatch(r"linux-image-[0-9][A-Za-z0-9.+-]*-psycachy(?:-[A-Za-z0-9]+)*", item["name"])
              and not item["name"].endswith(("-dbg", "-dbgsym"))]
    if len(images) != 1:
        raise ValueError("Expected exactly one PsyCachy image package")
    image = images[0]
    kernel = image["name"].removeprefix("linux-image-")
    matching = [item for item in metadata if item["name"] == f"linux-headers-{kernel}"]
    image_meta = [item for item in metadata if item["name"].startswith("linux-image-psycachy")]
    header_meta = [item for item in metadata if item["name"].startswith("linux-headers-psycachy")]
    if len(matching) != 1 or len(image_meta) != 1 or len(header_meta) != 1:
        raise ValueError("Expected matching image, headers and both kernel tracking packages")
    selected = [image, matching[0], image_meta[0], header_meta[0]]
    if any(item["arch"] != "amd64" or item["version"] != image["version"] for item in selected):
        raise ValueError("Image, headers and tracking packages must match in version and amd64 architecture")
    config = headers_config(packages / matching[0]["file"], matching[0]["name"])
    llvm = re.search(r"^CONFIG_CLANG_VERSION=([0-9]+)$", config, re.M)
    llvm_major = int(llvm[1]) // 10000 if "CONFIG_CC_IS_CLANG=y\n" in config and llvm else None
    if "CONFIG_CC_IS_CLANG=y\n" in config and (llvm_major is None or not 14 <= llvm_major <= 99):
        raise ValueError("Invalid packaged LLVM compiler family")
    btf = "CONFIG_DEBUG_INFO_BTF=y\n" in config
    scx = "CONFIG_SCHED_CLASS_EXT=y\n" in config
    for description, expected, actual in (("BTF", expected_btf, btf), ("sched_ext", expected_scx, scx)):
        if expected is not None and expected != actual:
            raise ValueError(f"Packaged {description} configuration does not match the requested expectation")
    return {"kernel_release": kernel, "version": image["version"], "llvm_major": llvm_major,
            "btf": btf, "sched_ext": scx, "image_meta": image_meta[0]["name"],
            "headers_meta": header_meta[0]["name"], "checksums": verified}


def run_logged(arguments, log, timeout, **kwargs):
    with log.open("a") as stream:
        stream.write("\n$ " + shlex.join(map(str, arguments)) + "\n")
        stream.flush()
        process = subprocess.Popen(list(map(str, arguments)), stdout=stream, stderr=subprocess.STDOUT,
                                   start_new_session=True, **kwargs)
        try:
            code = process.wait(timeout=timeout)
        except BaseException:
            # Let a namespace worker stop its current command and unmount first.
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            raise
        if code:
            raise RuntimeError(f"Command failed ({code}); see {log}: {arguments[0]}")


def apt_sources(distro, suite):
    config = DISTROS[distro]
    components = config["components"].replace(",", " ")
    return (f"deb {config['mirror']} {suite} {components}\n"
            f"deb {config['mirror']} {suite}-updates {components}\n"
            f"deb {config['security']} {suite}-security {components}\n")


def qemu_command(work, accel):
    return ["qemu-system-x86_64", "-machine", f"q35,accel={accel}", "-cpu", "max",
            "-m", "3072", "-smp", "2", "-nographic", "-monitor", "none", "-nic", "none",
            "-no-reboot", "-kernel", str(work / "vmlinuz"), "-initrd", str(work / "initrd.img"),
            "-blockdev", json.dumps({"driver": "file", "filename": str(work / "rootfs.ext4"), "node-name": "rootfile"}),
            "-blockdev", "driver=raw,file=rootfile,node-name=rootdisk",
            "-device", "virtio-blk-pci,drive=rootdisk",
            "-append", "root=/dev/vda rw rootwait console=ttyS0,115200n8 panic=1 "
            "init=/usr/lib/psycachy-validation/guest-init.sh"]


def check_guest(log, nonce):
    text = log.read_text(errors="replace")
    passed = set(re.findall(r"PSYCACHY_CHECK:([a-z_]+):" + re.escape(nonce), text))
    if f"PSYCACHY_VALIDATION_OK:{nonce}" not in text or not set(CHECKS).issubset(passed):
        missing = sorted(set(CHECKS) - passed)
        raise RuntimeError(f"Guest validation failed or stopped; missing checks: {missing}. See {log}")
    if f"PSYCACHY_VALIDATION_FAILED:{nonce}" in text:
        raise RuntimeError(f"Guest reported failure; see {log}")
    return sorted(passed)


def write_file(root, name, content, mode=0o644):
    path = root / name.lstrip("/")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(mode)


def worker(settings):
    work, output, packages = map(Path, (settings["work"], settings["output"], settings["packages"]))
    plan = settings["plan"]
    root = work / "root"
    root.mkdir()
    disk = work / "rootfs.ext4"
    with disk.open("xb") as stream:
        stream.truncate(settings["disk_gib"] * 1024 ** 3)
    host_log = output / "host.log"
    run = lambda args, timeout=600: run_logged(args, host_log, timeout)
    print("Creating isolated install-validation filesystem", flush=True)
    run(["mkfs.ext4", "-F", "-q", "-L", "psycachy-ci", disk])
    run(["mount", "-o", "loop", disk, root])
    try:
        distro = DISTROS[settings["distro"]]
        print(f"Bootstrapping {settings['distro']} {settings['suite']}", flush=True)
        run(["debootstrap", "--variant=minbase", "--arch=amd64", "--include=ca-certificates",
             f"--components={distro['components']}", f"--keyring={distro['keyring']}",
             settings["suite"], root, distro["mirror"]], settings["prepare_timeout"])
        write_file(root, "/etc/apt/sources.list", apt_sources(settings["distro"], settings["suite"]))
        write_file(root, "/etc/apt/apt.conf.d/99psycachy-ci", 'APT::Get::Assume-Yes "true";\nAPT::Install-Recommends "false";\n')
        write_file(root, "/usr/sbin/policy-rc.d", "#!/bin/sh\nexit 101\n", 0o755)
        write_file(root, "/etc/fstab", "/dev/vda / ext4 defaults 0 1\n")
        write_file(root, "/etc/hostname", "psycachy-validation\n")
        write_file(root, "/etc/hosts", "127.0.0.1 localhost psycachy-validation\n")
        resolv = root / "etc/resolv.conf"
        resolv.unlink(missing_ok=True)
        shutil.copyfile("/etc/resolv.conf", resolv)
        bundle = root / "opt/psycachy-packages"
        bundle.mkdir(parents=True)
        for name in plan["checksums"]:
            shutil.copyfile(packages / name, bundle / name)
        shutil.copyfile(packages / "SHA256SUMS", bundle / "SHA256SUMS")
        # Recheck after the copy, before executing anything from the bundle.
        if verify_bundle(bundle) != plan["checksums"]:
            raise RuntimeError("Package bundle changed after inspection")
        assets = root / "usr/lib/psycachy-validation"
        shutil.copytree(ASSETS, assets)
        for script in assets.glob("*.sh"):
            script.chmod(0o755)
        module = root / "usr/src/psycachy-ci-1.0"
        shutil.copytree(ASSETS / "dkms", module)
        (module / "build.sh").chmod(0o755)
        values = {"KERNEL_RELEASE": plan["kernel_release"], "IMAGE_META": plan["image_meta"],
                  "HEADERS_META": plan["headers_meta"], "EXPECT_BTF": str(int(plan["btf"])),
                  "EXPECT_SCX": str(int(plan["sched_ext"])), "VALIDATION_NONCE": settings["nonce"]}
        write_file(root, "/etc/psycachy-validation.env", "".join(f"{key}={shlex.quote(value)}\n" for key, value in values.items()))
        # A private /dev exposes only the basic character devices needed by APT.
        run(["mount", "-t", "tmpfs", "-o", "mode=755", "validation-dev", root / "dev"])
        for name, major, minor in (("null", 1, 3), ("zero", 1, 5), ("random", 1, 8), ("urandom", 1, 9), ("tty", 5, 0)):
            os.mknod(root / "dev" / name, stat.S_IFCHR | 0o666, os.makedev(major, minor))
            (root / "dev" / name).chmod(0o666)
        (root / "dev/pts").mkdir()
        (root / "dev/shm").mkdir(mode=0o1777)
        (root / "dev/shm").chmod(0o1777)
        (root / "dev/fd").symlink_to("/proc/self/fd")
        run(["mount", "-t", "devpts", "-o", "newinstance,ptmxmode=0666", "devpts", root / "dev/pts"])
        (root / "dev/ptmx").symlink_to("pts/ptmx")
        run(["mount", "-t", "proc", "proc", root / "proc"])
        run(["mount", "-t", "sysfs", "-o", "ro", "sysfs", root / "sys"])
        print("Installing the bundle and exercising kernel package hooks", flush=True)
        run(["chroot", root, "/usr/bin/env", "DEBIAN_FRONTEND=noninteractive", "/bin/bash",
             "/usr/lib/psycachy-validation/prepare.sh", plan["kernel_release"],
             str(plan["llvm_major"] or "gcc")], settings["prepare_timeout"])
        for name, source in (("vmlinuz", f"boot/vmlinuz-{plan['kernel_release']}"),
                             ("initrd.img", f"boot/initrd.img-{plan['kernel_release']}")):
            if not (root / source).is_file():
                raise RuntimeError(f"Installed kernel artifact is missing: {source}")
            shutil.copyfile(root / source, work / name)
    finally:
        # Preserve the useful diagnostics even if an APT/DKMS hook failed.
        for source, name in (("var/log/apt/term.log", "apt.log"),
                             ("var/log/dpkg.log", "dpkg.log"),
                             ("var/lib/dkms/psycachy-ci/1.0/build/make.log", "dkms-build.log")):
            if (root / source).is_file():
                shutil.copyfile(root / source, output / name)
        # Both successful and failing paths finish before QEMU can open the disk.
        run(["umount", "--recursive", root], 60)
    print("Booting installed kernel and checking DKMS, initramfs and GRUB configuration", flush=True)
    guest_log = output / "guest.log"
    run_logged(qemu_command(work, settings["accel"]), guest_log, settings["boot_timeout"])
    checks = check_guest(guest_log, settings["nonce"])
    (output / "guest-checks.json").write_text(json.dumps(checks, indent=2) + "\n")


def expectation(value):
    return {"auto": None, "enabled": True, "disabled": False}[value]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packages", type=Path, required=True)
    parser.add_argument("--distro", choices=DISTROS, required=True)
    parser.add_argument("--suite", help="defaults to trixie for Debian or noble for Ubuntu")
    parser.add_argument("--work-dir", type=Path, help="new empty workspace path; deleted unless --keep-work")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--prepare-timeout", type=int, default=1800)
    parser.add_argument("--boot-timeout", type=int, default=600)
    parser.add_argument("--disk-gib", type=int, default=12)
    parser.add_argument("--accel", choices=("tcg", "kvm"), default="tcg")
    parser.add_argument("--expect-btf", choices=("auto", "enabled", "disabled"), default="auto")
    parser.add_argument("--expect-sched-ext", choices=("auto", "enabled", "disabled"), default="auto")
    parser.add_argument("--keep-work", action="store_true", help="retain the disposable rootfs for debugging")
    parser.add_argument("--dry-run", action="store_true", help="verify bundle and print plan without root or changes")
    args = parser.parse_args(argv)
    suite = args.suite or DISTROS[args.distro]["suite"]
    if suite != DISTROS[args.distro]["suite"]:
        raise ValueError("Supported validation targets are Debian trixie and Ubuntu noble")
    if not 4 <= args.disk_gib <= 64 or not 30 <= args.boot_timeout <= 3600 or not 60 <= args.prepare_timeout <= 7200:
        raise ValueError("Disk size or timeout is outside the supported range")
    packages = args.packages.resolve(strict=True)
    plan = inspect_bundle(packages, expectation(args.expect_btf), expectation(args.expect_sched_ext))
    if args.dry_run:
        print(json.dumps({"distro": args.distro, "suite": suite, **plan}, indent=2))
        return 0
    if os.geteuid() != 0:
        raise ValueError("Install/boot validation requires root; use sudo or --dry-run")
    for tool in ("unshare", "debootstrap", "qemu-system-x86_64", "mkfs.ext4", "mount", "umount", "chroot"):
        if shutil.which(tool) is None:
            raise ValueError(f"Required host tool is missing: {tool}")
    if not Path(DISTROS[args.distro]["keyring"]).is_file():
        raise ValueError("Install the selected distro's archive keyring on the host")
    if args.output.is_symlink():
        raise ValueError("Output directory must not be a symbolic link")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if args.work_dir:
        work = args.work_dir.absolute()
        # Never reuse or recursively remove a caller's preexisting directory.
        work.mkdir(mode=0o700)
    else:
        work = Path(tempfile.mkdtemp(prefix="psycachy-install-validation-"))
    settings = {"work": str(work), "packages": str(packages), "output": str(output),
                "distro": args.distro, "suite": suite, "plan": plan, "nonce": secrets.token_hex(16),
                "disk_gib": args.disk_gib, "prepare_timeout": args.prepare_timeout,
                "boot_timeout": args.boot_timeout, "accel": args.accel}
    report = {"distro": args.distro, "suite": suite, **plan, "status": "failed",
              "boot_method": "QEMU direct kernel/initrd; GRUB configuration checked inside guest",
              "limitations": ["No firmware/Secure Boot boot-chain validation", "No physical hardware or GPU validation"]}
    try:
        if work == output or work in output.parents:
            raise ValueError("Output must be outside the disposable workspace")
        config = work / "settings.json"
        config.write_text(json.dumps(settings))
        # Kernel/package scripts can only mount within this isolated namespace.
        run_logged(["unshare", "--mount", "--propagation", "private", sys.executable,
                    Path(__file__).resolve(), "--internal-worker", config], output / "driver.log",
                   args.prepare_timeout * 2 + args.boot_timeout + 300)
        report["checks"] = json.loads((output / "guest-checks.json").read_text())
        report["status"] = "passed"
        print(f"Install and boot validation passed: {args.distro} {suite}, {plan['kernel_release']}")
    except BaseException as error:
        report["error"] = str(error)
        raise
    finally:
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        if not args.keep_work:
            shutil.rmtree(work)
    return 0


if __name__ == "__main__":
    def interrupted(_signal, _frame):
        raise RuntimeError("Install validation interrupted")

    signal.signal(signal.SIGTERM, interrupted)
    try:
        if len(sys.argv) == 3 and sys.argv[1] == "--internal-worker":
            worker(json.loads(Path(sys.argv[2]).read_text()))
        else:
            sys.exit(main())
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Install validation: {error}", file=sys.stderr)
        sys.exit(1)
