'use strict';
// Tests execute actual controller/editor methods with bounded DOM/IPC seams.
// They prove source state and sequencing, not native dialogs, storage or rendered pixels.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ROOT = path.resolve(__dirname, '..');
const data = (text) => 'data:text/javascript;base64,' + Buffer.from(text).toString('base64');
const editorURL = data(fs.readFileSync(path.join(ROOT, 'viewer/editor.js'), 'utf8'));
const themesURL = data(fs.readFileSync(path.join(ROOT, 'viewer/themes.js'), 'utf8'));
const controllerURL = data(fs.readFileSync(path.join(ROOT, 'viewer/workbench.js'), 'utf8')
  .replaceAll('"./editor.js"', JSON.stringify(editorURL)).replaceAll('"./themes.js"', JSON.stringify(themesURL)));
const modules = Promise.all([import(controllerURL), import(editorURL), import(themesURL)]);
const hash = 'a'.repeat(64), next = 'b'.repeat(64);
const deferred = () => { let resolve; const promise = new Promise((r) => { resolve = r; }); return { promise, resolve }; };
const flush = () => new Promise(setImmediate);
function harness(Workbench, TextDocument, text = 'saved') {
  const w = Object.create(Workbench.prototype), nodes = new Map(), calls = [];
  class Element {
    constructor() { this.children = []; this.value = ''; this.textContent = ''; this.hidden = false; this.disabled = false; this.open = false; this.dataset = {}; this.listeners = {}; }
    addEventListener(type, callback) { (this.listeners[type] ||= []).push(callback); }
    async fire(type) { for (const callback of this.listeners[type] || []) await callback({ target: this, currentTarget: this }); }
    replaceChildren(...children) { this.children = children; }
    append(...children) { this.children.push(...children); }
    showModal() { this.open = true; }
    close() { this.open = false; }
  }
  w.el = (id) => { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); };
  w.dom = { createElement: () => new Element() }; w.root = '/project'; w.project = 'TEST'; w.activeKey = '/project\0TEST\0a.py';
  const doc = new TextDocument({ path: 'a.py', text, sha256: hash }); doc.root = w.root; doc.project = w.project;
  w.documents = new Map([[w.activeKey, doc]]); w.files = [{ path: 'a.py', kind: 'python' }];
  w.viewer = { projects: [{ name: 'TEST', root: '/project' }], project: 'TEST', selected: 'Body', status: { state: 'certified' }, rebuilding: false,
    model: { tier: 'certified', hash, index: { variants: { neutral: {} } }, activeName: 'neutral' } };
  w.inspection = { stale: false, certified_hash: hash, manifest_sha256: hash }; w.busy = 0;
  w.recoveryWork = Promise.resolve(); w.recoveryTimer = null; w.transformDraftWork = Promise.resolve(); w.pendingTransform = null;
  w.transformUndo = []; w.transformRedo = []; w.settings = {}; w.errors = [];
  w.renderControls = () => {}; w.renderTabs = () => {}; w.refreshInspection = async () => {}; w.loadParameters = async () => {};
  w.editor = { render() {}, show() {} }; w.queueRecovery = () => { calls.push(['queue']); }; w.addError = () => {};
  w.bridge = { recovery: async (action, record) => { calls.push([action, record]); return {}; },
    setDirty: async (dirty) => { calls.push(['dirty', dirty]); return { dirty }; },
    workbench: async (action, payload) => { calls.push(['workbench', action, payload]); return { sha256: next }; } };
  return { w, doc, calls, nodes };
}

test('editor flex constraints keep source controls inside the available column', () => {
  const css = fs.readFileSync(path.join(ROOT, 'viewer/style.css'), 'utf8');
  const rule = (selector) => css.slice(css.indexOf(selector + ' {')).match(/\{([^}]+)\}/)[1];
  assert.match(rule('.editor-area'), /min-height:\s*calc\(2 \* var\(--editor-font-size\) \* var\(--editor-line-height\) \+ 2 \* var\(--space-2\)\)/);
  assert.match(rule('.editor-pane'), /overflow:\s*hidden\s*;/);
  assert.match(rule('.source-heading'), /flex:\s*none\s*;/);
  assert.match(rule('.document-pane > .notice, .document-pane > .find-bar, .document-pane > .parameter-panel'), /flex:\s*none\s*;/);
  assert.match(rule('#source-editor'), /min-height:\s*0\s*;/);
  assert.match(rule('.editor-footer'), /flex:\s*none\s*;/);
  assert.match(rule('.document-pane'), /overflow:\s*auto\s*;/);
});

