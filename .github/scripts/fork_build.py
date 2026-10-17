#!/usr/bin/env python3
"""Sync a fork without discarding its CI, and publish verified desktop packages."""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[2]
CONFIG = json.loads((ROOT / ".github/fork-build.json").read_text())
SHA = re.compile(r"[0-9a-f]{40}")


def command(*args, cwd=None):
    """Run an argument vector without a shell; failures stop further mutations."""
    executable = shutil.which(args[0])
    if executable is None:
        raise RuntimeError(f"Missing command: {args[0]}")
    result = subprocess.run([executable, *args[1:]], cwd=cwd, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(f"{args[0]} {args[1]} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def api(path, *args):
    """Return a GitHub JSON response, distinguishing errors from an empty result."""
    output = command("gh", "api", path, *args)
    return json.loads(output) if output else None


def check_sha(value):
    """Accept only a full commit ID, never a revision expression or command option."""
    if not SHA.fullmatch(value):
        raise ValueError("Expected a full, lowercase Git commit SHA")
    return value


def build_decision(runs, commit, config=CONFIG):
    """Coalesce changes while packaging; retry a failed revision at most once."""
    if any(run["status"] != "completed" for run in runs):
        return "busy"
    attempts = [run for run in runs if run["display_title"] == config["run_prefix"] + commit]
    if any(run["conclusion"] == "success" for run in attempts):
        return "built"
    if sum(run.get("run_attempt", 1) for run in attempts) >= config["max_attempts"]:
        return "failed-limit"
    return "build"


def merge_upstream(root, branch, upstream_url, upstream_branch):
    """Merge without force pushing; preserve fork changes and reject merge conflicts."""
    git = lambda *args: command("git", *args, cwd=root)
    git("check-ref-format", f"refs/heads/{branch}")
    git("check-ref-format", f"refs/heads/{upstream_branch}")
    if git("status", "--porcelain"):
        raise RuntimeError("Sync requires a clean checkout")
    git("fetch", "--no-tags", "origin", f"refs/heads/{branch}")
    git("checkout", "-B", branch, "FETCH_HEAD")
    before = git("rev-parse", "HEAD")
    git("fetch", "--no-tags", upstream_url, f"refs/heads/{upstream_branch}")
    upstream = git("rev-parse", "FETCH_HEAD")
    git("config", "user.name", "github-actions[bot]")
    git("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    try:
        git("merge", "--no-edit", upstream)
    except RuntimeError:
        git("merge", "--abort")
        raise
    commit = git("rev-parse", "HEAD")
    if commit != before:
        git("push", "origin", f"HEAD:refs/heads/{branch}")
    return commit, upstream


def summary(message):
    """Write the same actionable result to the log and Actions summary."""
    print(message)
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a", encoding="utf-8") as output:
            output.write(message + "\n")


def sync():
    """Discover this fork's parent, merge its default branch, and dispatch one build."""
    repository = os.environ["GITHUB_REPOSITORY"]
    metadata = api(f"repos/{repository}")
    if not metadata["fork"]:
        raise RuntimeError("Automatic fork sync must not run on an upstream repository")
    parent = metadata["parent"]
    branch = metadata["default_branch"]
    commit, upstream = merge_upstream(ROOT, branch, parent["clone_url"], parent["default_branch"])
    # Pagination retains a long-running build even if many other runs have finished.
    pages = api(f"repos/{repository}/actions/workflows/{CONFIG['workflow']}/runs?per_page=100",
                "--paginate", "--slurp")
    runs = [run for page in pages for run in page["workflow_runs"]]
    decision = build_decision(runs, commit)
    summary(f"Fork `{branch}`: `{commit}`; upstream `{upstream}`; build decision: **{decision}**.")
    if decision == "failed-limit":
        raise RuntimeError("Desktop build failed twice. Inspect and rerun the Package workflow manually.")
    if decision != "build":
        return
    api(f"repos/{repository}/actions/workflows/{CONFIG['workflow']}/dispatches", "--method", "POST",
        "-f", f"ref={branch}", "-f", "inputs[bump]=nightly", "-f", f"inputs[branch]={branch}",
        "-f", f"inputs[platforms]={CONFIG['selection']}", "-f", f"inputs[source_commit]={commit}")
    summary(f"Dispatched parallel Windows and macOS packaging for `{commit}`.")


def pin():
    """Validate the source branch independently of the branch hosting the workflow."""
    branch = os.environ["TARGET_BRANCH"]
    desktop = os.environ.get("PLATFORMS") == CONFIG["selection"]
    source_branch = CONFIG["source_branch"] if desktop else os.environ["DEFAULT_BRANCH"]
    if os.environ["BUMP"] != "nightly" or branch != source_branch:
        raise RuntimeError(f"Pinned nightly builds require source branch {source_branch}")
    if desktop and os.environ.get("IS_FORK") != "true":
        raise RuntimeError("Desktop-only publishing is for forks")
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a", encoding="utf-8") as destination:
            destination.write(f"source_branch={source_branch}\n")
    commit = os.environ.get("SOURCE_COMMIT", "")
    if not commit:
        return
    check_sha(commit)
    command("git", "check-ref-format", f"refs/heads/{branch}")
    if command("git", "rev-parse", "HEAD") != commit:
        raise RuntimeError("Checkout does not match the requested source commit")
    command("git", "merge-base", "--is-ancestor", commit, f"origin/{branch}")
    command("git", "checkout", "-B", branch, commit)


def expected_assets(manifest, version, symbols, config=CONFIG):
    """Derive the selected downloads from the shared release manifest."""
    names = set()
    for platform in config["platforms"]:
        arches = manifest["assets"][platform]
        names.update(arches.values())
        names.update(manifest["update_assets"].get(platform, {}).values())
        if symbols:
            names.update(manifest["symbols"]["archive"].format(
                version=version, platform=platform, arch=arch) for arch in arches)
    return names


def checksums(directory, manifest, version, symbols):
    """Reject incomplete or unexpected packages before hashing any release files."""
    expected = expected_assets(manifest, version, symbols)
    actual = {path.name for path in directory.iterdir()}
    if actual != expected or any(not (directory / name).is_file() for name in actual):
        raise RuntimeError(f"Package mismatch; missing={sorted(expected - actual)}, "
                           f"unexpected={sorted(actual - expected)}")
    digests = {}
    for name in sorted(expected):
        with (directory / name).open("rb") as source:
            digests[name] = hashlib.file_digest(source, "sha256").hexdigest()
    checksum_file = directory / manifest["checksums"]
    checksum_file.write_text("".join(f"{digest}  {name}\n" for name, digest in digests.items()),
                             encoding="utf-8")
    digests[checksum_file.name] = hashlib.sha256(checksum_file.read_bytes()).hexdigest()
    return digests


def verify_upload(release, digests):
    """Require GitHub's stored names and SHA-256 digests to match the local packages."""
    actual = {asset["name"]: asset.get("digest") for asset in release["assets"]}
    if actual != {name: f"sha256:{digest}" for name, digest in digests.items()}:
        raise RuntimeError("Uploaded release assets do not match the verified packages")


def verify_symbols(directory, manifest, version, commit):
    """Reject artifacts whose embedded provenance names another build or architecture."""
    for platform in CONFIG["platforms"]:
        for arch in manifest["assets"][platform]:
            name = manifest["symbols"]["archive"].format(version=version, platform=platform, arch=arch)
            with zipfile.ZipFile(directory / name) as archive:
                metadata = json.loads(archive.read(manifest["symbols"]["manifest"]))
            expected = {"commit": commit, "version": version, "platform": f"{platform}-{arch}"}
            if any(metadata.get(key) != value for key, value in expected.items()):
                raise RuntimeError(f"Artifact provenance mismatch: {name}")


def verify_recovery_run(run, jobs, commit, manifest):
    """Allow publication of completed desktop jobs even if an unselected platform failed."""
    if run["path"] != f".github/workflows/{CONFIG['workflow']}" or run["status"] != "completed":
        raise RuntimeError("Recovery requires a completed Package workflow")
    if run["head_sha"] != commit and run["display_title"] != CONFIG["run_prefix"] + commit:
        raise RuntimeError("Recovery source does not match the selected build")
    required = {"windows"}
    required.update(f"macOS {arch}" for arch in manifest["assets"]["macos"])
    passed = {job["name"] for job in jobs if job["conclusion"] == "success"}
    if missing := required - passed:
        raise RuntimeError(f"Desktop jobs did not all succeed: {sorted(missing)}")


def recover():
    """Validate the requested CI run before downloading any recovery artifacts."""
    run_id = os.environ["RECOVERY_RUN_ID"]
    if not re.fullmatch(r"[1-9][0-9]*", run_id):
        raise ValueError("Recovery run ID must be a positive integer")
    repository = os.environ["GITHUB_REPOSITORY"]
    run = api(f"repos/{repository}/actions/runs/{run_id}")
    pages = api(f"repos/{repository}/actions/runs/{run_id}/jobs?per_page=100", "--paginate", "--slurp")
    jobs = [job for page in pages for job in page["jobs"]]
    manifest = json.loads((ROOT / "packaging/release-assets.json").read_text())
    verify_recovery_run(run, jobs, check_sha(os.environ["RELEASE_COMMIT"]), manifest)


def find_release(repository, tag):
    """Find authenticated drafts as well as public releases without the published-tag endpoint."""
    pages = api(f"repos/{repository}/releases?per_page=100", "--paginate", "--slurp")
    return next((release for page in pages for release in page if release["tag_name"] == tag), None)


def latest_release(repository):
    """Treat a missing latest release as empty, but propagate authentication and API errors."""
    try:
        return api(f"repos/{repository}/releases/latest")
    except RuntimeError as error:
        if "(HTTP 404)" not in str(error):
            raise
        return None


def promote_release(repository, tag, commit, version):
    """Expose verified downloads through Latest without rolling back to an older source."""
    current = latest_release(repository)
    latest = current is None or current["tag_name"] == tag
    if not latest:
        previous = quote(current["tag_name"], safe="")
        comparison = api(f"repos/{repository}/compare/{previous}...{check_sha(commit)}")
        latest = comparison["status"] in {"ahead", "identical"}
    command("gh", "release", "edit", tag, "--repo", repository, "--draft=false", "--prerelease=false",
            f"--latest={str(latest).lower()}", "--title", f"v{version} ({commit[:10]})")
    return latest


def publish():
    """Publish verified immutable downloads and move Latest to the newest successful source."""
    repository = os.environ["GITHUB_REPOSITORY"]
    commit = check_sha(os.environ["RELEASE_COMMIT"])
    version = os.environ["RELEASE_VERSION"]
    directory = Path(os.environ["PACKAGE_DIRECTORY"]).resolve()
    manifest = json.loads((ROOT / "packaging/release-assets.json").read_text())
    symbols = command(sys.executable, str(ROOT / "packaging/release-symbols.py"), "has-symbols") == "true"
    if symbols:
        verify_symbols(directory, manifest, version, commit)
    digests = checksums(directory, manifest, version, symbols)
    tag = CONFIG["release_prefix"] + commit
    existing = find_release(repository, tag)
    if existing and not existing["draft"]:
        # Rebuilding can change signatures and timestamps; published downloads are immutable.
        if {asset["name"] for asset in existing["assets"]} != set(digests):
            raise RuntimeError("Published release is missing the complete download set")
        promote_release(repository, tag, commit, version)
        summary(f"Desktop release already published: {existing['html_url']}")
        return
    with tempfile.TemporaryDirectory() as scratch:
        notes = Path(scratch) / "notes.md"
        build_run = os.environ.get("RECOVERY_RUN_ID") or os.environ["GITHUB_RUN_ID"]
        run_url = f"https://github.com/{repository}/actions/runs/{build_run}"
        notes.write_text(f"Source: `{commit}`\n\n[GitHub Actions build]({run_url})\n\n"
                         "Windows x64 and macOS Intel/Apple Silicon. Unofficial fork build; "
                         "desktop signing is optional. SHA256SUMS.txt verifies every download.\n",
                         encoding="utf-8")
        if not existing:
            command("gh", "release", "create", tag, "--repo", repository, "--draft", "--prerelease",
                    "--latest=false", "--target", commit, "--title", f"Desktop {version} ({commit[:10]})",
                    "--notes-file", str(notes))
        command("gh", "release", "upload", tag, "--repo", repository, "--clobber",
                *(str(directory / name) for name in digests))
    release = find_release(repository, tag)
    if release is None:
        raise RuntimeError("Uploaded draft release is not visible to this token")
    verify_upload(release, digests)
    promote_release(repository, tag, commit, version)
    summary(f"Desktop release: https://github.com/{repository}/releases/tag/{tag}")


if __name__ == "__main__":
    try:
        {"sync": sync, "pin": pin, "publish": publish, "recover": recover}[sys.argv[1]]()
    except (KeyError, IndexError, OSError, RuntimeError, ValueError) as error:
        print(f"::error::{error}", file=sys.stderr)
        sys.exit(1)
