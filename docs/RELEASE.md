# Protected candidate publication

The release chain promotes checked candidate bytes; it does not turn authored gates
or a successful artifact upload into native proof. `docs/AUDIT.md` and
`docs/MACOS_RELEASE.md` retain the native GUI, final package, clean quarantined Finder
launch and minimum-OS verification gaps. Source-push permission alone does not
approve tagging, dispatch, environment changes or publication.

## Owner-managed approval

Before dispatch, review the final exact source, inspect both architectures' native
and final ZIP/DMG evidence, and select distribution. The `releases` environment must
have:

- One `required_reviewers` rule containing at least one configured user/team, with
  an explicit boolean `prevent_self_review` policy.
- Exactly one custom deployment policy: tag pattern **`v*`**, no branch/extra rule,
  `protected_branches: false`, `custom_branch_policies: true`.

The owner's selected policy matches FrameGuard's solo-maintainer process: the owner
is a required reviewer and `prevent_self_review: false`. Owner approval is allowed
under that policy, not under a policy forbidding self-review. Another person is not
an additional invented requirement. **The human must still approve the actual
GitHub environment gate**; chat authorization, a success label, administrator
bypass and an agent invoking an approval API are not substitutes.

Preparation queries protection before any job references `releases`, so an absent
or unprotected environment cannot be silently created by the job. Approval and
publication recheck the configuration. The recorded approving user's immutable ID
must match a currently required user, or a current member of an ID-bound required
team. Team identity and complete membership are read from owner-scoped GitHub APIs,
with a 200-member bound; unreadable, inconsistent or excessive membership blocks
rather than inferring eligibility. Current user-reviewer approval needs no team
API or additional credential. Removing reviewers, replacing the approving user or
enabling prevention of self-review blocks a no-longer-eligible approval.

No tool here creates or changes protection, approves the gate, creates/moves/deletes
a tag, or bypasses repository controls.

## Exact source, tag and dispatch

The existing annotated `vMAJOR.MINOR.PATCH` tag must directly target the reviewed
source SHA and match trusted `package.json` and both candidate versions. The first
release is **0.1.0**, with canonical tag **v0.1.0**. The publisher reads the source
version; stable canonical numeric versions only are supported. Prerelease channels
and automatic updates are outside this initial manual-download path.

Only the repository owner's first-attempt `workflow_dispatch` from that existing
tag is eligible. `GITHUB_REF` and `GITHUB_WORKFLOW_REF` must identify the exact tag;
GitHub's server-side run must identify the trusted publication workflow, owner,
tag and source SHA. Candidate and integration runs separately require trusted
**main pushes**, not tag/PR/manual/fork runs. There is no push/tag publication trigger.
Reruns are rejected: use a fresh owner dispatch and approval. The workflow must also
exist on the default branch for manual dispatch.

Inputs:

- `candidate_run_id`: successful exact-SHA main-push run of `macos-candidate.yml`.
  Every mandatory native step must succeed; stale, incomplete, failed or skipped
  gates are ineligible.
- `tag`: existing annotated version tag at that SHA; object and direct target are
  rechecked around verification and writes.
- `mode`: **draft** by default. Publication is a separate explicit owner choice.
  Both modes require the actual configured environment approval.
- `allow_ad_hoc`: **false** by default. Current promotion admits only explicitly
  opted-in **ad-hoc, non-hardened** candidates. This is not Developer ID,
  notarization or Gatekeeper acceptance. Unsupported signing evidence blocks;
  there is no unsigned fallback. Developer ID needs a separately verified contract.

GitHub release creation/update can require Workflows write when the resolved target
modifies workflow files relative to the default branch; `GITHUB_TOKEN` cannot gain
that permission through workflow YAML. The publisher requires an existing tag and
never creates it. Check the actual reviewed target/default-branch workflow state
before tagging/dispatch rather than adding broad credentials or ignoring a denial.
A 403/404 on a write is a failure, never evidence of first-release success.

## Mandatory source integration

Publication discovers `integration.yml` through its trusted workflow ID, enumerates
exact-SHA push runs without filtering for success, and selects the newest workflow
run number. A newer failed/in-progress run or a failed latest rerun cannot fall
back to an older green. Discovery and attempt-scoped job inventories are bounded
at 200 rows and must be complete and consistent.

