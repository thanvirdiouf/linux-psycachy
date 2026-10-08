#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Pin, verify, and configure immutable CachyOS patch snapshots."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import urllib.request


REPOSITORY = "CachyOS/kernel-patches"
FEATURE_CONFIG = {
    "acpi-call": {"ACPI_CALL": "m"},
    "aufs": {"AUFS_FS": "m"},
    "handheld": {symbol: "m" for symbol in (
        "MFD_STEAMDECK", "EXTCON_STEAMDECK", "SENSORS_STEAMDECK",
        "LEDS_STEAMDECK", "LEDS_VALVE", "HID_ASUS_ALLY", "HID_MSI",
        "ZOTAC_ZONE_HID", "ZOTAC_ZONE_PLATFORM", "SND_SOC_AW87XXX",
    )},
}
SHA256 = re.compile(r"[a-f0-9]{64}")
SHA1 = re.compile(r"[a-f0-9]{40}")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def api(path):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "PsyCachy-updater"}
    if token := os.environ.get("GH_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(f"https://api.github.com/repos/{REPOSITORY}/{path}", headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def upstream_index(series):
    commit = api("commits/master")["sha"]
    if not SHA1.fullmatch(commit):
        raise ValueError("Invalid upstream patch commit")
    tree = api(f"git/trees/{commit}?recursive=1")
    if tree.get("truncated"):
        raise ValueError("Upstream patch tree is truncated")
    entries = {entry["path"]: entry for entry in tree["tree"]
               if entry["type"] == "blob" and entry["path"].startswith(series + "/")}
    return commit, entries


def profile(root):
    features = json.loads((root / "src/patches/profile.json").read_text())["features"]
    if not isinstance(features, list) or any(feature not in FEATURE_CONFIG for feature in features):
        raise ValueError("Unsupported optional feature in src/patches/profile.json")
    if len(set(features)) != len(features):
        raise ValueError("Duplicate optional feature")
    return sorted(features)


def select_paths(series, features, entries):
    paths = [("0001-bore-cachy.patch", f"{series}/sched/0001-bore-cachy.patch")]
    if "acpi-call" in features:
        paths.append(("0002-acpi-call.patch", f"{series}/misc/0001-acpi-call.patch"))
    if "aufs" in features:
        pattern = re.compile(rf"{re.escape(series)}/misc/0001-aufs-{re.escape(series)}-merge-v(\d{{8}})\.patch")
        matches = [path for path in entries if pattern.fullmatch(path)]
        if not matches:
            raise ValueError(f"No AUFS patch available for {series}")
        paths.append(("0003-aufs.patch", max(matches)))
    if "handheld" in features:
        paths.append(("0004-handheld.patch", f"{series}/misc/0001-handheld.patch"))
    for _, path in paths:
        if path not in entries:
            raise ValueError(f"Selected upstream patch is missing: {path}")
    return paths


def download_patch(commit, path, entry):
    # Only selected, constructed paths from the commit-pinned tree reach this call.
    url = f"https://raw.githubusercontent.com/{REPOSITORY}/{commit}/{path}"
    with urllib.request.urlopen(url, timeout=60) as response:
        content = response.read(16 * 1024 * 1024 + 1)
    if len(content) > 16 * 1024 * 1024:
        raise ValueError(f"Upstream patch is unexpectedly large: {path}")
    blob = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
    if blob != entry["sha"]:
        raise ValueError(f"Upstream patch does not match the pinned Git blob: {path}")
    return content


def apply_patch(tree, path, dry_run=False):
    command = ["patch", "--batch", "--forward", "--fuzz=0", "-p1", "-d", str(tree), "-i", str(path)]
    if dry_run:
        command.append("--dry-run")
    return subprocess.run(command, text=True, capture_output=True)


def adapt_bore(content, tree):
    """Carry forward only the known context fix, without changing added code."""
    text = content.decode()
    old = "@@ -824,6 +824,31 @@ struct kmap_ctrl {\n #endif\n };\n \n"
    new = "@@ -839,6 +839,31 @@ struct task_ipi_mask {\n struct task_ipi_mask { };\n #endif\n \n"
    source = tree / "include/linux/sched.h"
    if text.count(old) != 1 or not source.is_file():
        raise ValueError("BORE needs an unfamiliar rebase; review its failing hunks manually")
    context = "struct task_ipi_mask { };\n#endif\n\nstruct task_struct {"
    if context not in source.read_text():
        raise ValueError("BORE context fix no longer matches the source; manual review required")
    return text.replace(old, new, 1).encode()


def store_snapshot(root, metadata, contents):
    identity = hashlib.sha256(canonical(metadata)).hexdigest()
    target = root / "src/patches/snapshots" / identity
    if target.exists():
        verify_snapshot(root, identity)
        return identity
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".snapshot-", dir=target.parent) as temporary:
        staging = Path(temporary)
        for name, content in contents.items():
            (staging / name).write_bytes(content)
        (staging / "patchset.json").write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
        staging.rename(target)
    return identity


