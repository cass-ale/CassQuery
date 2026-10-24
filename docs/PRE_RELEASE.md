# Pre-release checklist — v0.1.0

This checklist qualifies a release candidate. It does **not** grant permission to
publish or create a version tag. The first public version is **0.1.0**, tagged
**v0.1.0** after candidate validation and explicit owner authorization.

## Source and review

- [ ] `package.json`, npm lock root and app Info.plist agree on `0.1.0`.
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
  agree before/after tests. Explicit CI Metal selection is recorded; passing one
  host/backend is not evidence for every GPU/display configuration.
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
- [ ] GitHub environment **`releases`** allows only **`v*` tags**, has required
  independent reviewers and prevents self-review. A tag rule alone is not approval.
- [ ] Existing annotated `v0.1.0` tag resolves to the exact validated source SHA.
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

GitHub/SLSA provenance attestation, Developer ID/notarization integration, authenticated
automatic updates, minimum-OS/quarantined device checks and unavailable Hallmark lint
must not be inferred from other checks. `docs/RELEASE_ASSURANCE.md` records capabilities;
`docs/RELEASE.md` defines the protected promotion path; `docs/TESTING.md` explains test
levels. No process honestly guarantees 100% first-attempt release success.
