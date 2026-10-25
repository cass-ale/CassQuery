# v0.2 workbench: source-grounded CAD research

Research-only baseline: CassQuery `33c2d35f817ced4a3e85a54ed6ce148ce7030d62` and the additive [WORKBENCH.md](WORKBENCH.md) contract. This report informs implementation; it does not certify that any new editor, import, transform or export path works. The eight certified gates and the trusted-local-Python boundary remain unchanged.

## 1. The necessary v0.2 scope

A useful small-assembly workbench needs **safe source iteration and trustworthy geometry output**, not a simulated feature tree:

1. Multi-file Python/manifest editing with per-file dirty state, undo, explicit save, external-change conflict handling and durable inert draft recovery.
2. A narrow source-parameter panel for unambiguous top-level scalar literals. Changes edit authoritative source under compare-and-swap (CAS), then rebuild; they do not merely override an in-memory model.
3. An inspector showing the selected part/variant's certified BREP bounds, volume, area, solid count, units and certificate freshness. These are geometry facts, not mesh-derived manufacturing or material claims.
4. Real translation/rotation/positive uniform scaling persisted in the manifest, with explicit pivot/order and recertification. Camera/explode/hide/X-ray remain display-only.
5. Z-up axis/isometric views and fit, without losing the user's camera during rebuilds.
6. Export of the selected current certificate/variant/parts to STEP/STP, STL and BREP, plus a source-project archive and explicit provenance. Reuse certified shapes; do not rerun arbitrary project Python during export.

These close professional gaps in the contract without adding cloud collaboration, AI generation, a constraint solver or a generic CAD history editor. Native dialogs, restricted IPC, the required themes/settings/shortcuts and update constraints already specified by WORKBENCH remain requirements; this research does not replace them with a larger UI framework.

## 2. Two primary repositories, pinned and checked

### CQ-editor

Fresh main: **`71903d5630fa49fc6ed1b99c2df41dfca29f43fa`**, October 12, 2026.

Actual source/test evidence inspected: editor, code editor, debugger, traceback pane, object tree, live Workplane inspector, viewer, main-window wiring, shape conversion and application tests.

Exact Linux job:
https://github.com/CadQuery/CQ-editor/actions/runs/38269450012/job/114879799141

Fetched job metadata binds that commit to `Test Linux` success. Its raw transcript runs actual `xvfb-run pytest`, including `tests/test_app.py`. The transcript has skipped tests and warnings, and the same commit's Azure packaging check failed. **Neither a total test count nor a green Linux job is native macOS/Electron proof.** Selected tests below combine real Qt/kernel/file paths with mocked dialog or debugger components; their precise limits are retained.

### CadQuery

Fresh main: **`8178185a24a035c9af7f4679033be14267f6a6ef`**, October 24, 2026.

CassQuery's locked **2.8.0** tag: **`fb4c6d41863aee270c46ab64397f0d2675e74be0`**. Runtime API recommendations below use this shipped version rather than assuming every current-main change is installed. The CQGI implementation is byte-identical between these pins; importer/exporter/shape files differ, but the checked 2.8.0 API already supports the cited unit/shape functions.

The fresh main's CadQuery Azure check succeeded and container-build checks succeeded. Those are exact-commit check snapshots, not raw per-test transcripts or execution of CassQuery's new feature paths. No new CAD/GUI actors were run for this research. The primary evidence is actual pinned implementation, test assertions and the inspected CQ-editor test transcript.

## 3. Source editing, multi-file reload and recovery

### What CQ-editor actually does

