# Updates

CassQuery v0.2 adds stable-release checking, optional automatic download, verified
staging, and an explicit **Restart and install** action. Released v0.1 has none
of this code: upgrading v0.1 to v0.2 requires the ordinary manual installer.

## Channel and trust

The only channel is [cass-ale/CassQuery Releases](https://github.com/cass-ale/CassQuery/releases).
Checks use the public GitHub API without tokens or cookies. Only canonical stable
`vMAJOR.MINOR.PATCH` releases newer than the running version are selected; drafts,
prereleases, wrong architectures and other repositories are rejected.

The native ZIP must agree with BOTH the release asset's API SHA-256/size and
`SHA256SUMS.txt`. HTTPS redirects stay on the exact repository download path or
GitHub's repository-scoped release CDN. These checks authenticate downloaded bytes
against the trusted GitHub release origin. They are not an independent publisher
signature, Developer ID, notarization or Gatekeeper approval.

## Checking and download

Automatic checks run at launch when the last check is at least 24 hours old, then
at a bounded 24-hour interval while the app is running. They are an app timer, not
an external background service. Automatic download is disabled by default.
Checking failures are visible update errors; they do not stop the viewer or worker.

Downloads are streamed into private `userData/updates` staging. Limits are 500 MiB
compressed ZIP, 2 GiB expanded content, 10,000 entries and bounded metadata. ZIP
metadata/local headers, CRC, entry sizes, collisions, symlink ancestry/targets and
containment are validated before native extraction. This update channel accepts
ASCII archive names/links only; it does not pretend JavaScript lowercase handles
macOS Unicode case folding. Unicode project names and source contents outside the
application are not changed by this restriction.

A verified archive can be revealed through the update UI. The renderer cannot
supply an arbitrary filesystem path or URL to this action.

## Explicit installation

No update silently replaces a running application. **Restart and install** requires
an explicit user action, no unsaved drafts, no active builds/source mutations, and
an independently verified staged bundle. The target and its parent must be
writable, non-shared, user-owned directories. No sudo, global privileged helper,
quarantine removal or sandbox bypass is used. A system-managed `/Applications`
installation may require manual installation or moving the app into an owned
`~/Applications` directory; a verified download remains available to reveal.

The helper is copied from the signed app's ASAR into owned staging and runs with
the existing packaged Electron `RunAsNode` capability. The source signing profile
is explicitly ad-hoc/non-hardened. This is a custom installer, NOT an assertion
that Electron's standard `autoUpdater` / Squirrel.Mac supports this distribution.
Squirrel verifies the running application's designated code requirement; this
ad-hoc application's designated requirement is a build-specific code hash.

Before replacing anything, the helper validates the bundle ID, version, native
architecture, executable, code seal, minimum OS and complete byte/mode/link
inventory. It copies the candidate into owned sibling staging on the installation
filesystem and checks the copied inventory. The old PID must exit gracefully
before atomic directory renames. A previous verified app is retained.

The new app must acknowledge its exact nonce, PID, version, architecture and path
ONLY after its owned worker and same-origin real renderer are ready. A spawned
PID, module load or error-page load is not success. The nonce is consumed privately
before workers/renderers are spawned. On failed readiness, only the helper's own
new process/session is stopped and reaped before restoring the original. Restored
startup has a separate readiness acknowledgement. Failed or ambiguous recovery is
reported; an unverified success is never manufactured.

## Qualification ledger

Source/unit checks and mock HTTP responses are not native install proof. Before
shipping, an independent macOS CI gate must exercise the actual packaged helper,
old-PID exit, copied/new app seal, real worker/renderer acknowledgement, failed
launch rollback/reaping, unchanged project/user data, both architectures and the
unchanged ZIP/DMG release gates. Test-only versioned bundles are not future public
releases. No local GUI refusal is bypassed to produce that proof.

Primary implementation references:
- [Electron 45 autoUpdater signing notice](https://github.com/electron/electron/blob/54f961b44b83e4ece81065ab60a182fd0c38c01e/docs/api/auto-updater.md#macos)
- [Pinned Squirrel designated-requirement verification](https://github.com/Squirrel/Squirrel.Mac/blob/fffea30e4a339a7f7d69a3391314b72548d209bd/Squirrel/SQRLCodeSignature.m)
- [Electron RunAsNode fuse](https://github.com/electron/electron/blob/54f961b44b83e4ece81065ab60a182fd0c38c01e/docs/tutorial/fuses.md#runasnode)
