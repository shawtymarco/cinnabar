# Automatic desktop builds on a fork

The fork's default branch is `fork-ci`, which owns this configuration. `Sync fork
and build desktops` checks the parent repository's default branch every five minutes,
at minutes 2, 7, 12, 17 and so on. Five minutes is GitHub's minimum supported
schedule interval. GitHub can delay scheduled runs; this is a
polling interval, not a build completion deadline. The existing daily Package
schedule is skipped on forks.

The sync fast-forwards `dev` to the exact upstream commit, so `dev` has no extra
automation or merge commits. It also merges upstream into `fork-ci` to keep
release tooling current while preserving the fork's CI changes. It never force
pushes: unexpected commits on `dev` or conflicts on `fork-ci` require manual
resolution. A control-branch conflict does not undo an already completed source
sync, but prevents dispatch until resolved. Other branches, including `main`,
are left alone. `fork-ci` will be ahead of upstream; `dev` remains an exact mirror.

When migrating from automation on `dev`, first preserve that branch as `fork-ci`,
install this configuration there, and select it as the repository default. Only
then reset `dev` to upstream using an explicit lease on its previous SHA. This
one-time migration is separate from the scheduler, which never resets history.

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
manually dispatch Package on `fork-ci` with `bump=nightly`,
`platforms=windows-macos`, `branch=dev`, and `source_commit` set to the desired
upstream SHA. A new source revision gets its own retry budget.

Package checks out an exact source SHA and uses its existing release tooling,
cache, installer checks and smoke tests. Windows x64, macOS x86_64 and macOS arm64
run in parallel after preparation. Linux and Android are skipped for this
selection. The original all-platform option remains available manually.

All three builds must succeed before publication. Download names are derived
from `packaging/release-assets.json`, checked for completeness, checksummed, and
checked against the source provenance in the symbol archives, and verified
against GitHub's uploaded SHA-256 digests. A complete set is published
as an unofficial prerelease named `fork-desktop-<source SHA>`. Failed uploads
leave a draft; prior published builds remain available. These builds do not
replace official updater manifests or announce a release to the upstream API.
Signing is optional for desktop packages; without signing secrets, Windows is
unsigned and macOS is ad-hoc signed and unnotarized.

Completed CI artifacts can also be published without rebuilding. Check that all
three desktop jobs succeeded, download their installer and symbol artifacts,
and place their files in one empty directory. Run `.github/scripts/fork_build.py
publish` with `GITHUB_REPOSITORY`, `GITHUB_RUN_ID`, `RELEASE_COMMIT`,
`RELEASE_VERSION` and `PACKAGE_DIRECTORY` referring to that exact CI run. The
symbol archives must identify the requested source revision; the publication
checks and draft-to-public transition are identical to automatic publication.

GitHub may disable schedules after prolonged repository inactivity. Check Actions
for sync failures, expired tokens, conflicts or disabled schedules. To pause this
automation, disable **Sync fork and build desktops** in Actions.

Validate the scheduler and publication gates without compiling the client:

```sh
python3 -m unittest discover -s .github/tests -p test_fork_build.py -v
```
