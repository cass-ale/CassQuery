# Release-assurance comparison and implementation

CassQuery's release qualification separates source tests, real integration, native
execution, final package bytes, human visual approval and publication authority.
Implementation and synthetic regressions are not evidence that a new native build
or release succeeded.

## Source-to-publication contract

1. **Source freshness.** Exact committed SHA, source bytes/executable modes,
   before/after fingerprint and hashed stage logs bind preflight to the source.
   Dirty/stale source, failures, skips, missing stages or changed linked evidence
   reject qualification. A failing preflight must replace stale success evidence.
2. **Executed source and native gates.** Workflow contracts check reviewed commands,
   checkout, permissions and skip/error handling. Publication reads actual
   attempt-scoped native jobs and steps, not just overall run success. The common
   verifier binds complete inventories and each job's run ID/source SHA; supplied
   attempt/branch metadata must also agree. Readiness uses the same binding.
3. **Separate integration dependency.** The newest exact-source trusted-main push of
   `integration.yml` must succeed, including source context/dependencies, actual
   browser loop and mutation witness, independent report validation and retained
   evidence. Discovery does not filter for success: a newer failure or failed
   latest rerun cannot select an older green. Run/job inventories are bounded,
   complete and source-bound. The selected integration identity is recorded in the
   draft and rechecked alongside native jobs before every contents write.
4. **Original candidate evidence and bytes.** Per-architecture manifests bind source,
   version, run/attempt, complete linked reports/logs, screenshots, final ZIP/DMG and
   supply-chain inventory. The shared schema rejects incomplete or contradictory
   native/lifecycle/package evidence, changed bytes, wrong architecture/source,
   unsafe paths and missing/duplicate images. This validates evidence consistency,
   not human acceptance of the rendered frames.
5. **Distinct native/package levels.** Frozen worker real CAD/watcher/failure-retention,
   default bundled launch, native production controls/lifecycle, complete realized
   theme/size/state images, and final ZIP/DMG launches are separate required gates.
   Package inventories must agree with the checked prepackaged bundle. Read-only
   mounted/copied DMG launches require successful detachment and cleanup before
   success. No sandbox or host restriction is bypassed.
6. **Supply-chain identity.** Native dependency locks bind source graph, architecture
   compatibility and downloaded wheel hashes. CycloneDX inventory is compared to
   source-bound Python/npm lock contents, not treated as a nonempty SBOM label.
7. **Protected unchanged-byte promotion.** Explicit owner dispatch from the existing
   annotated source/version tag promotes the checked ZIP/DMG and original manifests
   without rebuilding, signing or recompression. Only the post-approval publication
   job receives contents write. Draft assets are fully re-downloaded and hashed;
   unknown/changed assets, API failures, stale source/run/attempt, tag movement or
   ineligible approval block publication. Published releases are never modified.

## Owner policy: FrameGuard comparison

The owner-selected GitHub policy is the same solo-maintainer approval model as
FrameGuard: configured required reviewer **cass-ale**, `prevent_self_review: false`,
custom deployment policies and version-tag dispatch. CassQuery restricts its
`releases` environment to exactly the `v*` tag rule. A configured owner may approve
when the explicit policy allows self-review; prevention of self-review is respected
when enabled. Requiring an unrelated second human was not the owner's policy.

The human must still approve the actual protected environment job. Chat release
permission does not supply that GitHub approval, and an agent never calls approval
or bypass APIs. Publication binds the approving user's immutable ID to a currently
required user or verified current required-team membership. A non-owner login alone,
a stale removed reviewer, unreadable membership or admin bypass does not establish
eligibility. Current owner-user approval requires no team credential.

The comparison does not copy FrameGuard's release rebuild, clobber reconciliation
or name/size-only draft checks into CassQuery's unchanged-candidate-byte contract.
Its deliberate zero-budget ad-hoc/non-notarized policy also does not imply Developer
ID, hardened runtime or Gatekeeper acceptance here. `allow_ad_hoc` remains an
explicit workflow opt-in, false by default; unsupported signing evidence fails.

## Remaining genuine gates and limitations

- Final source must be frozen and reviewed, and every required source/native/final
  package gate must run against that exact source. A prior run or diagnostic upload
  is not evidence for the repaired source.
- Inspect all required native frames. Synthetic screenshots, valid PNG structure,
  report hashes and aggregate CI success do not perform human visual approval.
- Actual protected approval, GitHub draft uploads/readback and public downloadable
  checksums must be verified when the owner executes the release. Synthetic REST
  fixtures are decision-edge tests, not live publication results.
- A normal partial upload resumes only missing files after rehashing existing assets.
  GitHub's documented 502 can leave an empty `starter` asset; this stops safely and
  needs owner inspection/explicitly authorized removal before fresh dispatch and
  approval. No silent delete, overwrite or automatic mutating retry is provided.
- GitHub can require Workflows write for release targets differing from current
  default-branch workflow files. Check actual target/default-branch state rather
  than treating every version pin as a permission failure or adding broad tokens.
  An existing tag prevents this publisher from creating a tag; API denials still
  fail closed. `GITHUB_TOKEN` cannot gain unsupported Workflows write through YAML.
- A source/byte-bound manifest is not a signed SLSA/GitHub provenance attestation.
  Developer ID/notarization/stapling, authenticated updates, clean-account
  quarantined Finder launch, advertised minimum-OS device checks and unavailable
  Hallmark lint remain separate capabilities/gaps, never inferred from other gates.
- Revalidation is not an atomic GitHub transaction. Do not mutate reviewed tags,
  assets or protection concurrently with promotion.

## Primary comparison evidence

FrameGuard workflow source at `f0bed77e774c534deb7d5f64e38c9ca560022351` requires
exact-SHA source Tests plus native Candidate before release. Its live environment
was read to verify owner-reviewer/self-review policy rather than inventing an
additional reviewer requirement. Its workflow states the deliberate ad-hoc policy.

A working exact-artifact publication pattern also exists in
[uv's executed workflow](https://github.com/astral-sh/uv/blob/7456b60bc6bc348e13e06e3691bf08e78d3aef54/.github/workflows/release.yml#L367-L433):
explicit artifact IDs select assembled archives, which are downloaded and uploaded
in the publication job without rebuilding them there.
[Release run 39441647119](https://github.com/astral-sh/uv/actions/runs/39441647119)
completed `publish-github` successfully. This is source/execution comparison, not
an independent re-download claim about upstream release-byte parity or a reason to
copy its credential service.

GitHub primary documentation defines
[environment protection](https://docs.github.com/en/rest/deployments/environments),
[approval history](https://docs.github.com/en/rest/actions/workflow-runs#get-the-review-history-for-a-workflow-run),
[artifact downloads](https://docs.github.com/en/rest/actions/artifacts#download-an-artifact),
[release upload/recovery](https://docs.github.com/en/rest/releases/assets#upload-a-release-asset)
and [release-target permissions](https://docs.github.com/en/rest/releases/releases#create-a-release).
The executable release contract and checked/not-checked distinctions are maintained
in `docs/RELEASE.md`, `docs/PRE_RELEASE.md`, `docs/AUDIT.md` and
`docs/MACOS_RELEASE.md`.
