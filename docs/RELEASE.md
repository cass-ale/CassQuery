# Protected candidate publication

This is an authored, unexecuted release chain—not a green native candidate or
release authorization. Source-push permission does **not** authorize dispatch,
tagging, environment configuration, draft creation or public publication.
`docs/AUDIT.md` and `docs/MACOS_RELEASE.md` retain the native GUI, final package,
clean quarantined Finder launch and minimum-OS verification gaps. This document
specifies the new publication boundary; it does not close those gaps.

## Owner setup and authorization

Before any dispatch, the owner must independently review the final exact source,
inspect both architectures' native/final ZIP/DMG evidence and select distribution.
The `releases` environment's `v*` tag rule is necessary but is not approval
protection. The reported configuration contains only `branch_policy`, so
publication remains blocked until the owner—not this tool—adds the required
reviewer rule. The complete required configuration is:

- A `required_reviewers` protection rule containing at least one real user/team.
- `prevent_self_review: true`; someone other than the dispatcher must approve.
- Custom deployment branch policies: **exactly one tag rule named `v*`**,
  no branch/extra rule, with `protected_branches: false` and
  `custom_branch_policies: true`.

The read-only preparation job queries GitHub's environment/protection and branch
policy APIs **before** a job can reference `releases`. Missing, empty, unreadable
or unprotected configuration blocks the workflow. Naming `environment: releases`
alone would silently create an unprotected environment and is not accepted as
proof. The approval job rechecks protection; the write job checks it again and
requires GitHub's recorded independent approval plus successful preparation and
approval steps. Any API denial—including 403—blocks; do not add a broad PAT as
a workaround. Removing protection mid-run blocks content writes on revalidation.

An existing annotated `vMAJOR.MINOR.PATCH` tag must directly target the exact
reviewed source commit and match trusted `package.json` as well as both candidate
versions. The first-release source version is **0.1.0**, with canonical tag
**v0.1.0**; the client reads the committed source version rather than hardcoding it.
The tool never creates, moves or deletes tags. Stable
canonical numeric versions only are supported; prerelease channels and automatic
updates are outside this manual-download initial release path.

## Explicit workflow inputs

Only the repository owner's first-attempt `workflow_dispatch` on the **existing
annotated tag named by the `tag` input** is eligible. The dispatch ref must be
exactly `refs/tags/<tag>` and begin `refs/tags/v`. The selected tag must point to
the successful trusted **main-push** candidate's exact commit. A `v*` policy does
not permit arbitrary tag source: full SHA, clean source fingerprint, annotated
object and candidate validation remain mandatory.

There is no push/tag trigger or automatic release. Creating a tag does not
start publication. Reruns are rejected; a new dispatch and fresh approval are
required. Select the existing tag as the dispatch ref (API `ref` or CLI `--ref`),
not `main`; the workflow must also exist on the default branch for manual dispatch.

- `candidate_run_id`: successful **main push** run of `macos-candidate.yml`, at the
  publication workflow's full 40-character source SHA. PR, fork, manual candidate,
  stale, incomplete, failed and skipped-gate runs are ineligible.
- `tag`: existing annotated version tag at that same SHA. Its object and direct
  target are checked before/after candidate validation and around release writes.
- `mode`: `draft` by default. `publish` is a separate, explicit owner choice.
  Both modes require protected independent approval; neither is an implicit
  continuation of an earlier source push.
- `allow_ad_hoc`: `false` by default. The present schema admits only explicitly
  opted-in **ad-hoc, non-hardened** candidates. This is not Developer ID publisher
  identity, notarization or Gatekeeper acceptance. Unsupported signing evidence
  blocks; there is no unsigned fallback. A future Developer ID profile needs
  separately validated signing/notarization/stapling evidence and workflow review.

## Candidate identity and exact-byte promotion

The common `tools/release_evidence.py` verifier binds the run and each native job's
mandatory successful steps to the source SHA. Each architecture artifact is
`CassQuery-mac-{arm64|x64}-{sha}`. Only those two nonexpired artifacts are accepted;
GitHub-reported run/repository/branch/SHA identity and archive SHA-256 must match.
The current run attempt is rechecked to prevent a stale or superseded attempt.