test('camera controls occupy a normal-flow row outside the model canvas', () => {
  const html = fs.readFileSync(path.join(ROOT, 'viewer/index.html'), 'utf8');
  const css = fs.readFileSync(path.join(ROOT, 'viewer/style.css'), 'utf8');
  const viewport = html.indexOf('<div class="viewport-pane">');
  const toolbar = html.indexOf('<nav class="camera-tools"', viewport);
  const toolbarEnd = html.indexOf('</nav>', toolbar);
  const stage = html.indexOf('<div id="stage">', viewport);
  assert.ok(viewport >= 0 && toolbar > viewport && toolbarEnd < stage);
  for (const id of ['camera-fit', 'camera-front', 'camera-side', 'camera-top'])
    assert.ok(html.slice(toolbar, toolbarEnd).includes(`id="${id}"`));
  const rule = css.match(/\.camera-tools\s*\{([^}]+)\}/)[1];
  assert.doesNotMatch(rule, /position\s*:\s*absolute|top\s*:|left\s*:/);
  assert.match(rule, /flex\s*:\s*none/);
});

test('uniform scale uses no offset step lattice while enforcing a positive lower bound', () => {
  const html = fs.readFileSync(path.join(ROOT, 'viewer/index.html'), 'utf8');
  const scale = html.match(/<input id="uniform-scale"[^>]*>/)[0];
  assert.match(scale, /step="any"/); const min = Number(scale.match(/min="([^"]+)"/)[1]);
  assert.ok(min > 0 && min < 0.5); assert.ok(1.5 >= min); assert.ok(0 < min);
});

test('eight offline palettes meet AA for text, state, focus and accent labels', async () => {
  const [, , { THEMES, contrast }] = await modules;
  assert.equal(THEMES.length, 8); assert.equal(new Set(THEMES.map((t) => t.id)).size, 8);
  for (const palette of THEMES) {
    for (const token of ['ink', 'muted', 'accent', 'bad', 'ok']) for (const surface of ['bg', 'panel', 'panel-2'])
      assert.ok(contrast(palette[token], palette[surface]) >= 4.5, `${palette.id}: ${token}/${surface}`);
    assert.ok(contrast(palette.accent, palette['on-accent']) >= 4.5, palette.id + ': accent label');
  }
});

test('frontend settings defaults equal the actual restrictive native schema', async () => {
  const [{ DEFAULT_SETTINGS, EDITOR_FONTS }] = await modules;
  const native = require('../shell/desktop.cjs');
  assert.deepEqual(DEFAULT_SETTINGS, native.DEFAULTS);
  for (const editorFont of Object.keys(EDITOR_FONTS)) assert.equal(native.settings({ editorFont }).editorFont, editorFont);
});

test('explicit dark palette keeps the brand light even when the system is light', async () => {
  const [, , { applyTheme }] = await modules;
  const properties = {}, root = { style: { setProperty(k, v) { properties[k] = v; } }, dataset: {} };
  applyTheme('midnight', root, false); assert.equal(root.dataset.theme, 'midnight'); assert.equal(properties['--brand-filter'], 'none');
  applyTheme('counterform-light', root, true); assert.match(properties['--brand-filter'], /brightness/);
});

test('export requires exact current certified source, not last certificate or draft widgets', async () => {
  const [{ canExport }] = await modules;
  const good = { native: true, dirty: false, busy: false, rebuilding: false, state: 'certified', model: { tier: 'certified', hash }, inspection: { stale: false, certified_hash: hash } };
  assert.equal(canExport(good), true);
  for (const change of [{ native: false }, { dirty: true }, { busy: true }, { rebuilding: true }, { state: 'stale' },
    { model: { tier: 'preview', hash } }, { inspection: { stale: true, certified_hash: hash } }, { inspection: { stale: false, certified_hash: next } }])
    assert.equal(canExport({ ...good, ...change }), false);
});

