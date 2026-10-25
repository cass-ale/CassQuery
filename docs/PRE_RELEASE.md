# Pre-release checklist — v0.2.0

This checklist qualifies a release candidate; it does not grant publication
permission. v0.1.0 is published. Create **v0.2.0** only at the exact new source
that passes all workbench, update and native/package gates.

## Source and review

- [ ] `package.json`, npm lock root and app Info.plist agree on `0.2.0`.
- [ ] Real code save, conflict/recovery, parameter edits, BREP transforms and
  selected STEP export/import pass through the production workbench.
- [ ] All eight themes, editor/model panes, shortcuts and settings are rendered
  and exercised; code-input focus never triggers model commands.
- [ ] Actual native Open File/Folder, New, Import, Export, Save a Copy and
  save/discard/cancel paths pass, with no simulated chooser credited as OS proof.
- [ ] GitHub update checks use the actual default client and published checksums.
- [ ] Native custom update install/rollback verifies seals, fresh app/worker
  acknowledgement, failed-child reaping and preserved settings/projects/drafts.
- [ ] Final source is committed, clean and independently reviewed at a full SHA.
- [ ] Pre-push checklist is complete; all required tests run with zero skips.
- [ ] Live integration and mutation witnesses pass against the exact source.
- [ ] Both native candidate jobs correspond to that **same trusted main-push SHA**;
  every required step—not just overall run status—succeeds.

## Artifact and native proof

- [ ] Candidate manifests bind source fingerprint, run/attempt/version/architecture,
  stage logs, reports, screenshots, ZIP/DMG assets and supply-chain inventory.
- [ ] Strict native Python hashes and runtime availability pass for ARM64 and x64.
- [ ] Frozen worker runs the real eight CAD gates, live save loop and failure retention.
- [ ] Native default bundled launch succeeds without development PATH/project overrides.
- [ ] Production close/reopen/single-instance/quit behavior succeeds, with no orphan workers.
- [ ] Dark/light × four realized widths × assembled/exploded states have nonzero
  checked coverage, no clipped geometry/text, and complete scroll/control captures.
- [ ] Post-save and failed-gate renders prove the displayed geometry and camera are correct.
- [ ] Independently inspect every required frame; nonempty PNG files do not approve visuals.
- [ ] Bundle identity, every native architecture, resources/icon/source hashes and seal
  agree before/after tests. Explicit CI SwiftShader selection is recorded; passing
  one host/backend is not evidence for every GPU/display configuration.
- [ ] **Final ZIP** extracts/copies/launches correctly with exact byte and mode parity.
- [ ] **Final DMG** verifies, mounts read-only, launches mounted/copied, detaches and
  cleans up before committing success. No sandbox/mount/permission bypasses.

## Distribution and approval

- [ ] Test a quarantined browser-download/Finder first launch on a clean account,
  without dev Python/Node, and on the advertised macOS 14 floor.
- [ ] Decide distribution/signing. Ad-hoc candidates require explicit opt-in and do
  not establish Developer ID, hardened runtime, notarization or Gatekeeper acceptance.
- [ ] Developer ID releases require a separate verified signing/notarization/stapling
  contract; no unsigned fallback. Current promotion does not claim that capability.
- [ ] GitHub environment **`releases`** allows only **`v*` tags** and has required
  reviewers. Approval must come from a currently eligible reviewer; self-review
  follows the owner-configured policy, matching FrameGuard. The agent never submits
  deployment approval or bypasses the protected human gate.
- [ ] Existing annotated `v0.2.0` tag resolves to the exact validated source SHA.
  Recheck tag object and target immediately before every promotion/publication boundary.
- [ ] Owner explicitly dispatches the release workflow from that version tag,
  chooses candidate run and distribution, and approves the protected gate.
- [ ] Default mode is **draft**, not publish. Actual publication requires separate
  explicit selection/authorization; source pushes do not trigger it.

## Final bytes and publication

- [ ] Promote exact checked candidate ZIP/DMG bytes—do not rebuild during promotion.
- [ ] Verify expected inventory, candidate manifests and SHA256SUMS; reject missing,
  extra, expired, stale or changed artifacts.
- [ ] Create/resume only a matching draft. Never modify a published release or
  overwrite existing draft assets with different bytes.
- [ ] Re-download every draft asset and compare its exact hash before publication.
- [ ] Revalidate source, tag, environment, approval history and candidate attempt.
- [ ] Publish only after all gates pass. Failures stop with the draft unpublished.
- [ ] Check public downloadable hashes and preserve the complete evidence ledger.

## Executable local validation

```sh
npm run preflight
python tools/readiness.py pre-release --run-json /path/to/exact-run.json --jobs-json /path/to/all-jobs.json
```

This verifies source/run checklist inputs only. Package hashes, native launch/image
approval, signing decisions and protected release authorization remain separate
mandatory gates; the command never creates a tag or publishes anything.

## Remaining explicit gaps

GitHub/SLSA provenance attestation, Developer ID/notarization, minimum-OS/quarantined
device checks and unavailable Hallmark lint must not be inferred from other checks.
The custom GitHub updater's HTTPS/checksum trust is not an independent publisher
signature; its native mechanism proof is separate from actual transport checks. `docs/RELEASE_ASSURANCE.md` records capabilities;
`docs/RELEASE.md` defines the protected promotion path; `docs/TESTING.md` explains test
levels. No process honestly guarantees 100% first-attempt release success.
