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

Requirements: Python with CadQuery 2.5+ and websockets available, Node.js 22+.

### Try the demo

```bash
git clone https://github.com/cass-ale/CassQuery.git
cd CassQuery
python3 -m venv .venv
.venv/bin/pip install "cadquery>=2.5" websockets   # Windows: .venv\Scripts\pip
npm install
npm run app
```

The shell opens on the bundled demo project (`testpart/`, a plate with holes and a boss) with zero configuration: it finds the repo-local `.venv`, spawns the worker with the demo as the only project, and the first certified build runs on its own. `CASSQUERY_PYTHON` overrides the Python the shell looks for; `CASSQUERY_PROJECTS` (os.pathsep-separated — `;` on Windows, `:` elsewhere) replaces the demo with your own projects.

### Run the worker directly

```bash
# project directories as arguments…
.venv/bin/python worker.py /path/to/projectA /path/to/projectB

# …or by environment
CASSQUERY_PROJECTS=/path/to/projectA:/path/to/projectB .venv/bin/python worker.py

# neither → the bundled testpart demo is served, and the worker says so
```

Then open the viewer at the URL the worker prints. The port is **3939** unless it is busy — the worker scans upward for a free port, prints the one it took, and writes `{port, pid}` to `~/.cassquery.json`, which is how the shell (and agents) find a running worker. `--port N` or `CASSQUERY_PORT=N` pins a starting port. With no arguments at all the worker still serves: the demo appears instead of an empty viewer. A project whose `viewer.json` is missing or invalid is skipped with a one-line error (also listed under `load_errors` in `GET /api/status`); the rest keep serving.

## Troubleshooting

- **"The CassQuery worker did not start" in the shell, or a startup error naming missing modules** — the Python in use has no CadQuery (or no websockets). The worker checks for numpy, CadQuery/OCP, and websockets before serving and exits with the list of what is missing. Point `CASSQUERY_PYTHON` at an interpreter that has them (the demo's `.venv`, if you made one).
- **The viewer opens on a different port than 3939** — 3939 was busy; the worker scanned upward. The printed URL and `~/.cassquery.json` always carry the real port.
- **Empty viewer / "No projects configured"** — the worker was started without projects and every configured project failed to load, or none was configured. Check the worker's console for the per-project skip reasons, and `load_errors` in `GET /api/status`.
- **A project is missing from the switcher** — its `viewer.json` failed to load (missing file, invalid JSON, or a missing `name`/`entry` key). The skip reason is printed at startup and kept in `load_errors`.

## Status

v1 is built and verified: both tiers live, all eight certified gates exercised (including a deliberate gate-failure run where the last certified model stayed on screen), the save → preview → certified loop proven end to end with render-checked screenshots, and the agent protocol driven headlessly against the running worker. Windows and macOS are the target desktops; it runs unpackaged in dev mode — no installers, signing, or onboarding, by design. The design projects it was built for are private; their models and payloads are not in this repository. The demo project (`testpart/`) and its payloads are the public proof of the loop.

Working on this repo — human or agent? Read [AGENTS.md](AGENTS.md) first: it carries the depth standard and the verification laws this codebase is held to.

## License

MIT — see [LICENSE](LICENSE).