test('recovery identity is canonical root plus project plus relative path, and all dirty files are retained', async () => {
  const [{ documentKey, recoveryRecords }, { TextDocument }] = await modules;
  assert.notEqual(documentKey('/one', 'P', 'a.py'), documentKey('/two', 'P', 'a.py'));
  const a = new TextDocument({ path: 'a.py', text: 'a', sha256: hash }); a.root = '/one'; a.project = 'P'; a.edit('first');
  const b = new TextDocument({ path: 'b.py', text: 'b', sha256: hash }); b.root = '/one'; b.project = 'P'; b.edit('second');
  assert.deepEqual(recoveryRecords([a, b]).map((r) => [r.root, r.path, r.text]), [['/one', 'a.py', 'first'], ['/one', 'b.py', 'second']]);
  assert.throws(() => recoveryRecords(Array(11).fill(a)));
  a.edit('x'.repeat(2 * 1024 * 1024 + 1)); assert.throws(() => recoveryRecords([a]));
});

test('typed recovery-path records preserve metadata and reject non-contained choices', async () => {
  const [{ recoveryChoices }] = await modules;
  const record = { path: '.cq-workbench-recovery-δ.py', sha256: hash, bytes: 123, kind: 'external' };
  assert.deepEqual(recoveryChoices([record, 'other.json']), [record, { path: 'other.json' }]);
  const unknown = { path: 'unknown.py', sha256: null, bytes: null, kind: 'external' };
  assert.deepEqual(recoveryChoices([unknown]), [unknown]);
  assert.deepEqual(recoveryChoices([{ path: '/etc/a.py' }, { path: '../a.py' }, { path: 'a\\b.py' }, { path: 'a.py', sha256: 'bad' }, { path: 'a.py', bytes: -1 }, 'a:bad.py']), []);
});

test('actual conflict modal exposes and reads typed preserved revisions without executing or saving them', async () => {
  const [{ Workbench }, { TextDocument }] = await modules; const { w, doc, calls } = harness(Workbench, TextDocument);
  doc.edit('draft'); doc.conflictDetails = { recovery_paths: [{ path: 'external.py', bytes: 8, sha256: next, kind: 'external' }] };
  w.bridge.workbench = async (action, payload) => { calls.push(['workbench', action, payload]); return payload.path === 'external.py' ? { text: 'external', sha256: next } : { text: 'disk', sha256: hash }; };
  await w.beginConflictResolution();
  assert.equal(w.el('conflict-choice').children.length, 3); assert.equal(w.el('conflict-choice').children[2].value, 'external.py');
  assert.match(w.el('conflict-choice').children[2].textContent, /8 B/); assert.doesNotMatch(w.el('conflict-choice').children[2].textContent, /object Object/);
  w.el('conflict-choice').value = 'external.py'; await w.chooseConflictRevision();
  assert.equal(w.el('conflict-preview').value, 'external'); assert.equal(doc.text, 'draft');
  assert.ok(calls.filter((c) => c[0] === 'workbench').every((c) => c[1] === 'read'));
});

test('valid unknown-metadata recovery remains selectable and readable without claiming historical verification', async () => {
  const [{ Workbench }, { TextDocument }] = await modules; const { w, doc, calls } = harness(Workbench, TextDocument);
  doc.edit('draft'); const record = { path: '.cq-workbench-recovery-fault.py', bytes: null, sha256: null, kind: 'external' };
  doc.conflictDetails = { recovery_paths: [record] };
  w.bridge.workbench = async (action, payload) => {
    calls.push(['workbench', action, payload]);
    return { text: payload.path === record.path ? 'rescued' : 'disk', sha256: next };
  };
  await w.beginConflictResolution();
  assert.equal(w.el('conflict-choice').children.length, 3);
  assert.match(w.el('conflict-choice').children[2].textContent, /hash unavailable/);
  assert.match(w.el('conflict-choice').children[2].textContent, /size unavailable/);
  assert.doesNotMatch(w.el('conflict-choice').children[2].textContent, /null B|object Object/);
  assert.deepEqual(w.conflictResolution.recoveries[0], record);
  w.el('conflict-choice').value = record.path; await w.chooseConflictRevision();
  assert.equal(w.el('conflict-preview').value, 'rescued'); assert.equal(doc.text, 'draft');
  assert.deepEqual(w.conflictResolution.readRevision, { path: record.path, sha256: next, historicalHashKnown: false });
  assert.match(w.el('workbench-status').textContent, /historical hash was unavailable/);
  assert.ok(calls.filter((call) => call[0] === 'workbench').every((call) => call[1] === 'read'));
});

