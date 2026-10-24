# macOS candidate and publication boundary

CassQuery now has a native `.app` build path with its own Python/CadQuery worker.
A packaged launch needs neither Node nor Python on PATH. macOS 14+ is the initial
build target; Apple Silicon and Intel are separate native candidates, not a
universal bundle. The target is not evidence of a clean-machine or minimum-OS test.

## Build and check

Use Node 22.23.4 and a standalone CPython 3.12 (with a discoverable shared
library), then from this repository. CI pins **3.12.10**, the stable
`actions/setup-python` release verified to have native macOS ARM64 and x64 binaries;
the initial local build used standalone 3.12.13 from a different provider. Keeping
the same Python minor preserves the locked cp312 wheel ABI.

Before changing the CI Python pin, check the live provider catalog, not whether
the version exists for Linux or another installer:

```sh
curl --fail --location --max-time 60 https://raw.githubusercontent.com/actions/python-versions/main/versions-manifest.json -o /tmp/python-versions.json
python tools/check_release.py --python-manifest /tmp/python-versions.json
```

`packaging/python-runtime.json` is the reviewed native subset of that catalog,
verified on October 24, 2026 (full catalog SHA-256
`cd09353c884196ca2d33f2273cce58a2950e39f558890e82ab19f1c5ebeb2072`);
both listed release archives returned HTTP 200. The snapshot records availability,
not archive content hashes. Update it only after verifying the chosen exact
release and both download assets.
Ordinary offline preflight validates against the checked-in subset.

