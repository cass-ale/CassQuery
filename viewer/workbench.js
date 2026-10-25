import { TextDocument, EditorView } from "./editor.js";
import { THEMES, applyTheme } from "./themes.js";

export const DEFAULT_SETTINGS = Object.freeze({ schema: 1, theme: "system", editorFont: "system-mono", editorFontSize: 14, tabSize: 4,
  lineWrap: false, editorVisible: true, autoBuild: false, reducedMotion: "system", updateCheck: "enabled",
  updateAutoDownload: false, updateChannel: "stable", recentProjects: [] });
export const EDITOR_FONTS = Object.freeze({
  "system-mono": '"SFMono-Regular", Consolas, "Liberation Mono", monospace',
  menlo: 'Menlo, "Liberation Mono", monospace', "sf-mono": '"SFMono-Regular", Menlo, monospace',
  consolas: 'Consolas, "Liberation Mono", monospace', monospace: 'monospace',
});
export const IDENTITY_TRANSFORM = Object.freeze({ translation: [0, 0, 0], rotation: [0, 0, 0], scale: 1 });
export function documentKey(root, project, path) { return [root, project, path].join("\0"); }
export function canExport({ native, dirty, busy, model, inspection, rebuilding, state }) {
  return !!(native && !dirty && !busy && !rebuilding && model?.tier === "certified" && inspection?.stale === false &&
    inspection.certified_hash === model.hash && state === "certified");
}
export function transformValues(translation, rotation, scale) {
  if (![translation, rotation].every((v) => Array.isArray(v) && v.length === 3 && v.every(Number.isFinite)) || !Number.isFinite(scale) || scale <= 0)
    throw new Error("Use finite XYZ values and a positive uniform scale");
  return { translation: [...translation], rotation: [...rotation], scale };
}
export function recoveryRecords(documents) {
  const records = [...documents].filter((doc) => doc.dirty).map((doc) => ({ root: doc.root, project: doc.project, ...doc.record() }));
  if (records.some((record) => new TextEncoder().encode(record.text).length > 2 * 1024 * 1024) || records.length > 10 || new TextEncoder().encode(JSON.stringify(records)).length > 4 * 1024 * 1024)
    throw new Error("Save drafts before recovery exceeds its 10-file / 4 MiB limit");
  return records;
}
export function recoveryChoices(paths) {
  if (!Array.isArray(paths)) return [];
  return paths.flatMap((value) => {
    const record = typeof value === "string" ? { path: value } : value;
    const path = record?.path;
    if (typeof path !== "string" || !path || path.startsWith("/") || path.includes("\\") || path.includes("\0") || path.includes(":") ||
      path.split("/").some((part) => !part || part === "." || part === "..") || !/\.(py|json)$/i.test(path)) return [];
    if (record.sha256 !== undefined && record.sha256 !== null && !/^[a-f0-9]{64}$/.test(record.sha256)) return [];
    if (record.bytes !== undefined && record.bytes !== null && (!Number.isSafeInteger(record.bytes) || record.bytes < 0)) return [];
    return [{ ...record, path }];
  });
}
function response(value) {
  if (!value || typeof value !== "object") throw new Error("Invalid desktop response");
  if (value.error) throw Object.assign(new Error(value.error), value);
  return value;
}

