# AGENTS.md — working rules for CassQuery

CassQuery is a local source-backed workbench for CadQuery projects: a warm Python
worker builds semantic payloads from a project's build-only entry point, and a
three.js viewer (browser or Electron shell) shows them as a two-tier live loop —
labelled previews for iteration, gate-checked certified builds for truth. The
v0.2 desktop editor and file/model/update boundaries are defined additively in
`docs/WORKBENCH.md`; its behavior must be independently verified before release.
The original design contract is `SPEC.md` in the viewer-tool workspace (v0.3);
this file is the standard of work for anyone — human or agent — changing code.

## The depth standard

Work the underlying problem, not the surface of the request. Before changing
anything, read the code the change must reach: callers, fixtures, the
manifest contract, the payload consumers. Prefer primary sources — the
running worker, the actual payload bytes, the rendered frame — over
summaries, comments, and memory of how it probably works.

Never grade your own work. Every claim of "fixed" or "working" needs an
independent check: a tool run, a test, a gate result, a screenshot looked at.
If no independent check exists for a claim, say so instead of asserting it.

## Verification laws

- **Render-verify generated artifacts.** A build is correct when the
  screenshot of it is looked at and right — never because the data that
  generated it looked right. The int16 incident is the standing lesson:
  every geometry gate passed while positions wrapped and folded the model;
  only the step screenshots showed it.
- **Interactive claims need the live loop exercised.** "It rebuilds on
  save" is proven by saving a file against the running worker and watching
  the viewer change — not by reading the watcher code.
- **Fail closed.** Gates fail closed: a failed certified build announces
  nothing and the last certified model stays on screen. Anything that
  cannot be done as specified stops and reports; it never substitutes a
  different method, route, or deliverable to get something done anyway.
- **Every delivery carries a checked vs not-checked ledger.** Name what
  was verified, how, and what was not verified. Silence about a gap is a
  false claim that it was checked.

## Engineering laws in this codebase

- **Units gate on every payload.** Measured extent vs source extent on
  every certified build. Small parts are where silent scale errors live;
  the gate is never optional, sampled, or bypassed.
- **Any encoding stage downstream of the gates gets its own roundtrip
  check.** The gates run on unquantized arrays; the payload-roundtrip
  gate exists because a packing bug (int16 wrapping a uint16 range) was
  invisible to all of them.
- **Spec changes go through the spec.** Behaviour the spec fixes — the two
  tiers, the eight gates and their thresholds, the payload shape, the
  agent protocol endpoints, loopback-only with no auth in v1 — changes by
  revising `SPEC.md` with a dated revision entry, never by code drift.
- **Manifests are the per-project contract.** A project is its
  `viewer.json` plus a build-only entry point. Do not grow per-project
  special cases into the worker; the manifest is where project facts live.

## Code style

Minimum code that works (Ponytail): does it need to exist → already in this
codebase → standard library or platform feature → installed dependency →
one line → the minimum that works. No unrequested abstractions, wrappers,
options, or configuration. Deletion beats addition. A deliberate shortcut
with a known limit gets a comment in the form
`shortcut: <the limit>, <when to upgrade>`.

Never cut: validation at trust boundaries (manifest loading, API inputs),
error handling that prevents data loss or a silently dead worker, security
(the loopback boundary), accessibility, or anything the task asked for.
Non-trivial new logic leaves one small test or assert-based self-check;
`npm run check` must pass.

## GUI bar

The viewer is held to the Hallmark floor: design tokens at `:root`, no
literal colours or fonts in components; a visible focus state on every
interactive element; `prefers-reduced-motion` respected; no eyebrow labels,
taglines, or helper text — if a screen needs them to make sense, fix the
layout. Run the Hallmark lint on `viewer/index.html` after any UI change.

Seamless interactivity is the product: every control responds immediately,
state changes are visible the moment they happen (a disconnect says so, a
build in progress says so), no per-frame layout work, and a rebuild never
confiscates the camera — direction survives, distance is recomputed.

## Release and brand verification

- `npm run preflight` runs real regressions, semantic release-input checks, and
  byte-for-byte brand regeneration. Brand changes go through
  `branding/source/` and `tools/make_brand_assets.py`, never edited exports.
- `CASSQUERY_BROWSER=/path/to/chrome npm run test:visual` uses real browser
  rendering and actual step clicks against the public TESTPLATE fixture;
  inspect its frames. It is not live CAD or native Mac launch proof.
- Source/frozen-worker smoke, native packaged renderer smoke, and final mounted/
  copied DMG smoke are separate gates. A success in one does not imply another.
- Candidate signing is explicitly ad-hoc/non-hardened. Do not claim Developer ID,
  notarization or Gatekeeper acceptance. Do not bypass host sandbox failures.
- Release readiness and every checked/not-checked gap live in `docs/AUDIT.md` and
  `docs/MACOS_RELEASE.md`; preserve their distinctions rather than promising
  first-attempt success.

## Workbench data and update safety

- Python files and the project manifest are authoritative. Unsaved editor buffers
  are inert drafts; restore them without executing or saving them automatically.
- File saves preserve conflicting revisions. Do not claim universal filesystem
  atomic CAS from a hash check followed by replacement. Recovery and interrupted
  transaction tests must retain actual draft/external bytes.
- Model transforms are real BREP edits with explicit units, pivot and order,
  applied before nominal geometry truth. Camera/explode/visibility are display-only.
- Inspect/export the retained current certificate, not the last preview or a
  second execution of project Python. Stale/failed/dirty work must not be labelled
  current certified export. STEP/STL/BREP do not reconstruct parametric history.
- Native dialogs and a restrictive preload broker own file authority. Keep
  sandbox, context isolation and no Node integration; never expose a general
  filesystem/shell/URL execution bridge or private worker capability.
- Drafts/settings live outside project and app resources. Recovery identity
  includes the canonical project root and relative file, not just a basename.
- Updates select the correct stable version/architecture and verify real bytes.
  Check/download tests are not installation proof: native restart, acknowledgement,
  backup/rollback and user-data preservation require separate qualification.
- Never control or modify the person's running installed app for automated tests;
  use owned isolated fixtures and userData, and honor host permission denials.

## Repo hygiene

Never commit: payloads for private projects, `evidence/`, `phase0/`,
`BUILD-LOG.md`, screenshots of private models, or anything from the private
design projects this tool was built for — their names stay out of public
files too. The only payloads in git are the TESTPLATE fixtures. The worker's
runtime outputs (payloads, registry file) are local state, not source.

No public posts, releases, or announcements about this repo without
Cassian's explicit direction. Commits and pushes happen only on Cassian's
explicit direction — a finished change sits in the working tree, verified
and reported, until he says ship it.