test('a changed preserved copy cannot be selected using an old claimed hash', async () => {
  const [{ Workbench }, { TextDocument }] = await modules; const { w, doc } = harness(Workbench, TextDocument);
  w.conflictResolution = { doc, disk: { text: 'disk', sha256: hash }, recoveries: [{ path: 'external.py', sha256: next }] };
  w.el('conflict-choice').value = 'external.py'; w.bridge.workbench = async () => ({ text: 'changed', sha256: hash });
  await assert.rejects(w.chooseConflictRevision(), /preserved revision changed/); assert.equal(doc.text, 'saved');
});

test('pending recovery save finishes before clean-document deletion; stale drafts cannot resurrect', async () => {
  const [{ Workbench }, { TextDocument }] = await modules; const { w, doc, calls } = harness(Workbench, TextDocument); const held = deferred();
  doc.edit('draft'); w.bridge.recovery = async (action) => { calls.push([action]); if (action === 'save') await held.promise; return {}; };
  const saving = w.persistRecovery(); await flush(); doc.saved(next); const deleting = w.deleteRecovery(doc);
  await flush(); assert.deepEqual(calls.map((c) => c[0]), ['save']); held.resolve(); await Promise.all([saving, deleting]);
  assert.deepEqual(calls.map((c) => c[0]), ['save', 'delete']);
});

test('queued recovery snapshots for older text revisions are canceled at dispatch', async () => {
  const [{ Workbench }, { TextDocument }] = await modules; const { w, doc, calls } = harness(Workbench, TextDocument); const held = deferred();
  w.recoveryWork = held.promise; doc.edit('old'); const old = w.persistRecovery(); doc.edit('latest'); held.resolve(); await old;
  assert.equal(calls.length, 0); await w.persistRecovery(); assert.equal(calls[0][1].text, 'latest');
});

test('new edits during queued cleanup skip deletion and persist the latest draft afterward', async () => {
  const [{ Workbench }, { TextDocument }] = await modules; const { w, doc, calls } = harness(Workbench, TextDocument); const held = deferred();
  w.recoveryWork = held.promise; const deletion = w.deleteRecovery(doc); doc.edit('new'); const saving = w.persistRecovery(); held.resolve();
  assert.equal(await deletion, false); await saving; assert.equal(calls.filter((c) => c[0] === 'delete').length, 0); assert.equal(calls.at(-1)[1].text, 'new');
});

test('actual save controller preserves later edits and uses the original CAS revision', async () => {
  const [{ Workbench }, { TextDocument }] = await modules; const { w, doc, calls } = harness(Workbench, TextDocument); const held = deferred();
  doc.edit('first'); w.bridge.workbench = async (action, payload) => { calls.push([action, payload]); await held.promise; return { sha256: next }; };
  const saving = w.saveDocument(doc); await flush(); doc.edit('latest'); held.resolve(); await saving;
  assert.equal(doc.text, 'latest'); assert.equal(doc.baseText, 'first'); assert.equal(doc.dirty, true);
  assert.equal(calls[0][1].expected_sha256, hash); assert.equal(calls[0][1].text, 'first');
});

test('failed operations replace running footer status without falsely claiming unrelated draft retention', async () => {
  const [{ Workbench }, { TextDocument }] = await modules; const { w, doc } = harness(Workbench, TextDocument);
  doc.edit('unsaved draft');
  w.bridge.workbench = async () => ({ error: 'External source changed', code: 'CONFLICT', status: 409 });
  await w.run(() => w.saveDocument(doc), 'Saving source');
  assert.equal(w.busy, 0);
  assert.equal(w.el('workbench-status').textContent, 'Operation failed — draft retained');
  assert.equal(doc.text, 'unsaved draft'); assert.equal(doc.dirty, true);
  assert.equal(w.el('workbench-error').hidden, false);
  await w.run(async () => { throw Object.assign(new Error('Destination unavailable'), { workbenchAction: 'export', code: 'OUTPUT_CONFLICT' }); }, 'Exporting certified source');
  assert.equal(w.el('workbench-status').textContent, 'Operation failed');
  assert.equal(w.busy, 0); assert.equal(doc.text, 'unsaved draft');
});