- Its editor guards new/open against a modified document, exposes native file actions and protects examples by redirecting Save to Save As:
  [editor.py:234–314](https://github.com/CadQuery/CQ-editor/blob/71903d5630fa49fc6ed1b99c2df41dfca29f43fa/cq_editor/widgets/editor.py#L234-L314).
- Saving writes text to the selected file and clears dirty state. This is not an atomic CAS write with an external-source fingerprint.
- External reload reinstalls watches after atomic replacement, preserves cursor/selection/scroll and inserts replacement text through the undo buffer. **It then resets modified state and rerenders**, without first resolving a dirty draft conflict:
  [editor.py:483–537](https://github.com/CadQuery/CQ-editor/blob/71903d5630fa49fc6ed1b99c2df41dfca29f43fa/cq_editor/widgets/editor.py#L483-L537).
- Persistence saves/restores the **filename**, not unsaved text:
  [editor.py:553–566](https://github.com/CadQuery/CQ-editor/blob/71903d5630fa49fc6ed1b99c2df41dfca29f43fa/cq_editor/widgets/editor.py#L553-L566).

The application tests exercise actual file saves and Qt input while mocking dialogs; the state-restore test rereads a saved file:
[tests/test_app.py:691–760](https://github.com/CadQuery/CQ-editor/blob/71903d5630fa49fc6ed1b99c2df41dfca29f43fa/tests/test_app.py#L691-L760).

The watcher test performs external changes, waits for the rerender signal, and explicitly verifies disabled autoreload does not fire:
[tests/test_app.py:2164–2212](https://github.com/CadQuery/CQ-editor/blob/71903d5630fa49fc6ed1b99c2df41dfca29f43fa/tests/test_app.py#L2164-L2212).

An imported-helper test introduces a real NameError, observes the error and repairs the helper:
[tests/test_app.py:1759–1798](https://github.com/CadQuery/CQ-editor/blob/71903d5630fa49fc6ed1b99c2df41dfca29f43fa/tests/test_app.py#L1759-L1798).

### CassQuery adaptation and limits

Keep WORKBENCH's stronger `expected_sha256` save conflict instead of copying CQ-editor's blind write/reload policy. One project can depend on several Python files and assets: the active editor's current file is not necessarily the build entry or the sole watched input.

Maintain draft records per project/root-relative file: text, baseline disk hash, dirty state and selection. Switching files preserves unsaved buffers. A clean buffer may reload from disk; a dirty buffer must preserve its draft and surface conflict. Deleted/renamed files and two same-basename helper files must not be silently associated with the wrong buffer. Save As creates a deliberately selected new file; it must not retarget the original project's authoritative entry implicitly.

Durable recovery is a **bounded, atomic, versioned text-data record** under native userData, outside project/application resources. It is not pickle, executable code or a second authoritative project. Restoration shows drafts and checks the baseline file hash; it does not save, import modules, build a project or run recovered Python automatically. Clear a draft only after verified save or explicit discard, not merely after a bridge request was sent. Settings/recent-project restoration likewise must not execute previously selected Python without the intended trust/open action.

Missing professional proof: dirty helper + external save must produce conflict without losing draft; crash/restart must recover an unsaved draft; recovery against changed/deleted disk content must remain inert; native close/switch/update guards must handle all dirty files, not only the visible tab. CQ-editor's filename persistence is **not** independent crash-recovery proof.

## 4. Plain Python compatibility: use a small explicit adapter

CassQuery currently requires a manifest entry returning `{names, parts}` from `build()`. Ordinary CQ-editor scripts may instead define module-level `result` or call `show_object`. A `.py` picker alone does not make those contracts compatible.

CQ-editor's actual method:

- Compiles current source, sets a module's `__file__` and temporarily establishes the script directory/import context:
  [debugger.py:205–230](https://github.com/CadQuery/CQ-editor/blob/71903d5630fa49fc6ed1b99c2df41dfca29f43fa/cq_editor/widgets/debugger.py#L205-L230).
- Injects `show_object(obj, name=None, options={})`, collects explicitly shown objects, and runs the source once. Only if none were shown does it discover CAD-valued module locals:
  [debugger.py:254–323](https://github.com/CadQuery/CQ-editor/blob/71903d5630fa49fc6ed1b99c2df41dfca29f43fa/cq_editor/widgets/debugger.py#L254-L323).
- That discovery includes Workplane, Shape, Assembly and Sketch, and its viewer conversion handles several geometry types:
  [cq_utils.py:26–68](https://github.com/CadQuery/CQ-editor/blob/71903d5630fa49fc6ed1b99c2df41dfca29f43fa/cq_editor/cq_utils.py#L26-L68).

**Do not copy arbitrary-local discovery or runtime object-ID naming.** A construction sketch, edge, intermediate workplane, AIS display object or empty result is not a valid certified solid; a viewer can display it without meeting CassQuery's eight-gate/positive-volume contract. Explicit duplicate show names must not silently overwrite another output.

Minimal compatibility policy:

1. Native selection explicitly authorizes trusted local Python and new manifest/adapter creation. Existing manifests retain their own entry:function behavior. Never overwrite an existing manifest or reserved generated entry.
2. Preserve the original script and helper/assets; do not copy an entire parent/home tree. If a new workspace is chosen, dependency copying needs an explicit limited policy. A consented sibling sidecar can preserve original relative imports without pretending it is a zero-write open.
3. Prefer a supported existing `build()` contract. Otherwise collect supported solid-valued `show_object` outputs; if there are no explicit outputs, accept a supported module-level `result`, not every CAD local. Do not append the fallback result to the same explicitly shown shape twice.
4. Use deterministic labels for unnamed outputs and reject collisions. Convert **all** supported Workplane shape values, not only `.val()` (which can discard multipart output). Require valid nonempty positive-volume solids. Assembly flattening needs its own placement/name tests; until supported, reject it clearly rather than dropping locations. Sketch/construction debug output is not certificate output.
5. Execute the original script only in the normal trusted build path, with correct compile filename, `__file__`, resource path and imports. A `runpy.run_path`-style adapter is a possible implementation, **not verified compatibility by this research**: test its `__name__`, module cache/reload, helper imports and callback semantics. Do not run once to discover output and again to certify/export.

Proof fixtures: named-parts build; result-only script; two explicit show outputs; callback plus result alias; multipart Workplane; duplicate labels; invalid/non-solid output; helper and `Path(__file__)` resource access; helper edit/reload; top-level side-effect witness; original input hashes unchanged. Reject unsupported forms instead of claiming general Python compatibility.

## 5. Source-backed parameter controls

CadQuery CQGI demonstrates the right discovery boundary: parse AST and examine **only top-level assignments**:
[cqgi.py:38–69](https://github.com/CadQuery/cadquery/blob/8178185a24a035c9af7f4679033be14267f6a6ef/cadquery/cqgi.py#L38-L69).

Its actual tests discover parameters, change values, reject unknown names/types and exclude function-local variables:
[tests/test_cqgi.py:43–79](https://github.com/CadQuery/cadquery/blob/8178185a24a035c9af7f4679033be14267f6a6ef/tests/test_cqgi.py#L43-L79),
[159–178](https://github.com/CadQuery/cadquery/blob/8178185a24a035c9af7f4679033be14267f6a6ef/tests/test_cqgi.py#L159-L178),
[217–233](https://github.com/CadQuery/cadquery/blob/8178185a24a035c9af7f4679033be14267f6a6ef/tests/test_cqgi.py#L217-L233).

But CQGI is **not** CassQuery's edit/safety implementation:

- Overrides mutate an AST for execution, not formatting-preserving authoritative source edits.
- Number conversion is coercive; boolean conversion uses truthiness; declarations are stored by name, and the finder handles only selected AST forms. Duplicate/chained/derived assignments are not a safe source edit contract.
- `validate()` is unimplemented; some metadata/error methods are placeholders.
- Its build runs real builtins and `exec`; the `build_options` parameter is not used to enforce the timeout/tessellation options claimed in the prose docs:
  [cqgi.py:75–130](https://github.com/CadQuery/cadquery/blob/8178185a24a035c9af7f4679033be14267f6a6ef/cadquery/cqgi.py#L75-L130),
  [274–314](https://github.com/CadQuery/cadquery/blob/8178185a24a035c9af7f4679033be14267f6a6ef/cadquery/cqgi.py#L274-L314).

Minimal v0.2 panel: expose unambiguous top-level `name = literal` scalar assignments only, with exact type, source file, source hash and literal span. Finite int/float, bool and bounded string are sufficient. Negative numeric literals require deliberate handling of UnaryOp; function defaults, expressions, calls, loops, comprehensions, attribute assignments, duplicate names and chained/destructuring assignments stay source-edit-only unless separately supported. Do not invent allowable engineering ranges from a default number. Show units/range only when explicitly declared by a supported project convention; otherwise do not imply the number is a dimension.

A control edit replaces the exact literal span after validating the same source/hash and reparsing, preserving comments, other same-valued literals, EOL and non-ASCII text. AST offsets are UTF-8 byte offsets; a character-index replacement can corrupt a line containing non-ASCII text. Apply changes through the same CAS/dirty/undo boundary as editing, then save and rebuild. Unsupported discovery produces an honest source-editor fallback, not `eval` or module execution.

Proof: same number in comments and other variables untouched; semicolon/signed literal/CRLF/Unicode; duplicate name rejected; helper-file scalar edit; nonfinite/coercive values rejected; stale source409 preserves draft; geometry/certificate changes after the actual saved parameter edit. No in-memory parameter override may silently make exported geometry diverge from the saved Python project.

## 6. Imported BREP does not contain design history

CadQuery explicitly states interchange formats do not carry its parametric data; Python is the fully parametric format:
[importexport.rst:11–15](https://github.com/CadQuery/cadquery/blob/fb4c6d41863aee270c46ab64397f0d2675e74be0/doc/importexport.rst#L11-L15).

The shipped STEP importer transfers reader roots and wraps resulting shapes into a new Workplane:
[importers/__init__.py:87–117](https://github.com/CadQuery/cadquery/blob/fb4c6d41863aee270c46ab64397f0d2675e74be0/cadquery/occ_impl/importers/__init__.py#L87-L117).

CQ-editor's apparent history inspector walks an **existing live Workplane.parent chain**, not a recovered STEP feature graph:
[cq_object_inspector.py:121–134](https://github.com/CadQuery/CQ-editor/blob/71903d5630fa49fc6ed1b99c2df41dfca29f43fa/cq_editor/widgets/cq_object_inspector.py#L121-L134).

A part tree may expose named bodies and source files; it must not label imported faces/solids as reconstructed sketches, constraints, fillets or editable feature history. Imported assets can be transformed and new source operations added, but shape topology is not original design intent. Face/edge indices are not stable identities across arbitrary source regeneration. Defer generic face-driven parametric editing, a sketch constraint UI and a feature tree until there is a defined source/topology correspondence and independent tests.

## 7. Real geometry transforms, units and inspection

### BREP transforms, not display tricks

CadQuery2.8 applies transformations through `gp_Trsf` and copied `BRepBuilderAPI_Transform` results. Rotate uses the supplied axis and degrees; scale uses the global origin:
[shapes.py:1120–1172](https://github.com/CadQuery/cadquery/blob/fb4c6d41863aee270c46ab64397f0d2675e74be0/cadquery/occ_impl/shapes.py#L1120-L1172).

A transform inspector therefore needs an explicit coordinate frame, origin/pivot and operation order. A viable minimal contract is uniform scale about origin, then fixed-axis X/Y/Z rotations, then translation in nominal millimeters. It is a **recommended contract to specify**, not an assumption already proven by WORKBENCH's vector fields. Do not show a center pivot if the implementation scales/rotates about world origin. Rigid rotation/translation preserve volume; uniform scale multiplies volume by factor cubed and area by factor squared. Roundtrip fixtures must check that the BREP, viewer and export agree.

Apply persistent transforms before nominal BREP truth is computed. Pose generation and transforms need one unambiguous ordering so the same transform is not applied twice or lost in a pose. Keep `unit_scale`'s existing mesh-versus-BREP mismatch gate behavior; it is not a substitute for a legitimate persisted geometry scale.

### Units and global kernel state

The shipped importer sets target cascade units; defaultMM normalizes units declared by STEP. The tests verify default volume preservation, targetM conversion and multipart STEP import:
[tests/test_importers.py:169–224](https://github.com/CadQuery/cadquery/blob/fb4c6d41863aee270c46ab64397f0d2675e74be0/tests/test_importers.py#L169-L224).

STEP export already supports `unit` and `outputUnit` in2.8.0. They describe internal geometry units and declared output units respectively:
[shapes.py:517–559](https://github.com/CadQuery/cadquery/blob/fb4c6d41863aee270c46ab64397f0d2675e74be0/cadquery/occ_impl/shapes.py#L517-L559).

Both import/export mutate **global `Interface_Static` settings**. Serialize those kernel operations with the actual build/export ownership boundary and explicitly setMM rather than relying on the last request's unit state. A threaded HTTP endpoint must not introduce concurrent import/export units races. The tests inspect headers and actual reimported bounds, not just output file existence:
[tests/test_exporters.py:980–1031](https://github.com/CadQuery/cadquery/blob/fb4c6d41863aee270c46ab64397f0d2675e74be0/tests/test_exporters.py#L980-L1031).

### Inspector facts and honest labels

Use retained certified BREP `BoundingBox`, `Area`, `Volume` and center functions:
[shapes.py:683–692](https://github.com/CadQuery/cadquery/blob/fb4c6d41863aee270c46ab64397f0d2675e74be0/cadquery/occ_impl/shapes.py#L683-L692),
[1104–1118](https://github.com/CadQuery/cadquery/blob/fb4c6d41863aee270c46ab64397f0d2675e74be0/cadquery/occ_impl/shapes.py#L1104-L1118).

Show coordinate bounds/dimensions inmm, area inmm² and volume inmm³, with selected part/variant and certificate identity/freshness. BBox is axis-aligned geometry extent with kernel tolerance, **not wall thickness, fit clearance or a physical measurement guarantee**. Volume is not mass without an explicit density. Tessellated/exploded/X-ray/hidden state must not change these facts.

CadQuery has actual BREP minimum-distance methods, but distance0 does not distinguish contact from overlapping/interpenetrating solids:
[shapes.py:1551–1575](https://github.com/CadQuery/cadquery/blob/fb4c6d41863aee270c46ab64397f0d2675e74be0/cadquery/occ_impl/shapes.py#L1551-L1575).

Do not market basic distance or minimum bbox dimension as interference, tolerance-stack, minimum-wall or safety analysis. Defer face/edge measurement and clearance tools unless selected topology, units, intersection interpretation and persistence are independently qualified.

## 8. Navigation and export the current certificate

CQ-editor's viewer implements Z-up top/bottom/front/back/left/right/isometric views. Fit accumulates displayed model bounds while excluding axes/view-cube helpers:
[viewer.py:367–426](https://github.com/CadQuery/CQ-editor/blob/71903d5630fa49fc6ed1b99c2df41dfca29f43fa/cq_editor/widgets/viewer.py#L367-L426).

Adapt the directions/fit intent, not its Qt/OCC architecture. Use camera controls only; view presets must not rewrite BREP or certify geometry. Text-input focus must suppress view shortcuts. Fit is an explicit user action; normal rebuilds preserve camera direction and recompute distance under the existing contract.

CQ-editor export collects selected source shapes, then exports their compound; it does not extract mesh vertices from the renderer:
[object_tree.py:501–522](https://github.com/CadQuery/CQ-editor/blob/71903d5630fa49fc6ed1b99c2df41dfca29f43fa/cq_editor/widgets/object_tree.py#L501-L522),
[cq_utils.py:121–132](https://github.com/CadQuery/CQ-editor/blob/71903d5630fa49fc6ed1b99c2df41dfca29f43fa/cq_editor/cq_utils.py#L121-L132).

But its selected export test only checks files exist, with a mocked Save dialog:
[tests/test_app.py:324–350](https://github.com/CadQuery/CQ-editor/blob/71903d5630fa49fc6ed1b99c2df41dfca29f43fa/tests/test_app.py#L324-L350).

CassQuery needs stronger proof: retain actual shapes corresponding to a successfully committed certificate, pin source/manifest/dependency snapshot + selected variant/parts + units + geometry hash + exporter/tolerance/runtime version, verify current freshness, and write only to the confirmed destination. Preview, failed certification, changed disk source, changed manifest/asset or unsaved draft must not masquerade as current export. The renderer owns draft knowledge, so it must block export while dirty; the worker must independently reject a stale disk certificate.

Use explicit STEP type for`.stp`; extension inference otherwise rejects it. BREP/STEP roundtrips verify solid count, validity, dimensions, volume and correct selected transform/variant. Compounding preserves geometry but does not automatically preserve assembly hierarchy, colors or part labels; if not exported through a qualified assembly path, disclose that limit and put named-part provenance in a companion/source-project record.

STL is mesh-based and does not carry the same STEP unit/history contract. `Shape.exportStl` defaults relative deflection; the generic wrapper uses its own default tolerance. These are not the viewer's certification tolerances:
[shapes.py:484–515](https://github.com/CadQuery/cadquery/blob/fb4c6d41863aee270c46ab64397f0d2675e74be0/cadquery/occ_impl/shapes.py#L484-L515).

For small detailed solids, choose and validate an explicit absolute-mm mesh-export tolerance and angular tolerance, rather than silently exporting the coarse preview mesh. No numeric manufacturing tolerance is invented here. Export itself can add triangulation/kernel state to retained shapes; protect retained certificate ownership and copy/export under the appropriate serialized kernel boundary.

A source ZIP contains only explicitly rooted project source/assets and manifest, never runtime caches, recovery journals or unrelated directories. It preserves editable Python intent; STEP/STL/BREP alone do not. Capture a consistent source snapshot rather than mixing file revisions during ZIP creation, and report provenance separately from the binary file's content hash.

## 9. Errors and trusted execution

CQ-editor emits real compile/runtime tracebacks, handles SyntaxError separately and displays filename/line/code. Its navigation signal highlights only the pseudo-script frame:
[traceback_viewer.py:57–111](https://github.com/CadQuery/CQ-editor/blob/71903d5630fa49fc6ed1b99c2df41dfca29f43fa/cq_editor/widgets/traceback_viewer.py#L57-L111).

Its actual test checks syntax, Python runtime and deeper kernel errors:
[tests/test_app.py:621–672](https://github.com/CadQuery/CQ-editor/blob/71903d5630fa49fc6ed1b99c2df41dfca29f43fa/tests/test_app.py#L621-L672).

CassQuery's baseline compresses errors into basename:line strings. Multi-file editing needs structured **root-relative path, line, optional column, exception type/message and build/source identity**; duplicate basenames cannot identify a helper. SyntaxError carries filename/lineno/offset on the exception and may need handling beyond traceback frames. Navigate only to contained known editable files, clamp invalid positions and render messages as text. Do not open arbitrary traceback paths or interpolate diagnostics into HTML.

The restricted native preload/workbench capability protects writes and dialogs; **it does not sandbox project Python**. Loading a project executes its source/imports in the trusted worker path, including possible external files/native extensions. Bounded IPC is not a bound on arbitrary user CAD code; engine/native crashes require an honest worker-failure/restart state, retained last certificate and inert drafts. Do not replace this with CQGI's unused timeout parameter or automatically execute recovered/untrusted project source.

## 10. Implementation proof gates and limits

Before a feature is called working, independently exercise:

- Real edit/save on entry and helper; dirty external conflict; file deletion; atomic CAS and interrupted save; crash/recovery without autoload execution.
- Actual scalar edit with formatting/Unicode/duplicate/type negatives and visible changed certified geometry.
- Imported STEP with multiple solids and different declared units; original input unchanged; invalid/no-solid import red; no reconstructed-history label.
- Persistent BREP transform with explicit order/pivot; transformed metrics and STEP/BREP reimport; old certificate stale after edit; failed gate retains last certified display but blocks current export.
- Export selected part/variant only, without running source again (side-effect witness), proper units/provenance and unchanged source/certificate; concurrent rebuild/export cannot mix snapshots.
- View/fit shortcut behavior under editor focus, noncanonical camera preservation and helper-excluded bounds; actual native dialog cancel/overwrite and dirty-close/update guards.
- New workbench states across required themes/native viewports, with inspected rendered frames and actual restricted bridge/IPC. Browser mock bridge, source counts and upstream Linux Qt tests do not replace native Electron/package proof.

Research checked pinned public code/test assertions, actual CQ-editor Linux test transcript and the shipped CadQuery API signatures. It did **not** execute new CassQuery feature code, private design projects, CAD exports or GUI/package actors. Upstream metadata is a snapshot, not full-repository correctness. The initial warning that main-only unit APIs might be absent was resolved by reading the2.8.0 tag: `unit`/`outputUnit` already exist; no dependency upgrade is justified by this research.

The minimum extension is a safe source-backed workbench with certificate-grounded geometry and export. Full sketcher/constraint solving, reconstructed imported history, arbitrary face-ID editing, manufacturing analysis, mass/material inference and collaboration remain outside v0.2 unless separately specified and proven.
