# Workbench assurance

This is the proof contract for the additive behavior in [WORKBENCH.md](WORKBENCH.md), not a report that v0.2 has passed. Preserve all existing preflight, eight certified CAD gates, source/frozen-worker, native cold/warm lifecycle, 16-cell render matrix, observed graphics backend, ZIP and DMG gates. New code or a passing browser bridge fixture cannot satisfy native editing, OS-dialog or update installation proof.

## Independent proof categories

- **Source workbench:** `tools/workbench_suite.cjs` uses real public worker/CAD/file bytes, production stores, native-like DOM input and a simulated desktop chooser. Its declared platform is `linux-source-browser-workbench`; `native_dialog_selection` remains `not checked`. Its `validateReport` / `--validate-report` admission must verify linked files/images, real transform/export/import geometry, source closure and the exact-source legacy baseline. Never relabel it as packaged native or OS-dialog selection.
- **Packaged editor/IPC:** the actual Electron app/preload and frozen worker, native input, current-window/origin authorization and the capability boundary. A startup-owned public fixture can prove editor → IPC → worker → disk/CAD. It does not prove a File/Folder/Save panel.
- **Native dialog selection:** actual menu/button → Electron/OS panel → observed select/cancel/overwrite decision → actual returned public path/output. Injected dialog providers and `app.emit('open-file')` are not native dialog proof. Without permitted automation or independent manual selection, report this gate blocked before submission; do not substitute or skip it.
- **Update client:** default Node fetch against the exact GitHub release/checksum/asset hosts; real version/architecture/hash/size and failure handling. A protocol metadata fixture or curl download is not default-client proof.
- **Update install/rollback:** `tools/update_smoke.py` runs the real custom helper on separately resealed app copies in an owned temporary installation. This is a fixture mechanism gate, not future GitHub release migration, Squirrel, Developer ID, or UI consent/dialog proof.

All required categories must be digest/source-bound into candidate qualification. A source-only, scoped or `qualifies=false` report must not be accepted as a complete native workbench gate.

## Causal action evidence

For each mutating action, prove:

**real input → current-window IPC → capability worker request → physical CAS/atomic source or manifest bytes → newly certified BREP/source snapshot → physical exported/reimported geometry → checked rendered frame.**

Use actual producer fields from WORKBENCH action responses and the runner, not invented expected labels. Preserve source/run/version/architecture, app/worker PIDs, launch nonce and ordered wall/monotonic checkpoints. Include new preload/desktop/updater/install and Python workbench/CAD modules in packed/source parity and executed source closure; the old four-shell-file trace is insufficient.

Mandatory checks and negative controls:

- **Open/create/import:** correct actual project/entry/files, dynamic registration/watcher, original input bytes unchanged; cancellation, existing directory and readonly bundled-resource writes do not create/overwrite files.
- **Save/conflict:** native typing and dirty state; actual returned SHA equals physical saved bytes; stale `expected_sha256` returns409, preserves external disk bytes and draft. Reload/keep draft/Save As outcomes are independently visible/persisted. AutoBuild-disabled save does not silently export a stale certificate.
- **Failure retention:** actual syntax/geometry/units failure produces no new certificate or export; prior certificate/hash/display remains with diagnostics. Recovery requires a new successful certification, not a label reset.
- **Transforms/export:** actual persisted manifest values and neutral/pose BREP changes before nominal-mm truth. Validate rotation/translation and scale cubed volume, reimport validity/extents/units and selected variant. Export uses retained certified source shapes: a real build-call counter must not increment merely to export. Dirty/stale/failed source rejects with no output. Camera/hide/X-ray/exploded display must not change exported geometry.
- **Settings/themes/shortcuts:** physical stores survive a fresh process/reload; actual editor/inspector frames for all eight declared themes, focus/errors and responsive layouts alongside the old native matrix. Keys inside text input edit text rather than move the camera; shortcuts outside input invoke actual actions.
- **Draft/close/crash:** production Save/Discard/Cancel paths preserve the intended actual bytes; failed save cannot close/restart/install. Actual owned crash and fresh-process recovery must link persisted draft/source hashes; same-renderer memory is not crash recovery.

