# SPDX-License-Identifier: GPL-2.0-only
"""Check release selection, archive verification, and proposed edits offline."""

import hashlib
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import update_kernel as updater


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="psycachy-updater-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "src/patches/7.2").mkdir(parents=True)
        (self.root / ".github/workflows").mkdir(parents=True)
        (self.root / "src/patches/7.2/0001-bore-cachy.patch").write_text("patch\n")
        (self.root / "src/patches/common").mkdir()
        (self.root / "src/patches/common/0002-debian-headers-config.patch").write_text("headers\n")
        (self.root / "src/patches/profile.json").write_text('{"features": []}')
        (self.root / "src/patches/SOURCES.md").write_text("Validated source: 7.2.9\n")
        self.manifest = self.root / "src/releases.tsv"
        self.manifest.write_text("# Pinned sources\n7.2.9\tcachyos-7.2.9-2\t" + "a" * 64 + "\n")
        self.default = self.root / "src/default-version"
        self.default.write_text("7.2.9\n")
        self.workflow = self.root / ".github/workflows/build-debs.yml"
        self.workflow.write_text((updater.ROOT / ".github/workflows/build-debs.yml").read_text())
        self.entries = updater.read_manifest(self.root)
        self.archive = b"source archive fixture"
        self.digest = hashlib.sha256(self.archive).hexdigest()
        self.recipe_patch = patch.object(updater.patchsets, "recipe_snapshot", side_effect=self.recipe)
        self.recipe_patch.start()
        self.addCleanup(self.recipe_patch.stop)
        self.index_patch = patch.object(updater.patchsets, "upstream_index", return_value=("a" * 40, {
            "7.2/sched/0001-bore-cachy.patch": {"sha": "b" * 40}}))
        self.index_patch.start()
        self.addCleanup(self.index_patch.stop)

    def recipe(self, release):
        match = updater.TAG.fullmatch(release)
        series, minor = match[1].rsplit(".", 1)
        recipe = (f"_major={series}\n_minor={minor}\n_tagrel={match[2]}\n"
                  "_srcname=cachyos-${_major}.${_minor}-${_tagrel}\n").encode()
        config = b"CONFIG_CACHY=y\n"
        return {"repository": updater.patchsets.RECIPE_REPOSITORY, "commit": "c" * 40,
                "path": "linux-cachyos/PKGBUILD", "config_path": "linux-cachyos/config",
                "source_release": release, "sha256": hashlib.sha256(recipe).hexdigest(),
                "config_sha256": hashlib.sha256(config).hexdigest()}, {
                    "upstream-PKGBUILD": recipe, "upstream-config": config}

    def mark_current(self):
        candidate, _ = updater.discover(self.root, self.entries, [], "7.2.9")
        entry = {"input_identity": candidate["input_identity"], "debian_revision": 1,
                 "source_sha256": "a" * 64, "source_release": "cachyos-7.2.9-2"}
        (self.root / "src/patches/releases.json").write_text(json.dumps({"7.2.9": entry}))

    def release(self, version="7.2.10", revision=1, **extra):
        tag = f"cachyos-{version}-{revision}"
        result = {
            "tag_name": tag, "draft": False, "prerelease": False,
            "assets": [{"name": f"{tag}.tar.gz", "state": "uploaded",
                        "size": len(self.archive), "digest": f"sha256:{self.digest}"}],
        }
        result.update(extra)
        return result

    def candidate(self):
        return updater.select_candidate(self.entries, [self.release()])

    def snapshot(self):
        return {path: path.read_bytes() for path in
                (self.manifest, self.default, self.workflow, self.root / "src/patches/SOURCES.md")}

    def test_selects_numeric_version_and_highest_source_revision(self):
        releases = [self.release("7.2.10", 2), self.release("7.2.11", 1),
                    self.release("7.2.11", 3), self.release("7.2.10", 9)]
        candidate = updater.select_candidate(self.entries, releases)
        self.assertEqual(candidate["release"], "cachyos-7.2.11-3")
        self.assertEqual(candidate["branch"], "codex/linux-7.2.11")

    def test_skips_rc_new_series_and_older_rebuilds(self):
        releases = [self.release("7.3.1"), self.release("7.2.9", 2), self.release("7.2.9", 1),
                    self.release("7.2.99", draft=True),
                    self.release("7.2.12", prerelease=True),
                    self.release(tag_name="cachyos-7.3-rc6-1"),
                    self.release("7.2.8")]
        self.assertIsNone(updater.select_candidate(self.entries, releases))

    def test_same_linux_version_source_revision_is_selected(self):
        selected = updater.select_candidate(self.entries, [self.release("7.2.9", 3), self.release("7.2.9", 99)])
        self.assertEqual(selected["release"], "cachyos-7.2.9-99")

    def test_follows_the_selected_default_series(self):
        self.entries["6.18.55"] = ("cachyos-6.18.55-1", "b" * 64)
        selected = updater.select_candidate(self.entries, [self.release("6.18.56"), self.release()], current="6.18.55")
        self.assertEqual(selected["version"], "6.18.56")

    def test_new_revision_of_latest_registered_version_is_selected(self):
        self.entries["7.2.11"] = ("cachyos-7.2.11-1", "b" * 64)
        selected = updater.select_candidate(self.entries,
                                            [self.release("7.2.10"), self.release("7.2.11", 2)],
                                            current="7.2.9")
        self.assertEqual(selected["release"], "cachyos-7.2.11-2")

    def test_requires_uploaded_matching_source_asset(self):
        releases = [self.release("7.2.11", assets=[]), self.release()]
        self.assertEqual(updater.select_candidate(self.entries, releases)["version"], "7.2.10")
        release = self.release()
        release["assets"][0]["state"] = "new"
        self.assertIsNone(updater.select_candidate(self.entries, [release]))

    def test_rejects_malformed_published_digest(self):
        release = self.release()
        release["assets"][0]["digest"] = "sha256:invalid"
        with self.assertRaisesRegex(ValueError, "published SHA-256"):
            updater.select_candidate(self.entries, [release])

    def test_download_hash_matches_published_digest(self):
        candidate = self.candidate()
        with patch.object(updater.urllib.request, "urlopen", return_value=io.BytesIO(self.archive)) as network:
            self.assertEqual(updater.archive_hash(candidate), self.digest)
        # Assets use the fixed GitHub URL and do not receive the API token.
        network.assert_called_once_with(candidate["url"], timeout=60)

    def test_rejects_archive_digest_or_size_mismatch(self):
        candidate = self.candidate()
        for field, value, message in (("digest", "sha256:" + "b" * 64, "SHA-256"),
                                      ("size", len(self.archive) + 1, "size")):
            with self.subTest(field=field):
                modified = dict(candidate, **{field: value})
                with patch.object(updater.urllib.request, "urlopen", return_value=io.BytesIO(self.archive)):
                    with self.assertRaisesRegex(ValueError, message):
                        updater.archive_hash(modified)

    def test_hashes_archive_when_publisher_has_no_digest(self):
        candidate = dict(self.candidate(), digest=None)
        with patch.object(updater.urllib.request, "urlopen", return_value=io.BytesIO(self.archive)):
            self.assertEqual(updater.archive_hash(candidate), self.digest)

    def test_applies_pin_default_and_provenance_without_changing_workflows(self):
        original_manifest = self.manifest.read_text()
        original_workflow = self.workflow.read_text()
        updater.apply_candidate(self.root, self.candidate(), self.digest)
        self.assertTrue(self.manifest.read_text().startswith(original_manifest))
        self.assertEqual(updater.read_manifest(self.root)["7.2.10"], ("cachyos-7.2.10-1", self.digest))
        self.assertEqual(self.default.read_text(), "7.2.10\n")
        self.assertEqual(self.workflow.read_text(), original_workflow)
        self.assertIn("Validated source: 7.2.9", (self.root / "src/patches/SOURCES.md").read_text())
        self.assertIn("Candidate source: Linux 7.2.10", (self.root / "src/patches/SOURCES.md").read_text())

    def test_invalid_default_or_missing_patch_changes_no_files(self):
        self.default.write_text("7.2.99\n")
        original = self.snapshot()
        with self.assertRaisesRegex(ValueError, "default kernel version"):
            updater.apply_candidate(self.root, self.candidate(), self.digest)
        self.assertEqual(self.snapshot(), original)
        (self.root / "src/patches/7.2/0001-bore-cachy.patch").unlink()
        with self.assertRaisesRegex(ValueError, "Missing series patch"):
            updater.apply_candidate(self.root, self.candidate(), self.digest)
        self.assertEqual(self.snapshot(), original)

    def test_duplicate_or_invalid_manifest_is_rejected(self):
        row = self.manifest.read_text().splitlines()[-1]
        for text in (row + "\n" + row + "\n", "7.2.9 cachyos-7.2.10-1 " + "a" * 64):
            with self.subTest(text=text):
                self.manifest.write_text(text)
                with self.assertRaises(ValueError):
                    updater.read_manifest(self.root)

    def test_release_api_pagination(self):
        first = [self.release("7.2.8")] * 100
        second = [self.release()]
        responses = [io.BytesIO(json.dumps(batch).encode()) for batch in (first, second)]
        with patch.object(updater.urllib.request, "urlopen", side_effect=responses) as network:
            candidate = updater.select_candidate(self.entries, updater.fetch_releases())
        self.assertEqual(candidate["version"], "7.2.10")
        self.assertEqual(network.call_count, 2)
        self.assertIn("page=2", network.call_args.args[0].full_url)

    def test_discovery_does_not_download_or_change_tracked_files(self):
        original = self.snapshot()
        output = self.root / "outputs"
        with patch.object(updater, "ROOT", self.root), \
                patch.object(updater, "fetch_releases", return_value=[self.release()]), \
                patch.object(updater, "archive_hash") as download, \
                patch("sys.argv", ["update_kernel.py"]), \
                patch("sys.stdout", new_callable=io.StringIO), \
                patch.dict(os.environ, GITHUB_OUTPUT=str(output)):
            updater.main()
        download.assert_not_called()
        self.assertEqual(self.snapshot(), original)
        self.assertIn("update=true\n", output.read_text())

    def test_candidate_race_aborts_before_download(self):
        original = self.snapshot()
        with patch.object(updater, "ROOT", self.root), \
                patch.object(updater, "fetch_releases", return_value=[self.release("7.2.11")]), \
                patch.object(updater, "archive_hash") as download, \
                patch("sys.argv", ["update_kernel.py", "--apply", "--expected-release", "cachyos-7.2.10-1"]):
            with self.assertRaisesRegex(ValueError, "candidate changed"):
                updater.main()
        download.assert_not_called()
        self.assertEqual(self.snapshot(), original)

    def test_no_update_reports_false(self):
        self.mark_current()
        output = self.root / "outputs"
        with patch.object(updater, "ROOT", self.root), \
                patch.object(updater, "fetch_releases", return_value=[]), \
                patch("sys.argv", ["update_kernel.py"]), \
                patch("sys.stdout", new_callable=io.StringIO), \
                patch.dict(os.environ, GITHUB_OUTPUT=str(output)):
            updater.main()
        self.assertEqual(output.read_text(), "update=false\n")

    def test_missing_recipe_provenance_proposes_existing_source_migration(self):
        candidate, inputs = updater.discover(self.root, self.entries, [], "7.2.9")
        self.assertEqual(candidate["release"], "cachyos-7.2.9-2")
        self.assertEqual(candidate["debian_revision"], 2)
        self.assertEqual(candidate["input_identity"], inputs["identity"])

    def test_patch_only_changes_propose_new_revision_and_branch(self):
        self.mark_current()
        with patch.object(updater.patchsets, "upstream_index", return_value=("d" * 40, {
                "7.2/sched/0001-bore-cachy.patch": {"sha": "e" * 40}})):
            candidate, _ = updater.discover(self.root, self.entries, [], "7.2.9")
        self.assertEqual(candidate["debian_revision"], 2)
        self.assertTrue(candidate["branch"].startswith("codex/linux-7.2.9-r2-"))

    def test_irrelevant_upstream_patch_commit_does_not_propose_update(self):
        self.mark_current()
        with patch.object(updater.patchsets, "upstream_index", return_value=("d" * 40, {
                "7.2/sched/0001-bore-cachy.patch": {"sha": "b" * 40},
                "7.2/misc/unselected.patch": {"sha": "f" * 40}})):
            self.assertEqual(updater.discover(self.root, self.entries, [], "7.2.9"), (None, None))

    def test_recipe_and_config_changes_are_detected(self):
        self.mark_current()
        original = self.recipe("cachyos-7.2.9-2")
        for key in ("sha256", "config_sha256"):
            with self.subTest(key=key), patch.object(updater.patchsets, "recipe_snapshot", return_value=(
                    {**original[0], key: "f" * 64}, original[1])):
                self.assertIsNotNone(updater.discover(self.root, self.entries, [], "7.2.9")[0])

    def test_changed_published_digest_of_existing_tag_requires_review(self):
        with self.assertRaisesRegex(ValueError, "digest changed"):
            updater.discover(self.root, self.entries, [self.release("7.2.9", 2)], "7.2.9")

    def test_input_race_aborts_before_archive_download(self):
        with patch.object(updater, "ROOT", self.root), \
                patch.object(updater, "fetch_releases", return_value=[self.release()]), \
                patch.object(updater, "archive_hash") as download, \
                patch("sys.argv", ["update_kernel.py", "--apply", "--expected-input-id", "f" * 64]):
            with self.assertRaisesRegex(ValueError, "candidate inputs changed"):
                updater.main()
        download.assert_not_called()

    def test_cli_reports_missing_keys_and_subprocess_failures(self):
        script = self.root / "scripts/update_kernel.py"
        script.parent.mkdir()
        script.write_text(Path(updater.__file__).read_text())
        errors = (KeyError("sha"), subprocess.CalledProcessError(2, ["tar", "-xzf", "archive.tar.gz"]))
        for error in errors:
            with self.subTest(error=type(error).__name__), \
                    patch.dict("sys.modules", patchsets=updater.patchsets), \
                    patch.object(updater.urllib.request, "urlopen", side_effect=error), \
                    patch("sys.argv", [str(script)]), \
                    self.assertRaises(SystemExit) as failure:
                runpy.run_path(str(script), run_name="__main__")
            expected = f"Missing expected key: {error}" if isinstance(error, KeyError) else str(error)
            self.assertEqual(failure.exception.code, expected)


if __name__ == "__main__":
    unittest.main()