The selected run must belong to this repository/head-repository, name the trusted
workflow, and be a terminal successful main push at the exact source. Exactly one
integration job must succeed; its run ID/source SHA must match. Each of these
steps must be completed and successful:

1. Record integration context.
2. Source integration dependencies.
3. Real CAD browser loop and mutation witness.
4. Validate independent report bytes.
5. Keep integration evidence.

The run/attempt/repository identity is pinned during candidate preparation, recorded
in the draft body and rechecked after downloads and before every contents write.
Native run/attempt/job gates are rechecked at those same write boundaries. A new run,
rerun or failed source gate requires fresh qualification, not a renamed passed label.
`tools/readiness.py` uses the same source/run/job contract for checklist inputs;
its success is not permission to tag or publish.

## Candidate identity and unchanged bytes

Only the two nonexpired artifacts named `CassQuery-mac-{arm64|x64}-{sha}` are accepted.
Authenticated API run/repository/branch/SHA identity and archive SHA-256 must match.
The Actions wrapper is downloaded through a constructed endpoint; approved HTTPS
GitHub/Azure storage redirects receive no bearer token. Bounded streaming/extraction
reject unsafe paths, symlinks/special files, duplicates/collisions, unsupported
encoding, ZIP bombs, forged counts/directories, split archives and ZIP64 wrappers.
ZIP metadata is bounded before `ZipFile` allocates entries. Nothing from the artifact
is executed or extracted into the source checkout.

The common verifier binds each staged `candidate.json` to original source
fingerprint, version, architecture, run/attempt, complete file inventory, reports,
linked bytes, native images, ZIP/DMG and supply-chain inventory. Both candidate
fingerprints must equal the publisher's clean exact-SHA source fingerprint.

Release inventory: the original two ZIPs, two DMGs, `candidate-arm64.json` and
`candidate-x64.json` (original manifests renamed, not rewritten), and derived
`SHA256SUMS.txt` hashing all six originals. No rebuilding, re-signing or recompression
occurs. Embedded ZIP/DMG bytes and modes are preserved. The manifests/checksums are
not owner-key signatures or GitHub provenance attestations.

## Draft, readback and failure recovery

Preparation/approval have only `contents: read` and `actions: read`; the publication
job receives `contents: write` only after both succeed. Checkout is exact-SHA and
does not persist credentials. No privileged plan/token is passed through an artifact.
The client defaults to verification-only. `--execute` additionally requires the
owner dispatch, trusted server context, actual eligible environment approval and
successful preparation/approval steps.

Release history is fully enumerated. A latest-release 404 permits bootstrap only
when no published release exists; authentication/server errors and contradictory
history block. The proposed stable version must exceed every published version.
Published releases are never modified.

Execution creates or resumes only an identity-matching draft. Existing assets must
have expected names, sizes and re-downloaded SHA-256 before any missing upload.
No deletes, overwrites or clobber are performed. After uploading, **every** draft
asset is re-downloaded and hashed; exact complete inventory, source, tag, approval,
native and latest integration identities are revalidated before optional publication.
Only explicit `mode: publish` can set `draft: false`.

An ordinary interrupted upload can resume missing files after fresh dispatch and
approval. GitHub HTTP 502 can instead leave an empty `starter` asset, which safely
blocks resume. The owner must inspect and explicitly authorize removal of that
failed draft asset, then dispatch again; the agent does not silently delete it.
Unknown assets or changed bytes also block for owner inspection. No mutating API
call is silently retried.

GitHub has no atomic transaction across tags, assets, protection and publication.
Rechecks detect changes at boundaries but cannot eliminate an external mutation
between the final read/write. Workflow concurrency serializes this chain, not
unrelated writers. Do not change reviewed state during promotion. Post-publication
changes are reported, never repaired by modifying a public release.

## Checked and not checked

Run the common/native fixture suite and promotion qualification regressions:

```sh
python3 -m unittest discover -s tests -p 'test_release_publish.py'
python3 -m unittest discover -s tests -p 'test_promotion_qualification.py'
npm run check
```

Synthetic API context and real fixture ZIP/file bytes verify decision edges,
including the actual publisher entrypoint. They are not live protected approval,
native execution, visual acceptance, signing, upload, downloaded draft or public
release proof. Human image inspection and actual successful exact-source
native/final-package runs remain separate prerequisites.
