"""Exercise fork build decisions, Git conflict handling, and release publication gates."""

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("fork_build", ROOT / ".github/scripts/fork_build.py")
BUILD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILD)
COMMIT = "a" * 40


def run_record(commit=COMMIT, status="completed", conclusion="failure", attempt=1):
    """Make a desktop dispatch response, including its immutable input revision."""
    return {"display_title": BUILD.CONFIG["run_prefix"] + commit, "status": status,
            "conclusion": conclusion, "run_attempt": attempt}


class DecisionTests(unittest.TestCase):
    def test_sync_merges_and_dispatches_on_the_default_branch(self):
        metadata = {"fork": True, "default_branch": "dev", "parent": {
            "clone_url": "https://example.invalid/upstream.git", "default_branch": "dev"}}
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "owner/fork"}), \
                patch.object(BUILD, "merge_upstream", return_value=(COMMIT, "b" * 40)) as merge, \
                patch.object(BUILD, "api", side_effect=[metadata, [], None]) as api:
            BUILD.sync()
        self.assertEqual(merge.call_args.args[1], "dev")
        self.assertIn("ref=dev", api.call_args.args)
        self.assertIn("inputs[branch]=dev", api.call_args.args)
        self.assertIn("inputs[source_commit]=" + COMMIT, api.call_args.args)

    def test_new_revision_builds(self):
        self.assertEqual(BUILD.build_decision([run_record("b" * 40)], COMMIT), "build")

    def test_running_older_revision_coalesces_new_changes(self):
        for status in ("queued", "in_progress", "waiting", "pending", "requested"):
            with self.subTest(status=status):
                self.assertEqual(BUILD.build_decision([run_record("b" * 40, status)], COMMIT), "busy")

    def test_success_is_not_built_again(self):
        self.assertEqual(BUILD.build_decision([run_record(conclusion="success")], COMMIT), "built")

    def test_failure_has_a_bounded_retry(self):
        self.assertEqual(BUILD.build_decision([run_record()], COMMIT), "build")
        self.assertEqual(BUILD.build_decision([run_record(), run_record()], COMMIT), "failed-limit")
        self.assertEqual(BUILD.build_decision([run_record(attempt=2)], COMMIT), "failed-limit")

    def test_old_all_platform_failure_does_not_consume_desktop_retry(self):
        old = run_record(attempt=2)
        old["display_title"] = "Package " + COMMIT
        self.assertEqual(BUILD.build_decision([old], COMMIT), "build")

    def test_revision_expressions_are_rejected(self):
        for value in ("dev", "HEAD~1", "--help", "a" * 39, "A" * 40):
            with self.subTest(value=value), self.assertRaises(ValueError):
                BUILD.check_sha(value)


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.upstream = base / "upstream"
        self.origin = base / "origin.git"
        self.work = base / "work"
        self.git(base, "init", "--initial-branch=dev", str(self.upstream))
        self.identity(self.upstream)
        self.commit(self.upstream, "app.txt", "original\n")
        self.git(base, "clone", "--bare", str(self.upstream), str(self.origin))
        self.git(base, "clone", str(self.origin), str(self.work))
        self.identity(self.work)
        self.commit(self.work, ".github/workflows/fork-sync.yml", "fork settings\n")
        self.git(self.work, "push", "origin", "dev")

    def git(self, cwd, *args):
        return BUILD.command("git", *args, cwd=cwd)

    def identity(self, root):
        self.git(root, "config", "user.name", "Test")
        self.git(root, "config", "user.email", "test@example.invalid")

    def commit(self, root, path, contents):
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents)
        self.git(root, "add", "--", path)
        self.git(root, "commit", "-m", "Update fixture")
        return self.git(root, "rev-parse", "HEAD")

    def test_merge_preserves_fork_configuration_and_pins_new_source(self):
        upstream = self.commit(self.upstream, "app.txt", "updated\n")
        head, source = BUILD.merge_upstream(self.work, "dev", str(self.upstream), "dev")
        self.assertEqual(source, upstream)
        self.assertEqual((self.work / "app.txt").read_text(), "updated\n")
        self.assertEqual((self.work / ".github/workflows/fork-sync.yml").read_text(), "fork settings\n")
        self.assertEqual(self.git(self.origin, "rev-parse", "dev"), head)
        self.assertEqual(BUILD.merge_upstream(self.work, "dev", str(self.upstream), "dev"), (head, source))

    def test_conflict_leaves_remote_unchanged_and_cleans_merge(self):
        self.commit(self.work, "app.txt", "fork edit\n")
        self.git(self.work, "push", "origin", "dev")
        before = self.git(self.origin, "rev-parse", "dev")
        self.commit(self.upstream, "app.txt", "upstream edit\n")
        with self.assertRaises(RuntimeError):
            BUILD.merge_upstream(self.work, "dev", str(self.upstream), "dev")
        self.assertEqual(self.git(self.origin, "rev-parse", "dev"), before)
        self.assertEqual(self.git(self.work, "status", "--porcelain"), "")

    def test_pin_rejects_commit_outside_default_branch(self):
        self.git(self.work, "checkout", "-b", "unrelated")
        other = self.commit(self.work, "app.txt", "other branch\n")
        with patch.dict(os.environ, {"TARGET_BRANCH": "dev", "DEFAULT_BRANCH": "dev", "BUMP": "nightly",
                                     "SOURCE_COMMIT": other, "IS_FORK": "true"}):
            previous = Path.cwd()
            try:
                os.chdir(self.work)
                with self.assertRaises(RuntimeError):
                    BUILD.pin()
            finally:
                os.chdir(previous)

    def test_pin_keeps_selected_ancestor_when_branch_advances(self):
        selected = self.git(self.work, "rev-parse", "HEAD")
        self.commit(self.work, "app.txt", "newer commit\n")
        self.git(self.work, "push", "origin", "dev")
        self.git(self.work, "checkout", "--detach", selected)
        with patch.dict(os.environ, {"TARGET_BRANCH": "dev", "DEFAULT_BRANCH": "dev", "BUMP": "nightly",
                                     "SOURCE_COMMIT": selected, "IS_FORK": "true"}):
            previous = Path.cwd()
            try:
                os.chdir(self.work)
                BUILD.pin()
            finally:
                os.chdir(previous)
        self.assertEqual(self.git(self.work, "rev-parse", "HEAD"), selected)
        self.assertEqual(self.git(self.work, "symbolic-ref", "--short", "HEAD"), "dev")



