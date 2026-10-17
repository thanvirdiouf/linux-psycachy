# SPDX-License-Identifier: GPL-2.0-only
"""Verify release promotion provenance and package checks entirely offline."""

import hashlib
import io
import json
import shutil
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts import publish_release as publisher


REPOSITORY = "example/linux-psycachy"
RUN_ID = 123456
SOURCE = "a" * 40


class ReleaseLookupTests(unittest.TestCase):
    def test_published_release_is_found_by_tag_without_listing(self):
        gh = Mock()
        release = {"id": 12, "tag_name": "v1", "draft": False}
        gh.api.return_value = release
        self.assertEqual(publisher.find_release(gh, "v1"), release)
        gh.api.assert_called_once_with("releases/tags/v1", missing_ok=True)

    def test_draft_is_found_on_later_listing_page(self):
        gh = Mock()
        draft = {"id": 12, "tag_name": "v1", "draft": True}
        gh.api.side_effect = [None, [{"tag_name": f"other-{i}"} for i in range(100)], [draft]]
        self.assertEqual(publisher.find_release(gh, "v1"), draft)
        self.assertEqual([call.args[0] for call in gh.api.call_args_list],
                         ["releases/tags/v1", "releases?per_page=100&page=1",
                          "releases?per_page=100&page=2"])

    def test_missing_release_returns_none_after_last_listing_page(self):
        gh = Mock()
        gh.api.side_effect = [None, []]
        self.assertIsNone(publisher.find_release(gh, "v1"))

    def test_ambiguous_drafts_are_rejected(self):
        gh = Mock()
        gh.api.side_effect = [None, [{"tag_name": "v1", "id": i} for i in (12, 13)]]
        with self.assertRaisesRegex(ValueError, "Multiple releases"):
            publisher.find_release(gh, "v1")

    def test_lookup_error_is_not_treated_as_missing_release(self):
        gh = Mock()
        gh.api.side_effect = ValueError("HTTP 403")
        with self.assertRaisesRegex(ValueError, "HTTP 403"):
            publisher.find_release(gh, "v1")
        self.assertEqual(gh.api.call_count, 1)


class ReleaseFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="psycachy-release-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.packages = {}
        self.real_run = subprocess.run

    def bundle(self, version="7.3.4"):
        folder = self.root / version
        folder.mkdir()
        release = f"{version}-psycachy-llvm"
        package_version = f"{version}-1"
        for package in (f"linux-image-{release}", f"linux-headers-{release}",
                        "linux-libc-dev", "psycachy-nvidia-support"):
            name = f"{package}_{package_version}_amd64.deb"
            (folder / name).write_text(f"offline package fixture: {name}\n")
            self.packages[name] = {"Package": package, "Version": package_version,
                                   "Architecture": "amd64"}
        (folder / "BUILD.txt").write_text(
            f"Kernel: {version}\nCommit: {SOURCE}\nRun: {RUN_ID}\n")
        (folder / "kernel.config").write_text("CONFIG_CC_IS_CLANG=y\nCONFIG_LTO_CLANG_THIN=y\n")
        (folder / "TOOLCHAIN.txt").write_text("clang version 18\n")
        (folder / "BUILD-TUNING.txt").write_text("ThinLTO tuning: enabled\n")
        # A release verifier must inspect this file as data, never execute it.
        (folder / "install-packages.sh").write_text(
            "#!/bin/sh\necho downloaded-script-executed >&2\nexit 91\n")
        modules = ("nvidia", "nvidia-modeset", "nvidia-drm", "nvidia-uvm", "nvidia-peermem")
        (folder / "NVIDIA-VALIDATION.txt").write_text(
            f"NVIDIA open modules: 595.1.2\nKernel: {release}\nLLVM family: 18\n"
            + "NVIDIA source SHA-256: " + "b" * 64 + "\n"
            + "".join(f"{module}: {release} SMP mod_unload modversions\n" for module in modules)
            + "Compilation and module metadata passed. No GPU boot/load test performed.\n")
        self.checksums(folder)
        return folder

    def checksums(self, folder):
        # Use the same command as the build instead of mirroring its implementation.
        files = sorted(path.name for path in folder.glob("*.deb")) + ["install-packages.sh"]
        result = self.real_run(["sha256sum", *files], cwd=folder,
                               capture_output=True, text=True, check=True)
        (folder / "SHA256SUMS").write_text(result.stdout)

    def subprocess(self, command, *args, **kwargs):
        if command[0] == "dpkg-deb":
            filename = next(Path(value).name for value in command if value.endswith(".deb"))
            fields = self.packages[filename]
            requested = [value for value in command if value in fields]
            output = "\n".join(fields[field] for field in requested) + "\n"
            return subprocess.CompletedProcess(command, 0, output, "")
        if command[0] == "sha256sum":
            return self.real_run(command, *args, **kwargs)
        raise AssertionError(f"Unexpected executable during verification: {command}")

    def check_output(self, command, *args, **kwargs):
        if command[0] == "dpkg-deb":
            filename = next(Path(str(value)).name for value in command if str(value).endswith(".deb"))
            fields = self.packages[filename]
            requested = [value for value in command if value in fields]
            if not requested:
                raise AssertionError(f"dpkg-deb must query package metadata explicitly: {command}")
            output = "\n".join(fields[field] for field in requested) + "\n"
            return output if kwargs.get("text") or kwargs.get("encoding") else output.encode()
        raise AssertionError(f"Unexpected executable during verification: {command}")

    def verify(self, folder, version="7.3.4", commit=SOURCE, run_id=RUN_ID):
        with patch.object(publisher.subprocess, "check_output", side_effect=self.check_output), \
                patch.object(publisher.subprocess, "run", side_effect=self.subprocess):
            return publisher.verify_packages(folder, version, commit, run_id)



class ReleaseTests(ReleaseFixture):
    def test_metadata_drives_different_kernel_versions(self):
        for version in ("6.18.56", "7.3.4", "8.0.12"):
            with self.subTest(version=version):
                self.verify(self.bundle(version), version)

    def test_build_metadata_must_match_selected_run_and_source(self):
        for field, value in (("Kernel", "7.2.11"), ("Commit", "c" * 40), ("Run", "987654")):
            with self.subTest(field=field):
                folder = self.bundle()
                metadata = (folder / "BUILD.txt").read_text()
                (folder / "BUILD.txt").write_text("\n".join(
                    f"{field}: {value}" if line.startswith(field + ":") else line
                    for line in metadata.splitlines()) + "\n")
                with self.assertRaises(ValueError):
                    self.verify(folder)
                for path in folder.iterdir():
                    path.unlink()
                folder.rmdir()

    def test_duplicate_build_identity_fields_are_rejected(self):
        folder = self.bundle()
        build = folder / "BUILD.txt"
        build.write_text(build.read_text() + f"Commit: {SOURCE}\n")
        with self.assertRaises(ValueError):
            self.verify(folder)

    def test_checksum_corruption_and_omitted_package_are_rejected(self):
        folder = self.bundle()
        package = next(folder.glob("*.deb"))
        original = package.read_bytes()
        package.write_bytes(original + b"corrupted")
        with self.assertRaises((ValueError, subprocess.CalledProcessError)):
            self.verify(folder)
        package.write_bytes(original)
        checksums = folder / "SHA256SUMS"
        checksums.write_text("\n".join(line for line in checksums.read_text().splitlines()
                                       if package.name not in line) + "\n")
        with self.assertRaises(ValueError):
            self.verify(folder)

    def test_every_required_package_must_exist(self):
        for prefix in ("linux-image-", "linux-headers-", "linux-libc-dev_", "psycachy-nvidia-support_"):
            with self.subTest(prefix=prefix):
                folder = self.bundle()
                next(folder.glob(prefix + "*.deb")).unlink()
                self.checksums(folder)
                with self.assertRaises(ValueError):
                    self.verify(folder)
                for path in folder.iterdir():
                    path.unlink()
                folder.rmdir()

    def test_wrong_architecture_and_kernel_package_identity_are_rejected(self):
        folder = self.bundle()
        headers = next(folder.glob("linux-headers-*.deb"))
        self.packages[headers.name]["Architecture"] = "arm64"
        with self.assertRaises(ValueError):
            self.verify(folder)
        self.packages[headers.name]["Architecture"] = "amd64"
        self.packages[headers.name]["Package"] = "linux-headers-7.2.11-psycachy-llvm"
        with self.assertRaises(ValueError):
            self.verify(folder)

    def test_missing_nvidia_validation_is_rejected(self):
        folder = self.bundle()
        (folder / "NVIDIA-VALIDATION.txt").unlink()
        with self.assertRaises(ValueError):
            self.verify(folder)

    def test_nvidia_report_for_a_different_kernel_is_rejected(self):
        folder = self.bundle()
        report = folder / "NVIDIA-VALIDATION.txt"
        report.write_text(report.read_text().replace("7.3.4-psycachy-llvm", "7.2.11-psycachy-llvm"))
        with self.assertRaises(ValueError):
            self.verify(folder)

    def test_incomplete_nvidia_module_report_is_rejected(self):
        folder = self.bundle()
        report = folder / "NVIDIA-VALIDATION.txt"
        report.write_text("\n".join(line for line in report.read_text().splitlines()
                                     if not line.startswith("nvidia-peermem:")) + "\n")
        with self.assertRaises(ValueError):
            self.verify(folder)

    def test_downloaded_installer_is_only_read_as_data(self):
        folder = self.bundle()
        self.verify(folder)
        # The strict subprocess mocks above reject sh, bash, or any downloaded path.


