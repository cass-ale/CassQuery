# v0.2 workbench contract

This is an additive editor contract for the released v0.1 viewer. It does not alter
the eight certified gate IDs, thresholds, units, preview/certified ordering, or
trusted-local-Python execution model. Referenced private SPEC.md is unavailable;
no private protocol revision is invented.

## Source and geometry truth
- Python files and viewer.json are authoritative; editor buffers are drafts until saved.
- Existing manifest projects retain entry:function and named-parts build() behavior.
- Plain .py files without a manifest first return ADAPTER_REQUIRED without writing or
  executing the script. Native confirmation may create a new manifest and unique adapter
  beside the explicitly selected trusted script, preserving its original/helpers/assets.
  Broad root/home adaptation and overwriting existing sidecars are rejected. The adapter
  supports named-parts build(), explicit solid show_object outputs, or a solid result;
  it does not claim arbitrary CAD-local/Assembly/Sketch compatibility.
- STEP/STP import is a BREP asset referenced by a generated editable build-only Python
  entry. Export supports STEP/STP, STL, BREP and project ZIP/Python source. No false
  promise of recovering parametric history from imported boundary representations.
- Model inspector edits are real BREP transforms (translation XYZ mm, rotation XYZ deg,
  positive uniform scale), persisted under viewer.json.workbench.transforms[part].
  They apply to neutral geometry BEFORE nominal-mm truth and consistently to poses.
  Order is uniform scale about the world origin, rotations X then Y then Z about
  the world origin, then translation in millimeters. UI and saved metadata must
  state this convention rather than silently using a mesh center or camera axes.
  A transformed model must rebuild/certify before export; mesh-only viewport state
  (camera, visibility, X-ray, exploded display) is not an exported geometry edit.
- Exports require current certified source/manifest snapshot and selected variant;
  retain actual source shapes for that successful certification, not rebuild arbitrary
  project code a second time or export the last certificate after an unsaved/stale edit.

## Desktop boundary
- Native shell owns dialogs, trusted project path selection, settings and update UI.
- Add one restrictive preload bridge with contextBridge/ipcRenderer; nodeIntegration
  stays false, contextIsolation and sandbox stay true. IPC validates invoking window
  and same-origin worker URL; no general filesystem, shell, arbitrary URL or exec API.
- Worker additive endpoint: POST /api/workbench with {action,...payload} and
  X-CassQuery-Workbench header matching a random per-launch CASSQUERY_WORKBENCH_TOKEN.
  Existing unauthenticated loopback read/agent protocol remains intact.
- Native main brokers this endpoint with bounded response/request sizes. Token is
  not committed, returned in status, sent to internet, or exposed as renderer state.
- Standalone development/browser editing must explicitly provision the capability;
  no default unrestricted file-write route. Native bridge unavailable is an honest
  readonly/desktop-required state, not a fake button success.
- Project file paths are root-relative, checked against real containment; reject
  traversal, symlinks escaping root, invalid encoding, NUL, unsupported files,
  oversized documents, and writes to frozen application/bundled demo resources.

## Actions and responses
Every action returns a JSON object or a typed HTTP error {error,code}; conflicts are409.
- open {path}: native-selected folder or .py/.step/.stp; returns project status and
  {project,root,entry,files,read_only}. Registration and watcher support are dynamic.
- list {project}: {project,entry,files:[{path,kind,bytes}],read_only}.
- read {project,path}: {path,text,sha256,read_only}. UTF-8 Python/JSON text, max2MiB.
- save {project,path,text,expected_sha256}: guarded sibling-file commit; normal saves
  compare the loaded revision. A concurrent external replacement must be preserved,
  not silently discarded by a final check/replace race. Displaced-revision conflicts
  return a typed conflict and recoverable file/hash details; the editor retains its
  draft and requires explicit resolution. Do not claim universal atomic compare-and-swap
  against uncooperative writers that mutate an already-open inode.
- saveAs {project,path,destination,text}: native Save dialog-selected Python copy;
  save the draft without changing the active entry or pretending dependencies moved.
  The visible label is Save a copy. Confirmation authorizes only that destination.