class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.latest = patch.object(BUILD, "latest_release", return_value=None)
        self.latest.start()
        self.addCleanup(self.latest.stop)
        self.manifest = json.loads((ROOT / "packaging/release-assets.json").read_text())
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.version = "1.2.3"
        self.names = BUILD.expected_assets(self.manifest, self.version, True)
        for name in self.names:
            (self.directory / name).write_bytes(name.encode())
        for platform in BUILD.CONFIG["platforms"]:
            for arch in self.manifest["assets"][platform]:
                name = self.manifest["symbols"]["archive"].format(
                    version=self.version, platform=platform, arch=arch)
                with zipfile.ZipFile(self.directory / name, "w") as archive:
                    archive.writestr(self.manifest["symbols"]["manifest"], json.dumps({
                        "commit": COMMIT, "version": self.version, "platform": f"{platform}-{arch}"}))

    def test_all_selected_architectures_are_required(self):
        for platform in BUILD.CONFIG["platforms"]:
            self.assertTrue(set(self.manifest["assets"][platform].values()).issubset(self.names))
        digests = BUILD.checksums(self.directory, self.manifest, self.version, True)
        self.assertEqual(set(digests), self.names | {self.manifest["checksums"]})
        release = {"assets": [{"name": name, "digest": "sha256:" + digest}
                              for name, digest in digests.items()]}
        BUILD.verify_upload(release, digests)
        release["assets"][0]["digest"] = "sha256:" + "0" * 64
        with self.assertRaises(RuntimeError):
            BUILD.verify_upload(release, digests)

    def test_missing_package_prevents_publication(self):
        (self.directory / next(iter(self.names))).unlink()
        with self.assertRaises(RuntimeError):
            BUILD.checksums(self.directory, self.manifest, self.version, True)
        self.assertFalse((self.directory / self.manifest["checksums"]).exists())

    def test_unexpected_package_prevents_publication(self):
        (self.directory / "unexpected.apk").touch()
        with self.assertRaises(RuntimeError):
            BUILD.checksums(self.directory, self.manifest, self.version, True)

    def test_symbols_must_name_the_requested_source_commit(self):
        BUILD.verify_symbols(self.directory, self.manifest, self.version, COMMIT)
        with self.assertRaises(RuntimeError):
            BUILD.verify_symbols(self.directory, self.manifest, self.version, "b" * 40)

    def recovery_fixture(self):
        run = {"path": ".github/workflows/" + BUILD.CONFIG["workflow"], "status": "completed",
               "head_sha": COMMIT, "display_title": "Package"}
        jobs = [{"name": "windows", "conclusion": "success"},
                {"name": "Android arm64 APK", "conclusion": "failure"}]
        jobs.extend({"name": f"macOS {arch}", "conclusion": "success"}
                    for arch in self.manifest["assets"]["macos"])
        return run, jobs

    def test_recovery_allows_unselected_android_failure(self):
        run, jobs = self.recovery_fixture()
        BUILD.verify_recovery_run(run, jobs, COMMIT, self.manifest)

    def test_recovery_rejects_failed_desktop_job(self):
        run, jobs = self.recovery_fixture()
        jobs[0]["conclusion"] = "failure"
        with self.assertRaises(RuntimeError):
            BUILD.verify_recovery_run(run, jobs, COMMIT, self.manifest)

    def test_recovery_rejects_another_source(self):
        run, jobs = self.recovery_fixture()
        with self.assertRaises(RuntimeError):
            BUILD.verify_recovery_run(run, jobs, "b" * 40, self.manifest)

    def test_recovery_accepts_control_workflow_with_pinned_source(self):
        run, jobs = self.recovery_fixture()
        run["head_sha"] = "b" * 40
        run["display_title"] = BUILD.CONFIG["run_prefix"] + COMMIT
        BUILD.verify_recovery_run(run, jobs, COMMIT, self.manifest)

    def publish_environment(self):
        return patch.dict(os.environ, {"GITHUB_REPOSITORY": "owner/fork", "RELEASE_COMMIT": COMMIT,
                                      "RELEASE_VERSION": self.version, "GITHUB_RUN_ID": "123",
                                      "PACKAGE_DIRECTORY": str(self.directory)})

    def test_bad_upload_never_makes_a_draft_public(self):
        with self.publish_environment(), patch.object(BUILD, "command", return_value="true") as commands, \
                patch.object(BUILD, "api", side_effect=[[], [[{
                    "tag_name": BUILD.CONFIG["release_prefix"] + COMMIT, "assets": []}]]]):
            with self.assertRaises(RuntimeError):
                BUILD.publish()
        self.assertFalse(any("--draft=false" in call.args for call in commands.call_args_list))

    def test_published_release_is_not_overwritten_by_a_different_rebuild(self):
        release = {"tag_name": BUILD.CONFIG["release_prefix"] + COMMIT, "draft": False,
                   "assets": [{"name": name} for name in self.names | {self.manifest["checksums"]}],
                   "html_url": "https://example.invalid/release"}
        with self.publish_environment(), patch.object(BUILD, "command", return_value="true") as commands, \
                patch.object(BUILD, "api", return_value=[[release]]):
            BUILD.publish()
        self.assertFalse(any(call.args[:3] == ("gh", "release", "upload")
                             for call in commands.call_args_list))
        self.assertTrue(any("--latest=true" in call.args for call in commands.call_args_list))

    def test_incomplete_published_release_is_not_promoted(self):
        release = {"tag_name": BUILD.CONFIG["release_prefix"] + COMMIT, "draft": False, "assets": []}
        with self.publish_environment(), patch.object(BUILD, "command", return_value="true") as commands, \
                patch.object(BUILD, "api", return_value=[[release]]):
            with self.assertRaises(RuntimeError):
                BUILD.publish()
        self.assertFalse(any("--latest=true" in call.args for call in commands.call_args_list))

    def test_draft_can_be_published_when_tag_endpoint_returns_not_found(self):
        draft = {"tag_name": BUILD.CONFIG["release_prefix"] + COMMIT, "draft": True,
                 "assets": [{"name": "payload", "digest": "sha256:abc"}]}

        def draft_api(path, *args):
            if "/releases/tags/" in path:
                raise RuntimeError("HTTP 404: draft is unavailable by tag")
            return [[draft]]

        with self.publish_environment(), patch.object(BUILD, "command", return_value="true") as commands, \
                patch.object(BUILD, "checksums", return_value={"payload": "abc"}), \
                patch.object(BUILD, "api", side_effect=draft_api):
            BUILD.publish()
        self.assertTrue(any("--draft=false" in call.args for call in commands.call_args_list))