export class Workbench {
  constructor(document, bridge, viewer) {
    this.dom = document; this.bridge = bridge; this.viewer = viewer;
    this.settings = { ...DEFAULT_SETTINGS }; this.documents = new Map(); this.files = [];
    this.project = null; this.root = null; this.activeKey = null; this.readOnly = true;
    this.inspection = null; this.updateState = null; this.inspectionKey = null; this.pendingTransform = null; this.transformDraftWork = Promise.resolve(); this.projectRequest = 0; this.fileRequest = 0; this.inspectRequest = 0;
    this.busy = 0; this.settingsRequest = 0; this.recoveryTimer = null; this.recoveryWork = Promise.resolve(); this.recoveries = [];
    this.transformUndo = []; this.transformRedo = []; this.errors = []; this.lastDiagnostic = ""; this.projectAction = "create";
    this.editor = new EditorView({ input: this.el("source-editor"), gutter: this.el("line-numbers"), mirror: this.el("editor-mirror"),
      position: this.el("cursor-position"), changed: (doc) => this.sourceChanged(doc) });
    this.bind();
  }
  el(id) { return this.dom.getElementById(id); }
  get active() { return this.documents.get(this.activeKey); }
  get dirty() { return [...this.documents.values()].some((doc) => doc.dirty) || !!this.pendingTransform; }
  get hasCurrentCertificate() {
    return canExport({ native: !!this.bridge, dirty: this.dirty, busy: false, model: this.viewer.model, inspection: this.inspection,
      rebuilding: this.viewer.rebuilding, state: this.viewer.status?.state });
  }
  get sourceDirty() { return [...this.documents.values()].some((doc) => doc.dirty && !doc.modelDerived); }
  async api(action, payload = {}) {
    if (!this.bridge?.workbench) throw new Error("This operation requires the CassQuery desktop app");
    try { return response(await this.bridge.workbench(action, payload)); }
    catch (error) {
      error.workbenchAction = action; error.workbenchProject = payload.project;
      error.workbenchPath = action === "transform" ? "viewer.json" : payload.path;
      throw error;
    }
  }
  status(text) { this.el("workbench-status").textContent = text; }
  reportError(error, doc = this.active) {
    const message = error.message || String(error); this.el("workbench-error").textContent = message; this.el("workbench-error").hidden = false;
    const sourceConflict = error.code === "CONFLICT" && ["save", "setParameter", "resolveConflict", "transform", "read"].includes(error.workbenchAction);
    if (sourceConflict && error.workbenchAction === "transform") doc = this.documents.get(documentKey(this.root, error.workbenchProject, "viewer.json")) || null;
    if (sourceConflict && doc && error.workbenchProject === doc.project && error.workbenchPath === doc.path) {
      doc.conflicted = true; doc.conflictDetails = error.details || error;
      const paths = recoveryChoices(doc.conflictDetails.recovery_paths).map((record) => record.path);
      this.el("conflict-message").textContent = message + (paths.length ? ` Preserved copies: ${paths.join(", ")}` : " Your draft is intact.");
      this.el("conflict-banner").hidden = false; this.inspection = null; this.queueRecovery();
    }
    this.status(sourceConflict && doc && error.workbenchProject === doc.project && error.workbenchPath === doc.path ?
      "Operation failed — draft retained" : "Operation failed");
    this.addError(message); this.renderControls(); return false;
  }
  async run(work, label = "Working") {
    if (this.busy) { this.status("Finish the current operation first"); return false; }
    this.busy++; this.status(label); this.renderControls();
    try { const result = await work(); this.el("workbench-error").hidden = true; return result; }
    catch (error) { return this.reportError(error); }
    finally { this.busy--; this.renderControls(); }
  }
  async initialize() {
    for (const theme of THEMES) { const option = this.dom.createElement("option"); option.value = theme.id; option.textContent = theme.label; this.el("theme-select").append(option); }
    if (!this.bridge) { this.settings.editorVisible = false; this.applySettings(); this.renderControls(); await this.syncView(); return; }
    try { this.settings = { ...DEFAULT_SETTINGS, ...response(await this.bridge.settings("get")) }; }
    catch (error) { this.reportError(error); }
    this.applySettings();
    try { const recovered = response(await this.bridge.recovery("get")); this.recoveries = Array.isArray(recovered.drafts) ? recovered.drafts : []; }
    catch (error) { this.reportError(error); }
    this.unsubscribe = this.bridge.onCommand((command) => this.command(command));
    this.viewer.beforeProjectChange = () => this.guard("Save drafts before switching projects?");
    this.viewer.beforeBuild = async () => {
      try { if (this.dirty) await this.saveAll(); return true; } catch (error) { return this.reportError(error); }
    };
    await this.syncView();
  }
  applySettings() {
    applyTheme(this.settings.theme);
    this.dom.documentElement.style.setProperty("--editor-font-size", this.settings.editorFontSize + "px");
    this.dom.documentElement.style.setProperty("--font-mono", EDITOR_FONTS[this.settings.editorFont] || EDITOR_FONTS["system-mono"]);
    this.el("source-editor").style.tabSize = String(this.settings.tabSize);
    this.editor.configure(this.settings); this.viewer.configureView({ reducedMotion: this.settings.reducedMotion });
    const fields = { "theme-select": "theme", "editor-font": "editorFont", "editor-font-size": "editorFontSize", "tab-size": "tabSize", "line-wrap": "lineWrap",
      "editor-visible": "editorVisible", "auto-build": "autoBuild", "reduced-motion": "reducedMotion", "update-check-mode": "updateCheck", "update-auto-download": "updateAutoDownload" };
    for (const [id, key] of Object.entries(fields)) {
      const element = this.el(id); if (element.type === "checkbox") element.checked = !!this.settings[key]; else element.value = this.settings[key];
    }
    this.el("editor-pane").hidden = !this.settings.editorVisible;
    this.el("pane-divider").hidden = !this.settings.editorVisible;
    this.el("workspace").classList.toggle("editor-visible", this.settings.editorVisible);
    this.el("pane-toggle").setAttribute("aria-pressed", String(this.settings.editorVisible));
  }
  async patchSettings(patch) {
    const before = { ...this.settings }, request = ++this.settingsRequest;
    this.settings = { ...this.settings, ...patch }; this.applySettings();
    if (!this.bridge) { this.status("Theme preview only — desktop app required to persist settings"); return; }
    try { const saved = response(await this.bridge.settings("patch", patch)); if (request === this.settingsRequest) { this.settings = { ...this.settings, ...saved }; this.applySettings(); } }
    catch (error) { if (request === this.settingsRequest) { this.settings = before; this.applySettings(); this.reportError(error); } }
  }
  choosePane(pane) {
    this.el("workspace").dataset.pane = pane;
    this.el("code-tab").setAttribute("aria-pressed", String(pane === "code")); this.el("model-tab").setAttribute("aria-pressed", String(pane === "model"));
    if (pane === "code" && !this.settings.editorVisible) this.patchSettings({ editorVisible: true });
    if (pane === "model") requestAnimationFrame(() => this.viewer.fit());
  }
  async syncView() {
    const project = this.viewer.project, root = this.viewer.status?.root || null;
    if (project !== this.project || root !== this.root) {
      this.project = project; this.root = root; this.files = []; this.activeKey = null; this.inspection = null; this.inspectionKey = null;
      this.editor.show(null); this.renderFiles(); this.renderTabs(); this.showRecovery();
      if (this.bridge && project) {
        const request = ++this.projectRequest;
        try {
          const listed = await this.api("list", { project });
          if (request !== this.projectRequest || project !== this.project) return;
          if (!Array.isArray(listed.files) || typeof listed.root !== "string") throw new Error("Invalid project file list");
          this.root = listed.root; this.files = listed.files; this.readOnly = listed.read_only === true; this.renderFiles();
          const entry = listed.source || listed.entry;
          if (this.files.some((f) => f.path === entry)) await this.openFile(entry);
          this.showRecovery();
        } catch (error) { this.reportError(error); }
      }
    }
    this.renderPartTree(); this.renderControls();
    const diagnostic = this.el("diag").textContent;
    if (diagnostic && diagnostic !== this.lastDiagnostic) { this.lastDiagnostic = diagnostic; this.addError(diagnostic); }
    if (this.bridge && project && this.viewer.model?.tier === "certified") {
      const key = [project, this.viewer.model.hash, this.viewer.model.index.gen, this.viewer.model.index.built_at, this.viewer.model.activeName, this.viewer.status?.state].join("/");
      if (key !== this.inspectionKey) { this.inspectionKey = key; await this.refreshInspection(); }
    }
  }
  async refreshInspection() {
    const project = this.project, variant = this.viewer.model?.activeName || "neutral", request = ++this.inspectRequest;
    if (!project || !this.bridge) return;
    try {
      const inspection = await this.api("inspect", { project, variant });
      if (request !== this.inspectRequest || project !== this.project || variant !== this.viewer.model?.activeName) return;
      this.inspection = inspection; this.renderFacts(); this.renderControls();
    } catch (error) { if (project === this.project) this.reportError(error); }
  }
  renderFiles() {
    const nav = this.el("files"); nav.replaceChildren();
    for (const file of this.files) {
      const button = this.dom.createElement("button"); button.type = "button"; button.dataset.path = file.path;
      button.textContent = file.path; button.disabled = file.kind === "asset";
      button.addEventListener("click", () => this.run(() => this.openFile(file.path), "Loading source")); nav.append(button);
    }
  }
  renderTabs() {
    const nav = this.el("file-tabs"); nav.replaceChildren();
    for (const [key, doc] of this.documents) if (doc.project === this.project && doc.root === this.root) {
      const button = this.dom.createElement("button"); button.type = "button"; button.dataset.path = doc.path;
      button.textContent = doc.path + (doc.dirty ? " •" : ""); button.setAttribute("aria-pressed", String(key === this.activeKey));
      button.addEventListener("click", () => { this.activeKey = key; this.editor.show(doc); this.renderTabs(); this.renderControls(); this.loadParameters(); }); nav.append(button);
    }
  }
  async openFile(path, reload = false, line = null) {
    if (!this.project || !this.files.some((file) => file.path === path && file.kind !== "asset")) throw new Error("Choose a known project text file");
    const key = documentKey(this.root, this.project, path), request = ++this.fileRequest, project = this.project;
    let doc = this.documents.get(key);
    if (!doc || reload) {
      const read = await this.api("read", { project, path });
      if (request !== this.fileRequest || project !== this.project) return false;
      doc = new TextDocument(read); doc.project = project; doc.root = this.root;
      if (!this.documents.has(key) && this.documents.size >= 32) {
        const clean = [...this.documents].find(([k, d]) => !d.dirty && k !== this.activeKey);
        if (!clean) throw new Error("Save drafts before opening more than 32 documents"); this.documents.delete(clean[0]);
      }
      this.documents.set(key, doc);
    }
    this.activeKey = key; this.editor.show(doc); this.el("editor-empty").hidden = true;
    this.renderTabs(); this.renderControls(); await this.loadParameters();
    if (line) { this.choosePane("code"); this.editor.goto(line); }
    return true;
  }
  sourceChanged(doc) {
    this.inspection = null;
    if (doc.modelDerived) { doc.modelDerived = false; this.pendingTransform = null; this.inspectorRevision = null; }
    this.renderTabs(); this.renderControls(); this.queueRecovery();
    this.bridge?.setDirty(this.dirty).then((result) => response(result)).catch((error) => this.reportError(error));
    if (!this.dirty && this.bridge) {
      this.inspectionKey = null;
      this.refreshInspection().catch((error) => this.reportError(error));
      this.loadParameters().catch((error) => this.reportError(error));
    }
    if (this.el("source-editor").dataset.wrapLimited === "true") this.status("Wrapping is unavailable above 5,000 lines; source editing and line numbers remain active");
  }
  queueRecovery() {
    clearTimeout(this.recoveryTimer);
    if (this.bridge) this.recoveryTimer = setTimeout(() => this.persistRecovery().catch((error) => this.reportError(error)), 300);
  }
  async persistRecovery() {
    if (!this.bridge) return;
    const records = recoveryRecords(this.documents.values());
    const snapshots = records.map((record) => ({ record, revision: this.documents.get(documentKey(record.root, record.project, record.path)).revision }));
    this.recoveryWork = this.recoveryWork.catch(() => {}).then(async () => {
      for (const { record, revision } of snapshots) {
        const doc = this.documents.get(documentKey(record.root, record.project, record.path));
        if (!doc || !doc.dirty || doc.revision !== revision || doc.baseSha !== record.base_sha256) continue;
        response(await this.bridge.recovery("save", { root: doc.root, project: doc.project, ...doc.record() }));
      }
    });
    return this.recoveryWork;
  }
  async deleteRecovery(doc, force = false) {
    if (!this.bridge) return true;
    const revision = doc.revision, key = documentKey(doc.root, doc.project, doc.path);
    this.recoveryWork = this.recoveryWork.catch(() => {}).then(async () => {
      if (this.documents.get(key) !== doc || doc.revision !== revision || (!force && doc.dirty)) return false;
      response(await this.bridge.recovery("delete", { root: doc.root, project: doc.project, path: doc.path })); return true;
    });
    return this.recoveryWork;
  }
  async saveDocument(doc) {
    if (!doc || !doc.dirty) return;
    if (doc.readOnly) throw new Error("Read-only source can be saved as a copy, not overwritten");
    if (!this.viewer.projects.some((p) => p.name === doc.project && p.root === doc.root)) throw new Error("Project location changed; save a copy of this draft");
    const text = doc.text;
    try {
      const result = await this.api("save", { project: doc.project, path: doc.path, text: doc.sourceText, expected_sha256: doc.baseSha });
      doc.saved(result.sha256, text); this.inspection = null;
      if (!doc.dirty) await this.deleteRecovery(doc); else this.queueRecovery();
    } catch (error) { if (error.code === "CONFLICT" || error.status === 409) doc.conflicted = true; this.reportError(error, doc); throw error; }
  }
  async saveAll() {
    if (!this.bridge) throw new Error("Source saving requires the CassQuery desktop app");
    clearTimeout(this.recoveryTimer); await this.recoveryWork.catch(() => {});
    for (const doc of this.documents.values()) if (doc.dirty && !doc.modelDerived) await this.saveDocument(doc);
    await this.transformDraftWork;
    if (this.pendingTransform) {
      const pending = this.pendingTransform;
      await this.applyTransform(transformValues(pending.translation, pending.rotation, pending.scale));
    }
    this.renderTabs(); this.renderControls(); response(await this.bridge.setDirty(this.dirty));
    if (this.dirty) { await this.persistRecovery(); throw new Error("New edits remain unsaved; save again before closing"); }
    this.el("conflict-banner").hidden = true; this.status("Source saved"); await this.refreshInspection(); await this.loadParameters();
  }
  async saveCopy() {
    const doc = this.active; if (!doc) throw new Error("Choose a source file first");
    const result = await this.api("saveAs", { project: doc.project, path: doc.path, text: doc.sourceText, expected_sha256: doc.baseSha });
    if (!result.cancelled) this.status("Copy saved; the original draft and project are unchanged");
    return result;
  }
  async beginConflictResolution() {
    const doc = this.active; if (!doc) throw new Error("Choose the conflicted source file");
    await this.persistRecovery();
    const disk = await this.api("read", { project: doc.project, path: doc.path });
    this.conflictResolution = { doc, disk, text: doc.sourceText, revision: doc.revision,
      recoveries: recoveryChoices(doc.conflictDetails?.recovery_paths) };
    const select = this.el("conflict-choice"); select.replaceChildren();
    for (const [value, label] of [["draft", "Current unsaved draft"], ["disk", "Current disk revision"]]) {
      const option = this.dom.createElement("option"); option.value = value; option.textContent = label; select.append(option);
    }
    for (const record of this.conflictResolution.recoveries) {
      const option = this.dom.createElement("option"); option.value = record.path;
      option.textContent = "Preserved revision: " + record.path + (Number.isSafeInteger(record.bytes) ? ` · ${record.bytes} B` : "") +
        (record.sha256 ? " · " + record.sha256.slice(0, 8) : " · hash unavailable") + (record.bytes === null ? " · size unavailable" : "");
      select.append(option);
    }
    this.el("conflict-preview").value = doc.sourceText; this.el("conflict-dialog").showModal();
  }
  async chooseConflictRevision() {
    const resolution = this.conflictResolution; if (!resolution) return;
    const choice = this.el("conflict-choice").value;
    if (choice === "draft") resolution.text = resolution.doc.sourceText;
    else if (choice === "disk") resolution.text = resolution.disk.text;
    else {
      const record = resolution.recoveries.find((entry) => entry.path === choice); if (!record) throw new Error("Choose a known preserved revision");
      const read = await this.api("read", { project: resolution.doc.project, path: record.path });
      if (record.sha256 && record.sha256 !== read.sha256) throw Object.assign(new Error("The preserved revision changed; reopen comparison before resolving"), { code: "CONFLICT" });
      resolution.readRevision = { path: record.path, sha256: read.sha256, historicalHashKnown: !!record.sha256 };
      if (!record.sha256) this.status("Preserved revision read successfully; its historical hash was unavailable");
      resolution.text = read.text;
    }
    this.el("conflict-preview").value = resolution.text;
  }
  async resolveConflict() {
    const resolution = this.conflictResolution; if (!resolution || resolution.doc !== this.active) throw new Error("Conflict selection changed; reopen resolution");
    const doc = resolution.doc;
    if (doc.revision !== resolution.revision) throw new Error("The draft changed after choosing a conflict revision; reopen the comparison");
    const result = await this.api("resolveConflict", { project: doc.project, path: doc.path, text: resolution.text, expected_sha256: resolution.disk.sha256 });
    if (result.cancelled) return;
    if (doc.revision === resolution.revision) doc.edit(resolution.text, doc.selection);
    doc.saved(result.sha256, resolution.text); doc.conflictDetails = null;
    if (!doc.dirty) await this.deleteRecovery(doc); else await this.persistRecovery();
    this.editor.render(); this.renderTabs(); response(await this.bridge.setDirty(this.dirty));
    this.inspection = null; this.el("conflict-dialog").close(); await this.refreshInspection(); this.status("Selected revision saved; rebuild to certify");
  }
  async discardAll() {
    if (!this.bridge) throw new Error("Draft recovery requires the CassQuery desktop app");
    clearTimeout(this.recoveryTimer); await this.transformDraftWork.catch(() => {}); await this.recoveryWork.catch(() => {});
    for (const [key, doc] of this.documents) if (doc.dirty) {
      const revision = doc.revision;
      if (!await this.deleteRecovery(doc, true) || doc.revision !== revision) throw new Error("New edits arrived while discarding; your draft is intact");
      this.documents.delete(key); if (key === this.activeKey) this.activeKey = null;
    }
    this.pendingTransform = null; this.inspectorRevision = null;
    this.editor.show(this.active || null); this.el("editor-empty").hidden = !!this.active; this.renderTabs(); this.renderControls(); response(await this.bridge.setDirty(false));
  }
  async guard(message) {
    if (!this.dirty) return true;
    this.el("guard-message").textContent = message; const dialog = this.el("draft-guard");
    if (dialog.open) return false;
    const choice = await new Promise((resolve) => { dialog.addEventListener("close", () => resolve(dialog.returnValue || "cancel"), { once: true }); dialog.showModal(); });
    if (choice === "cancel") return false;
    try { if (choice === "save") await this.saveAll(); else await this.discardAll(); return true; }
    catch (error) { return this.reportError(error); }
  }
  async openProject(action, payload = {}) {
    if (!await this.guard("Save drafts before opening another project?")) return false;
    const result = await this.api(action, payload); if (result.cancelled) return false;
    if (typeof result.project !== "string") throw new Error("Desktop did not register a project");
    if (!await this.viewer.refreshProjects(result.project)) throw new Error("Project registered but selection was cancelled or superseded; choose it explicitly to open");
    await this.syncView();
    if (this.viewer.project !== result.project || this.project !== result.project) throw new Error("Project selection changed before source loaded");
    this.status("Project opened"); this.choosePane("code"); return true;
  }
  async startProject(action) {
    if (!await this.guard("Save drafts before creating a workspace?")) return;
    this.projectAction = action; this.el("project-dialog-title").textContent = action === "import" ? "Import STEP into a new project" : "New project";
    this.el("project-dialog").showModal(); this.el("project-name").focus(); this.el("project-name").select();
  }
  showRecovery() {
    const matches = this.recoveries.filter((draft) => draft.root === this.root && draft.project === this.project);
    this.el("recovery-banner").hidden = !matches.length;
    this.el("recovery-message").textContent = `${matches.length} recoverable source draft${matches.length === 1 ? "" : "s"} for this project`;
  }
  async restoreDrafts() {
    const drafts = this.recoveries.filter((d) => d.root === this.root && d.project === this.project);
    if (!await this.guard("Save current drafts before restoring recovery?")) return;
    for (const draft of drafts) {
      await this.openFile(draft.path, true); this.active.recover(draft); this.editor.show(this.active);
    }
    this.renderTabs(); this.renderControls(); this.el("recovery-banner").hidden = true;
    await this.persistRecovery(); response(await this.bridge.setDirty(this.dirty)); this.status("Recovery restored as unsaved drafts; no source was executed or saved");
  }
  renderPartTree() {
    const tree = this.el("part-tree"); tree.replaceChildren(); const model = this.viewer.model;
    const parts = model?.variants.get(model.activeName)?.parts; if (!parts) return;
    for (const [name, part] of parts) {
      const button = this.dom.createElement("button"); button.type = "button"; button.dataset.part = name;
      const hidden = part.meshes.every((mesh) => !mesh.visible); button.dataset.hidden = String(hidden);
      button.textContent = name + (hidden ? " — hidden" : ""); button.disabled = model.tier !== "certified";
      button.setAttribute("aria-pressed", String(name === this.viewer.selected));
      button.addEventListener("click", () => { this.viewer.selectPart(name); this.el("part-inspector").open = true; this.renderFacts(); }); tree.append(button);
    }
    this.renderFacts();
  }
  renderFacts() {
    const facts = this.el("part-facts"); facts.replaceChildren();
    const part = this.inspection?.parts?.find((p) => p.name === this.viewer.selected); if (!part) return;
    const add = (name, value) => { const dt = this.dom.createElement("dt"), dd = this.dom.createElement("dd"); dt.textContent = name; dd.textContent = value; facts.append(dt, dd); };
    const variant = this.inspection.variant || "neutral"; add("BREP variant", variant); add("Certificate", this.inspection.stale ? "Stale" : "Current");
    if (Array.isArray(part.bbox) && part.bbox.length === 6) {
      for (const [i, axis] of ["X", "Y", "Z"].entries()) add(`${axis} extent`, (part.bbox[i + 3] - part.bbox[i]).toFixed(3) + " mm");
    }
    if (Number.isFinite(part.volume)) add("Volume", part.volume.toFixed(3) + " mm³");
    if (Number.isFinite(part.area)) add("Surface", part.area.toFixed(3) + " mm²");
    const revision = [this.root, this.viewer.selected, this.inspection.manifest_sha256].join("/");
    if (this.pendingTransform || this.inspectorRevision === revision) return;
    this.inspectorRevision = revision;
    const transforms = this.inspection.transforms || {}, t = Object.hasOwn(transforms, part.name) ? transforms[part.name] : IDENTITY_TRANSFORM;
    for (const [i, axis] of ["x", "y", "z"].entries()) {
      this.el("translation-" + axis).value = t.translation?.[i] ?? 0; this.el("rotation-" + axis).value = t.rotation?.[i] ?? 0;
    }
    this.el("uniform-scale").value = t.scale ?? 1;
  }
  draftTransform() {
    if (this.sourceDirty || !this.inspection?.manifest_sha256 || !this.viewer.selected) return;
    const pending = this.pendingTransform || { project: this.project, root: this.root, part: this.viewer.selected, expected: this.inspection.manifest_sha256 };
    const number = (id) => this.el(id).value.trim() ? Number(this.el(id).value) : NaN;
    pending.translation = ["x", "y", "z"].map((a) => number("translation-" + a));
    pending.rotation = ["x", "y", "z"].map((a) => number("rotation-" + a)); pending.scale = number("uniform-scale");
    this.pendingTransform = pending; this.bridge.setDirty(true).then(response).catch((e) => this.reportError(e)); this.renderControls();
    this.transformDraftWork = this.transformDraftWork.catch(() => {}).then(async () => {
      if (pending !== this.pendingTransform) return;
      const key = documentKey(pending.root, pending.project, "viewer.json"); let doc = this.documents.get(key);
      if (!doc) {
        const read = await this.api("read", { project: pending.project, path: "viewer.json" });
        if (pending !== this.pendingTransform || pending.root !== this.root || pending.project !== this.project) return;
        doc = this.documents.get(key);
        if (!doc) { doc = new TextDocument(read); doc.project = pending.project; doc.root = pending.root; this.documents.set(key, doc); }
      }
      if (doc.dirty && !doc.modelDerived) throw new Error("The manifest has a newer source draft; your transform values are intact");
      if (doc.baseSha !== pending.expected) throw Object.assign(new Error("Manifest changed before the transform draft; your values are intact"), { code: "CONFLICT" });
      const manifest = JSON.parse(doc.baseText); manifest.workbench ||= {}; manifest.workbench.transforms ||= {};
      Object.defineProperty(manifest.workbench.transforms, pending.part, { value: { translation: pending.translation, rotation: pending.rotation, scale: pending.scale }, enumerable: true, configurable: true, writable: true });
      doc.modelDerived = true; doc.edit(JSON.stringify(manifest, null, 2) + "\n", doc.selection);
      this.renderTabs(); this.queueRecovery();
    }).catch((e) => { this.reportError(e); throw e; });
    this.transformDraftWork.catch(() => {});
  }
  async applyTransform(values, mode = "apply") {
    if (this.sourceDirty) throw new Error("Save source drafts before editing a BREP transform");
    const stack = mode === "undo" ? this.transformUndo : this.transformRedo;
    const operation = mode === "apply" ? null : stack.at(-1);
    const project = operation?.project || this.pendingTransform?.project || this.project, part = operation?.part || this.pendingTransform?.part || this.viewer.selected;
    if (!project || !part || project !== this.project || (operation?.root && operation.root !== this.root)) throw new Error("Select a certified part in this project");
    const expected = operation?.expected || this.pendingTransform?.expected || this.inspection?.manifest_sha256;
    if (!expected) throw new Error("Wait for current source inspection before transforming");
    const result = await this.api("transform", { project, part, ...values, expected_sha256: expected });
    const hash = result.manifest_sha256 || result.sha256;
    if (mode === "apply") { this.transformUndo.push({ project, root: this.root, part, before: result.previous || IDENTITY_TRANSFORM, after: values, expected: hash }); this.transformRedo = []; if (this.transformUndo.length > 50) this.transformUndo.shift(); }
    else {
      stack.pop(); const other = mode === "undo" ? this.transformRedo : this.transformUndo; other.push({ ...operation, expected: hash });
      if (stack.length) stack.at(-1).expected = hash;
    }
    this.pendingTransform = null; this.inspectorRevision = null;
    this.inspection = null; this.status("BREP transform saved — rebuilding certification");
    await this.refreshInspection(); this.renderControls();
    const manifest = this.documents.get(documentKey(this.root, this.project, "viewer.json"));
    if (manifest && (!manifest.dirty || manifest.modelDerived)) {
      const revision = manifest.revision;
      const read = await this.api("read", { project, path: "viewer.json" });
      if (read.sha256 !== hash) throw Object.assign(new Error("Manifest changed after transforming; keep your source draft and reload explicitly"),
        { code: "CONFLICT", workbenchAction: "transform", workbenchProject: project, workbenchPath: "viewer.json" });
      if (manifest.revision === revision) manifest.text = read.text.replace(/\r\n/g, "\n");
      manifest.saved(read.sha256, read.text); manifest.modelDerived = false;
      if (!manifest.dirty) await this.deleteRecovery(manifest); else await this.persistRecovery();
      if (manifest === this.active) this.editor.render();
    }
    response(await this.bridge.setDirty(this.dirty)); this.renderTabs(); this.renderControls();
  }
  async undoTransform(redo = false) {
    const operation = (redo ? this.transformRedo : this.transformUndo).at(-1); if (!operation) return;
    await this.applyTransform(redo ? operation.after : operation.before, redo ? "redo" : "undo");
  }
  async loadParameters() {
    const doc = this.active, panel = this.el("parameters"); panel.replaceChildren();
    this.el("parameter-panel").hidden = !doc?.path.endsWith(".py") || !this.bridge;
    if (!doc?.path.endsWith(".py") || !this.bridge || doc.dirty) return;
    try {
      const result = await this.api("parameters", { project: doc.project, path: doc.path });
      if (doc !== this.active || doc.dirty) return;
      for (const parameter of result.parameters || []) {
        const label = this.dom.createElement("label"), name = this.dom.createElement("span"), input = this.dom.createElement("input"), button = this.dom.createElement("button");
        name.textContent = parameter.name; input.dataset.parameter = parameter.name;
        input.type = parameter.kind === "bool" ? "checkbox" : ["int", "float"].includes(parameter.kind) ? "number" : "text";
        if (input.type === "checkbox") input.checked = parameter.value; else input.value = parameter.value;
        if (input.type === "number") input.step = parameter.kind === "int" ? "1" : "any";
        input.disabled = result.read_only || this.dirty; button.type = "button"; button.textContent = "Apply"; button.disabled = input.disabled;
        button.addEventListener("click", () => this.run(async () => {
          if (this.dirty) throw new Error("Save all drafts before changing a source parameter");
          const value = input.type === "checkbox" ? input.checked : input.type === "number" ? Number(input.value) : input.value;
          if (typeof value === "number" && !Number.isFinite(value)) throw new Error("Parameter must be finite");
          const key = documentKey(doc.root, doc.project, doc.path);
          if (this.documents.get(key) !== doc || doc !== this.active || doc.baseSha !== result.sha256)
            throw new Error("The source document changed; refresh parameters before applying");
          const revision = doc.revision;
          const saved = await this.api("setParameter", { project: doc.project, path: doc.path, name: parameter.name, value, expected_sha256: result.sha256 });
          if (!this.viewer.projects.some((project) => project.name === doc.project && project.root === doc.root))
            throw new Error("Project location changed after parameter save; your local draft is retained");
          const current = await this.api("read", { project: doc.project, path: doc.path });
          if (current.sha256 !== saved.sha256) throw Object.assign(new Error("Source changed after parameter save; keep the current draft and reload explicitly"),
            { code: "CONFLICT", workbenchAction: "setParameter", workbenchProject: doc.project, workbenchPath: doc.path });
          if (this.documents.get(key) !== doc) { this.status("Parameter saved; the reloaded document was left unchanged"); return; }
          if (doc.revision === revision) doc.edit(current.text, doc.selection);
          doc.saved(saved.sha256, current.text); this.inspection = null;
          if (doc.dirty) await this.persistRecovery(); else await this.deleteRecovery(doc);
          response(await this.bridge.setDirty(this.dirty));
          if (doc === this.active) this.editor.render(); this.renderTabs(); this.renderControls();
          await this.refreshInspection(); await this.loadParameters();
          this.status(doc.dirty ? "Parameter saved to Python; newer source edits remain unsaved" : "Parameter persisted to Python source");
        }, "Saving parameter")); label.append(name, input, button); panel.append(label);
      }
    } catch (error) { if (doc === this.active && error.code !== "SYNTAX_ERROR") this.reportError(error, doc); }
  }
  renderControls() {
    const doc = this.active, blocked = !!this.busy, native = !!this.bridge;
    for (const id of ["openfile", "openfolder", "newproject", "importbtn"]) this.el(id).disabled = !native || blocked;
    this.el("savefile").disabled = !native || !this.dirty || blocked;
    this.el("saveas").disabled = !native || !doc || blocked;
    const current = canExport({ native, dirty: this.dirty, busy: blocked, model: this.viewer.model, inspection: this.inspection, rebuilding: this.viewer.rebuilding, state: this.viewer.status?.state });
    this.el("exportbtn").disabled = this.el("export-confirm").disabled = !current;
    this.el("export-selected").disabled = !this.viewer.selected;
    if (!this.viewer.selected) this.el("export-selected").checked = false;
    const transformCurrent = canExport({ native, dirty: this.sourceDirty, busy: blocked, model: this.viewer.model, inspection: this.inspection, rebuilding: this.viewer.rebuilding, state: this.viewer.status?.state });
    const transform = transformCurrent && !this.readOnly && !!this.viewer.selected;
    for (const id of ["translation-x", "translation-y", "translation-z", "rotation-x", "rotation-y", "rotation-z", "uniform-scale", "transform-apply", "transform-reset"]) this.el(id).disabled = !transform;
    this.el("transform-undo").disabled = !native || this.dirty || blocked || !this.transformUndo.length;
    this.el("transform-redo").disabled = !native || this.dirty || blocked || !this.transformRedo.length;
    this.el("undo-btn").disabled = !doc || doc.readOnly || !doc.undoStack.length;
    this.el("redo-btn").disabled = !doc || doc.readOnly || !doc.redoStack.length;
    this.el("document-status").dataset.dirty = String(!!doc?.dirty);
    this.el("document-status").textContent = doc ? `${doc.path} · ${doc.conflicted ? "external conflict" : doc.dirty ? "unsaved draft" : doc.readOnly ? "read-only" : "saved"}` : native ? "Loading source" : "Desktop app required";
    this.el("editor-file-info").textContent = doc ? (doc.path.endsWith(".py") ? "Python · UTF-8" : "JSON · UTF-8") +
      (this.el("source-editor").dataset.wrapLimited === "true" ? " · Wrapping limited above 5,000 lines" : "") : "";
    this.el("source-provenance").textContent = this.dirty ? "Unsaved source — certified export disabled" : current ? "Current certified source" : "Waiting for current certification";
    this.el("conflict-banner").hidden = !doc?.conflicted;
    this.el("update-check-btn").disabled = !native || blocked;
    this.el("update-download-btn").disabled = !native || blocked || this.updateState?.phase !== "available";
    this.el("update-reveal-btn").disabled = !native || blocked || this.updateState?.phase !== "ready";
    this.el("update-open-btn").disabled = !native || blocked || this.dirty || this.updateState?.phase !== "ready";
  }
  showExport() {
    if (!this.hasCurrentCertificate) throw new Error("Save source and wait for current certification before exporting");
    const select = this.el("export-variant"); select.replaceChildren();
    for (const name of Object.keys(this.viewer.model.index.variants)) { const option = this.dom.createElement("option"); option.value = name; option.textContent = name; select.append(option); }
    select.value = this.viewer.model.activeName; this.el("export-dialog").showModal();
  }
  async update(action) {
    const state = response(await this.bridge.update(action)); if (!state.cancelled) this.applyUpdateState(state);
  }
  applyUpdateState(state) {
    this.updateState = state;
    const phase = state.phase || "idle";
    this.el("update-status").textContent = state.error || (phase === "ready" ? `Verified ${state.version} ready. Installation requires explicit restart.` : phase === "available" ? `${state.version} available` : phase === "idle" ? "No newer verified stable release" : `${phase}${state.progress ? " · " + Math.round(state.progress * 100) + "%" : ""}`);
    this.el("update-download-btn").disabled = phase !== "available";
    this.el("update-open-btn").disabled = phase !== "ready" || this.dirty;
    this.el("update-notice").hidden = !["available", "ready"].includes(phase);
    this.el("update-notice").textContent = state.version ? `Update ${state.version}` : "Update";
    this.renderControls();
  }
  addError(message) {
    if (this.errors.at(-1) === message) return; this.errors.push(message); if (this.errors.length > 20) this.errors.shift();
    this.el("error-history").hidden = false; const list = this.el("error-items"); list.replaceChildren();
    for (const text of this.errors) {
      const item = this.dom.createElement("li");
      const matches = [...text.matchAll(/File ["']([^"']+)["'], line (\d+)/g)];
      let location = null;
      for (const match of matches) {
        let known = match[1], absolute = known.startsWith(this.root + "/"); if (absolute) known = known.slice(this.root.length + 1);
        const ambiguous = !absolute && !known.includes("/") && this.files.filter((f) => f.path.split("/").at(-1) === known).length > 1;
        if (!ambiguous && this.files.some((f) => f.path === known)) location = { path: known, line: Number(match[2]) };
      }
      if (location) {
        const button = this.dom.createElement("button"); button.type = "button"; button.textContent = text;
        button.addEventListener("click", () => this.run(() => this.openFile(location.path, false, location.line), "Opening error location")); item.append(button);
      } else { const value = this.dom.createElement("pre"); value.textContent = text; item.append(value); }
      list.append(item);
    }
  }
  async command(payload) {
    if (!payload || typeof payload.command !== "string") return;
    const name = payload.command;
    if (name === "updateState" && payload.state) { this.applyUpdateState(payload.state); return; }
    const actions = {
      openFile: () => this.openProject("openFile"), openFolder: () => this.openProject("openFolder"), new: () => this.startProject("create"),
      import: () => this.startProject("import"), save: () => this.saveAll(), saveAs: () => this.saveCopy(), export: () => this.showExport(),
      settings: () => { this.el("settings-dialog").showModal(); if (this.bridge) this.update("state").catch((e) => this.reportError(e)); },
      build: async () => { if (this.dirty) await this.saveAll(); await this.viewer.rebuild(); },
      pane: () => this.patchSettings({ editorVisible: !this.settings.editorVisible }), find: () => this.showFind(),
      undo: () => this.dom.activeElement?.closest("#editor-pane") ? this.editor.undo() : this.undoTransform(),
      redo: () => this.dom.activeElement?.closest("#editor-pane") ? this.editor.undo(true) : this.undoTransform(true),
      discard: () => this.discardAll(),
    };
    if (!Object.hasOwn(actions, name)) return;
    const closing = payload.reason === "close" || payload.reason === "quit";
    if (closing) this.el("source-editor").readOnly = true;
    await this.run(actions[name], name === "save" ? "Saving source" : "Working");
    if (closing) this.el("source-editor").readOnly = !this.active || this.active.readOnly;
  }
  showFind() { this.el("find-bar").hidden = false; this.el("find-query").focus(); this.el("find-query").select(); }
  bind() {
    const click = (id, work) => this.el(id).addEventListener("click", () => this.run(work));
    const commands = { openfile: "openFile", openfolder: "openFolder", newproject: "new", importbtn: "import", savefile: "save", saveas: "saveAs", exportbtn: "export", settingsbtn: "settings" };
    for (const [id, command] of Object.entries(commands)) this.el(id).addEventListener("click", () => this.command({ command }));
    click("undo-btn", () => this.editor.undo()); click("redo-btn", () => this.editor.undo(true));
    this.el("code-tab").addEventListener("click", () => this.choosePane("code")); this.el("model-tab").addEventListener("click", () => this.choosePane("model"));
    this.el("pane-toggle").addEventListener("click", () => this.patchSettings({ editorVisible: !this.settings.editorVisible }));
    click("camera-fit", () => this.viewer.fit()); click("camera-front", () => this.viewer.setCameraView("front")); click("camera-side", () => this.viewer.setCameraView("side")); click("camera-top", () => this.viewer.setCameraView("top"));
    for (const [button, dialog] of [["settings-close", "settings-dialog"], ["export-close", "export-dialog"], ["keys-close", "keyboard-dialog"], ["project-cancel", "project-dialog"]]) this.el(button).addEventListener("click", () => this.el(dialog).close());
    this.el("helpbtn").addEventListener("click", () => this.el("keyboard-dialog").showModal());
    this.el("findbtn").addEventListener("click", () => this.showFind());
    this.el("find-close").addEventListener("click", () => { this.el("find-bar").hidden = true; this.el("source-editor").focus(); });
    const find = (backwards = false) => { this.el("find-status").textContent = this.editor.find(this.el("find-query").value, backwards, this.el("find-case").checked) ? "" : "Not found"; };
    this.el("find-bar").addEventListener("submit", (e) => { e.preventDefault(); find(); }); this.el("find-prev").addEventListener("click", () => find(true));
    this.el("conflict-keep").addEventListener("click", () => { this.status("Draft kept; save a copy or reload explicitly to resolve the conflict"); });
    click("conflict-copy", () => this.saveCopy()); click("conflict-resolve", () => this.beginConflictResolution());
    this.el("conflict-cancel").addEventListener("click", () => this.el("conflict-dialog").close());
    this.el("conflict-choice").addEventListener("change", () => this.run(() => this.chooseConflictRevision(), "Reading revision"));
    this.el("conflict-form").addEventListener("submit", (e) => { e.preventDefault(); this.run(() => this.resolveConflict(), "Resolving source conflict"); });
    click("conflict-reload", async () => {
      const doc = this.active; if (!doc || !await this.guard("Reload the current disk file? Unsaved drafts will be discarded.")) return;
      await this.openFile(doc.path, true); this.status("Current disk source loaded");
    });
    click("recovery-restore", () => this.restoreDrafts());
    click("recovery-discard", async () => {
      for (const draft of this.recoveries.filter((r) => r.root === this.root && r.project === this.project)) response(await this.bridge.recovery("delete", { root: draft.root, project: draft.project, path: draft.path }));
      this.recoveries = this.recoveries.filter((r) => r.root !== this.root || r.project !== this.project); this.showRecovery();
    });
    this.el("project-form").addEventListener("submit", (e) => {
      e.preventDefault(); const name = this.el("project-name").value.trim(); if (!name) return;
      this.el("project-dialog").close(); this.run(() => this.openProject(this.projectAction, { name }), "Creating workspace");
    });
    this.el("export-form").addEventListener("submit", (e) => {
      e.preventDefault(); this.run(async () => {
        if (!this.hasCurrentCertificate) throw new Error("Certification is no longer current");
        const result = await this.api("export", { project: this.project, format: this.el("export-format").value, variant: this.el("export-variant").value,
          ...(this.el("export-selected").checked ? { parts: [this.viewer.selected] } : {}) });
        if (!result.cancelled) { this.status(`Exported ${result.bytes} bytes from certified source`); this.el("export-dialog").close(); }
      }, "Exporting certified source");
    });
    for (const id of ["translation-x", "translation-y", "translation-z", "rotation-x", "rotation-y", "rotation-z", "uniform-scale"])
      { this.el(id).required = true; this.el(id).addEventListener("input", () => this.draftTransform()); }
    this.el("transform-form").addEventListener("submit", (e) => {
      e.preventDefault(); if (!this.el("transform-form").checkValidity()) return;
      this.run(() => this.applyTransform(transformValues(["x", "y", "z"].map((a) => Number(this.el("translation-" + a).value)),
        ["x", "y", "z"].map((a) => Number(this.el("rotation-" + a).value)), Number(this.el("uniform-scale").value))), "Persisting BREP transform");
    });
    click("transform-reset", () => this.applyTransform(IDENTITY_TRANSFORM)); click("transform-undo", () => this.undoTransform()); click("transform-redo", () => this.undoTransform(true));
    const settings = { "theme-select": "theme", "editor-font": "editorFont", "editor-font-size": "editorFontSize", "tab-size": "tabSize", "line-wrap": "lineWrap", "editor-visible": "editorVisible", "auto-build": "autoBuild", "reduced-motion": "reducedMotion", "update-check-mode": "updateCheck", "update-auto-download": "updateAutoDownload" };
    for (const [id, key] of Object.entries(settings)) this.el(id).addEventListener("change", () => {
      const input = this.el(id); if (!input.checkValidity()) return;
      this.patchSettings({ [key]: input.type === "checkbox" ? input.checked : ["editorFontSize", "tabSize"].includes(key) ? Number(input.value) : input.value });
    });
    click("update-check-btn", () => this.update("check")); click("update-download-btn", () => this.update("download"));
    click("update-reveal-btn", () => this.update("reveal")); click("update-open-btn", () => this.update("install"));
    this.el("update-notice").addEventListener("click", () => this.command({ command: "settings" }));
    this.el("errors-clear").addEventListener("click", () => { this.errors = []; this.el("error-items").replaceChildren(); this.el("error-history").hidden = true; });
    this.bindDivider();
    window.addEventListener("cassquery:viewchange", () => this.syncView().catch((e) => this.reportError(e)));
    const media = matchMedia("(prefers-color-scheme: dark)"); media.addEventListener("change", () => { if (this.settings.theme === "system") this.applySettings(); });
    window.addEventListener("keydown", (e) => {
      if (e.defaultPrevented || this.dom.querySelector("dialog[open]")) return;
      const modifier = (e.metaKey || e.ctrlKey) && !e.altKey, key = e.key.toLowerCase();
      if (modifier) {
        if (key === "z" && e.target.closest("input, textarea") && e.target.id !== "source-editor") return;
        const commands = { o: e.shiftKey ? "openFolder" : "openFile", n: "new", s: e.shiftKey ? "saveAs" : "save", f: "find", b: "build", e: "export", ",": "settings", z: e.shiftKey ? "redo" : "undo" };
        if (Object.hasOwn(commands, key)) { e.preventDefault(); e.stopPropagation(); this.command({ command: commands[key] }); }
      } else if (e.key === "Escape") {
        if (this.dom.querySelector("dialog[open]")) return;
        if (!this.el("find-bar").hidden) { this.el("find-bar").hidden = true; this.el("source-editor").focus(); }
      } else if (!e.target.closest("input, textarea, select, [contenteditable], #editor-pane")) {
        if (["ArrowLeft", "ArrowRight"].includes(e.key) && e.target.closest("#pane-divider")) return;
        if (key === "f") this.viewer.fit(); else if (["1", "2", "3"].includes(key)) this.viewer.setCameraView({ 1: "front", 2: "side", 3: "top" }[key]);
      }
    }, true);
  }
  bindDivider() {
    const divider = this.el("pane-divider"); let bounds = null;
    const set = (percent) => { percent = Math.max(25, Math.min(65, percent)); this.dom.documentElement.style.setProperty("--editor-share", percent + "%"); divider.setAttribute("aria-valuenow", String(Math.round(percent))); };
    divider.addEventListener("pointerdown", (e) => { if (e.button !== 0) return; bounds = this.el("workspace").getBoundingClientRect(); divider.setPointerCapture(e.pointerId); e.preventDefault(); });
    divider.addEventListener("pointermove", (e) => { if (bounds) set((e.clientX - bounds.left) / bounds.width * 100); });
    divider.addEventListener("pointerup", () => { bounds = null; }); divider.addEventListener("pointercancel", () => { bounds = null; });
    divider.addEventListener("keydown", (e) => { if (["ArrowLeft", "ArrowRight"].includes(e.key)) { e.preventDefault(); e.stopPropagation(); set(Number(divider.getAttribute("aria-valuenow")) + (e.key === "ArrowRight" ? 5 : -5)); } });
  }
}

if (typeof window !== "undefined" && typeof document !== "undefined") {
  const start = () => { const workbench = new Workbench(document, window.cassquery, window.__cq); window.__workbench = workbench; workbench.initialize().catch((error) => workbench.reportError(error)); };
  if (window.__cq) start(); else window.addEventListener("cassquery:ready", start, { once: true });
}