test('save conflicts keep original drafts and metadata; save a copy never marks the original clean', async () => {
  const [{ Workbench }, { TextDocument }] = await modules; const { w, doc } = harness(Workbench, TextDocument);
  doc.edit('draft'); w.bridge.workbench = async () => ({ error: 'Concurrent revision preserved', code: 'CONFLICT', status: 409, details: { recovery_paths: [{ path: 'old.py', sha256: next, bytes: 10 }] } });
  await assert.rejects(w.saveDocument(doc)); assert.equal(doc.text, 'draft'); assert.equal(doc.baseSha, hash); assert.equal(doc.conflicted, true);
  assert.equal(doc.conflictDetails.recovery_paths[0].path, 'old.py'); assert.doesNotMatch(w.el('conflict-message').textContent, /object Object/);
  w.bridge.workbench = async () => ({ copy: true, active_project_changed: false, path: '/copy.py' }); await w.saveCopy();
  assert.equal(doc.dirty, true); assert.equal(doc.text, 'draft'); assert.equal(w.project, 'TEST');
});

test('conflict-resolution snapshot cannot overwrite a newer local edit before or during IPC', async () => {
  const [{ Workbench }, { TextDocument }] = await modules; const { w, doc } = harness(Workbench, TextDocument); const held = deferred();
  doc.edit('draft'); w.conflictResolution = { doc, disk: { sha256: hash }, text: 'chosen', revision: doc.revision };
  w.bridge.workbench = async () => { await held.promise; return { sha256: next }; };
  const resolving = w.resolveConflict(); await flush(); doc.edit('newest'); held.resolve(); await resolving;
  assert.equal(doc.text, 'newest'); assert.equal(doc.baseText, 'chosen'); assert.equal(doc.dirty, true);
  w.conflictResolution.revision = doc.revision - 1; await assert.rejects(w.resolveConflict(), /draft changed/);
});

test('export dialog authorization is independent of the UI busy-disable flag', async () => {
  const [{ Workbench }, { TextDocument }] = await modules; const { w } = harness(Workbench, TextDocument);
  w.busy = 1; w.el('exportbtn').disabled = true; assert.equal(w.hasCurrentCertificate, true); w.showExport();
  assert.equal(w.el('export-dialog').open, true); assert.equal(w.el('export-variant').children[0].value, 'neutral');
});

test('export destination conflicts do not turn an unrelated source into a conflicted draft', async () => {
  const [{ Workbench }, { TextDocument }] = await modules; const { w, doc } = harness(Workbench, TextDocument);
  const error = Object.assign(new Error('Destination changed; output copies preserved'), { code: 'CONFLICT', status: 409,
    workbenchAction: 'export', workbenchProject: 'TEST', details: { recovery_paths: [{ path: '/outside/export.step', sha256: next, bytes: 100 }] } });
  w.reportError(error); assert.equal(doc.conflicted, false); assert.equal(doc.dirty, false); assert.equal(doc.baseSha, hash);
  const otherRead = Object.assign(new Error('Other file changed'), { code: 'CONFLICT', status: 409, workbenchAction: 'read', workbenchProject: 'TEST', workbenchPath: 'other.py' });
  w.reportError(otherRead); assert.equal(doc.conflicted, false);
});

test('discard waits for an in-flight model draft and cannot resurrect it after the native clean acknowledgment', async () => {
  const [{ Workbench }, { TextDocument }] = await modules; const { w, calls } = harness(Workbench, TextDocument); const held = deferred();
  for (const name of ['translation-x', 'translation-y', 'translation-z', 'rotation-x', 'rotation-y', 'rotation-z']) w.el(name).value = '0';
  w.el('translation-x').value = '2'; w.el('uniform-scale').value = '1';
  w.bridge.workbench = async () => { await held.promise; return { path: 'viewer.json', text: '{"name":"TEST"}', sha256: hash }; };
  w.draftTransform(); await flush(); let finished = false;
  const discarded = w.discardAll().then(() => { finished = true; }); await flush();
  assert.equal(finished, false, 'native clean acknowledgment preceded model-draft completion');
  held.resolve(); await Promise.all([discarded, w.transformDraftWork]);
  assert.equal(w.pendingTransform, null); assert.equal(w.dirty, false);
  assert.ok(![...w.documents.values()].some((doc) => doc.modelDerived));
  assert.equal(calls.at(-1)[0], 'dirty'); assert.equal(calls.at(-1)[1], false);
});

