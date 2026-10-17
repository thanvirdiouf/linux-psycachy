#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Discover stable CachyOS updates; optionally pin a reviewed candidate."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import urllib.request

try:
    from . import patchsets
except ImportError:
    import patchsets


TAG = re.compile(r"cachyos-(\d+\.\d+\.\d+)-(\d+)")
HASH = re.compile(r"[a-f0-9]{64}")
ROOT = Path(__file__).resolve().parents[1]


def version_tuple(version):
    return tuple(map(int, version.split(".")))


def read_manifest(root):
    entries = {}
    for line in (root / "src/releases.tsv").read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = line.split()
        if len(fields) != 3:
            raise ValueError("Invalid release manifest entry")
        version, tag, digest = fields
        match = TAG.fullmatch(tag)
        if not match or match[1] != version or not HASH.fullmatch(digest):
            raise ValueError("Invalid release manifest entry")
        if version in entries:
            raise ValueError(f"Duplicate manifest version: {version}")
        entries[version] = (tag, digest)
    if not entries:
        raise ValueError("The release manifest is empty")
    return entries


def fetch_releases():
    # Query the list, rather than /latest, which may point at a release candidate.
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "PsyCachy-updater"}
    token = os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    for page in range(1, 21):
        url = f"https://api.github.com/repos/CachyOS/linux/releases?per_page=100&page={page}"
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=60) as response:
            releases = json.load(response)
        if not isinstance(releases, list):
            raise ValueError("Unexpected GitHub releases response")
        yield from releases
        if len(releases) < 100:
            return
    raise ValueError("Release pagination limit exceeded; update the discovery limit")


def read_default(root, entries):
    version = (root / "src/default-version").read_text().strip()
    if version not in entries:
        raise ValueError("The default kernel version must be registered in src/releases.tsv")
    return version


def select_candidate(entries, releases, current=None, include_current=False):
    current = current or max(entries, key=version_tuple)
    series = current.rsplit(".", 1)[0]
    current = max((version for version in entries
                   if version.rsplit(".", 1)[0] == series), key=version_tuple)
    current_revision = int(TAG.fullmatch(entries[current][0])[2])
    candidates = []
    for release in releases:
        match = TAG.fullmatch(release.get("tag_name", ""))
        if not match or release.get("draft") or release.get("prerelease"):
            continue
        version, revision = match.groups()
        if version.rsplit(".", 1)[0] != series or version_tuple(version) < version_tuple(current):
            continue
        if (version == current and (int(revision) < current_revision
                                   or (int(revision) == current_revision and not include_current))):
            continue
        tag = release["tag_name"]
        assets = [asset for asset in release.get("assets", [])
                  if asset.get("name") == f"{tag}.tar.gz" and asset.get("state") == "uploaded"]
        if len(assets) != 1 or assets[0].get("size", 0) <= 0:
            continue
        asset = assets[0]
        digest = asset.get("digest")
        if digest is not None and (not isinstance(digest, str)
                                   or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest)):
            raise ValueError(f"Invalid published SHA-256 digest for {tag}")
        candidates.append({
            "version": version, "release": tag,
            "branch": f"codex/linux-{version}",
            "url": f"https://github.com/CachyOS/linux/releases/download/{tag}/{tag}.tar.gz",
            "digest": digest, "size": asset["size"],
        })
    return max(candidates, key=lambda item: (version_tuple(item["version"]),
               int(TAG.fullmatch(item["release"])[2])), default=None)


def discover(root, entries, releases, current):
    """Discover source rebuilds and changes to the inputs of an existing source."""
    series = current.rsplit(".", 1)[0]
    current = max((version for version in entries if version.rsplit(".", 1)[0] == series), key=version_tuple)
    candidate = select_candidate(entries, releases, current, include_current=True)
    if candidate is None:
        tag, digest = entries[current]
        candidate = {"version": current, "release": tag,
                     "url": f"https://github.com/CachyOS/linux/releases/download/{tag}/{tag}.tar.gz",
                     "digest": f"sha256:{digest}", "size": None}
    previous = patchsets.release_locks(root).get(candidate["version"], {})
    inputs = patchsets.discover_inputs(root, candidate)
    unchanged_source = (candidate["version"] in entries
                        and entries[candidate["version"]][0] == candidate["release"])
    if unchanged_source:
        # A mutable release asset must never silently replace an already pinned
        # tag, even when the publisher changes its advertised hash.
        expected = "sha256:" + entries[candidate["version"]][1]
        if candidate["digest"] and candidate["digest"] != expected:
            raise ValueError("Published source digest changed for a pinned release; review the replacement manually")
        candidate["digest"] = expected
        if previous.get("input_identity") == inputs["identity"]:
            return None, None
    candidate["input_identity"] = inputs["identity"]
    candidate["debian_revision"] = previous.get("debian_revision", 1) + 1 if unchanged_source or previous else 1
    candidate["branch"] = f"codex/linux-{candidate['version']}-r{candidate['debian_revision']}-{inputs['identity'][:12]}"
    return candidate, inputs


