#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Pin, verify, and configure immutable CachyOS patch snapshots."""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import urllib.request


REPOSITORY = "CachyOS/kernel-patches"
RECIPE_REPOSITORY = "CachyOS/linux-cachyos"
RECIPE_DIRECTORY = "linux-cachyos"
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


def api(path, repository=REPOSITORY):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "PsyCachy-updater"}
    if token := os.environ.get("GH_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(f"https://api.github.com/repos/{repository}/{path}", headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def upstream_index(series, commit=None):
    commit = commit or api("commits/master")["sha"]
    if not SHA1.fullmatch(commit):
        raise ValueError("Invalid upstream patch commit")
    tree = api(f"git/trees/{commit}?recursive=1")
    if tree.get("truncated"):
        raise ValueError("Upstream patch tree is truncated")
    entries = {entry["path"]: entry for entry in tree["tree"]
               if entry["type"] == "blob" and entry["path"].startswith(series + "/")}
    return commit, entries


def git_file(repository, commit, path):
    """Read a file at an immutable commit, checking GitHub's Git blob identity."""
    if not SHA1.fullmatch(commit):
        raise ValueError("Invalid upstream recipe commit")
    entry = api(f"contents/{path}?ref={commit}", repository)
    if entry.get("type") != "file" or entry.get("encoding") != "base64":
        raise ValueError(f"Unexpected upstream file response: {path}")
    content = base64.b64decode(entry["content"], validate=False)
    if len(content) > 16 * 1024 * 1024:
        raise ValueError(f"Upstream file is unexpectedly large: {path}")
    blob = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
    if blob != entry["sha"]:
        raise ValueError(f"Upstream file does not match the pinned Git blob: {path}")
    return content


def recipe_release(content):
    """Recognize the upstream stable source expression without executing shell."""
    text = content.decode()
    fields = {}
    for name, pattern in (("_major", r"\d+\.\d+"), ("_minor", r"\d+"), ("_tagrel", r"\d+")):
        matches = re.findall(rf"^[ \t]*{name}=([^\n]*)$", text, re.MULTILINE)
        if len(matches) != 1 or not re.fullmatch(pattern, matches[0].strip()):
            raise ValueError(f"Unsupported PKGBUILD assignment for {name}; review upstream recipe manually")
        fields[name] = matches[0].strip()
    sources = re.findall(r"^[ \t]*_srcname=([^\n]*)$", text, re.MULTILINE)
    expressions = {"cachyos-${_major}.${_minor}-${_tagrel}",
                   '"cachyos-${_major}.${_minor}-${_tagrel}"', "'cachyos-${_major}.${_minor}-${_tagrel}'"}
    if (len(sources) != 1 or sources[0].strip() not in expressions
            or re.search(r"^[ \t]*_rcver=", text, re.MULTILINE)):
        raise ValueError("Unsupported PKGBUILD source expression; review upstream recipe manually")
    return f"cachyos-{fields['_major']}.{fields['_minor']}-{fields['_tagrel']}"


def recipe_snapshot(source_release):
    """Find the newest directory snapshot that actually targets this source tag.

    A newer PKGBUILD may already target the next kernel. Walk directory history
    to retain config-only changes while refusing to pair that recipe with an
    older archive. The bounded search fails closed when manual review is needed.
    """
    for page in range(1, 11):
        commits = api(f"commits?path={RECIPE_DIRECTORY}&per_page=100&page={page}", RECIPE_REPOSITORY)
        if not isinstance(commits, list):
            raise ValueError("Unexpected upstream recipe history response")
        for item in commits:
            commit = item["sha"]
            recipe = git_file(RECIPE_REPOSITORY, commit, f"{RECIPE_DIRECTORY}/PKGBUILD")
            try:
                release = recipe_release(recipe)
            except ValueError:
                # RC and old recipe formats are not stable candidates. Their
                # shell is never evaluated, including during history search.
                continue
            if release != source_release:
                continue
            config = git_file(RECIPE_REPOSITORY, commit, f"{RECIPE_DIRECTORY}/config")
            metadata = {"repository": RECIPE_REPOSITORY, "commit": commit,
                        "path": f"{RECIPE_DIRECTORY}/PKGBUILD", "sha256": hashlib.sha256(recipe).hexdigest(),
                        "config_path": f"{RECIPE_DIRECTORY}/config", "config_sha256": hashlib.sha256(config).hexdigest(),
                        "source_release": release}
            return metadata, {"upstream-PKGBUILD": recipe, "upstream-config": config}
        if len(commits) < 100:
            break
    raise ValueError(f"No supported stable recipe snapshot targets {source_release}; review upstream before updating")


def discover_inputs(root, candidate, patch_commit=None):
    """Resolve reviewable immutable recipe, configuration and selected patches."""
    recipe, contents = recipe_snapshot(candidate["release"])
    series = candidate["version"].rsplit(".", 1)[0]
    features = profile(root)
    commit, entries = upstream_index(series, patch_commit) if patch_commit else upstream_index(series)
    paths = select_paths(series, features, entries)
    # Only used content affects discovery. An unrelated patch or README change
    # must not cause daily rebuilds; the actual commits still enter provenance.
    inputs = {"source_release": candidate["release"],
              "recipe_sha256": recipe["sha256"], "config_sha256": recipe["config_sha256"],
              "features": features, "patch_blobs": {path: entries[path]["sha"] for _, path in paths},
              "debian_patch_sha256": hashlib.sha256(
                  (root / "src/patches/common/0002-debian-headers-config.patch").read_bytes()).hexdigest()}
    return {"identity": hashlib.sha256(canonical(inputs)).hexdigest(), "recipe": recipe,
            "contents": contents, "features": features, "commit": commit,
            "entries": entries, "paths": paths}


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


def adapt_bore(content, tree, details=""):
    """Carry forward only the known context fix, without changing added code.

    ``details`` is the failing ``patch`` output; it is appended to errors so a
    reviewer can see which hunks need attention.
    """
    text = content.decode()
    failure = f":\n{details.strip()}" if details.strip() else ""
    old = "@@ -824,6 +824,31 @@ struct kmap_ctrl {\n #endif\n };\n \n"
    new = "@@ -839,6 +839,31 @@ struct task_ipi_mask {\n struct task_ipi_mask { };\n #endif\n \n"
    source = tree / "include/linux/sched.h"
    if text.count(old) != 1 or not source.is_file():
        raise ValueError("BORE needs an unfamiliar rebase; review its failing hunks manually" + failure)
    context = "struct task_ipi_mask { };\n#endif\n\nstruct task_struct {"
    if context not in source.read_text():
        raise ValueError("BORE context fix no longer matches the source; manual review required" + failure)
    return text.replace(old, new, 1).encode()


def adapt_handheld(content, tree, details=""):
    """Preserve the 7.2.10 millipercent declaration in backlight hunk context."""
    text = content.decode()
    failure = f":\n{details.strip()}" if details.strip() else ""
    path = "drivers/gpu/drm/amd/display/amdgpu_dm/amdgpu_dm.c"
    marker = f"diff --git a/{path} b/{path}\n"
    old = (
        "@@ -5419,6 +5431,7 @@ static void amdgpu_dm_backlight_set_level(struct amdgpu_display_manager *dm,\n"
        " \tstruct amdgpu_dm_backlight_caps *caps;\n"
        " \tstruct dc_link *link;\n"
        " \tu32 brightness = 0;\n"
        "+\tu32 prev_brightness;\n"
        " \tbool rc = false, reallow_idle = false;\n"
        " \tstruct drm_connector *connector;\n"
        " \tstruct dc_stream_state *stream;\n"
    )
    source = tree / path
    if text.count(marker) != 1 or not source.is_file():
        raise ValueError("Handheld needs an unfamiliar rebase; review its failing hunks manually" + failure)
    start = text.index(marker)
    end = text.find("\ndiff --git ", start + len(marker))
    end = len(text) if end == -1 else end
    section = text[start:end]
    if section.count(old) != 1:
        raise ValueError("Handheld needs an unfamiliar rebase; review its failing hunks manually" + failure)
    new = old.replace("-5419,6 +5431,7", "-5419,7 +5431,8").replace(
        "+\tu32 prev_brightness;\n", "+\tu32 prev_brightness;\n \tu32 millipercent = 0;\n")
    context = "".join(line[1:] for line in new.splitlines(keepends=True) if line.startswith(" "))
    if source.read_text().count(context) != 1:
        raise ValueError("Handheld context fix no longer matches the source; manual review required" + failure)
    return (text[:start] + section.replace(old, new, 1) + text[end:]).encode()


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
    if recipe := metadata.get("recipe"):
        if (recipe.get("repository") != RECIPE_REPOSITORY or not SHA1.fullmatch(recipe.get("commit", ""))
                or recipe.get("path") != f"{RECIPE_DIRECTORY}/PKGBUILD"
                or recipe.get("config_path") != f"{RECIPE_DIRECTORY}/config"):
            raise ValueError("Invalid upstream recipe provenance")
        for filename, digest in (("upstream-PKGBUILD", recipe["sha256"]),
                                 ("upstream-config", recipe["config_sha256"])):
            path = folder / filename
            if (not isinstance(digest, str) or not SHA256.fullmatch(digest) or path.is_symlink()
                    or hashlib.sha256(path.read_bytes()).hexdigest() != digest):
                raise ValueError(f"Recipe snapshot checksum mismatch: {filename}")
        if recipe_release((folder / "upstream-PKGBUILD").read_bytes()) != recipe["source_release"]:
            raise ValueError("Recipe snapshot targets a different source release")
    return folder, metadata


def release_locks(root):
    lockfile = root / "src/patches/releases.json"
    locks = json.loads(lockfile.read_text()) if lockfile.exists() else {}
    if not isinstance(locks, dict) or any(not isinstance(entry, dict) for entry in locks.values()):
        raise ValueError("Invalid patch release lock document")
    for entry in locks.values():
        revision = entry.get("debian_revision", 1)
        if type(revision) is not int or revision < 1:
            raise ValueError("Invalid Debian package revision in patch lock")
    return locks


def load(root, version):
    locks = release_locks(root)
    if version not in locks:
        return None, None
    entry = locks[version]
    rows = [line.split() for line in (root / "src/releases.tsv").read_text().splitlines()
            if line.split() and line.split()[0] == version]
    if (len(rows) != 1 or rows[0][2] != entry["source_sha256"]
            or entry.get("source_release", rows[0][1]) != rows[0][1]):
        raise ValueError("Patch lock does not match the pinned source archive")
    folder, metadata = verify_snapshot(root, entry["snapshot"])
    if metadata.get("recipe", {}).get("source_release", rows[0][1]) != rows[0][1]:
        raise ValueError("Upstream recipe does not match the pinned source archive")
    return folder, metadata


def refresh(root, candidate, archive, inputs=None):
    series = candidate["version"].rsplit(".", 1)[0]
    inputs = inputs or discover_inputs(root, candidate)
    if inputs["recipe"]["source_release"] != candidate["release"]:
        raise ValueError("Upstream recipe does not match the candidate source archive")
    features = inputs["features"]
    commit, entries, paths = inputs["commit"], inputs["entries"], inputs["paths"]
    config = {key: value for feature in features for key, value in FEATURE_CONFIG[feature].items()}
    metadata = {"upstream_commit": commit, "features": features, "config": config,
                "upstream_paths": sorted(entries), "patches": [],
                "recipe": inputs["recipe"], "input_identity": inputs["identity"]}
    contents = dict(inputs["contents"])
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
                content = adapt_bore(content, tree, result.stdout + result.stderr)
                patch.write_bytes(content)
                adaptation = "task_ipi_mask context only"
                result = apply_patch(tree, patch, dry_run=True)
            if result.returncode and name == "0004-handheld.patch":
                content = adapt_handheld(content, tree, result.stdout + result.stderr)
                patch.write_bytes(content)
                adaptation = "amdgpu_dm millipercent declaration context only"
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
    parser.add_argument("command", choices=("list", "config-id", "configure", "verify-config", "base-config", "debian-revision", "metadata"))
    parser.add_argument("root", type=Path)
    parser.add_argument("version")
    parser.add_argument("tree", nargs="?", type=Path)
    args = parser.parse_args()
    folder, metadata = load(args.root, args.version)
    config = metadata["config"] if metadata else {}
    # configure with an empty feature set is a no-op and needs no tree.
    if args.tree is None and (args.command == "verify-config" or (args.command == "configure" and config)):
        parser.error(f"{args.command} requires the kernel source tree argument")
    if args.command == "list":
        if not metadata:
            print(args.root / f"src/patches/{args.version.rsplit('.', 1)[0]}/0001-bore-cachy.patch")
            print(args.root / "src/patches/common/0002-debian-headers-config.patch")
        else:
            for patch in metadata["patches"]:
                print(folder / patch["name"])
    elif args.command == "config-id":
        if metadata and metadata.get("recipe"):
            print(hashlib.sha256(canonical({"config": config,
                "base_config_sha256": metadata["recipe"]["config_sha256"]})).hexdigest())
        elif config:
            print(hashlib.sha256(canonical(config)).hexdigest())
    elif args.command == "base-config":
        print(folder / "upstream-config" if metadata and metadata.get("recipe") else args.root / "src/config")
    elif args.command == "debian-revision":
        print(release_locks(args.root).get(args.version, {}).get("debian_revision", 1))
    elif args.command == "metadata":
        print(json.dumps(metadata or {}, indent=2, sort_keys=True))
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
