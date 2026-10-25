# v0.2 product depth and acceptance

The workbench is a tool for iterating named CAD assemblies, not a second general
purpose IDE or a browser scene editor. Keep Python, saved project metadata,
certified BREP and exported geometry distinguishable and consistent.

## The professional step beyond the feature list

Protect the design work, not just the visible model. A useful editor must recover
unsaved drafts, reject stale writes, explain build failures at the responsible
source line, and refuse a current-model export when only an old certificate is
available. A visual transform is not a CAD edit until it survives rebuild and
appears in a real exported solid.

## Raw evidence to start from

1. The project's actual Python entry, imported helper modules, viewer.json and
   source assets; their byte hashes before and after an edit.
2. Actual CadQuery/OCP BREP solids, their units, validity, dimensions and volumes;
   not the triangle mesh or an exploded display pose.
3. Working editor source and executable tests, pinned to a revision. Existing
   editor behavior is a comparator, not a requirement to copy known data-loss
   bugs or assume its packaging works.
4. Native packaged app activity and final exported bytes; not authored feature
   checklists, synthetic dialog replies, mock IPC or future-release metadata.

## Scope that directly supports the design loop

| Work | Essential behavior | Independent acceptance |
|---|---|---|
| Open | Native-selected project folder, Python or STEP asset; explicit trust for executing local Python | Original input untouched; chosen source/entry identified; correct worker project loaded |
| Edit code | Multiple actual Python/JSON files, line numbers, find, indentation, undo/redo | Typed edits reach saved source bytes and a newly built model |
| Recover work | Bounded draft recovery and save/discard/cancel guards | Fresh process restores unsaved text; cancel leaves disk/model/draft unchanged |
| Resolve conflict | Compare-and-swap save and explicit external-change state | Concurrent disk modification rejects save; user's draft remains intact |
| Edit parameters | Finite supported top-level literal values, discovered without execution | Only the selected literal span changes; a real certification reflects its geometry |
| Edit model | Named part selection and real translation/rotation/uniform scaling | Saved transform is applied to BREP before certification; export/reimport reflects it |
| Inspect | Part dimensions, bounding box, volume, source/variant/certificate identity | Values match retained certified BREP, not tessellation or display explosion |
| Diagnose | Build progress, error location, failed-gate details and last-certified retention | Syntax/gate failures preserve truth; source navigation reaches the responsible line |
| Export | STEP/STL/BREP, Python copy and project archive with explicit semantics | Exact bytes reimport; valid solids/units/dimensions/volume; stale exports reject |
| Navigate | Fit/axis views, file/part tree, recent projects, pane controls and focus-safe shortcuts | Real input works; text entry is not intercepted; project change honors dirty guards |
| Personalize | Eight themes, accessible focus/contrast, persistent editor and build preferences | Rendered themes/settings and restart persistence, not just token counts |
| Update | GitHub stable release discovery, correct architecture, verified download and safe installation | Real transport; corruption/arch/version rejection; separate native install/rollback proof |

## Intentional limits

- Opening arbitrary Python executes trusted local code. A localhost header or
  sandboxed renderer does not sandbox Python.
- STEP imports contain boundary representation, not recoverable parametric
  history. No invented feature tree, sketch constraints or automatically
  recovered dimensions are promised.
- Export is explicitly nominal millimeters. Camera, hidden parts and exploded
  display are presentation state unless the export action explicitly selects
  a part/variant. They do not silently alter solids.
- The project input fingerprint covers its supported filesystem boundary;
  arbitrary external dependencies read by trusted Python are not made hermetic.
- Automatic update checking/downloading is not proof of automatic installation.
  Ad-hoc macOS distribution must not claim Developer ID/notarization, Gatekeeper
  acceptance or a working standard Squirrel updater without appropriate proof.
- LSP, arbitrary code completion, debugger, visual sketching, Boolean/history
  reconstruction, collaboration, cloud services, AI generation and manufacturing
  analysis are not added speculatively to this release.

## Methods and proof

Use AST parsing for parameter discovery; exact byte-hash CAS and atomic
replacement for saves; serialized CadQuery/OCP operations for build/import/export;
real BREP round trips for geometry; real keyboard/pointer actions for UI; separate
native bridge/dialog/package/update probes for desktop behavior. Retain raw
reports, source inventories, exported hashes and inspected frames outside git.

A future release is ready only when a new exact source passes source tests,
real workbench workflows, independent adversarial review, native package gates,
and the protected release chain. Preserve explicit not-checked boundaries.
