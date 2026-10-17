#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Publish the verified packages from a successful Actions run without rebuilding.

Requires gh (authenticated with Actions read and Contents write access) and dpkg-deb.
Only repository-owned build/update runs are accepted. Downloaded code is never run.
"""

import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile


MODULES = ("nvidia", "nvidia-modeset", "nvidia-drm", "nvidia-uvm", "nvidia-peermem")
WORKFLOWS = {".github/workflows/build-debs.yml", ".github/workflows/update-kernel.yml"}
UPDATER_FILES = {"src/releases.tsv", "src/default-version", "src/patches/SOURCES.md",
                 "src/patches/releases.json"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest_file(path):
    with path.open("rb") as stream:
        return "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()


def read_build(dist):
    fields = {}
    for line in (dist / "BUILD.txt").read_text().splitlines():
        require(": " in line, "Malformed artifact build identity")
        key, value = line.split(": ", 1)
        require(key not in fields, "Duplicate artifact build identity field")
        fields[key] = value
    require(fields.keys() == {"Kernel", "Commit", "Run"}, "Unexpected artifact build identity fields")
    return fields


class GitHub:
    def __init__(self, repo):
        self.repo = repo

    def command(self, *args):
        return subprocess.check_output(["gh", *map(str, args)], text=True).strip()

    def api(self, path, method="GET", fields=None, missing_ok=False):
        command = ["gh", "api", f"repos/{self.repo}/{path}", "--method", method]
        if fields is not None:
            command += ["--input", "-"]
        result = subprocess.run(command, input=json.dumps(fields) if fields is not None else None,
                                capture_output=True, text=True)
        if result.returncode:
            if missing_ok and "HTTP 404" in result.stderr:
                return None
            raise ValueError(f"GitHub API failed for {path}: {result.stderr.strip()}")
        return json.loads(result.stdout)

    def content(self, path, commit):
        entry = self.api(f"contents/{path}?ref={commit}")
        require(entry.get("encoding") == "base64", f"Cannot read source metadata: {path}")
        return base64.b64decode(entry["content"])

    def download(self, run_id, artifact_name, directory):
        self.command("run", "download", run_id, "--repo", self.repo,
                     "--name", artifact_name, "--dir", directory)


def verify_run(gh, run):
    require(run["status"] == "completed" and run["conclusion"] == "success",
            f"Build is not ready: {run['status']} / {run['conclusion']}")
    require(run["repository"]["full_name"] == gh.repo
            and run["head_repository"]["full_name"] == gh.repo,
            "Fork builds cannot be promoted")
    require(run["path"] in WORKFLOWS, "Not a supported kernel build workflow")
    allowed = {"workflow_dispatch", "schedule"} if run["path"].endswith("update-kernel.yml") else {
        "workflow_dispatch", "push"}
    require(run["event"] in allowed, "Only repository push, manual, and updater runs can be promoted")


def verify_packages(dist, version, commit, run_id):
    entries = list(dist.iterdir())
    require(entries and all(path.is_file() and not path.is_symlink() for path in entries),
            "The artifact must contain only regular files")
    require(all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+~-]*", path.name) for path in entries),
            "Unexpected release asset filename")
    build = read_build(dist)
    require(build == {"Kernel": version, "Commit": commit, "Run": str(run_id)},
            "Artifact source identity does not match its run")
    for name in ("kernel.config", "BUILD.txt", "TOOLCHAIN.txt", "BUILD-TUNING.txt",
                 "NVIDIA-VALIDATION.txt", "install-packages.sh", "SHA256SUMS"):
        require((dist / name).is_file() and (dist / name).stat().st_size,
                f"Missing release metadata: {name}")
    checksums = {}
    for line in (dist / "SHA256SUMS").read_text().splitlines():
        match = re.fullmatch(r"([a-f0-9]{64}) [ *](?:\./)?([A-Za-z0-9][A-Za-z0-9._+~-]*)", line)
        require(match is not None, "Invalid checksum manifest entry")
        checksum, name = match.groups()
        require(name not in checksums, f"Duplicate checksum entry: {name}")
        require((dist / name).is_file(), f"Missing checksummed file: {name}")
        require(digest_file(dist / name) == "sha256:" + checksum, f"Checksum mismatch: {name}")
        checksums[name] = checksum
    packages = sorted(dist.glob("*.deb"))
    require({path.name for path in packages} | {"install-packages.sh"} <= checksums.keys(),
            "The checksum manifest does not cover all release packages")
    kernel_release = f"{version}-psycachy-llvm"
    package_version = None
    meta = (dist / "PACKAGE-BUILD.json").is_file()
    if meta:
        identity = json.loads((dist / "PACKAGE-BUILD.json").read_text())
        require(identity.keys() == {"kernel_release", "package_version", "toolchain"}
                and identity["toolchain"] == "clang-thinlto", "Invalid package build metadata")
        kernel_release = identity["kernel_release"]
        package_version = identity["package_version"]
        require(re.fullmatch(re.escape(version) + r"-psycachy-llvm(?:-minimal)?(?:-r[1-9][0-9]*)?", kernel_release)
                and re.fullmatch(re.escape(version) + r"-[1-9][0-9]*", package_version),
                "Package identity differs from the built kernel")
    expected = {f"linux-image-{kernel_release}", f"linux-headers-{kernel_release}", "psycachy-nvidia-support"}
    suffix = '-llvm-minimal' if '-minimal' in kernel_release else '-llvm'
    if meta:
        expected |= {f"linux-image-psycachy{suffix}", f"linux-headers-psycachy{suffix}"}
        for filename in ('OPTIMIZATIONS.json', 'PACKAGE-BUILD.json'):
            require(filename in checksums, f"Missing checksummed build metadata: {filename}")
    allowed = expected | {"linux-libc-dev", f"linux-image-{kernel_release}-dbg"}
    found = set()
    versions = set()
    for package in packages:
        architecture = subprocess.check_output(
            ["dpkg-deb", "--field", str(package), "Architecture"], text=True).strip()
        name = subprocess.check_output(
            ["dpkg-deb", "--field", str(package), "Package"], text=True).strip()
        require(architecture == "amd64", f"Wrong package architecture: {package.name}")
        require(name in allowed and name not in found, f"Unexpected or duplicate package: {name}")
        if name != 'psycachy-nvidia-support':
            current_version = subprocess.check_output(
                ['dpkg-deb', '--field', str(package), 'Version'], text=True).strip()
            versions.add(current_version)
            require(package_version is None or current_version == package_version,
                    f"Wrong package version: {name}")
        if meta and name.startswith(('linux-image-psycachy', 'linux-headers-psycachy')):
            kind = 'image' if name.startswith('linux-image-') else 'headers'
            depends = subprocess.check_output(
                ['dpkg-deb', '--field', str(package), 'Depends'], text=True).strip()
            require(depends == f'linux-{kind}-{kernel_release} (= {package_version})',
                    f"Wrong tracking package dependency: {name}")
        found.add(name)
    require(expected <= found and len(versions) == 1, "Missing or mismatched kernel or tracking packages")
    validation = (dist / "NVIDIA-VALIDATION.txt").read_text().splitlines()
    require(f"Kernel: {kernel_release}" in validation
            and "Compilation and module metadata passed. No GPU boot/load test performed." in validation
            and all(any(line.startswith(f"{module}: {kernel_release} ")
                        for line in validation) for module in MODULES),
            "NVIDIA validation does not describe a passing build for this kernel")
    return {path.name: {"size": path.stat().st_size, "digest": digest_file(path)}
            for path in sorted(entries)}


def verify_source(gh, run, version, commit, dist):
    require(re.fullmatch(r"[a-f0-9]{40}", commit), "Invalid built source commit")
    require(gh.api(f"commits/{commit}")["sha"] == commit, "Built commit is not in this repository")
    if run["path"].endswith("build-debs.yml"):
        require(commit == run["head_sha"], "Built commit differs from the selected build run")
    else:
        comparison = gh.api(f"compare/{run['head_sha']}...{commit}")
        files = comparison.get("files", [])
        require(comparison["status"] == "ahead" and comparison["ahead_by"] > 0
                and comparison["base_commit"]["sha"] == run["head_sha"] and 0 < len(files) < 300,
                "Updater source must be a complete, forward-only comparison from its workflow commit")
        require(all((item["filename"] in UPDATER_FILES
                     or item["filename"].startswith("src/patches/snapshots/"))
                    and item.get("status") in {"added", "modified"} for item in files),
                "Updater source changes builder code outside the permitted source pins")
        require(gh.content("src/default-version", commit).decode().strip() == version,
                "Updater default version differs from the built kernel")
    rows = [line.split() for line in gh.content("src/releases.tsv", commit).decode().splitlines()
            if line.strip() and not line.lstrip().startswith("#")]
    matches = [row for row in rows if row[0] == version]
    require(len(matches) == 1 and len(matches[0]) == 3
            and re.fullmatch(r"[a-f0-9]{64}", matches[0][2]), "Kernel has no unique pinned source archive")
    lock = json.loads(gh.content("src/patches/releases.json", commit)).get(version)
    if lock:
        require(re.fullmatch(r"[a-f0-9]{64}", lock["snapshot"])
                and lock["source_sha256"] == matches[0][2], "Patch lock differs from the source archive")
        source = json.loads(gh.content(f"src/patches/snapshots/{lock['snapshot']}/patchset.json", commit))
        canonical = json.dumps(source, sort_keys=True, separators=(",", ":")).encode()
        require(hashlib.sha256(canonical).hexdigest() == lock["snapshot"], "Invalid patch snapshot identity")
        require((dist / "PATCHES.json").is_file()
                and json.loads((dist / "PATCHES.json").read_text()) == source,
                "Artifact patch metadata differs from the built source lock")
    else:
        require(not (dist / "PATCHES.json").exists(), "Artifact has unpinned patch metadata")
    if (dist / 'PACKAGE-BUILD.json').is_file():
        identity = json.loads((dist / 'PACKAGE-BUILD.json').read_text())
        builder_revision = int(gh.content('src/package-revision', commit).decode().strip())
        source_revision = (lock or {}).get('debian_revision', 1)
        require(builder_revision > 0 and type(source_revision) is int and source_revision > 0,
                'Invalid source package revision')
        revision = builder_revision + source_revision - 1
        optimization = json.loads((dist / 'OPTIMIZATIONS.json').read_text())
        require(optimization.get('profile') in ('full', 'minimal'), 'Invalid build feature profile')
        suffix = '-minimal' if optimization['profile'] == 'minimal' else ''
        if revision > 1:
            suffix += f'-r{revision}'
        require(identity['package_version'] == f'{version}-{2 * revision}'
                and identity['kernel_release'] == f'{version}-psycachy-llvm{suffix}',
                'Package revision differs from its source lock')
        config = (dist / 'kernel.config').read_text()
        if optimization['profile'] == 'full':
            require(all(f'CONFIG_{symbol}=y' in config.splitlines() for symbol in
                        ('DEBUG_INFO_BTF', 'DEBUG_INFO_BTF_MODULES', 'SCHED_CLASS_EXT')),
                    'Full build omitted required BTF/sched_ext features')


def verify_tag(gh, tag, commit):
    ref = gh.api(f"git/ref/tags/{tag}", missing_ok=True)
    if ref is None:
        return False
    target = ref["object"]
    for _ in range(5):
        if target["type"] != "tag":
            break
        target = gh.api(f"git/tags/{target['sha']}")["object"]
    require(target["type"] == "commit" and target["sha"] == commit,
            "Release tag does not resolve to the validated source commit")
    return True


def find_release(gh, tag):
    # The tag endpoint only returns published releases. Authenticated release
    # listings also include drafts, which may not have a Git tag yet.
    release = gh.api(f"releases/tags/{tag}", missing_ok=True)
    if release is not None:
        return release
    page = 1
    while True:
        releases = gh.api(f"releases?per_page=100&page={page}")
        matches = [item for item in releases if item["tag_name"] == tag]
        require(len(matches) <= 1, "Multiple releases have the requested tag")
        if matches:
            return matches[0]
        if len(releases) < 100:
            return None
        page += 1


def verify_assets(gh, tag, release, expected, allow_missing=False):
    uploaded = {asset["name"]: asset for asset in release["assets"]}
    require(len(uploaded) == len(release["assets"]) and uploaded.keys() <= expected.keys(),
            "Release has duplicate or unexpected assets")
    if not allow_missing:
        require(uploaded.keys() == expected.keys(), "Release is missing expected assets")
    for name, asset in uploaded.items():
        require(asset["size"] == expected[name]["size"] and asset.get("state") == "uploaded",
                f"Incomplete or wrong-sized release asset: {name}")
        digest = asset.get("digest")
        if not digest:
            with tempfile.TemporaryDirectory(prefix="psycachy-asset-") as temporary:
                gh.command("release", "download", tag, "--repo", gh.repo, "--pattern", name,
                           "--dir", temporary)
                digest = digest_file(Path(temporary) / name)
        require(digest == expected[name]["digest"], f"Uploaded digest mismatch: {name}")
    return expected.keys() - uploaded.keys()


def release_notes(repo, run, version, commit):
    return f"""PsyCachy Linux {version} for generic x86-64 Debian/Ubuntu systems.