Replay actual outputs after changing physical files and rehashing all record links. Reject missing identity/frames/files, stale CAS or certificate, renderer-only transforms, wrong variants, zero geometry, skipped callbacks, same-process persistence, fake native dialogs, and hidden failed install/rollback. Compare full normalized image inventories without losing duplicates or relying on JSON object key order. Never reorder semantic actions/checkpoints to make them pass.

## Native update fixture gate

Invocation on the normal macOS CI runner:

```
python tools/update_smoke.py --app "$APP" --arch "$ARCH" --output "evidence/update-$ARCH"
python tools/update_smoke.py --validate-report "evidence/update-$ARCH/update.json"
```

The required pipeline step is **Native update installation and rollback**. The primary report is `update.json`, schema1, kind `native-update-fixture`. It must have real native execution, explicit `fixture_only`, source-before/after fingerprint binding, original and fixture versions, and checked `install` and `rollback` cases. Missing native execution has no simulated-success fallback.

The driver:

1. Requires an exact clean source and a source-parity/signature-checked built app. Measures actual app inventory and temporary disk headroom before making independent copies; no hardlink fallback. The supplied app is never installed/replaced and is checked unchanged afterward.
2. Creates a distinct fixture version by changing actual Info.plist versions and ASAR package version/header integrity, then freshly ad-hoc signs the owned clone while preserving signing metadata. This describes a genuinely different fixture app, not a relabeled current/future release. The failure fixture has a declared nonexistent ASAR main entry and a valid native/signing profile.
3. Runs the packed/current install helper with its default real inspection, wait, rename and launch operations. No fake `launch`, `isAlive` or inspection callbacks are used. The helper is executed through the actual app's RunAsNode-enabled binary and signals readiness before the old fixture exits.
4. Uses the opt-in native `quit-<pid>.json` nonce control solely to invoke production `app.quit`/owned worker shutdown. This lab control is not a user-consent or OS-dialog test.
5. Requires unique actual `ready-<pid>.json` and PNG records for old and new/restored apps: exact source, version, architecture, app/worker PIDs, native code tuple, public TESTPLATE certificate/eight gates and live observed WebGL. Bare PID, module import or timeout alone cannot acknowledge health.
6. Binds actual helper `ack.json` or `rollback-ack.json`, result and launch records to the fresh install nonce, app/version/architecture/PID and app inventory/seal. A rollback requires failed new app/group reaping **before** restored-app readiness and matching restored PID/ACK; restoring backup bytes while children remain live is not success.
7. Preserves physical before/after bytes of seven owned public fixture files: workbench settings, recovery draft, userData sentinel and both project/recovery source/manifest pairs. This deliberately excludes volatile Chromium cache/database bookkeeping and the updater's correctly changing control state. Prepared fixture recovery data is not proof that the UI created that draft; the native workbench gate covers that separately.
8. Reaps only sessions created by this driver or nonce-bound helper. It refuses reused/unrelated PIDs and retains scratch on cleanup uncertainty. Scratch is not removed before process cleanup. Private install tokens are fingerprinted recursively in published receipts; the workbench capability is never exposed.

`validate_report(report, directory, expected_sha=..., expected_arch=..., expected_version=...)` verifies linked records and actual ready/ACK/result/source/data/frame relationships. File byte/hash checks, realized viewport coverage, fresh process identity and strict rollback flags remain mandatory. Fixture report success does not authorize a tag or publication.

## Qualification ledger

The new source-level validator tests use explicit protocol-only physical fixtures and negative controls. They are not evidence of an actual Mac install, native dialog or default updater network success. Actual packaged workbench, OS-dialog selection, default Node client and install/rollback execution still require their real artifacts on the normal Mac CI host and independent review.

Do not claim standard macOS automatic update signing prerequisites for the ad-hoc channel. If the install/rollback or user-facing consent/download path cannot be qualified, disclose explicit download/reveal/installer handoff only and leave automatic installation blocked. v0.2.0/tagv0.2.0 requires all exact-source candidate gates and explicit owner authorization; no older passing app, future-version model or source-only bridge substitutes for that proof.