def archive_hash(candidate, destination=None):
    digest = hashlib.sha256()
    size = 0
    # The authentication token is only sent to the API, never to asset redirects.
    partial = Path(str(destination) + ".part") if destination else None
    try:
        with open(partial, "wb") if partial else tempfile.TemporaryFile() as archive:
            with urllib.request.urlopen(candidate["url"], timeout=60) as response:
                while chunk := response.read(1024 * 1024):
                    digest.update(chunk)
                    size += len(chunk)
                    archive.write(chunk)
        actual = digest.hexdigest()
        if candidate["size"] is not None and size != candidate["size"]:
            raise ValueError("Downloaded archive size does not match the release asset")
        if candidate["digest"] and candidate["digest"] != f"sha256:{actual}":
            raise ValueError("Downloaded archive does not match the published SHA-256 digest")
        if partial:
            partial.replace(destination)
        return actual
    finally:
        if partial:
            partial.unlink(missing_ok=True)


def adaptations(metadata):
    """Describe patches the updater modified relative to upstream."""
    return [f"`{item['name']}` ({item['adaptation']})"
            for item in (metadata or {}).get("patches", []) if item.get("adaptation")]


def apply_candidate(root, candidate, digest, snapshot=None, metadata=None):
    version, tag = candidate["version"], candidate["release"]
    match = TAG.fullmatch(tag)
    if not match or match[1] != version:
        raise ValueError("Candidate source release does not match its kernel version")
    patch = root / f"src/patches/{version.rsplit('.', 1)[0]}/0001-bore-cachy.patch"
    if not snapshot and not patch.is_file():
        raise ValueError(f"Missing series patch: {patch}")
    entries = read_manifest(root)
    read_default(root, entries)
    manifest = root / "src/releases.tsv"
    provenance = root / "src/patches/SOURCES.md"
    lockfile = root / "src/patches/releases.json"
    locks = patchsets.release_locks(root)
    if not HASH.fullmatch(digest):
        raise ValueError("Invalid source archive SHA-256")
    old = entries.get(version)
    revision = candidate.get("debian_revision", locks.get(version, {}).get("debian_revision", 1) + 1 if old else 1)
    if type(revision) is not int or revision < 1 or (old and revision <= locks.get(version, {}).get("debian_revision", 1)):
        raise ValueError("Debian build revision must increase for an existing kernel version")
    if old and int(match[2]) < int(TAG.fullmatch(old[0])[2]):
        raise ValueError("Refusing to downgrade the pinned source revision")
    if old and old[0] == tag and old[1] != digest:
        raise ValueError("Refusing to replace a pinned source tag with a different archive")
    if snapshot:
        _, verified = patchsets.verify_snapshot(root, snapshot)
        if metadata != verified:
            raise ValueError("Snapshot metadata does not match the reviewed candidate")
        if candidate.get("input_identity") and candidate["input_identity"] != metadata.get("input_identity"):
            raise ValueError("Snapshot inputs do not match the reviewed candidate")
        if metadata.get("recipe", {}).get("source_release", tag) != tag:
            raise ValueError("Recipe snapshot does not match the candidate source")
        previous = locks.get(version)
        if previous and previous.get("snapshot") == snapshot and old == (tag, digest):
            raise ValueError("Candidate inputs are already registered")
        entry = {"source_sha256": digest, "source_release": tag, "snapshot": snapshot,
                 "debian_revision": revision, "input_identity": metadata.get("input_identity")}
        if old:
            history = list((previous or {}).get("history", []))
            history.append({**{key: value for key, value in (previous or {}).items() if key != "history"},
                            "source_release": old[0], "source_sha256": old[1],
                            "debian_revision": (previous or {}).get("debian_revision", 1)})
            entry["history"] = history
        locks[version] = entry
    elif old:
        raise ValueError("Replacing a registered source requires an immutable input snapshot")
    # Validate and prepare all contents before modifying tracked files.
    adapted = adaptations(metadata) if snapshot else []
    row = f"{version}\t{tag}\t{digest}"
    manifest_text = ("\n".join(row if line.split() and line.split()[0] == version else line
                               for line in manifest.read_text().splitlines()) + "\n" if old else
                     manifest.read_text().rstrip("\n") + f"\n{row}\n")
    notes = provenance.read_text().rstrip("\n") + (
        f"\n\n## Candidate source: Linux {version}\n\n"
        f"- [CachyOS source release](https://github.com/CachyOS/linux/releases/tag/{tag})\n"
        f"- Archive SHA-256: `{digest}`\n" +
        (f"- Patch snapshot: `{snapshot}`\n"
         f"- Upstream patch commit: `{metadata['upstream_commit']}`\n"
         f"- Debian build revision: `{revision}`\n" +
         (f"- [Upstream recipe snapshot](https://github.com/CachyOS/linux-cachyos/tree/{metadata['recipe']['commit']}/linux-cachyos)\n"
          f"- Upstream config SHA-256: `{metadata['recipe']['config_sha256']}`\n"
          if metadata.get("recipe") else "") +
         f"- Optional features: {', '.join(metadata['features']) or 'none'}.\n" +
         (f"- Local patch adaptations: {', '.join(adapted)}.\n" if adapted else "") + "\n"
         if snapshot else "- Reuses the series BORE patch and shared Debian headers patch.\n\n") +
        "Registered by the updater. Compilation and boot validation must be reviewed\n"
        "before describing this version as validated.\n")
    manifest.write_text(manifest_text)
    (root / "src/default-version").write_text(version + "\n")
    provenance.write_text(notes)
    if snapshot:
        lockfile.write_text(json.dumps(locks, indent=2, sort_keys=True) + "\n")


