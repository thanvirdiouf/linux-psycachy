# SPDX-License-Identifier: GPL-2.0-only
"""Exercise pinned patch snapshots with real offline tar and patch tools."""

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from scripts import patchsets, update_kernel


class PatchsetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="psycachy-patchsets-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "src/patches/common").mkdir(parents=True)
        (self.root / "src/patches/profile.json").write_text(json.dumps({"features": ["handheld", "aufs", "acpi-call"]}))
        self.tree = self.root / "source"
        self.tree.mkdir()
        for filename in ("bore", "acpi", "aufs", "handheld", "headers"):
            (self.tree / f"{filename}.txt").write_text("old\n")
        self.contents = {
            "7.2/sched/0001-bore-cachy.patch": self.diff("bore"),
            "7.2/misc/0001-acpi-call.patch": self.diff("acpi"),
            "7.2/misc/0001-aufs-7.2-merge-v20260914.patch": self.diff("aufs"),
            "7.2/misc/0001-handheld.patch": self.diff("handheld"),
        }
        self.entries = {path: {"sha": self.blob_sha(content)} for path, content in self.contents.items()}
        self.commit = "a" * 40
        self.archive = self.root / "src/cachyos-7.2.10-1.tar.gz"
        with tarfile.open(self.archive, "w:gz") as archive:
            archive.add(self.tree, arcname="source")
        (self.root / "src/patches/common/0002-debian-headers-config.patch").write_bytes(self.diff("headers"))
        self.candidate = {"version": "7.2.10", "release": "cachyos-7.2.10-1"}

    @staticmethod
    def diff(name):
        return f"--- a/{name}.txt\n+++ b/{name}.txt\n@@ -1 +1 @@\n-old\n+new\n".encode()

    @staticmethod
    def blob_sha(content):
        return hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()

    def refresh(self):
        with patch.object(patchsets, "upstream_index", return_value=(self.commit, self.entries)), \
                patch.object(patchsets, "download_patch", side_effect=lambda commit, path, entry: self.contents[path]):
            return patchsets.refresh(self.root, self.candidate, self.archive)

    def lock(self, identity, version="7.2.10"):
        digest = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        (self.root / "src/releases.tsv").write_text(f"{version}\tcachyos-{version}-1\t{digest}\n")
        (self.root / "src/patches/releases.json").write_text(json.dumps({version: {"source_sha256": digest, "snapshot": identity}}))

    def test_refresh_pins_selected_patches_configuration_and_shared_fix(self):
        identity, metadata = self.refresh()
        folder, verified = patchsets.verify_snapshot(self.root, identity)
        self.assertEqual(metadata, verified)
        self.assertEqual(len(metadata["patches"]), 5)
        self.assertEqual(metadata["features"], ["acpi-call", "aufs", "handheld"])
        self.assertEqual(metadata["config"]["AUFS_FS"], "m")
        self.assertEqual(metadata["config"]["ACPI_CALL"], "m")
        self.assertEqual(metadata["config"]["HID_ASUS_ALLY"], "m")
        self.assertTrue((folder / "0009-debian-headers-config.patch").is_file())
        self.assertEqual(list((self.root / "src").glob(".prepare-patches-*")), [])

    def test_same_snapshot_is_reused_across_kernel_versions(self):
        first, _ = self.refresh()
        self.candidate["version"] = "7.2.11"
        second, _ = self.refresh()
        self.assertEqual(first, second)
        self.assertEqual(len(list((self.root / "src/patches/snapshots").iterdir())), 1)

    def test_disabled_features_are_not_fetched_or_configured(self):
        (self.root / "src/patches/profile.json").write_text('{"features": []}')
        identity, metadata = self.refresh()
        self.assertEqual(len(metadata["patches"]), 2)
        self.assertEqual(metadata["config"], {})
        self.assertTrue(patchsets.verify_snapshot(self.root, identity))

    def test_alternative_and_unknown_patches_are_only_in_inventory(self):
        self.entries["7.2/sched/0001-prjc-cachy.patch"] = {"sha": "b" * 40}
        self.entries["7.2/misc/new-feature.patch"] = {"sha": "c" * 40}
        _, metadata = self.refresh()
        self.assertIn("7.2/misc/new-feature.patch", metadata["upstream_paths"])
        selected = {item["upstream_path"] for item in metadata["patches"]}
        self.assertNotIn("7.2/misc/new-feature.patch", selected)
        self.assertNotIn("7.2/sched/0001-prjc-cachy.patch", selected)

    def test_missing_selected_feature_stops_before_snapshot_creation(self):
        del self.entries["7.2/misc/0001-handheld.patch"]
        with self.assertRaisesRegex(ValueError, "missing"):
            self.refresh()
        self.assertFalse((self.root / "src/patches/snapshots").exists())

    def test_patch_conflict_creates_no_snapshot_and_cleans_source_tree(self):
        self.contents["7.2/misc/0001-acpi-call.patch"] = self.diff("acpi").replace(b"-old", b"-missing")
        with self.assertRaisesRegex(ValueError, "Patch preparation failed"):
            self.refresh()
        self.assertFalse((self.root / "src/patches/snapshots").exists())
        self.assertEqual(list((self.root / "src").glob(".prepare-patches-*")), [])

    def test_aufs_selection_uses_latest_dated_patch(self):
        self.entries["7.2/misc/0001-aufs-7.2-merge-v20260824.patch"] = {"sha": "d" * 40}
        paths = patchsets.select_paths("7.2", ["aufs"], self.entries)
        self.assertEqual(paths[-1][1], "7.2/misc/0001-aufs-7.2-merge-v20260914.patch")

    def test_corrupted_patch_and_metadata_are_rejected(self):
        identity, _ = self.refresh()
        folder = self.root / "src/patches/snapshots" / identity
        bore = folder / "0001-bore-cachy.patch"
        original = bore.read_bytes()
        bore.write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            patchsets.verify_snapshot(self.root, identity)
        bore.write_bytes(original)
        metadata = json.loads((folder / "patchset.json").read_text())
        metadata["upstream_commit"] = "b" * 40
        (folder / "patchset.json").write_text(json.dumps(metadata))
        with self.assertRaisesRegex(ValueError, "metadata was modified"):
            patchsets.verify_snapshot(self.root, identity)

    def test_source_hash_mismatch_is_rejected(self):
        identity, _ = self.refresh()
        self.lock(identity)
        self.assertEqual(patchsets.load(self.root, "7.2.10")[1]["upstream_commit"], self.commit)
        (self.root / "src/releases.tsv").write_text("7.2.10 cachyos-7.2.10-1 " + "f" * 64)
        with self.assertRaisesRegex(ValueError, "pinned source"):
            patchsets.load(self.root, "7.2.10")

    def test_invalid_snapshot_path_is_rejected(self):
        for identity in ("../outside", "a" * 63, "A" * 64):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                patchsets.verify_snapshot(self.root, identity)

    def test_download_verifies_commit_pinned_blob_without_api_token(self):
        content = self.diff("bore")
        with patch.object(patchsets.urllib.request, "urlopen", return_value=io.BytesIO(content)) as network:
            actual = patchsets.download_patch(self.commit, "7.2/sched/0001-bore-cachy.patch", {"sha": self.blob_sha(content)})
        self.assertEqual(actual, content)
        self.assertIn(self.commit, network.call_args.args[0])
        with patch.object(patchsets.urllib.request, "urlopen", return_value=io.BytesIO(content)):
            with self.assertRaisesRegex(ValueError, "pinned Git blob"):
                patchsets.download_patch(self.commit, "7.2/sched/0001-bore-cachy.patch", {"sha": "f" * 40})

    def test_index_uses_same_commit_for_tree_and_rejects_truncation(self):
        with patch.object(patchsets, "api", side_effect=[{"sha": self.commit}, {"tree": [], "truncated": False}]) as api:
            self.assertEqual(patchsets.upstream_index("7.2"), (self.commit, {}))
        self.assertIn(self.commit, api.call_args.args[0])
        with patch.object(patchsets, "api", side_effect=[{"sha": self.commit}, {"tree": [], "truncated": True}]):
            with self.assertRaisesRegex(ValueError, "truncated"):
                patchsets.upstream_index("7.2")

    def test_known_bore_rebase_only_changes_context(self):
        (self.tree / "include/linux").mkdir(parents=True)
        (self.tree / "include/linux/sched.h").write_text("struct task_ipi_mask { };\n#endif\n\nstruct task_struct {")
        content = b"@@ -824,6 +824,31 @@ struct kmap_ctrl {\n #endif\n };\n \n+added_code\n struct task_struct {\n"
        adapted = patchsets.adapt_bore(content, self.tree)
        self.assertIn(b"struct task_ipi_mask", adapted)
        self.assertEqual([line for line in content.splitlines() if line.startswith(b"+")],
                         [line for line in adapted.splitlines() if line.startswith(b"+")])
        with self.assertRaisesRegex(ValueError, "unfamiliar rebase"):
            patchsets.adapt_bore(b"different upstream hunk", self.tree)

    def test_optional_configuration_is_checked_after_olddefconfig(self):
        identity, metadata = self.refresh()
        self.lock(identity)
        config = self.tree / ".config"
        config.write_text("".join(f"CONFIG_{symbol}={value}\n" for symbol, value in metadata["config"].items()))
        helper = Path(patchsets.__file__)
        command = ["python3", str(helper), "verify-config", str(self.root), "7.2.10", str(self.tree)]
        subprocess.run(command, check=True)
        config.write_text(config.read_text().replace("CONFIG_AUFS_FS=m\n", ""))
        result = subprocess.run(command, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CONFIG_AUFS_FS=m", result.stderr)

    def test_invalid_or_duplicate_feature_policy_is_rejected(self):
        for features in (["unknown"], ["aufs", "aufs"], "aufs"):
            (self.root / "src/patches/profile.json").write_text(json.dumps({"features": features}))
            with self.subTest(features=features), self.assertRaises(ValueError):
                patchsets.profile(self.root)

    def test_discovery_does_not_refresh_patches_without_new_kernel(self):
        (self.root / "src/releases.tsv").write_text("7.2.9 cachyos-7.2.9-2 " + "a" * 64)
        (self.root / "src/default-version").write_text("7.2.9\n")
        with patch.object(update_kernel, "ROOT", self.root), \
                patch.object(update_kernel, "fetch_releases", return_value=[]), \
                patch.object(patchsets, "refresh") as refresh, \
                patch.object(update_kernel, "archive_hash") as download, \
                patch("sys.argv", ["update_kernel.py", "--apply"]), \
                patch("sys.stdout", new_callable=io.StringIO):
            update_kernel.main()
        refresh.assert_not_called()
        download.assert_not_called()

    def test_new_kernel_update_downloads_pins_patches_and_records_review_inventory(self):
        (self.root / "src/releases.tsv").write_text("7.2.9 cachyos-7.2.9-2 " + "a" * 64 + "\n")
        (self.root / "src/default-version").write_text("7.2.9\n")
        (self.root / "src/patches/SOURCES.md").write_text("Previously validated source\n")
        data = self.archive.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        tag = "cachyos-7.2.10-1"
        release = {"tag_name": tag, "assets": [
            {"name": tag + ".tar.gz", "state": "uploaded", "size": len(data), "digest": "sha256:" + digest}]}
        self.entries["7.2/misc/new-optional.patch"] = {"sha": "c" * 40}
        def response(url, timeout):
            if "/linux/releases/download/" in url:
                return io.BytesIO(data)
            return io.BytesIO(self.contents[url.split(self.commit + "/", 1)[1]])
        with patch.object(update_kernel, "ROOT", self.root), \
                patch.object(update_kernel, "fetch_releases", return_value=[release]), \
                patch.object(patchsets, "upstream_index", return_value=(self.commit, self.entries)), \
                patch.object(patchsets.urllib.request, "urlopen", side_effect=response), \
                patch("sys.argv", ["update_kernel.py", "--apply", "--expected-release", tag]), \
                patch.dict(os.environ, RUNNER_TEMP=str(self.root), GITHUB_OUTPUT=str(self.root / "outputs")), \
                patch("sys.stdout", new_callable=io.StringIO):
            update_kernel.main()
        self.assertEqual((self.root / "src/default-version").read_text(), "7.2.10\n")
        self.assertEqual(update_kernel.read_manifest(self.root)["7.2.9"], ("cachyos-7.2.9-2", "a" * 64))
        folder, metadata = patchsets.load(self.root, "7.2.10")
        self.assertEqual(metadata["upstream_commit"], self.commit)
        self.assertEqual(len(metadata["patches"]), 5)
        self.assertEqual(hashlib.sha256(self.archive.read_bytes()).hexdigest(), digest)
        notes = (self.root / "kernel-update-pr.md").read_text()
        self.assertIn("new-optional.patch", notes)
        self.assertIn("acpi-call, aufs, handheld", notes)
        self.assertIn("Previously validated source", (self.root / "src/patches/SOURCES.md").read_text())

    def test_failed_archive_download_preserves_cached_archive(self):
        before = self.archive.read_bytes()
        candidate = {"url": "https://github.com/example", "size": 7, "digest": "sha256:" + "f" * 64}
        with patch.object(update_kernel.urllib.request, "urlopen", return_value=io.BytesIO(b"corrupt")):
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                update_kernel.archive_hash(candidate, self.archive)
        self.assertEqual(self.archive.read_bytes(), before)
        self.assertFalse(Path(str(self.archive) + ".part").exists())


if __name__ == "__main__":
    unittest.main()
