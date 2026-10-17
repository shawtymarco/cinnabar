# Automatic desktop builds on a fork

The default branch is `dev`, containing both application code and fork CI.
`Sync fork and build desktops` checks upstream every five minutes, at minutes
2, 7, 12, 17 and so on. Five minutes is GitHub's minimum supported schedule
interval; queueing may delay actual execution. The inherited daily Package
schedule is skipped on forks.

Synchronization merges the parent's default branch into `dev`, preserving fork
CI changes. It never force pushes. Merge conflicts stop the run without changing
the remote branch. Automation and merge commits make `dev` ahead of upstream;
this is expected. The former `fork-ci` branch is retained as historical backup
and is no longer the default or the scheduler's target.

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
manually dispatch Package on `dev` with `bump=nightly`,
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

Completed CI artifacts can also be published without rebuilding. Dispatch
**Publish completed desktop build** on `dev`, with the Package `run_id` and
its full `source_commit`. If the original run is still active, recovery waits
without holding the publication lock. It verifies that all three desktop jobs succeeded,
downloads only their installers and symbols, and reads the version from that
source commit. The symbol archives must identify the requested revision. The
publication checks and draft-to-public transition are identical to automatic
publication; both publishers serialize publication of the same source SHA.

GitHub may disable schedules after prolonged repository inactivity. Check Actions
for sync failures, expired tokens, conflicts or disabled schedules. To pause this
automation, disable **Sync fork and build desktops** in Actions.

Validate the scheduler and publication gates without compiling the client:

```sh
python3 -m unittest discover -s .github/tests -p test_fork_build.py -v
```
