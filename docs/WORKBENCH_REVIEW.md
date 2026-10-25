# v0.2 workbench independent review

## Verdict and scope

**Named source scopes are approved; complete v0.2 qualification remains pending.**
The reviewed implementation preserves the original eight gates, thresholds, units,
preview/certified ordering and trusted-local-Python model. Private SPEC.md is
unavailable; no private protocol revision is invented. Source tests, protocol
fixtures, a source browser loop, native dialogs, frozen runtime, update installs
and final packages are separate evidence categories.

This reviewer owns only this document, tests/test_workbench_security.py and private
bounded scratch controls. No implementation files, installed app, GUI/native actors,
commits, pushes, tags, releases or GitHub writes were performed by this reviewer.

## Exact source clearances

- Backend worker: `bfe5524f18a32e2f8d99cd0d59b7669793d20c366561bde7c1dc8e2c31733987`.
- Workbench backend: `d0f1ad4b77de8f32d0c469e72ce14a46c77eeef0d163530ea0bfee9589cfbef0`.
- CAD I/O: `bbec1f73605e6ffede72bb8296e0e899a82c1b66e69125c57f820e56cb2ddd69`.
- Main: `99a3956a3d73bdcb1723130b8deab82746d6ee6a698f745f5c4f5e2b65ce7e66`.
- Desktop broker: `b23db29083f3a773f70e241cb03faaa1006ff6b675f0ad1f712c94a32558f125`.
- Preload: `65db411065bd786a182a7d9f3f540f66d2cded976e148648a915f559375ad953`.
- Editor: `1b098aefeb82e3752f1e8472270d70d155ae60f193ee3b5d7773d293ea214441`.
- Editor controller: `3f1cbde890cf3d72818732404bc93b81d2a0b09737a0d27b755b36099a982964`.
- Viewer: `c0162ea604730b1176a435bb27226636bb7d592bb0960ddb9f1d1840018c1ba2`.
- Themes: `f8dd514b4bfc276b0c63796701c14aba711df13aba2c92da2cd3bfa7db480db0`.
- Updater: `87578c17ab94e8f3977187fd2b25ff642e329ebd1debf04fc8cb5fb337a489bf`.
- Update helper: `17add5fd754c177b85b346f7f9b2a05c58ecc3f913fdb6c381b4a29a4b535d96`.
- Network-contract CLI: `dfab25ce8f10813bd5ee64c5346c43a9ef8ca0c758c149e258149f3205464e81`.

These are source clearances, not whole-tree or final-release fingerprints. Changed
bytes require fresh review/proof; an older successful actor is not rebadged with a
new module hash. Final native-update evidence-consumer source approval awaits its
owner freeze after the independently verified binding repairs.

## Confirmed defects and independent repair checks

### Revision preservation and interrupted saves

A final hash check followed by replacement lost an external revision committed
between those operations. Two cooperating workbench writers already serialized
correctly; that positive did not prove atomicity against independent writers.
The repaired descriptor-relative Darwin exchange captures the actual displaced
entry. Conflicts preserve that revision and the draft, report any active-file
change truthfully and block build/export until explicit resolution. No blind
restore races another revision, and unsupported exchange returns 501 without a
replace-only fallback.

An I/O failure after exchange initially returned an ordinary error without marking
the changed project conflicted. The repeated failure now returns typed unresolved
metadata. A prepared journal blocks fresh Project/registration/automatic execution
and queueing; explicit current-revision resolution removes the journal while
retaining older recovery bytes. Malformed journals enter manual blocked recovery.
Eight independent security regressions pass without skips.

This is qualified revision preservation, not universal atomic CAS against arbitrary
uncoordinated/open-inode writers or a hardware power-loss guarantee.

### Native authority, cancellation and recovery transport

The initial dirty-export check did not cover edits made while a native Save dialog
was pending. The repeated same-window dirty transition now causes zero HTTP
mutation; window/frame/origin/epoch and dirty-revision checks remain restrictive.
Save/Discard acknowledgements require a current decision and successful save
receipt; a bare dirty-false cannot make another close see an unsaved project clean.

Actual worker conflicts use flat JSON metadata, while the initial broker accepted
only nested test-fixture details. Real producer-shaped errors now preserve bounded
validated source/output recovery records. Metadata never grants arbitrary file
access. Output paths must match the native-selected destination and recovery files
must be its siblings. Known macOS /var versus /private/var aliases are normalized
on native-picked input only, without following the output leaf or accepting a
renderer-chosen destination.

Moving the initialized settings/recovery root and replacing it with a symlink
initially redirected a normal settings patch outside storage. The exact retarget
now rejects and preserves both moved-original and external bytes. Captured root
identity checks precede reads/writes/commit; cleanup does not follow the retarget.
All current desktop tests and the unchanged veto/late-result/cleanup lifecycle
negatives pass. Dialog doubles are not OS-panel proof.

### Source/geometry/export truth

A valid empty source helper certified through all eight gates but project ZIP and
Python-source export rejected its zero-byte record. The repeated real CAD export
now includes exact empty bytes and their SHA/size provenance. Empty STEP/binary
geometry and invalid BREP remain rejected.

