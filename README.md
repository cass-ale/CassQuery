# CassQuery

A local desktop viewer for CadQuery projects: edit the source, and the model in front of you updates — no export, no re-import, no twenty-second wait to find out what a fillet did.

CassQuery was built for parametric jewelry design, where one project is a chain of interdependent parts and the honest way to check a change is to *look at it*: posed, sectioned, X-rayed, measured. It is a personal tool, built in the open.

## Why it exists

The standard CadQuery viewers get their "live" feel from re-executing your script and re-sending the model. On a real project that re-execution *is* the product's own build chain — session verification, STEP exports, nested runs — and it costs tens of seconds per edit. The viewer isn't slow; the round trip is.

CassQuery splits the round trip in two:

- **Preview tier** — a build-only entry point per project (shapes, nothing else) tessellated coarse and pushed the moment a file changes. Fast, and labelled PREVIEW on screen so it is never mistaken for the truth.
- **Certified tier** — the full gate suite (below) on demand. Only certified models carry authored step states, measurements, and spec data. If a certified build fails its gates, the last certified model stays on screen.

## What it does

- **Semantic payloads, not mesh dumps.** Parts arrive named, with face and edge identity intact — so picking, per-part finishes, and measurement work on the model, not on a triangle soup.
- **A units gate on every payload.** Small parts are where unit handling silently dies (a 0.23 mm wall becoming 0.20 mm is a failed part, not a rounding choice). Every payload is checked for scale, extent, and per-part volume before it is allowed on screen.
- **Authored steps.** A project can declare posed states — a joint bent, a component drawn out, an exploded view, an X-ray ghost — and the viewer steps through them instantly, because they are certified builds, not re-poses.
- **Camera that respects you.** Rebuilds keep your camera direction by default (`keep` mode); the model updates, your viewpoint doesn't get confiscated.
- **Finishes per part.** Brushed, polished, per material — assigned in the project manifest, applied in the payload.
- **An agent protocol on loopback.** The same worker serves a small local API — rebuild, status, gate results as data, body list, set step/camera, screenshot — so an AI assistant can drive the viewer and verify its own work against the gates instead of admiring a render.

## Architecture

```
CadQuery project ──► worker.py ──► semantic payload ──► viewer (three.js)
  viewer.json          warm Python     {name, hash}          │
  entry (build-only)   watcher +       announce over         └── Electron shell
                       build thread    local websocket       (spawns the worker)
```

- `worker.py` — one warm process: file watcher, build thread, HTTP server for the viewer and payloads, websocket announce, and the agent protocol. A failed build announces nothing; the last good payload is retained.
- `viewer/` — the three.js frontend (vendored three.js in `viewer/vendor/`), including the step player, X-ray and clipping views, and finish rendering.
- `shell/` — a thin Electron shell that spawns the worker and hosts the viewer. The viewer also runs in any modern browser against the worker directly.
- `testpart/` — a trivial public demo project (a plate with holes and a boss) with its manifest, used to prove the loop end to end.
- `payloads/` — generated payloads. The TESTPLATE payloads are committed as a fixture; other projects' payloads are generated locally and are not committed.

## Projects

A project is any directory with:

- a **build-only entry** — a Python module that builds and returns the project's shapes, without session verification, exports, or other side effects;
- a **`viewer.json`** manifest naming the entry, the project's class (`live` for fast projects, `on-demand` for slow ones whose steps are built when asked), its authored steps, and per-part finishes.

## Run it

Requirements: Python with CadQuery 2.5+ available, Node.js 22+.

```bash
# the worker (serves the viewer at http://127.0.0.1:<port>)
CASSQUERY_PROJECTS=/path/to/projectA:/path/to/projectB python worker.py

# or the desktop shell (spawns the worker itself)
npm install
npm run app        # CASSQUERY_PYTHON overrides the Python it looks for
```

## Status

v1, in active development. Windows and macOS are the target desktops; it runs unpackaged in dev mode — no installers, signing, or onboarding, by design. The design projects it was built for are private; their models and payloads are not in this repository. The demo project (`testpart/`) and its payloads are the public proof of the loop.

## License

MIT — see [LICENSE](LICENSE).