def report(candidate):
    values = {"update": "true" if candidate else "false"}
    if candidate:
        values.update({key: candidate[key] for key in ("version", "release", "branch", "input_identity", "debian_revision")})
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a") as stream:
            for key, value in values.items():
                stream.write(f"{key}={value}\n")
    print(json.dumps(values, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Download, hash, and register the candidate")
    parser.add_argument("--expected-release", help="Abort if the discovered candidate has changed")
    parser.add_argument("--expected-input-id", help="Abort if the pinned recipe, config or patch inputs have changed")
    args = parser.parse_args()
    entries = read_manifest(ROOT)
    candidate, inputs = discover(ROOT, entries, fetch_releases(), read_default(ROOT, entries))
    if args.expected_release and (not candidate or candidate["release"] != args.expected_release):
        raise ValueError("The upstream candidate changed; rerun the update workflow")
    if args.expected_input_id and (not candidate or candidate["input_identity"] != args.expected_input_id):
        raise ValueError("The upstream candidate inputs changed; rerun the update workflow")
    if candidate and args.apply:
        archive = ROOT / f"src/{candidate['release']}.tar.gz"
        digest = archive_hash(candidate, archive)
        snapshot, metadata = patchsets.refresh(ROOT, candidate, archive, inputs)
        apply_candidate(ROOT, candidate, digest, snapshot, metadata)
        notes = Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir())) / "kernel-update-pr.md"
        notes.write_text(
            f"Updates Linux {candidate['version']} to Debian build revision {candidate['debian_revision']}\n"
            f"from `{candidate['release']}` and pins archive\n"
            f"SHA-256 `{digest}`. Refreshes selected patches at upstream commit\n"
            f"`{metadata['upstream_commit']}` and pins snapshot `{snapshot}`.\n"
            f"Pins [the matching CachyOS recipe and configuration](https://github.com/CachyOS/linux-cachyos/tree/{metadata['recipe']['commit']}/linux-cachyos).\n"
            f"Input identity: `{candidate['input_identity']}`.\n"
            f"Optional features: {', '.join(metadata['features']) or 'none'}.\n"
            "Previous source pins and package revisions remain in the lock history; their snapshots are retained.\n\n"
            "The update workflow builds this exact branch commit and uploads Debian packages.\n"
            "Open its run to inspect the build and download the artifacts.\n\n"
            "- [ ] Review patch application and the resolved kernel configuration.\n"
            "- [ ] Confirm package compilation and checksum verification succeeded.\n"
            "- [ ] Install and boot-test the packages, including hardware and DKMS modules.\n\n"
            "Merge after validation. GitHub Release publication remains manual.\n")
        adapted = adaptations(metadata)
        if adapted:
            with notes.open("a") as stream:
                stream.write("\nLocally adapted patches (these differ from upstream):\n\n")
                stream.writelines(f"- {item}\n" for item in adapted)
                stream.write("\n- [ ] Review each adaptation against the upstream patch.\n")
        selected = {item['upstream_path'] for item in metadata['patches'] if item['upstream_path']}
        unselected = sorted(set(metadata['upstream_paths']) - selected)
        if unselected:
            with notes.open("a") as stream:
                stream.write("\nOther upstream patches available for review (not applied):\n\n")
                stream.writelines(f"- `{path}`\n" for path in unselected)
    report(candidate)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"Missing expected key: {error}" if isinstance(error, KeyError) else str(error))
