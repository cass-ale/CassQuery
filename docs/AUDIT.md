# CassQuery audit and delivery ledger

This is a development/review pass, not a published or release-ready product.
Base commit: `40c916b66c0b7d6bf1d1d80e7a7263bfecc96cc0`. The initial handoff was
uncommitted; the owner subsequently authorized committing and pushing this reviewed
source. No release tags, manual release dispatches, binary publication, or human
outreach are authorized by that source push.

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

- Full `npm run preflight` on equipped macOS: **130 tests pass (44 Node, 86 Python),
  zero skips**, plus semantic release contracts and all 42 brand exports. Native
  icon pixel checks and real CAD/WebSocket tests are included.
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
- Automatic updates, authenticated update manifests, candidate-byte promotion and
  public release workflow are deliberately not implemented.

## Raw release evidence

FrameGuard source `f0bed77e774c534deb7d5f64e38c9ca560022351` and actual Actions logs
established first-release 404 parsing, >100MB Git payload publication failure,
platform-dependent harness assumptions and green-tests-but-rendered-clipping
failures. Its observed macOS build was ad-hoc/notarization skipped. Its text-only
workflow/cache checks had independently reproduced gaps; CassQuery does not copy
those weaknesses. Details and action links: `docs/MACOS_RELEASE.md`.

## Review boundary

The reviewed source commit is the source of truth; local edits require fresh checks.
Do not publish a release until exact-SHA native CI, real Mac GUI, final DMG and
distribution/signing decisions pass. A
100% first-attempt success guarantee is not supportable; known failures are made
explicit, tested where possible, and prevented from silently becoming success.