From the repository:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-build.txt
python -m pip check
npm ci
npm run preflight
npm run build:mac:dir
python tools/worker_smoke.py --worker dist/cassquery-worker/cassquery-worker --output evidence/worker.json
python tools/macos_smoke.py --app dist/mac-app/mac-arm64/CassQuery.app --output evidence/mac
```

For Intel the app directory is `dist/mac-app/mac/CassQuery.app`; build natively on
Intel. `npm run build:mac` also creates architecture-labelled ZIP and DMG files.
On a host allowed to mount disk images, check the **final** DMG:

```sh
python tools/dmg_smoke.py --dmg dist/mac-app/CassQuery-0.1.0-mac-arm64.dmg --arch arm64 --output evidence/dmg --checked-app dist/mac-app/mac-arm64/CassQuery.app --checked-inventory evidence/mac/bundle-before.json
```

Inspect every generated assembled/apart screenshot. A nonempty PNG and passing
model gates are not visual approval. `macos_smoke.py` verifies bundle ID, version,
native executable architecture and seal, uses isolated Electron userData and
worker state, strips development interpreter paths, and checks worker shutdown.
The worker smoke exercises real socket collisions, real CadQuery builds,
save-triggered updates, all eight certified gates and a deliberate units failure.
`--signed` additionally requires Gatekeeper acceptance and a stapled ticket;
it is not used for an ad-hoc candidate.

The native candidate explicitly uses ad-hoc signing with hardened runtime disabled.
That verifies package integrity, not publisher identity or Gatekeeper acceptance.
A later Developer ID profile must separately enable hardened runtime and appropriate
entitlements, signing and notarization; it cannot inherit candidate defaults.

Runtime files go to writable application state, never inside `.app` resources.
The shell starts its own worker and accepts only its PID-bound private readiness
registry; an unrelated HTTP 200 or stale global registry cannot select the app's
endpoint. `CASSQUERY_PYTHON` deliberately selects an external environment for
projects needing additional Python packages; it is never an automatic packaged
fallback. Project Python code is trusted local code, **not sandboxed**: do not
open a project you do not trust. Loopback Host/Origin checks do not change that.

## FrameGuard lessons applied

The raw audit used FrameGuard source `f0bed77e774c534deb7d5f64e38c9ca560022351`
and saved actual failed Actions job logs, not release summaries.

- First-release metadata 404 broke publication even after native builds passed
  ([run 38685201770](https://github.com/cass-ale/FrameGuard/actions/runs/38685201770)).
  CassQuery's protected promotion now tests first-release 404 bootstrap separately
  from 401/403/5xx; error JSON is never treated as a version. Automatic updating
  remains unimplemented.
- Binaries over GitHub's per-file Git limit broke an update mirror
  ([run 38691226131](https://github.com/cass-ale/FrameGuard/actions/runs/38691226131)).
  Native candidates stay in Actions artifact storage, not Git history.
- Mac-only approval missed Windows CRLF, process-order and 1x display assumptions
  ([run 39556596983](https://github.com/cass-ale/FrameGuard/actions/runs/39556596983)).
  CassQuery checks both advertised Mac architectures independently and tests
  cross-process readiness with an actual child, not a same-thread substitute.
- Green unit tests/startup still missed rendered clipping
  ([run 39700147304](https://github.com/cass-ale/FrameGuard/actions/runs/39700147304)).
  The candidate runs the real packaged renderer, stores principal-state frames,
  and requires human visual inspection. This is not a full display-scale audit.
- Text-only workflow tests accepted an injected skipped smoke step. CassQuery
  parses YAML and rejects conditional/ignored/missing/reordered required gates;
  regression tests deliberately disable the render gate and remove an architecture.
- FrameGuard's observed native package was ad-hoc signed, notarization skipped
  ([run 39710088862](https://github.com/cass-ale/FrameGuard/actions/runs/39710088862)).
  CassQuery names ad-hoc seal checks separately from Developer ID/notarization.

Dependencies and actions are exact-pinned; `package-lock.json` locks npm's graph
and `packaging/requirements-macos.lock` binds Python's graph to the reviewed native
wheel SHA-256s. Native frozen functional checks remain mandatory because static
import/warning checks cannot prove CadQuery/OCP and lazy binary dependencies work.

## Verification commands

With a local Chrome/Chromium executable, `CASSQUERY_BROWSER=/path/to/chrome npm run test:visual`
executes the real viewer against the public TESTPLATE fixture in both themes at
450, 700, 1000 and 1320 pixels. It clicks actual step buttons, checks every projected
mesh vertex, measures text/layout overflow and writes screenshots to `evidence/browser/`.
This fixture server has no live CAD/WebSocket connection; its honest disconnected
notice remains visible. Never present these frames as native Mac launch proof.

## Before a public first release

The candidate workflow has `contents: read`, no release credentials and no publish
step. It runs on main pushes, PRs and explicit dispatch; merely adding the file
locally is **not** a green Actions result. Each artifact records source SHA,
checksums and resolved Python dependencies. No local result authorizes a release.

Publication remains blocked until:

1. The reviewed final source is committed/pushed with explicit owner authorization,
   and both exact-SHA native candidate jobs and every mandatory smoke step pass.
2. The final ZIP/DMG, including mounted and copied launches, is checked, and its
   screenshots are inspected; an untested package/architecture is not advertised.
3. The owner selects distribution/signing. For Developer ID distribution, sign
   all embedded native code, notarize, staple and require `spctl`/`stapler` success
   with **no unsigned fallback**. The candidate does not provision those secrets.
4. A quarantined browser-download/Finder launch is tested on a clean account and
   the advertised minimum macOS version, without development Python/Node.
5. The explicit version-tag publication workflow promotes the unchanged candidate
   bytes, verifies its draft inventory by re-downloading/hashing every asset, and
   passes the configured `releases` protected approval gate before publication.
   See [RELEASE.md](RELEASE.md); no source push creates tags or releases.

Automatic updates and authenticated update manifests remain unimplemented.
Protected candidate promotion is authored and tested, not an executed public
release. The first version is **0.1.0 / v0.1.0**. Complete the separate
[pre-push](PRE_PUSH.md) and [pre-release](PRE_RELEASE.md) checklists, including
all required machine and human gates. No honest engineering process can guarantee
a 100% first-attempt rate; these gates make remaining risks explicit.