- parameters {project,path}: source-derived finite top-level numeric/boolean literals,
  with current source hash; discovery parses AST and never executes Python.
- setParameter {project,path,name,value,expected_sha256}: exact literal-span replacement
  with compare-and-swap; preserve source outside the selected parameter and queue build.
- inspect {project}: retained certified BREP part dimensions/bbox/volume, nominal-mm units,
  certificate hash/time/source fingerprint and an explicit stale flag. Preview geometry
  never inherits the certified label, and stale source cannot export as current truth.
- create {directory,name}: explicitly selected parent, new unique project only;
  editable example entry and manifest, never overwrite existing directory.
- import {path,directory,name}: STEP/STP asset copied into new project and editable
  generated Python entry; precise format errors; original input unchanged.
- export {project,path,format,variant,parts}: main-selected destination, bounded supported
  format, overwrite only after native Save dialog confirmation. Returns path/hash/bytes.
- transform {project,part,translation:[x,y,z],rotation:[x,y,z],scale,expected_sha256}:
  CAS manifest update and queued rebuild; expected_sha256 is the current manifest hash.
  Reset uses identity values, not deleting arbitrary project facts.

## Editor and UX
- Open File/Folder/New Project/Save/Save As/Import/Export native menus and visible actions.
- Python editing: selection-preserving input, line numbers, undo/redo, find, indent/unindent,
  editor size/font/tab settings; no external fonts/scripts or pretending to support LSP.
- Dirty indicator and save/discard/cancel guards for switch/open/close/restart/update.
  External conflict preserves draft; user can reload, keep draft, or save as new file.
- Part tree, source-backed transform inspector, camera preset views/fit, visible preview/
  certification/error state. Diagnostics navigate known Python filename/line when present.
- Editor pane follows active project and actual file, not demo hardcoded source.
- Eight explicit themes minimum: Counterform Dark, Counterform Light, Graphite, Midnight,
  Forest, Sand, Paper, High Contrast. Accessible text/focus/errors for each; system default
  follows native dark/light unless explicit theme selected.
- Settings schema1: theme, editorFontSize12..24, tabSize2|4, lineWrap, editorVisible,
  autoBuild (save-controlled), reducedMotion(system|on), updateCheck(enabled/manual),
  updateAutoDownload(false default), updateChannel(stable), recentProjects bounded10.
- Shortcuts platform-aware Cmd/Ctrl+O(open), Shift+O(folder), N(new), S(save), Shift+S(saveas),
  Z/Shift+Z undo/redo, F(find), B(rebuild), comma(settings), E(export), Escape(close modal),
  F(fit only outside editor),1/2/3 axis views only outside text input. No intercept text typing.

## Updating
- Link exact GitHub Releases repository; stable canonical newer semver only, correctarch.
- Check/notify and optional automatic checking/download. Auth-free HTTPS, bounded JSON,
  allowed API/download hosts, no token leakage. Validate asset size and SHA-256 against
  published checksums/digest; never install a mismatched or wrongarch payload.
- Standard Electron macOS autoUpdater signing prerequisites must be researched for current
  ad-hoc distribution. Automatic in-place replacement may not be represented as working
  without real packaged install/rollback proof. Safest supported installer handoff remains
  explicit download + reveal/open with restart/user authorization if platform auto-install
  cannot be qualified. Settings/UI state must disclose limitation, never silently skip it.
- User data/projects survive updates; dirty drafts block restart/install. No universal
  filesystem/sandbox bypass, global privileged helper, or unsigned-to-signed fallback.

## Proof
Real edit/save/conflict/reload, failed build retaining certified display, transform BREP
roundtrip/export/import, files/folder dialogs, all8 themes/settings persistence, shortcuts
with textfocus, update check/version/hash/arch/error handling, and native default/warm/
ZIP/DMG gates remain independent. Source tests or browser mock bridge are not native proof.
Version0.2.0/tagv0.2.0 only after exact-source candidate qualification and owner authorization.
