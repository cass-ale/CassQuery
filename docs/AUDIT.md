# CassQuery audit and delivery ledger

This ledger distinguishes implemented capabilities, executed evidence and release
approval. The owner authorized source pushes and subsequently authorized a tagged
**v0.1.0** release after the repaired candidate passes. That authorization does not
approve a failed build or allow the agent to bypass the protected human gate.

## Released v0.1.0 and current work

[v0.1.0](https://github.com/cass-ale/CassQuery/releases/tag/v0.1.0) was published
from `33c2d35f817ced4a3e85a54ed6ce148ce7030d62`. Native candidate
[40152969599](https://github.com/cass-ale/CassQuery/actions/runs/40152969599)
and integration [40152969663](https://github.com/cass-ale/CassQuery/actions/runs/40152969663)
passed on the same source. Publication
[40162195548](https://github.com/cass-ale/CassQuery/actions/runs/40162195548)
passed after the configured human approval. Downloaded ARM64/Intel candidates,
all 390 declared files per architecture, final package bytes and 630 new native
frames were independently verified. All four public ZIP/DMG assets were then
re-downloaded and matched to their qualified SHA-256s.

The qualified distribution is ad-hoc/non-hardened, not notarized. It does not
claim macOS 14 device execution, quarantined Finder launch, hardware-Metal
qualification, Developer ID identity or Gatekeeper acceptance. Local sandbox
restrictions below remain real; native/package proof came from separate CI,
not a local permission bypass. Hallmark lint remains unavailable.

v0.2.0 implementation is in progress under [WORKBENCH.md](WORKBENCH.md) and
[WORKBENCH_PRODUCT.md](WORKBENCH_PRODUCT.md). New editor/import-export/update
behavior must earn new exact-source production and native evidence; v0.1's
successful release does not qualify changed v0.2 source.

## Current v0.2 source evidence

The current implementation's source workflow was exercised on a coherent
uncommitted development copy. Same-source legacy integration passed four
scenarios/22 checks including its broken-boundary witness. The workbench passed
three scenarios/11 checks: capability/opening, typed source save and geometry
change, external conflict, syntax retention, inert draft restoration, persistent
BREP transform, selected STEP export/import, eight themes, settings and shortcuts.
Reports and inspected frames remain outside git. This source browser run uses
production worker, filesystem and broker code with a simulated native chooser;
it is not native OS-dialog or update-install proof.

Focused source and independent reviews repaired actual save/overwrite races,
recovery metadata/queue and late-edit loss, stale watcher reconciliation,
mesh-contaminated BREP bounds, scale-input validation and project-selection races.
A new frozen Python worker passes actual eight-gate/save/failure retention and
new workbench API/STEP roundtrip tests. Its compiled helper code matches sources;
two fresh application-state starts pass the unchanged 30-second budget. UI changed
after that build, so its embedded viewer is not current final-app evidence.

Final stable-source preflight and exact-source native ARM64/Intel CI are pending.
Native file-dialog actions, default-client GitHub transport, custom install/rollback,
final ZIP/DMG bytes and new frames must pass before a v0.2.0 tag. No native permission
failure is waived, and no old v0.1/development evidence is relabelled as final v0.2.

## Earlier component ledger

The remaining historical entries record the tests and failures at their stated
source revisions. Statements that a replacement run or publication had not yet
executed belong to those earlier snapshots, not the published v0.1 result above.

## Implemented

- Native macOS Electron bundle with its own Python/CadQuery/OCP worker, explicit
  ad-hoc identity, versioned architecture-labelled packaging, and isolated writable
  state. Apple Silicon/Intel candidate matrix, no publication credentials.
- Independent full first-party worker/viewer audits and confirmed fixes: loopback
  Host/Origin/static path validation, bounded/malformed API input rejection,
  paired listener readiness, cancellation/absolute deadlines/TERM-KILL cleanup,
  fresh source imports, stale-generation/manifest suppression, immutable variant
  files and last-certified retention, malformed geometry/wire roundtrip checks.
- Correct uniform CAD scaling, CAD Z-up, creased normals that preserve planes and
  smooth cylinders, and responsive demo framing instead of invalid fixed cameras.
- Counterform identity: vector master/outlined wordmark, app icons/native ICNS/ICO,
  optical sizes, favicons, adaptive README banners/social preview/style guide,
  light/dark UI tokens, keyboard focus/reduced motion/44-pixel touch targets.
- Fail-closed release input/workflow semantics and missing/stale screenshot gates;
  exact action/package pins and a recorded full Python dependency graph.

## Checked

- Source preflight at `4385901`: **242 tests pass, zero skips**. This historical
  result did not execute the native lifecycle and is not proof of the subsequent
  repairs. The merged source must receive a fresh complete preflight and native
  candidate run, with source fingerprints and hashed logs unchanged.
- Real Linux production-path integration: four scenarios and 21 observed
  invariants pass with real CadQuery/OCP, HTTP/WebSocket, save watcher and
  explicitly sandbox-enabled Chromium. Eight required screenshots are hash-bound.
  A deliberately broken HTTP boundary is caught both as a normal-run witness and
  as an exact-suite CLI negative, not an unrelated startup failure. The run exposed
  and repaired real restart-port reuse and stale loading-state defects.
- Real source and frozen-worker subprocesses: all eight CAD gates, occupied HTTP
  and WS pair scanning, actual save -> preview -> certified rebuild, deliberate
  failed units gate retaining the prior certified index and geometry.
- Independent focused native CAD/HTTP/WS/publication review, including 2x extents
  and 8x volumes for valid uniformly scaled Base/Post/Cap shapes.
- Exact vendored THREE normal test: planar-face normal agreement, smooth radial
  cylinder normals, plus renderer integration/disposal/failure tests.
- Real headless Linux Chrome against **public committed TESTPLATE fixture bytes**:
  dark/light at 450/700/1000/1320 pixels, actual DOM step clicks, 16 screenshots,
  zero page exceptions, horizontal overflow, clipped control text or projected
  mesh vertices. Independent reviewer inspected corrected desktop/mobile frames.
- Exact brand export reproduction on both macOS ARM64 and Linux x86_64: all 42
  output bytes match. The checked exporter uses exact rational polygon filling,
  integer supersample resolution and deterministic PNG encoding, with independent
  corruption/no-write tests and native icon decoding.
- Text/state tokens meet WCAG AA 4.5:1 against every defined surface in both themes.
- Native ICNS iconutil decode/pixel roundtrip at ten representations; independent
  small-icon, PNG/ICO/vector/source/corruption tests; deterministic export gates.
- Apple Silicon .app assembly and ad-hoc seal/identity/version/native architecture
  checks. Final icon bytes must match the generated source before delivery.

## Blocked or not checked

- **Native Mac GUI:** Electron aborts before a window with Mach bootstrap rendezvous
  `Permission denied (1100)` in this execution sandbox. No bypass attempted. Browser
  fixture renders are NOT substituted as proof of native packaged launch.
- **Final DMG create/mount/copy:** local `hdiutil create` returns `Operation not
  permitted`. The final-package smoke is authored but not executed here.
- **Intel runtime and GitHub Actions:** the initial candidate run
  [40119847501](https://github.com/cass-ale/CassQuery/actions/runs/40119847501)
  failed on both architectures before dependencies or app tests ran: setup-python
  has no macOS 3.12.13 binary. The repaired workflow pins the provider's stable
  3.12.10 native release, checks availability for both architectures, and records
  nonempty runner diagnostics before setup. A passing replacement CI run remains
  required; verified wheel availability is not an Intel runtime result.
- **Developer ID, notarization, Gatekeeper, clean quarantined Finder launch and
  minimum macOS device:** not verified. Ad-hoc seal integrity does not prove these.
- **Hallmark lint:** required tool could not be found; not replaced with a different
  linter. CSS contrast/layout evidence does not claim the missing tool passed.
- General authored cameras, multi-project native UI, GPU context loss, full branch
  coverage and a complete display-scale/accessibility-device matrix remain outside
  the checked evidence. SPEC.md referenced by AGENTS is absent publicly; gate
  thresholds and the unauthenticated loopback API shape were not invented/changed.
- Automatic updates and authenticated update manifests remain unimplemented.
  Protected exact-byte candidate promotion is implemented and covered by negative
  tests; no public release has yet executed that path.

## Raw release evidence

FrameGuard source `f0bed77e774c534deb7d5f64e38c9ca560022351` and actual Actions logs
established first-release 404 parsing, >100MB Git payload publication failure,
platform-dependent harness assumptions and green-tests-but-rendered-clipping
failures. Its observed macOS build was ad-hoc/notarization skipped. Its text-only
workflow/cache checks had independently reproduced gaps; CassQuery does not copy
those weaknesses. Details and action links: `docs/MACOS_RELEASE.md`.

## Current assurance chain

The stronger source/evidence/package/protected-publication gates are specified in
[RELEASE_ASSURANCE.md](RELEASE_ASSURANCE.md) and [RELEASE.md](RELEASE.md). They bind
clean source snapshots, exact native CI steps, all final package/image/log hashes,
strict wheel locks and SBOM inventories. Do not confuse their implementation and
negative tests with a completed native run or authorization to publish.

The earlier candidate `40122566011` completed Apple Silicon successfully but Intel
failed initializing EGL/GPU before geometry appeared. The follow-up `40127666104`
forced SwiftShader and both architectures failed its Vulkan initialization. The
repair explicitly selects ANGLE Metal, Chromium 156.0.8078.48's primary/default
macOS backend, and records the request; ordinary desktops keep their default path.
Renderer console errors are retained using Electron 45's current event signature
and stop readiness immediately. Same-source Rebuild is proved by a new certified
build timestamp, not a source-generation increment. These are source-reviewed
repairs; successful native Metal execution remains required on the new commit.

The live GitHub environment is `releases`, restricted to tag pattern `v*`, with
owner-managed required reviewer `cass-ale` and self-review prevention disabled,
matching FrameGuard's verified release policy. Publication requires recorded
approval by a currently configured eligible reviewer; the agent never approves
its own deployment. An existing annotated version tag must match the exact passing
native and integration source. No source push creates tags or releases.

## v0.1 source push — release remains on hold

The first-release target is **0.1.0 / v0.1.0**. After reviewing the readiness hold,
the owner explicitly authorized pushing these source changes. That authorization
does not create a version tag, dispatch publication or approve a binary release.
The local ARM64 app builds at 0.1.0 and passes source/icon/version/seal and every
embedded Mach-O architecture check. Its frozen worker passes the real eight CAD
checks, live save/failure retention and runtime/shutdown proof. The ARM64 ZIP builds
and validates CRC/path/inventory. ZIP/DMG native launch proof, Intel execution,
new exact-source CI and independent protected release approval remain required.
Therefore the release is **HOLD / NOT READY**, not a promised first-try success.
The authorized source push enables exact-source Actions validation; local sandbox
restrictions are not bypassed. Complete [PRE_PUSH.md](PRE_PUSH.md),
[PRE_RELEASE.md](PRE_RELEASE.md) and [TESTING.md](TESTING.md), preserving every
checked/not-checked distinction.

## Review boundary

The reviewed source commit is the source of truth; local edits require fresh checks.
Do not publish a release until exact-SHA native CI, real Mac GUI, final DMG and
distribution/signing decisions pass. A
100% first-attempt success guarantee is not supportable; known failures are made
explicit, tested where possible, and prevented from silently becoming success.
