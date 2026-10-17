# Automatic desktop builds on a fork

Merge this configuration into the fork's default `dev` branch. `Sync fork and
build desktops` checks the parent repository's default branch every ten minutes,
at minutes 7, 17, 27, 37, 47 and 57. GitHub can delay scheduled runs; this is a
polling interval, not a build completion deadline. The existing daily Package
schedule is skipped on forks.

The sync merges upstream into the fork's default branch, preserving the fork's
CI changes. It never force pushes. A merge conflict stops the run and needs a
manual resolution; no remote branch is changed on conflict. Other branches,
including `main`, are left alone. The fork's commit ID therefore differs from
upstream even when its application source is identical.

If upstream changes workflow files, GitHub may reject a push made with the
default `GITHUB_TOKEN`. Set the repository Actions secret `FORK_SYNC_TOKEN` to a
fine-grained token restricted to this fork, with Contents, Workflows and Actions
read/write permissions. Do not put tokens in workflow files or pull requests.
Without the secret, the sync tries `GITHUB_TOKEN` and reports any permission
failure; it does not skip workflow updates or reset the fork.

Only one Package run is dispatched at a time. New commits arriving during a
build accumulate on `dev`; the next poll builds the newest revision. A successful
revision is not rebuilt. Failed or cancelled revisions get at most two attempts
including manual reruns, then need attention. Use **Re-run failed jobs**, or
manually dispatch Package with `bump=nightly`, `platforms=windows-macos`, and the
default branch. A new source revision gets its own retry budget.

Package checks out an exact source SHA and uses its existing release tooling,
cache, installer checks and smoke tests. Windows x64, macOS x86_64 and macOS arm64
run in parallel after preparation. Linux and Android are skipped for this
selection. The original all-platform option remains available manually.

All three builds must succeed before publication. Download names are derived
from `packaging/release-assets.json`, checked for completeness, checksummed, and
verified against GitHub's uploaded SHA-256 digests. A complete set is published
as an unofficial prerelease named `fork-desktop-<source SHA>`. Failed uploads
leave a draft; prior published builds remain available. These builds do not
replace official updater manifests or announce a release to the upstream API.
Signing is optional for desktop packages; without signing secrets, Windows is
unsigned and macOS is ad-hoc signed and unnotarized.

GitHub may disable schedules after prolonged repository inactivity. Check Actions
for sync failures, expired tokens, conflicts or disabled schedules. To pause this
automation, disable **Sync fork and build desktops** in Actions.

Validate the scheduler and publication gates without compiling the client:

```sh
python3 -m unittest discover -s .github/tests -p test_fork_build.py -v
```