def verify_snapshot(root, identity):
    if not isinstance(identity, str) or not SHA256.fullmatch(identity):
        raise ValueError("Invalid patch snapshot identity")
    folder = root / "src/patches/snapshots" / identity
    metadata = json.loads((folder / "patchset.json").read_text())
    if hashlib.sha256(canonical(metadata)).hexdigest() != identity:
        raise ValueError("Patch snapshot metadata was modified; create a new snapshot")
    patches = metadata["patches"]
    if not patches or not isinstance(patches, list):
        raise ValueError("Empty patch snapshot")
    for patch in patches:
        name = patch["name"]
        if not re.fullmatch(r"\d{4}-[a-z0-9-]+\.patch", name):
            raise ValueError("Invalid snapshot patch filename")
        path = folder / name
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != patch["sha256"]:
            raise ValueError(f"Patch checksum mismatch: {name}")
    for symbol, value in metadata["config"].items():
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", symbol) or value not in ("y", "m", "n"):
            raise ValueError("Invalid snapshot configuration")
    return folder, metadata


def load(root, version):
    lockfile = root / "src/patches/releases.json"
    locks = json.loads(lockfile.read_text()) if lockfile.exists() else {}
    if version not in locks:
        return None, None
    entry = locks[version]
    rows = [line.split() for line in (root / "src/releases.tsv").read_text().splitlines()
            if line.split() and line.split()[0] == version]
    if len(rows) != 1 or rows[0][2] != entry["source_sha256"]:
        raise ValueError("Patch lock does not match the pinned source archive")
    return verify_snapshot(root, entry["snapshot"])


def refresh(root, candidate, archive):
    series = candidate["version"].rsplit(".", 1)[0]
    features = profile(root)
    commit, entries = upstream_index(series)
    paths = select_paths(series, features, entries)
    config = {key: value for feature in features for key, value in FEATURE_CONFIG[feature].items()}
    metadata = {"upstream_commit": commit, "features": features, "config": config,
                "upstream_paths": sorted(entries), "patches": []}
    contents = {}
    with tempfile.TemporaryDirectory(prefix=".prepare-patches-", dir=root / "src") as temporary:
        tree = Path(temporary) / "source"
        tree.mkdir()
        subprocess.run(["tar", "-xzf", str(archive), "-C", str(tree), "--strip-components=1"], check=True)
        for name, path in paths + [("0009-debian-headers-config.patch", None)]:
            content = download_patch(commit, path, entries[path]) if path else (
                root / "src/patches/common/0002-debian-headers-config.patch").read_bytes()
            upstream_digest = hashlib.sha256(content).hexdigest()
            patch = Path(temporary) / name
            patch.write_bytes(content)
            adaptation = None
            result = apply_patch(tree, patch, dry_run=True)
            if result.returncode and name == "0001-bore-cachy.patch":
                content = adapt_bore(content, tree)
                patch.write_bytes(content)
                adaptation = "task_ipi_mask context only"
                result = apply_patch(tree, patch, dry_run=True)
            if result.returncode:
                raise ValueError(f"Patch preparation failed for {name}:\n{result.stdout}\n{result.stderr}")
            result = apply_patch(tree, patch)
            if result.returncode:
                raise ValueError(f"Patch application failed for {name}:\n{result.stdout}\n{result.stderr}")
            contents[name] = content
            metadata["patches"].append({"name": name, "sha256": hashlib.sha256(content).hexdigest(),
                                        "upstream_path": path, "upstream_sha256": upstream_digest,
                                        "adaptation": adaptation})
    identity = store_snapshot(root, metadata, contents)
    return identity, metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("list", "config-id", "configure", "verify-config"))
    parser.add_argument("root", type=Path)
    parser.add_argument("version")
    parser.add_argument("tree", nargs="?", type=Path)
    args = parser.parse_args()
    folder, metadata = load(args.root, args.version)
    config = metadata["config"] if metadata else {}
    if args.command == "list":
        if not metadata:
            print(args.root / f"src/patches/{args.version.rsplit('.', 1)[0]}/0001-bore-cachy.patch")
            print(args.root / "src/patches/common/0002-debian-headers-config.patch")
        else:
            for patch in metadata["patches"]:
                print(folder / patch["name"])
    elif args.command == "config-id":
        if config:
            print(hashlib.sha256(canonical(config)).hexdigest())
    elif args.command == "configure":
        for symbol, value in config.items():
            flag = {"y": "--enable", "m": "--module", "n": "--disable"}[value]
            subprocess.run([str(args.tree / "scripts/config"), "--file", str(args.tree / ".config"), flag, symbol], check=True)
    elif args.command == "verify-config":
        resolved = (args.tree / ".config").read_text().splitlines()
        for symbol, value in config.items():
            expected = f"CONFIG_{symbol}={value}" if value != "n" else f"# CONFIG_{symbol} is not set"
            if expected not in resolved:
                raise ValueError(f"Selected feature configuration is missing: CONFIG_{symbol}={value}")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error))
