# Testing CassQuery

CassQuery's test tiers answer different questions. Passing a parser or a mocked
native boundary does not prove that a saved CAD file reaches a rendered model.
The production-path suite below supplies that missing proof for the **Linux
source worker and Chromium viewer**, not for the packaged macOS application.

## Production-path integration

Run from a Linux checkout with Python 3.12, the runtime dependency versions in
`requirements-build.txt`, Node, `playwright-core`, and an explicitly
installed Chrome/Chromium executable with a working Linux sandbox. The runner
sets `chromiumSandbox: true` explicitly and has no sandbox-off fallback. No
browser or Python dependency is
downloaded by the runner. `playwright-core` may be installed in a separate tools
directory; do not modify the app's dependency graph to run this suite.

```sh
CASSQUERY_PYTHON=/absolute/path/to/python3.12 \
CASSQUERY_BROWSER=/absolute/path/to/chrome \
CASSQUERY_PLAYWRIGHT=/absolute/path/to/node_modules/playwright-core \
CASSQUERY_INTEGRATION_OUTPUT=/absolute/path/to/new-evidence-directory \
node tools/integration_suite.cjs
```

`CASSQUERY_PLAYWRIGHT` is optional when normal Node module resolution can find
`playwright-core`. `PYTHONPATH` can name an isolated runtime installation when
needed. Never point the suite at private models or real preferences. The runner
copies only the worker, viewer, and bundled public `testpart`; it creates its
second project, malformed manifest, pose, and failing sources inside an owned
temporary directory. It copies **no committed payloads**. The first worker
starts with **no project arguments and no project environment variable**.

Four scenarios cross the actual production boundaries:

1. **Live truth:** default demo is built by CadQuery/OCP and passes all eight
   certified gates. A real Rebuild button emits an HTTP request. Saving a small
   geometry change drives watcher → preview announcement → labelled rendered
   preview → fresh certified announcement/model. A pointer-orbited camera keeps
   its direction. Deliberate `unit_scale=2` fails the units gate; certified index
   bytes and **every indexed variant's bytes** stay unchanged, no certified
   announcement is emitted, and the viewer retains the last certified model.
2. **Interaction ordering:** actual step buttons and arrow keys, delayed real
   lazy variant loading with rapid A–B–A input, X-ray material/depth-write changes,
   range-keyboard explode transforms, and pointer raycast → Hide → Show all.
   Projecting a model coordinate chooses a pointer location; it does not invoke
   the viewer's selection or step functions.
3. **Isolation and reconnect:** one malformed manifest cannot prevent two valid
   projects loading. A syntax-error project has no model or certified index,
   then recovers through the real Rebuild control. A delayed second-project HTTP
   response cannot replace the first project's model after switching back.
   Browser networking is taken offline, the owned worker restarts, CAD rebuilds
   while announcements are missed, and the real reconnect `hello` snapshot
   supplies the new certified hash and geometry.
4. **Boundary and witness:** the running production listeners reject malformed
   JSON, non-object/non-finite JSON, hostile Host/Origin, static traversal, and
   hostile WebSocket Origin. An isolated worker-source copy omits the HTTP
   boundary check; the **same real HTTP assertion** then fails with 200 instead
   of 403. Startup failure or an unrelated exception is not a mutation witness.

Transport timing uses Playwright `route.continue()` after a controlled delay,
never `route.fulfill()` or a replacement server. Browser observations read
`window.__cq` geometry/camera state; actions use DOM, keyboard, pointer, supported
HTTP requests, and actual saved files. The suite never calls `__cq.goStep` or
assigns application state.

## Evidence and fail-closed behavior

Each run requires a new output directory. `integration-results.json` is schema
1, with an explicit platform, source SHA-256s, scenario/check observations,
received WebSocket frames, screenshot names/descriptions/SHA-256s, errors, and a
process/listener cleanup ledger. Nonzero exit means failed or incomplete work.
Every declared scenario and check is mandatory; no `--skip-*` options exist.

The absolute deadline is four minutes, including cleanup, in addition to
individual HTTP/browser waits. Graceful cancellation begins ten seconds before
the hard deadline. Worker and browser processes run in owned Linux
process groups; graceful shutdown escalates to group termination. The runner
checks that its groups and listeners are gone. An absolute-deadline failure
writes a red report and terminates owned groups; it never claims successful
cleanup without checking it. Evidence remains outside the source temporary
copy. No screenshots or payloads belong in commits.

Validate a complete report and its screenshot bytes independently:

```sh
node tools/integration_suite.cjs --validate-report /path/to/integration-results.json
python3 -m unittest discover -s tests -p test_integration_contract.py -v
```

The standard-library Python contract tests execute the actual Node report
validator and reject omitted/duplicated scenarios/checks, red checks, errors,
unproven cleanup, absent/unrelated mutation witnesses, missing/tampered evidence,
and unknown skip flags. Every check's observations are validated independently
of its `pass` label: gate IDs/results, decoded mesh extents, fresh build times,
ordered announcements and hashes, actual camera delta, retained bytes/models,
interaction state, reconnect snapshot, rejection statuses, and baseline/mutant
behavior. Mesh counts must be positive integers, physical extents exact finite
positive three-vectors, part identities unique and complete, mesh-position and
visibility arrays nonempty, and build times finite numbers rather than coerced
strings. Contradictory observations, empty objects/arrays, and placeholder
`fixture` evidence cannot be green. Typed parser fixtures in the contract tests
are explicitly synthetic; they are not browser proof.

For an explicit negative runner test, use another new output directory and run
`node tools/integration_suite.cjs --mutation`. This applies the omitted-boundary
mutation only to the temporary source copy, executes the real suite, and must
exit nonzero at `host-origin`. It must not be counted as a passing suite run.
The normal run already includes a bounded isolated mutation witness.

Screenshots are captured evidence, not a claim that every frame was reviewed.
Before delivery, an independent reviewer must inspect the frames and the report,
confirm the stated appearance and invariant observations, and verify the exact
source hashes being delivered. A green automated result cannot replace that
review.

## Execution tiers and release boundary

- **Contracts/unit tests:** cheap local feedback about manifests, encoding,
  HTTP input, release rules, and controlled native boundaries. They cannot prove
  real CAD/browser or packaged-app behavior.
- **Public-fixture rendering:** `tools/viewer_render.cjs` checks layouts and
  framing against committed public payloads. Its fixture server is deliberately
  not a live-worker proof.
- **Production-path source integration:** this suite exercises real Linux CAD,
  HTTP/WS, watcher, and Chromium WebGL/input, with isolated public fixtures.
- **Native/package gates:** the existing macOS worker/app/ZIP/DMG smoke tools and
  architecture-specific candidate jobs prove those native artifacts. Linux
  screenshots cannot stand in for Finder launch, Electron menus/dialogs,
  signing, notarization, Gatekeeper, or either Mac architecture.

A separate Ubuntu source-integration CI job may install the pinned runtime graph
from `requirements-build.txt` with Python 3.12 and install a Linux
browser/test-only Playwright separately. The macOS release wheel lock is not a
Linux runtime lock; this source test is not release supply-chain attestation.
Keep native release gates mandatory and preserve their existing trusted
provenance rules. A source-suite pass is additional evidence, not release
approval, a security override, or authorization to push/tag/publish.
