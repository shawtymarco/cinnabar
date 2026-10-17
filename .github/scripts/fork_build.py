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
    """Attach an ancestor of the default branch without changing remote release refs."""
    branch = os.environ["TARGET_BRANCH"]
    if os.environ["BUMP"] != "nightly" or branch != os.environ["DEFAULT_BRANCH"]:
        raise RuntimeError("Pinned and desktop-only builds require nightly on the default branch")
    if (os.environ.get("PLATFORMS") == CONFIG["selection"]
            and os.environ.get("IS_FORK") != "true"):
        raise RuntimeError("Desktop-only publishing is for forks")
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


def publish():
    """Publish an immutable prerelease only after every selected build succeeds."""
    repository = os.environ["GITHUB_REPOSITORY"]
    commit = check_sha(os.environ["RELEASE_COMMIT"])
    version = os.environ["RELEASE_VERSION"]
    directory = Path(os.environ["PACKAGE_DIRECTORY"]).resolve()
    manifest = json.loads((ROOT / "packaging/release-assets.json").read_text())
    symbols = command(sys.executable, str(ROOT / "packaging/release-symbols.py"), "has-symbols") == "true"
    digests = checksums(directory, manifest, version, symbols)
    tag = CONFIG["release_prefix"] + commit
    # Drafts are visible only to writers, so enumerate them using authenticated REST.
    pages = api(f"repos/{repository}/releases?per_page=100", "--paginate", "--slurp")
    existing = next((release for page in pages for release in page if release["tag_name"] == tag), None)
    if existing and not existing["draft"]:
        # Rebuilding can change signatures and timestamps; published downloads are immutable.
        summary(f"Desktop release already published: {existing['html_url']}")
        return
    with tempfile.TemporaryDirectory() as scratch:
        notes = Path(scratch) / "notes.md"
        run_url = f"https://github.com/{repository}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
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
    release = api(f"repos/{repository}/releases/tags/{tag}")
    verify_upload(release, digests)
    command("gh", "release", "edit", tag, "--repo", repository, "--draft=false", "--prerelease",
            "--latest=false")
    summary(f"Desktop release: https://github.com/{repository}/releases/tag/{tag}")


if __name__ == "__main__":
    try:
        {"sync": sync, "pin": pin, "publish": publish}[sys.argv[1]]()
    except (KeyError, IndexError, OSError, RuntimeError, ValueError) as error:
        print(f"::error::{error}", file=sys.stderr)
        sys.exit(1)
