# Release-assurance comparison and implementation

CassQuery originally had native candidate builds but not FrameGuard's full
source-to-publication assurance chain. This pass closes concrete control gaps;
it does not authorize publishing a release or assert that unexecuted native gates
are green.

## Source-backed gaps and fixes

1. **Checks were not bound to source freshness.** FrameGuard fingerprints source,
   tools, logs and evidence. CassQuery now records exact committed source SHA,
   file bytes/modes, before/after fingerprint, tool-stage logs and their hashes.
   Candidate preflight rejects dirty source, failures, skips, missing stages and
   source changes—even cleanliness-only changes. An early rejection overwrites a
   previous success report rather than leaving stale evidence.
2. **Workflow text could pass without executing a gate.** Both projects' text-only
   contracts had false-greens. CassQuery now checks exact gate commands, source
   checkout SHA, permissions and whole-job conditions. Tests replace commands
   with no-ops, remove architecture coverage and inject skips. Publication also
   reads the actual exact-run jobs/steps; successful workflow status alone is not
   sufficient.
3. **Candidate hashes were not linked to checked packages.** Each architecture
   now stages a portable `candidate.json` binding source/run/attempt/version,
   all mandatory gate reports and logs, linked native reports, screenshot hashes,
   ZIP/DMG bytes and the SBOM. Verification rejects unlisted files, traversal,
   symlinks, same-size corruption, failed nested reports, wrong architectures,
   source mismatch and incomplete/duplicate image or matrix coverage.
4. **Native coverage was shallow and ZIPs were unchecked.** The native smoke now
   proves bundled default resources with no development PATH, checks every native
   binary architecture, icon/source/version parity and immutable bundle bytes.
   It exercises real input and production quit/close/reopen/single-instance paths,
   dark/light × four actual widths × two steps, scrolled controls and post-save/
   failure captures. Final ZIP extraction/copy and read-only DMG mounted/copy
   launches are separate gates. DMG success is written only after detachment and
   cleanup, never while the image remains mounted.
5. **Supply-chain pins were versions, not byte identities.** The Python graph is
   now hash-locked to 73 PyPI-verified wheels covering 51 packages on both native
   architectures. Preflight validates exact graph/hash-set equality and wheel
   compatibility. Both architecture hash-checked downloads were exercised. The
   mandatory CycloneDX inventory records actual Python/npm names, versions and
   hashes/integrities; verification compares them to the source-bound lock files,
   not merely a nonempty SBOM label.
6. **Publication was only a document.** `release.yml` and `release_publish.py`
   implement explicit owner-dispatched promotion of the exact verified candidate
   bytes—no rebuild. The gate requires a matching existing annotated version tag,
   trusted exact-SHA main-push candidates with every native step passing, a
   configured protected `releases` environment limited to `v*` tags, independent required
   review and prevention of self-review. Permission to write contents is isolated
   after approval. Draft inventory is downloaded and hashed before optional
   publication; tag, source, environment and approval are rechecked before writes.
   First-release 404 is distinct from authorization/API failures. Existing public
   releases cannot be modified, and version changes must be monotonic.

## What is deliberately not copied from FrameGuard

FrameGuard's observed macOS candidates were ad-hoc/notarization skipped. Its full
local profile is not a package build; its release workflow rebuilds candidate
bytes, checks draft inventory names/sizes rather than redownloaded hashes, and
its signed updater manifests remain deferred. CassQuery explicitly separates
these capabilities. It does not claim ad-hoc seal checks establish Developer ID,
Gatekeeper or authenticated automatic updates.

## Remaining human/machine gates

- The configured environment is **`releases`**, restricted to **`v*` tags**.
  It currently has no required reviewers. The publication gate validates the exact
  annotated version tag, candidate SHA and configured tag policy, and refuses
  publication until required reviewers and prevent-self-review are present. Nothing
  here creates or weakens repository settings; approval configuration remains an
  owner-managed GitHub setting.
- **No publication is authorized or executed.** Default dispatch mode is draft;
  even draft writes require explicit workflow execution after protection checks.
  Ad-hoc distribution requires an explicit opt-in. Developer ID promotion is
  blocked until a separately verified signing/notarization contract exists.
- **No provenance attestation claim:** a source/byte-bound manifest is not a signed
  SLSA/GitHub attestation. Attestation remains explicit future work rather than a
  silently optional step presented as implemented.
- The stronger native matrix/lifecycle/final ZIP/DMG chain must run on the exact
  new source commit. Local Electron/hdiutil execution remains sandbox-blocked;
  no restriction is bypassed or substituted with browser fixture evidence.
- Clean-account quarantined Finder launch, the advertised macOS 14 device floor,
  notarization/Gatekeeper and required Hallmark lint remain unverified.

## Primary comparison evidence

FrameGuard source `f0bed77e774c534deb7d5f64e38c9ca560022351`; upstream
`0c4d0c350523c95cbcebeaed376ab2574640f7b8` differs only in AGENTS/README.
Its raw workflows and gate implementations, real failed Actions logs and negative
control probes grounded this comparison. The earlier CassQuery candidate
[40122566011](https://github.com/cass-ale/CassQuery/actions/runs/40122566011)
is evidence for its older source, not the expanded assurance chain. Every delivery
must distinguish executed proof from authored gates.