test('manual manifest edits during transform receipt refresh are preserved as newer dirty source', async () => {
  const [{ Workbench, documentKey }, { TextDocument }] = await modules; const { w } = harness(Workbench, TextDocument); const held = deferred();
  const doc = new TextDocument({ path: 'viewer.json', text: '{"name":"TEST"}', sha256: hash }); doc.root = '/project'; doc.project = 'TEST';
  doc.modelDerived = true; doc.edit('{"name":"TEST","workbench":{}}');
  w.documents.set(documentKey(doc.root, doc.project, doc.path), doc);
  w.bridge.workbench = async (action) => action === 'transform' ? { manifest_sha256: next } :
    (await held.promise, { path: 'viewer.json', text: '{"name":"TEST","saved":true}', sha256: next });
  const applying = w.applyTransform({ translation: [1, 0, 0], rotation: [0, 0, 0], scale: 1 }); await flush();
  doc.edit('{"name":"TEST","latest_manual_edit":true}'); doc.modelDerived = false;
  held.resolve(); await applying;
  assert.equal(doc.text, '{"name":"TEST","latest_manual_edit":true}'); assert.equal(doc.dirty, true);
});

test('actual parameter Apply callback preserves healthy receipts and later drafts during setter or read latency', async () => {
  const [{ Workbench }, { TextDocument }] = await modules;
  for (const editDuring of ['setParameter', 'read', null]) {
    const { w, doc, calls } = harness(Workbench, TextDocument, 'WIDTH = 2\n');
    const held = deferred(); let rendered = 0; w.editor.render = () => { rendered++; };
    w.bridge.workbench = async (action, payload) => {
      calls.push(['workbench', action, payload]);
      if (action === 'parameters') return { sha256: hash, parameters: [{ name: 'WIDTH', value: 2, kind: 'int' }], read_only: false };
      if (action === editDuring) await held.promise;
      if (action === 'setParameter') return { project: 'TEST', path: 'a.py', sha256: next };
      if (action === 'read') return { path: 'a.py', text: 'WIDTH = 3\n', sha256: next };
      throw new Error('Unexpected action: ' + action);
    };
    await Workbench.prototype.loadParameters.call(w);
    const label = w.el('parameters').children[0], input = label.children[1], button = label.children[2];
    input.value = '3'; const applying = button.fire('click');
    if (editDuring) {
      while (!calls.some((call) => call[1] === editDuring)) await flush();
      doc.edit('WIDTH = 2  # NEW draft\n', [21, 21]); held.resolve();
    }
    await applying;
    assert.equal(doc.baseSha, next, `${editDuring}: baseline must match the actual save receipt`);
    assert.equal(doc.baseText, 'WIDTH = 3\n');
    assert.equal(doc.text, editDuring ? 'WIDTH = 2  # NEW draft\n' : 'WIDTH = 3\n', `${editDuring}: newer draft lost`);
    assert.equal(doc.dirty, !!editDuring); assert.equal(rendered, 1);
    const dirty = calls.filter((call) => call[0] === 'dirty');
    assert.equal(dirty.at(-1)?.[1], !!editDuring, 'native dirty acknowledgment must reflect the surviving draft');
    if (editDuring) assert.equal(calls.find((call) => call[0] === 'save')?.[1].text, 'WIDTH = 2  # NEW draft\n');
    assert.equal(calls.find((call) => call[1] === 'setParameter')[2].expected_sha256, hash);
  }
});

test('parameter receipt hash mismatch does not rebase or replace a local draft', async () => {
  const [{ Workbench }, { TextDocument }] = await modules;
  const { w, doc } = harness(Workbench, TextDocument, 'WIDTH = 2\n');
  w.bridge.workbench = async (action) => action === 'parameters' ?
    { sha256: hash, parameters: [{ name: 'WIDTH', value: 2, kind: 'int' }], read_only: false } :
    action === 'setParameter' ? { sha256: next } : { text: 'external revision', sha256: hash };
  await Workbench.prototype.loadParameters.call(w);
  const label = w.el('parameters').children[0]; label.children[1].value = '3'; await label.children[2].fire('click');
  assert.equal(doc.text, 'WIDTH = 2\n'); assert.equal(doc.baseSha, hash); assert.equal(doc.baseText, 'WIDTH = 2\n');
  assert.match(w.el('workbench-error').textContent, /Source changed after parameter save/);
});