class PromotionTests(ReleaseFixture):
    def setUp(self):
        super().setUp()
        self.run = {
            "id": RUN_ID, "run_number": 42, "run_attempt": 2,
            "status": "completed", "conclusion": "success",
            "head_sha": SOURCE, "head_branch": "master", "event": "workflow_dispatch",
            "path": ".github/workflows/build-debs.yml",
            "repository": {"full_name": REPOSITORY},
            "head_repository": {"full_name": REPOSITORY},
            "html_url": f"https://github.com/{REPOSITORY}/actions/runs/{RUN_ID}",
        }
        self.artifact = {
            "id": 456789, "name": f"psycachy-7.3.4-amd64-{RUN_ID}-2",
            "expired": False, "size_in_bytes": 1024,
            "workflow_run": {"id": RUN_ID, "head_sha": SOURCE},
        }
        self.artifacts = [self.artifact]
        self.bundle_path = self.bundle()
        self.gh = Mock()
        self.gh.repo = REPOSITORY
        self.gh.api.side_effect = self.api
        self.gh.content.side_effect = self.content
        self.gh.download.side_effect = self.download
        self.existing_tag = None
        self.existing_release = None
        self.comparison = {"status": "identical", "files": []}
        self.gh.command.side_effect = self.command

    @property
    def tag(self):
        return f"psycachy-7.3.4-run-{RUN_ID}-2"

    def release(self, draft=False):
        return {
            "id": 13579, "tag_name": self.tag, "prerelease": False,
            "draft": draft, "target_commitish": SOURCE,
            "published_at": None if draft else "2026-10-17T10:00:00Z",
            "html_url": f"https://github.com/{REPOSITORY}/releases/tag/{self.tag}",
            "assets": [{"name": path.name, "size": path.stat().st_size, "state": "uploaded",
                        "digest": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()}
                       for path in self.bundle_path.iterdir()],
        }

    def command(self, *args):
        self.assertEqual(args[:2], ("release", "upload"))
        path = Path(args[3])
        self.existing_release["assets"].append({
            "name": path.name, "size": path.stat().st_size, "state": "uploaded",
            "digest": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
        })

    def assert_no_mutation(self):
        self.gh.command.assert_not_called()
        self.assertTrue(all(call.kwargs.get("method", "GET") == "GET"
                            for call in self.gh.api.call_args_list))

    def api(self, path, method="GET", fields=None, missing_ok=False):
        if path == "releases" and method == "POST":
            self.assertEqual(fields["target_commitish"], SOURCE)
            self.assertTrue(fields["draft"])
            self.existing_release = self.release(draft=True)
            self.existing_release["assets"] = []
            return self.existing_release
        if path == "releases/13579" and method == "PATCH":
            self.assertFalse(fields["draft"])
            self.existing_release["draft"] = False
            self.existing_release["published_at"] = "2026-10-17T10:00:00Z"
            self.existing_tag = {"object": {"type": "commit", "sha": SOURCE}}
            return self.existing_release
        if path == "releases/13579" and method == "GET":
            return self.existing_release
        if path.startswith("releases?per_page=100&page="):
            return [self.existing_release] if self.existing_release else []
        if path.split("?")[0] == f"actions/runs/{RUN_ID}":
            return self.run
        if path.split("?")[0] == f"actions/runs/{RUN_ID}/artifacts":
            return {"total_count": len(self.artifacts), "artifacts": self.artifacts}
        if "/jobs" in path:
            return {"total_count": 2, "jobs": [
                {"name": "Validate builder", "status": "completed", "conclusion": "success"},
                {"name": "Build amd64 Debian packages", "status": "completed", "conclusion": "success"},
            ]}
        if path.startswith("git/ref/tags/"):
            return self.existing_tag
        if path.startswith("releases/tags/"):
            # GitHub's tag endpoint returns published releases, not drafts.
            if self.existing_release and not self.existing_release["draft"]:
                return self.existing_release
            if not missing_ok:
                raise ValueError("GitHub API failed: gh: Not Found (HTTP 404)")
            return None
        if path.startswith("commits/"):
            return {"sha": SOURCE}
        if path.startswith("compare/"):
            return self.comparison
        raise AssertionError(f"Unexpected GitHub request: {method} {path}")

    def content(self, path, commit):
        self.assertEqual(commit, SOURCE)
        if path == "src/releases.tsv":
            return ("7.3.4\tcachyos-7.3.4-1\t" + "d" * 64 + "\n").encode()
        if path == "src/patches/releases.json":
            return b"{}"
        if path == "src/default-version":
            return b"7.3.4\n"
        raise AssertionError(f"Unexpected source metadata lookup: {path}")

    def download(self, run_id, artifact_name, directory):
        self.assertEqual(run_id, RUN_ID)
        self.assertEqual(artifact_name, self.artifact["name"])
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        for path in self.bundle_path.iterdir():
            shutil.copyfile(path, directory / path.name)

    def promote(self, dry_run=True):
        with patch.object(publisher, "GitHub", return_value=self.gh), \
                patch.object(publisher.subprocess, "check_output", side_effect=self.check_output), \
                patch.object(publisher.subprocess, "run", side_effect=self.subprocess), \
                patch("sys.stdout", new_callable=io.StringIO):
            return publisher.promote(REPOSITORY, RUN_ID, dry_run=dry_run)

    def test_failed_and_pending_runs_do_not_download_or_publish(self):
        for status, conclusion in (("in_progress", None), ("completed", "failure"),
                                   ("completed", "cancelled")):
            with self.subTest(status=status, conclusion=conclusion):
                self.run.update(status=status, conclusion=conclusion)
                with self.assertRaises(ValueError):
                    self.promote(dry_run=False)
                self.gh.download.assert_not_called()
                self.gh.command.assert_not_called()

    def test_dry_run_verifies_without_publishing(self):
        self.promote()
        self.gh.download.assert_called_once()
        self.gh.command.assert_not_called()

    def test_expired_ambiguous_and_wrong_attempt_artifacts_are_rejected(self):
        for mode in ("expired", "ambiguous", "wrong-attempt"):
            with self.subTest(mode=mode):
                artifact = dict(self.artifact)
                if mode == "expired":
                    artifact["expired"] = True
                elif mode == "wrong-attempt":
                    artifact["name"] = artifact["name"].removesuffix("-2") + "-1"
                self.artifacts = [artifact, dict(artifact, id=999)] if mode == "ambiguous" else [artifact]
                with self.assertRaises(ValueError):
                    self.promote(dry_run=False)
                self.gh.download.assert_not_called()
                self.gh.command.assert_not_called()


    def test_promotion_derives_kernel_version_from_run_artifact(self):
        for version in ("6.18.56", "8.0.12"):
            with self.subTest(version=version):
                self.bundle_path = self.bundle(version)
                self.artifact["name"] = f"psycachy-{version}-amd64-{RUN_ID}-2"
                self.gh.content.side_effect = lambda path, commit: (
                    self.content(path, commit).replace(b"7.3.4", version.encode()))
                result = self.promote()
                self.assertEqual(result["version"], version)
                self.assertEqual(result["tag"], f"psycachy-{version}-run-{RUN_ID}-2")
                self.assert_no_mutation()

    def test_partial_draft_is_resumed_without_reuploading_verified_assets(self):
        self.existing_release = self.release(draft=True)
        self.existing_release["assets"] = self.existing_release["assets"][:1]
        uploaded_name = self.existing_release["assets"][0]["name"]
        result = self.promote(dry_run=False)
        self.assertEqual(result["status"], "published")
        self.assertEqual(self.gh.command.call_count, len(list(self.bundle_path.iterdir())) - 1)
        self.assertNotIn(uploaded_name, [Path(call.args[3]).name for call in self.gh.command.call_args_list])
        self.assertFalse(any(call.kwargs.get("method") == "POST" for call in self.gh.api.call_args_list))

    def test_fully_uploaded_draft_is_published_without_new_release_or_uploads(self):
        self.existing_release = self.release(draft=True)
        result = self.promote(dry_run=False)
        self.assertEqual(result["status"], "published")
        self.gh.command.assert_not_called()
        self.assertFalse(any(call.kwargs.get("method") == "POST"
                             for call in self.gh.api.call_args_list))

    def test_draft_dry_run_uses_existing_release_without_mutation(self):
        self.existing_release = self.release(draft=True)
        self.assertEqual(self.promote()["status"], "would_resume")
        self.assert_no_mutation()

    def test_run_rerun_during_verification_stops_before_mutation(self):
        reads = 0
        def api(path, *args, **kwargs):
            nonlocal reads
            result = self.api(path, *args, **kwargs)
            if path == f"actions/runs/{RUN_ID}":
                reads += 1
                return dict(result, run_attempt=3 if reads > 1 else 2)
            return result
        self.gh.api.side_effect = api
        with self.assertRaisesRegex(ValueError, "attempt changed"):
            self.promote(dry_run=False)
        self.assert_no_mutation()

    def test_run_rerun_after_upload_keeps_release_as_draft(self):
        reads = 0
        def api(path, *args, **kwargs):
            nonlocal reads
            result = self.api(path, *args, **kwargs)
            if path == f"actions/runs/{RUN_ID}":
                reads += 1
                return dict(result, run_attempt=3 if reads > 2 else 2)
            return result
        self.gh.api.side_effect = api
        with self.assertRaisesRegex(ValueError, "attempt changed"):
            self.promote(dry_run=False)
        self.assertTrue(self.existing_release["draft"])
        self.assertFalse(any(call.kwargs.get("method") == "PATCH" for call in self.gh.api.call_args_list))

    def test_successful_publication_uploads_all_verified_assets(self):
        result = self.promote(dry_run=False)
        self.assertEqual(result["status"], "published")
        self.assertEqual(result["tag"], self.tag)
        self.assertEqual(self.gh.command.call_count, len(list(self.bundle_path.iterdir())))
        self.assertFalse(self.existing_release["draft"])

    def test_build_source_mismatch_stops_before_publication(self):
        self.run["head_sha"] = "e" * 40
        with self.assertRaisesRegex(ValueError, "Built commit differs"):
            self.promote(dry_run=False)
        self.assert_no_mutation()

    def test_source_must_register_the_built_kernel(self):
        original = self.content
        self.gh.content.side_effect = lambda path, commit: (
            b"6.18.56 cachyos-6.18.56-1 " + b"f" * 64 + b"\n"
            if path == "src/releases.tsv" else original(path, commit))
        with self.assertRaisesRegex(ValueError, "pinned source archive"):
            self.promote(dry_run=False)
        self.assert_no_mutation()

    def test_artifact_patch_metadata_must_match_source_snapshot(self):
        snapshot = {"kernel": "7.3.4", "patches": []}
        snapshot_json = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
        identity = hashlib.sha256(snapshot_json.encode()).hexdigest()
        lock = {"7.3.4": {"snapshot": identity, "source_sha256": "d" * 64}}
        original = self.content
        def content(path, commit):
            if path == "src/patches/releases.json":
                return json.dumps(lock).encode()
            if path == f"src/patches/snapshots/{identity}/patchset.json":
                return snapshot_json.encode()
            return original(path, commit)
        self.gh.content.side_effect = content
        patch_metadata = self.bundle_path / "PATCHES.json"
        patch_metadata.write_text(json.dumps({"kernel": "7.2.11", "patches": []}))
        with self.assertRaisesRegex(ValueError, "patch metadata differs"):
            self.promote(dry_run=False)
        self.assert_no_mutation()
        patch_metadata.write_text(snapshot_json)
        self.assertEqual(self.promote()["status"], "would_publish")

    def test_updater_comparison_rejects_changes_to_builder_code(self):
        self.run.update(path=".github/workflows/update-kernel.yml", head_sha="e" * 40)
        self.comparison = {
            "status": "ahead", "ahead_by": 1, "base_commit": {"sha": self.run["head_sha"]},
            "files": [{"filename": "build.sh", "status": "modified"}],
        }
        with self.assertRaisesRegex(ValueError, "builder code"):
            self.promote(dry_run=False)
        self.assert_no_mutation()
        self.comparison["files"] = [{"filename": "src/default-version", "status": "modified"}]
        self.assertEqual(self.promote()["status"], "would_publish")

    def test_existing_tag_must_resolve_to_built_commit(self):
        self.existing_tag = {"object": {"type": "commit", "sha": "f" * 40}}
        with self.assertRaisesRegex(ValueError, "tag does not resolve"):
            self.promote(dry_run=False)
        self.assert_no_mutation()

    def test_existing_verified_release_is_idempotent(self):
        self.existing_tag = {"object": {"type": "commit", "sha": SOURCE}}
        self.existing_release = self.release()
        for _ in range(2):
            result = self.promote(dry_run=False)
            self.assertEqual(result["status"], "already_published")
            self.assertEqual(result["url"], self.existing_release["html_url"])
        self.assert_no_mutation()

    def test_existing_release_with_missing_or_wrong_assets_is_rejected(self):
        self.existing_tag = {"object": {"type": "commit", "sha": SOURCE}}
        self.existing_release = self.release()
        self.existing_release["assets"][0]["digest"] = "sha256:" + "f" * 64
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            self.promote(dry_run=False)
        self.assert_no_mutation()
        self.existing_release = self.release()
        self.existing_release["assets"].pop()
        with self.assertRaisesRegex(ValueError, "missing expected assets"):
            self.promote(dry_run=False)
        self.assert_no_mutation()


if __name__ == "__main__":
    unittest.main()