class LatestReleaseTests(unittest.TestCase):
    def test_first_success_becomes_latest(self):
        with patch.object(BUILD, "latest_release", return_value=None), \
                patch.object(BUILD, "command") as command:
            self.assertTrue(BUILD.promote_release("owner/fork", "new", COMMIT, "1.2.3"))
        self.assertIn("--latest=true", command.call_args.args)
        self.assertIn("--prerelease=false", command.call_args.args)

    def test_older_recovery_cannot_replace_newer_latest(self):
        for status in ("behind", "diverged"):
            with self.subTest(status=status), \
                    patch.object(BUILD, "latest_release", return_value={"tag_name": "newer"}), \
                    patch.object(BUILD, "api", return_value={"status": status}), \
                    patch.object(BUILD, "command") as command:
                self.assertFalse(BUILD.promote_release("owner/fork", "older", COMMIT, "1.2.3"))
                self.assertIn("--latest=false", command.call_args.args)

    def test_successful_descendant_replaces_latest(self):
        with patch.object(BUILD, "latest_release", return_value={"tag_name": "older"}), \
                patch.object(BUILD, "api", return_value={"status": "ahead"}), \
                patch.object(BUILD, "command") as command:
            self.assertTrue(BUILD.promote_release("owner/fork", "newer", COMMIT, "1.2.3"))
        self.assertIn("--latest=true", command.call_args.args)

    def test_api_error_is_not_treated_as_missing_latest(self):
        with patch.object(BUILD, "api", side_effect=RuntimeError("Forbidden (HTTP 403)")):
            with self.assertRaises(RuntimeError):
                BUILD.latest_release("owner/fork")
        with patch.object(BUILD, "api", side_effect=RuntimeError("Not Found (HTTP 404)")):
            self.assertIsNone(BUILD.latest_release("owner/fork"))


if __name__ == "__main__":
    unittest.main()