Built with Clang/LLVM and ThinLTO, BORE, BBR3, and ADIOS.
TOOLCHAIN.txt records compiler versions; kernel.config records the resolved configuration.
PATCHES.json, when present, records the selected optional features and patch provenance.
OPTIMIZATIONS.json records feature and profiling choices for new builds.
Full builds retain BTF and sched_ext; minimal builds explicitly omit them.
Tracking packages allow upgrades through a configured signed APT repository.

Download the packages, install-packages.sh, and SHA256SUMS into one directory.
Verify with: `sha256sum --check SHA256SUMS`
Install with: `bash install-packages.sh`
NVIDIA users with a distribution NVIDIA DKMS driver: `bash install-packages.sh --nvidia`
NVIDIA-VALIDATION.txt records the pinned open driver compilation test; GPU boot/load
compatibility and Secure Boot trust still require testing on the user's system.

Source commit: https://github.com/{repo}/commit/{commit}
Build: https://github.com/{repo}/actions/runs/{run['id']}/attempts/{run['run_attempt']}
Packages are promoted directly from this successful build without recompiling.
"""


def promote(repo, run_id, dry_run=False):
    require(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo), "Invalid owner/repository")
    require(isinstance(run_id, int) and run_id > 0, "Run ID must be a positive integer")
    gh = GitHub(repo)
    run = gh.api(f"actions/runs/{run_id}")
    verify_run(gh, run)
    attempt = run["run_attempt"]
    artifacts = gh.api(f"actions/runs/{run_id}/artifacts?per_page=100")
    require(artifacts["total_count"] <= 100, "Too many artifacts; refuse an incomplete artifact listing")
    pattern = re.compile(rf"psycachy-([0-9]+\.[0-9]+\.[0-9]+)-amd64-{run_id}-{attempt}")
    matches = [item for item in artifacts["artifacts"] if pattern.fullmatch(item["name"])
               and not item["expired"]]
    require(len(matches) == 1, "Expected exactly one nonexpired package artifact for the latest attempt")
    artifact = matches[0]
    version = pattern.fullmatch(artifact["name"])[1]
    tag = f"psycachy-{version}-run-{run_id}-{attempt}"
    with tempfile.TemporaryDirectory(prefix="psycachy-release-") as temporary:
        dist = Path(temporary) / "packages"
        gh.download(run_id, artifact["name"], dist)
        build = read_build(dist)
        commit = build.get("Commit", "")
        expected = verify_packages(dist, version, commit, run_id)
        verify_source(gh, run, version, commit, dist)
        tag_exists = verify_tag(gh, tag, commit)
        release = find_release(gh, tag)
        if release:
            require(release["tag_name"] == tag and not release["prerelease"], "Unexpected release identity")
            require(tag_exists or (release["draft"] and release["target_commitish"] == commit),
                    "Release does not target the built commit")
            missing = verify_assets(gh, tag, release, expected, allow_missing=release["draft"])
            if not release["draft"]:
                require(release["published_at"], "Release has no publication timestamp")
                return {"status": "already_published", "url": release["html_url"], "tag": tag}
        else:
            missing = expected.keys()
        if dry_run:
            return {"status": "would_resume" if release else "would_publish", "tag": tag,
                    "version": version, "commit": commit, "run_id": run_id, "attempt": attempt,
                    "assets": sorted(expected)}
        latest = gh.api(f"actions/runs/{run_id}")
        verify_run(gh, latest)
        require(latest["run_attempt"] == attempt, "Source run attempt changed during verification")
        if release is None:
            release = gh.api("releases", method="POST", fields={
                "tag_name": tag, "target_commitish": commit, "draft": True,
                "name": f"PsyCachy {version} (run {run_id}.{attempt})",
                "body": release_notes(repo, run, version, commit)})
        require(release["draft"], "Release must remain a draft while uploading")
        release_id = release["id"]
        for name in sorted(missing):
            gh.command("release", "upload", tag, dist / name, "--repo", repo)
        # Retain the release ID from discovery/creation: drafts cannot be read
        # through the published-release-by-tag endpoint.
        release = gh.api(f"releases/{release_id}")
        verify_assets(gh, tag, release, expected)
        verify_tag(gh, tag, commit)
        require(release["id"] == release_id and release["draft"] and not release["prerelease"]
                and release["tag_name"] == tag and release["target_commitish"] == commit,
                "Draft changed during upload; leaving it unpublished")
        latest = gh.api(f"actions/runs/{run_id}")
        verify_run(gh, latest)
        require(latest["run_attempt"] == attempt, "Source run attempt changed during upload")
        gh.api(f"releases/{release['id']}", method="PATCH", fields={"draft": False, "make_latest": "true"})
        release = gh.api(f"releases/{release_id}")
        require(release["id"] == release_id and release["tag_name"] == tag
                and not release["draft"] and release["published_at"], "Release was not published")
        require(verify_tag(gh, tag, commit), "Published release has no source tag")
        verify_assets(gh, tag, release, expected)
        return {"status": "published", "url": release["html_url"], "tag": tag,
                "commit": commit, "assets": len(expected)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="GitHub owner/repository")
    parser.add_argument("--run-id", required=True, type=int, help="Successful build or updater run")
    parser.add_argument("--dry-run", action="store_true", help="Verify packages and provenance without publishing")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(promote(args.repo, args.run_id, args.dry_run), indent=2))
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Release promotion failed: {error}\n")


if __name__ == "__main__":
    main()