The separate export-output replacement path initially lost a concurrent external
file despite correct STEP output/hash and 16 passing tests. It now uses actual
Darwin exchange: external writes before/after exchange and post-syscall faults
retain the displaced revision and an independent prepared STEP copy with typed
reconciliation metadata. New-output creation remains create-only. The source
project is not incorrectly marked conflicted by an output-only failure.

Post-mesh CadQuery BoundingBox used attached triangulation padding as nominal
truth. The exact BREP helper uses AddOptimal with both triangulation and shape-
tolerance flags false. Independently measured public Base bounds stay
[-20,-15,0,20,15,6] before/after coarse meshing while the default box expands.
Nineteen real CAD regressions pass without widened gate/roundtrip tolerances.

A late watcher baseline event also invalidated a nonlive project just manually
certified from those exact bytes. The repeated same-input event now preserves its
generation/current certificate; genuinely different bytes still stale it, advance
generation and prevent exporting the retained certificate as current.

### Editor drafts and source-directed controls

Recovery records are keyed by canonical root/project/relative file. Both known-
hash objects and valid null/unknown-metadata records are selectable through the
controller, with honest unavailable labels and read-only comparison; no hash is
invented and a known stale hash still rejects. No restoration automatically saves
or executes a draft.

Independent controller checks preserve newer edits across save, conflict resolution,
parameter Apply and transform receipt reads. Parameter Apply initially overwrote a
manual draft entered during its IPC await and marked it clean; the repeated
actual generated Apply callback now keeps the newer text dirty while updating
only the saved baseline. Recovery save/delete queues fence document identity and
revision; pending model reads cannot resurrect discarded JSON after clean ACK.
Output conflicts do not mark unrelated Python source conflicted. The actual run4
workbench failure also exposed a clean-transition observer dead end: editing a
restored draft back to disk text cleared dirty state but left inspection null with
an unchanged certificate key, disabling transform Apply. Pinned run4-source and
latest-source controls prove the repair refetches inspection once all drafts are
clean; genuinely dirty/stale source remains blocked. All 33 current editor/
controller regressions pass. Actual source-browser rerun remains required; method/
DOM seams do not prove native typing or panels.

### Capability and update boundaries

Missing/wrong/duplicate capability headers reject before workbench dispatch. The
renderer receives no private token/general fs/exec/dependency-install API. A build
error initially repeated its capability through owned public diagnostics; status,
history and broadcast now redact it while preserving useful errors. Trusted Python
can read its environment; this is not a sandbox/exfiltration-prevention claim.

The updater's final asynchronous install guard initially crossed the aggregate
deadline yet called quit. The original controlled healthy/expired cases now pass:
expired guard causes error, helper stop and zero quit; the healthy guard proceeds.
All 28 updater tests pass. The parser also accepts both actual existing qualified
v0.1 package archives with exact digests, CRC/metadata and ASCII names; that is
parser compatibility, not a v0.2 install or future-release qualification.

The new native-update consumer initially admitted four physical rehashed bad
protocol fixtures: missing initial/restarted frames, wrong self-consistent initial
source and reused attempt fingerprints. The repeated controls now reject; the
proper healthy model and nine consumer tests pass. Source/initial/new frame/PID/
request/inspection/ACK relationships must be checked on actual CI artifacts.
Protocol models never count as native execution.

## Checked and not checked

**Checked:** exact source reads; original counterexamples and healthy controls;
eight owned security regressions; real kernel/CAD output and before/after bounds;
current desktop/editor/updater tests; physical protocol-consumer negatives. The
actual Linux legacy browser/worker loop at worker1cc60/workbenchd0f/CAD702 passed
four scenarios/22checks, isolation/recovery and a Host/Origin mutation negative.
Its nine physical frame hashes match and all nine frames were individually
inspected. That actor is explicitly historical source-browser proof, not current
bounds-code/native editor/update proof.

**Not checked/qualified:** complete current-source browser workbench loop and all
eight rendered editor themes; actual macOS File/Folder/Save/Cancel panels; actual
owned crash/recovery; frozen current worker/module parity; default Node network
success on the normal Mac runner; actual packaged update/install/rollback and
user-data preservation; both-architecture native cold/warm/16-cell/ZIP/DMG
candidates and same-SHA integration. Host Node network refusals remain refusals;
no TLS/proxy/permission bypass or curl-as-default-client substitution is permitted.
Ad-hoc/non-hardened signing does not establish Developer ID/notarization/Gatekeeper,
quarantined clean-device first launch or minimum-OS hardware/runtime acceptance.

## Doneness

Final qualification requires the real input → current-window IPC → capability
worker → physical revision/source → current certified BREP → exported/reimported
bytes → independently checked frames chain. Keep every confirmed failure red until
its original control rejects and its real positive still works. Bind final source,
run/attempt/version/architecture, complete inventories and required new/existing
gates. Tags/publication additionally require owner authorization and protected
human approval. No older v0.1 app, source-only bridge, future-version fixture or
unit count substitutes for those gates.