test('returning source text to its saved baseline refreshes certification without enabling dirty-source transforms', async () => {
  const [{ Workbench }, { TextDocument }] = await modules; const { w, doc } = harness(Workbench, TextDocument);
  const inspected = { stale: false, certified_hash: hash, manifest_sha256: hash };
  w.refreshInspection = async () => { w.inspection = inspected; };
  doc.edit('draft'); w.sourceChanged(doc); await flush();
  assert.equal(w.inspection, null); assert.equal(w.sourceDirty, true);
  doc.edit(doc.baseText); w.sourceChanged(doc); await flush();
  assert.equal(w.dirty, false); assert.deepEqual(w.inspection, inspected);
});

test('own pending transform remains editable while dirty source, stale certificates and wrong parts remain blocked', async () => {
  const [{ Workbench, documentKey }, { TextDocument }] = await modules; const { w, doc } = harness(Workbench, TextDocument);
  const render = () => Workbench.prototype.renderControls.call(w);
  w.readOnly = false; w.updateState = null;
  render(); assert.equal(w.el('transform-apply').disabled, false);
  w.pendingTransform = { project: 'TEST', root: '/project', part: 'Body', expected: hash, translation: [1, 0, 0], rotation: [0, 0, 0], scale: 1 };
  const manifest = new TextDocument({ path: 'viewer.json', text: '{"name":"TEST"}', sha256: hash }); manifest.root = '/project'; manifest.project = 'TEST';
  manifest.modelDerived = true; manifest.edit('{"name":"TEST","workbench":{"transforms":{}}}');
  w.documents.set(documentKey('/project', 'TEST', 'viewer.json'), manifest); render();
  assert.equal(w.el('transform-apply').disabled, false); assert.equal(w.el('translation-x').disabled, false);
  assert.equal(w.el('exportbtn').disabled, true, 'unsaved model inputs must still block export');
  doc.edit('unrelated draft'); render(); assert.equal(w.el('transform-apply').disabled, true);
  doc.edit(doc.baseText); w.inspection.stale = true; render(); assert.equal(w.el('transform-apply').disabled, true);
});

test('Open status is never reported successful when explicit project selection was canceled or superseded', async () => {
  const [{ Workbench }, { TextDocument }] = await modules; const { w } = harness(Workbench, TextDocument);
  w.guard = async () => true; w.bridge.workbench = async () => ({ project: 'OPENED' });
  w.viewer.refreshProjects = async () => false; w.syncView = async () => {}; w.choosePane = () => {};
  w.status('Opening project'); await assert.rejects(w.openProject('openFolder'), /selection was cancelled or superseded/);
  assert.notEqual(w.el('workbench-status').textContent, 'Project opened'); assert.equal(w.viewer.project, 'TEST');
  w.viewer.refreshProjects = async () => true;
  await assert.rejects(w.openProject('openFolder'), /selection changed before source loaded/);
});

test('source-backed transform undo uses a CAS receipt and rejects dirty source or root mismatch', async () => {
  const [{ Workbench, transformValues }, { TextDocument }] = await modules; const { w, doc, calls } = harness(Workbench, TextDocument);
  const values = transformValues([1, 2, 3], [4, 5, 6], 2);
  w.bridge.workbench = async (action, payload) => { calls.push([action, payload]); return { manifest_sha256: next, previous: { translation: [0, 0, 0], rotation: [0, 0, 0], scale: 1 } }; };
  await w.applyTransform(values); assert.equal(calls[0][1].expected_sha256, hash); assert.equal(w.transformUndo[0].expected, next); assert.equal(w.transformUndo[0].root, '/project');
  await w.undoTransform(); assert.equal(calls[2]?.[1]?.expected_sha256 || calls[1]?.[1]?.expected_sha256, next);
  doc.edit('dirty'); await assert.rejects(w.applyTransform(values), /Save source drafts/);
  assert.throws(() => transformValues([0, 0, 0], [0, 0, 0], 0));
});