The Actions wrapper ZIP is downloaded using a constructed REST endpoint. Storage
redirects must be approved HTTPS GitHub/Azure artifact hosts and receive **no**
GitHub bearer token. Bounded streaming and extraction reject absolute/traversal/
backslash/control-character paths, symlinks, special files, duplicate/colliding
paths, encryption, unsupported compression, excessive count/size and ZIP bombs.
The wrapper's entry count and central-directory bytes/records are bounded **before**
`ZipFile` can allocate metadata; forged counts, split archives and ZIP64 wrappers
are rejected. This deliberately supports ordinary wrappers below 2 GiB only.
Nothing from an artifact is executed or extracted into the source checkout.

Each staged architecture root contains `candidate.json`, `evidence/` and
`dist/mac-app/` assets. The common schema-1 verifier checks repository, source SHA
and fingerprint, version, architecture, run/attempt, gate evidence, complete file
inventory, byte sizes and SHA-256. The publisher independently fingerprints its
clean exact-SHA source checkout and requires both candidate fingerprints to match
that trusted source, not merely each other. It checks the promotion bytes again.

The release inventory contains the original two ZIPs and two DMGs plus
`candidate-arm64.json` and `candidate-x64.json`—the original root manifests renamed,
not rewritten. `SHA256SUMS.txt` is the only newly derived release file and hashes
all six original files. Source/run/tag provenance is also recorded in the draft
body. No rebuilding, re-signing, recompression, Git binary mirror or updater is
performed. These checksums/provenance records are **not** owner-key signatures or
GitHub build attestations.

## Draft before public publication

Preparation and approval jobs have only `contents: read` and `actions: read`.
Only the final job, after both succeed, receives `contents: write` plus
`actions: read`. Each job independently downloads/verifies the original candidate;
no privileged token, secret, shell environment or promotion plan is passed in an
artifact. Checkout is pinned and does not persist credentials. No external
release action or signing credential is used.

The release client defaults to verification-only (zero contents writes). Its
`--execute` mode additionally verifies the owner dispatch event inputs, GitHub
server-side workflow identity, exact tag ref and source SHA, first run attempt,
successful required preparation/approval steps and recorded independent environment
review. `GITHUB_WORKFLOW_REF` must identify this workflow at that same exact tag;
the server-side publication run's `head_branch` must be the selected tag name.
The candidate run and its artifact identities still require `head_branch: main`.

The client enumerates release history rather than trusting relevance/first-page
results. A true latest-release **404** permits bootstrap only when no published
release exists; 401/403/429/5xx and malformed metadata block. The version must be
strictly newer than every published version. A published tag is never changed.

Execution creates a **draft** or resumes only an identity-matching draft. It
never deletes/replaces an existing asset. Existing draft files must have expected
names, sizes and downloaded SHA-256 before any additional upload; unknown files
or same-size corruption block. After all uploads, it re-downloads **every** draft
asset, requires the complete exact inventory, and verifies every size/hash. It
then rechecks approval, protection, candidate attempt, monotonic history and tag
identity. Only an explicit `mode: publish` dispatch can set `draft: false`.
Failures leave the draft for owner inspection; there is no automatic cleanup or
silent retry of a mutating API call.

GitHub offers no atomic transaction spanning tag refs, assets, environment rules
and release publication. Rechecks detect changes at each boundary but cannot
eliminate a concurrent external administrator mutation between the final read and
write. Workflow concurrency serializes this chain's runs, not unrelated writers.
Restrict external release/tag write access and never change reviewed state during
promotion. A post-publication tag change is reported, never "repaired" by mutating
an already public release.

## Checked / not checked

Local negative unit tests and semantic workflow tests exercise the publication
policy, network/status handling, ZIP trust boundary, draft integrity, dispatch and
approval guards. The common evidence verifier has its own tests. Run:

```sh
python3 -m unittest discover -s tests -p 'test_release_publish.py'
npm run check
```

These tests use bounded mock REST responses and real temporary ZIP/file bytes;
they are **not** a live protected approval, native build, signing, upload, downloaded
GitHub draft or public publication result. No tag, environment, credential, draft,
release, commit, push or outreach was performed while implementing this chain.
Fresh exact-source independent review and actual successful native candidate
results remain prerequisites before the owner chooses to dispatch.
