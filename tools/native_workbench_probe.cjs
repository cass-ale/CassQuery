'use strict';
// Native editor/IPC/CAD observation only. OS chooser selection and install/rollback remain separate gates.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { isDeepStrictEqual } = require('node:util');
const { pngDimensions } = require('./integration_suite.cjs');
const { request, recordWorkbenchTraffic, validateEvidence, THEMES, captureScripts, canvasPixels, validateRendering } = require('./workbench_suite.cjs');
const { settings: validateSettings } = require('../shell/desktop.cjs');
const PRELOAD_KEYS = ['bytes','method','path','resources_path','schema','sha256','web_contents_id'];
const BRIDGE_KEYS = ['onCommand','recovery','setDirty','settings','update','workbench'];

function validatePreloadRecord(identity, { resourcesPath, webContentsId, nativeSource }) {
  assert.ok(identity && typeof identity === 'object' && !Array.isArray(identity), 'Missing constructor preload identity');
  assert.deepEqual(Object.keys(identity).sort(), PRELOAD_KEYS, 'Invalid constructor preload identity fields');
  assert.equal(identity.schema, 1); assert.equal(identity.method, 'owned BrowserWindow constructor');
  assert.ok(typeof resourcesPath === 'string' && path.isAbsolute(resourcesPath) && path.resolve(resourcesPath) === resourcesPath);
  assert.equal(identity.resources_path, resourcesPath);
  assert.ok(Number.isSafeInteger(webContentsId) && webContentsId > 0); assert.equal(identity.web_contents_id, webContentsId);
  const expected = path.join(resourcesPath, 'app.asar', 'shell', 'preload.cjs');
  assert.ok(typeof identity.path === 'string' && path.isAbsolute(identity.path));
  assert.equal(identity.path, expected, 'Constructor preload path does not match actual app resources');
  assert.ok(Number.isSafeInteger(identity.bytes) && identity.bytes > 0 && identity.bytes <= 1024 ** 2);
  assert.match(identity.sha256, /^[a-f0-9]{64}$/);
  assert.ok(nativeSource && Array.isArray(nativeSource.files) && nativeSource.files.length === 8 && new Set(nativeSource.files.map(file => file.path)).size === 8);
  const source = nativeSource.files.find(file => file.path === 'shell/preload.cjs');
  assert.ok(source && source.bytes === identity.bytes && source.sha256 === identity.sha256, 'Preload identity differs from executed shell source');
  return expected;
}
function validatePreloadIdentity(identity, context) {
  const expected = validatePreloadRecord(identity, context);
  assert.equal(fs.lstatSync(expected).isSymbolicLink(), false, 'Preload must be a regular app resource');
  assert.equal(fs.realpathSync(expected), expected, 'Preload path is not canonical');
  const bytes = fs.readFileSync(expected);
  assert.equal(bytes.length, identity.bytes); assert.equal(hash(bytes), identity.sha256, 'Actual preload bytes differ from constructor/source identity');
}
function validatePreloadIPC(evidence, report) {
  assert.equal(evidence.node_integration, false); assert.equal(evidence.context_isolation, true); assert.equal(evidence.sandbox, true);
  assert.equal(evidence.bridge_present, true); assert.equal(evidence.project_root_matches, true);
  validatePreloadRecord(evidence.preload_identity, { resourcesPath: report.resources_path, webContentsId: report.web_contents_id, nativeSource: report.native_source });
  assert.deepEqual(evidence.preload_identity, report.preload_identity);
  assert.ok(Number.isSafeInteger(report.window_id) && report.window_id > 0);
  assert.deepEqual(evidence.bridge.keys, BRIDGE_KEYS); assert.deepEqual(evidence.bridge.function_types, BRIDGE_KEYS.map(() => 'function'));
  assert.equal(evidence.bridge.node_require_type, 'undefined'); assert.equal(evidence.bridge.node_process_type, 'undefined');
  const ipc = evidence.ipc;
  assert.equal(ipc.method, 'actual contextBridge invoke'); assert.equal(ipc.window_id, report.window_id); assert.equal(ipc.web_contents_id, report.web_contents_id);
  assert.equal(ipc.origin, report.worker_origin);
  const origin = new URL(ipc.origin); assert.equal(origin.protocol, 'http:'); assert.equal(origin.hostname, '127.0.0.1');
  assert.ok(Number.isInteger(Number(origin.port)) && Number(origin.port) > 0 && Number(origin.port) <= 65535 && origin.origin === ipc.origin);
  assert.deepEqual(ipc.settings_result, validateSettings(ipc.settings_result), 'Actual settings IPC response is incomplete or invalid');
  assert.equal(ipc.workbench_http_status, 200); assert.deepEqual(ipc.workbench_http_response, ipc.workbench_result);
  assert.ok(Array.isArray(report.worker_requests) && report.worker_requests.some(record => record.action === 'list' && record.project === 'WORKBENCHPLATE'
    && record.status === 200 && isDeepStrictEqual(record.response, ipc.workbench_result)), 'Preload IPC response is not bound to an actual worker HTTP trace');
  const listed = ipc.workbench_result;
  assert.equal(listed.project, 'WORKBENCHPLATE'); assert.equal(listed.root, report.project_root);
  assert.equal(listed.entry, 'part_entry.py'); assert.equal(Object.hasOwn(listed, 'error'), false);
  assert.ok(typeof report.project_root === 'string' && path.isAbsolute(report.project_root));
  assert.ok(Array.isArray(listed.files) && listed.files.length > 0 && listed.files.length <= 2048 && listed.files.every(file =>
    typeof file.path === 'string' && file.path && !path.isAbsolute(file.path) && !file.path.includes('\\') && file.path.split('/').every(part => part && part !== '.' && part !== '..')
    && Number.isSafeInteger(file.bytes) && file.bytes >= 0 && ['python','json','asset'].includes(file.kind)) &&
    listed.files.some(file => file.path === listed.entry && file.kind === 'python' && Number.isSafeInteger(file.bytes) && file.bytes > 0) &&
    listed.files.some(file => file.path === 'viewer.json' && file.kind === 'json' && file.bytes > 0) && new Set(listed.files.map(file => file.path)).size === listed.files.length,
    'Actual worker IPC project/file response is incomplete');
  assert.equal(listed.read_only, false);
}
const CHECKS = ['preload-ipc', 'edit-save-volume', 'external-conflict', 'syntax-retains-certified', 'eight-themes', 'font-tabs'];
const hash = (bytes) => createHash('sha256').update(bytes).digest('hex');
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
function ownedPath(ownedRoot, candidate) {
  const root = path.resolve(ownedRoot), resolved = path.resolve(candidate);
  return resolved === root || resolved.startsWith(root + path.sep);
}
function validateEditorReport(report, directory) {
  assert.equal(report.schema, 1); assert.equal(report.platform, 'native-electron-workbench-editor');
  assert.equal(report.editor_checks_pass, true); assert.equal(report.qualifies_workbench, false);
  assert.equal(report.native_dialog_selection.status, 'blocked');
  assert.equal(report.native_dialog_selection.reason, 'Native OS dialog selection was not exercised by this editor probe');
  assert.match(report.challenge, /^[a-f0-9]{64}$/);
  assert.ok(report.driver && Number.isInteger(report.driver.bytes) && report.driver.bytes > 0); assert.match(report.driver.sha256, /^[a-f0-9]{64}$/);
  assert.ok(report.native_source && Array.isArray(report.native_source.files) && report.native_source.files.length >= 8);
  assert.equal(report.packaged, true); assert.equal(report.errors.length, 0);
  assert.ok(Number.isInteger(report.app_pid) && report.app_pid > 0 && Number.isInteger(report.worker_pid) && report.worker_pid > 0 && report.app_pid !== report.worker_pid);
  assert.deepEqual(report.checks.map((c) => c.id), CHECKS);
  for (const check of report.checks) {
    assert.equal(check.pass, true);
    if (check.id === 'preload-ipc') {
      validatePreloadIPC(check.evidence, report);
    } else validateEvidence(check.id, check.evidence);
  }
  const root = fs.realpathSync(directory);
  assert.ok(report.images.length >= THEMES.length + 3);
  for (const record of report.images) {
    assert.ok(typeof record.path === 'string' && path.basename(record.path) === record.path);
    const file = path.join(root, record.path); assert.equal(fs.lstatSync(file).isSymbolicLink(), false);
    const bytes = fs.readFileSync(file); assert.equal(hash(bytes), record.sha256); assert.equal(bytes.length, record.bytes);
    const dimensions = pngDimensions(bytes); assert.equal(dimensions.width, record.width); assert.equal(dimensions.height, record.height);
    assert.ok(Array.isArray(record.viewport) && record.viewport.length === 2 && record.viewport.every((v) => Number.isInteger(v) && v >= 100));
    assert.ok(record.capture_scale >= 1 && [1, record.dpr].includes(record.capture_scale));
    assert.equal(record.width, record.viewport[0] * record.capture_scale); assert.equal(record.height, record.viewport[1] * record.capture_scale);
    const rect = record.rendering.canvas_rect;
    validateRendering(record, canvasPixels(bytes, Object.fromEntries(Object.entries(rect).map(([key, value]) => [key, value * record.capture_scale]))));
  }
  return true;
}

function observeNativeDialogs(directory, win, challenge) {
  const { dialog } = require('electron'), originals = new Map();
  const record = { schema: 1, app_pid: process.pid, challenge, method: 'observed actual Electron dialogs; no provider substitution', calls: [] };
  const file = path.join(directory, 'native-dialogs.json');
  const write = () => fs.writeFileSync(file, JSON.stringify(record, null, 2) + '\n'); write();
  for (const method of ['showOpenDialog', 'showSaveDialog', 'showMessageBox']) {
    const original = dialog[method]; originals.set(method, original);
    dialog[method] = async function (...args) {
      if (args[0] !== win) return original.apply(this, args);
      let action;
      try { action = JSON.parse(fs.readFileSync(path.join(directory, 'native-action.json'))); } catch { /* Unrequested dialogs are never qualifying input. */ }
      const call = { method, action_id: action?.id, properties: args[1]?.properties || [], buttons: args[1]?.buttons || [], started_at: Date.now(), result: null };
      record.calls.push(call); write();
      try { const result = await original.apply(this, args); call.result = method === 'showOpenDialog' ? { canceled: result.canceled, filePaths: result.filePaths } : method === 'showSaveDialog' ? { canceled: result.canceled, filePath: result.filePath } : { response: result.response }; return result; }
      finally { call.completed_at = Date.now(); write(); }
    };
  }
  win.once('closed', () => { for (const [method, original] of originals) dialog[method] = original; });
}

const FULL_CHECKS = ['native-open-file', 'native-open-folder', 'native-new-project', 'native-transform', 'native-export-step',
  'native-import-roundtrip', 'native-save-copy', 'native-cancel', 'dirty-cancel', 'dirty-save', 'dirty-discard'];

async function qualifyWorkbench(win, directory, context) {
  const { ownedRoot, projectRoot, challenge, driver, native_source, workerPid, workerPort, click, text, key, js, current, geometry, image, traffic, waitFor } = context;
  const full = { schema: 1, pass: false, qualifies_workbench: false, source_sha: process.env.GITHUB_SHA || null,
    app_pid: process.pid, worker_pid: workerPid, challenge, driver, native_source, checks: [], images: [],
    native_dialog_selection: { status: 'blocked', records: [] }, error: null, started_at: Date.now() };
  const projects = path.join(ownedRoot, 'projects'), exportsDir = path.join(ownedRoot, 'exports'); fs.mkdirSync(exportsDir, { recursive: true });
  const deadline = Date.now() + 240000; let actionId = 0;
  const write = () => fs.writeFileSync(path.join(directory, 'workbench-qualification.json'), JSON.stringify(full, null, 2) + '\n'); write();
  const check = (id, evidence) => full.checks.push({ id, pass: true, evidence });
  async function until(label, predicate) {
    while (Date.now() < deadline - 10000) { const value = await predicate(); if (value) return value; await delay(80); }
    throw new Error('Full native workbench deadline: ' + label);
  }
  async function select(name) {
    await waitFor(win, `Array.from(document.querySelectorAll('#projects button')).some(b=>b.textContent.includes(${JSON.stringify(name)}))`);
    const index = await js(`Array.from(document.querySelectorAll('#projects button')).findIndex(b=>b.textContent.includes(${JSON.stringify(name)}))`);
    await click(win, `#projects button:nth-child(${index + 1})`); await current(name);
  }
  async function nativeAction(kind, target, trigger, trust = true) {
    const id = ++actionId;
    const ledgerBefore = JSON.parse(fs.readFileSync(path.join(directory, 'native-dialogs.json')));
    const expectedMethod = kind === 'save' ? 'showSaveDialog' : 'showOpenDialog';
    const pending = ledgerBefore.calls.findLastIndex((call) => call.method === expectedMethod && !call.result);
    const observerIndex = pending >= 0 ? pending : ledgerBefore.calls.length;
    const instruction = { schema: 1, id, app_pid: process.pid, challenge, kind, target: target || null, trust, observer_index: observerIndex };
    fs.writeFileSync(path.join(directory, 'native-action.json'), JSON.stringify(instruction) + '\n');
    await trigger();
    const input = await until('standard OS input ' + id, () => {
      const file = path.join(directory, 'native-action-result.json');
      if (!fs.existsSync(file)) return false; const value = JSON.parse(fs.readFileSync(file)); return value.id === id ? value : false;
    });
    assert.equal(input.challenge, challenge); assert.equal(input.app_pid, process.pid);
    if (!input.pass) throw new Error('Native OS input blocked: ' + input.error);
    const observed = await until('actual native dialog result ' + id, () => {
      const ledger = JSON.parse(fs.readFileSync(path.join(directory, 'native-dialogs.json')));
      const call = ledger.calls[observerIndex];
      return call && call.method === expectedMethod && call.result ? call : false;
    });
    if (kind === 'cancel') { assert.equal(observed.result.canceled, true); assert.deepEqual(observed.result.filePaths, []); }
    else {
      assert.equal(observed.result.canceled, false);
      const selected = kind === 'save' ? observed.result.filePath : observed.result.filePaths[0];
      assert.equal(path.resolve(selected), path.resolve(target), 'OS result differs from actual requested public selection');
    }
    const record = { id, kind, target, observer_index: observerIndex, native_call: observed, os_input: input };
    full.native_dialog_selection.records.push(record); full.images.push(input.image); return record;
  }
  async function frame(name) { const record = await image('full-' + name); full.images.push(record); }
  async function exportStep(target) {
    await click(win, '#exportbtn');
    assert.equal(await js(`document.getElementById('export-format').value`), 'step');
    if (!await js(`document.getElementById('export-selected').checked`)) await click(win, '#export-selected');
    await nativeAction('save', target, () => click(win, '#export-confirm'), false);
    await until('actual STEP file published', () => fs.existsSync(target) && fs.statSync(target).size > 100);
    assert.ok(fs.readFileSync(target, 'utf8').startsWith('ISO-10303-21;')); return hash(fs.readFileSync(target));
  }
  try {
    const fileRoot = path.join(projects, 'NATIVEDIALOGFILE'), file = path.join(fileRoot, 'part_entry.py'), fileBefore = hash(fs.readFileSync(file));
    await nativeAction('open', file, () => click(win, '#openfile'));
    await current('NATIVEDIALOGFILE'); assert.equal(hash(fs.readFileSync(file)), fileBefore);
    check('native-open-file', { path: file, source_before: fileBefore, source_after: fileBefore }); await frame('open-file');
    const folder = path.join(projects, 'NATIVEDIALOGFOLDER');
    await nativeAction('folder', folder, () => click(win, '#openfolder')); await current('NATIVEDIALOGFOLDER');
    check('native-open-folder', { root: folder, actual_registered_project: 'NATIVEDIALOGFOLDER' }); await frame('open-folder');
    await click(win, '#newproject'); await text('#project-name', 'NATIVENEWPLATE');
    await nativeAction('folder', projects, () => { key('Enter'); }, true); await current('NATIVENEWPLATE');
    const created = path.join(projects, 'NATIVENEWPLATE'); assert.ok(fs.existsSync(path.join(created, 'viewer.json')) && fs.existsSync(path.join(created, 'part_entry.py')));
    check('native-new-project', { root: created, manifest_sha256: hash(fs.readFileSync(path.join(created, 'viewer.json'))) }); await frame('new-project');
    await select('WORKBENCHPLATE'); const before = await geometry('WORKBENCHPLATE');
    await click(win, '#part-tree button[data-part="Base"]');
    for (const [selector, value] of [['#translation-x', '3'], ['#translation-y', '-2'], ['#translation-z', '5'], ['#rotation-z', '90'], ['#uniform-scale', '1.5']]) { await text(selector, value); key('Tab'); }
    await click(win, '#transform-apply'); await current('WORKBENCHPLATE'); const after = await geometry('WORKBENCHPLATE');
    assert.ok(Math.abs(after.volume / before.volume - 3.375) < 1e-5);
    check('native-transform', { before, after, persisted: JSON.parse(fs.readFileSync(path.join(projectRoot, 'viewer.json'))).workbench.transforms.Base }); await frame('transform');
    const exported = path.join(exportsDir, 'native-export.step');
    await click(win, '#part-tree button[data-part="Base"]');
    await waitFor(win, `window.__cq.selected === 'Base' && window.__cq.model.activeName === 'neutral' && document.querySelector('#part-tree button[data-part="Base"]').getAttribute('aria-pressed') === 'true'`);
    const exportHash = await exportStep(exported);
    fs.copyFileSync(exported, path.join(directory, 'native-export.step'));
    check('native-export-step', { path: 'native-export.step', bytes: fs.statSync(exported).size, sha256: exportHash, geometry: after }); await frame('export');
    await click(win, '#importbtn'); await text('#project-name', 'NATIVEROUNDTRIP');
    await nativeAction('open', exported, () => { key('Enter'); }, false);
    await nativeAction('folder', projects, async () => {}, true); await current('NATIVEROUNDTRIP');
    const importedParts = (await request('http://127.0.0.1:' + workerPort, '/api/parts?project=NATIVEROUNDTRIP')).json.parts;
    assert.equal(importedParts.length, 1); const imported = { volume: importedParts[0].volume, solids: importedParts[0].solids, min: importedParts[0].bbox.slice(0, 3), max: importedParts[0].bbox.slice(3) };
    assert.ok(Math.abs(imported.volume - after.volume) < after.volume * 1e-6);
    for (const field of ['min', 'max']) assert.ok(imported[field].every((value, i) => Math.abs(value - after[field][i]) < 1e-5));
    assert.equal(hash(fs.readFileSync(exported)), exportHash);
    check('native-import-roundtrip', { exported_geometry: after, imported_geometry: imported, export_sha256: exportHash }); await frame('import');
    const importedRoot = path.join(projects, 'NATIVEROUNDTRIP'), metadata = JSON.parse(fs.readFileSync(path.join(importedRoot, 'viewer.json')));
    const sourceFile = path.join(importedRoot, metadata.workbench?.source || metadata.entry.split(':')[0]);
    const savedSource = fs.readFileSync(sourceFile, 'utf8'), draft = savedSource + '\n# keep this native Save Copy draft\n'; await text('#source-editor', draft);
    const copy = path.join(exportsDir, 'native-copy.py');
    await nativeAction('save', copy, () => click(win, '#saveas'), false);
    await until('actual Save Copy bytes', () => fs.existsSync(copy) && fs.readFileSync(copy, 'utf8') === draft);
    assert.equal(fs.readFileSync(sourceFile, 'utf8'), savedSource); assert.equal(await js('document.getElementById("source-editor").value'), draft);
    check('native-save-copy', { copy_sha256: hash(fs.readFileSync(copy)), draft_sha256: hash(Buffer.from(draft)), original_sha256: hash(fs.readFileSync(sourceFile)), active_project_changed: false }); await frame('save-copy');
    const dialogsBefore = JSON.parse(fs.readFileSync(path.join(directory, 'native-dialogs.json'))).calls.length;
    await click(win, '#openfolder'); await waitFor(win, `document.getElementById('draft-guard').open`); await click(win, '#draft-guard button[value="cancel"]');
    assert.equal(await js('document.getElementById("source-editor").value'), draft); assert.equal(JSON.parse(fs.readFileSync(path.join(directory, 'native-dialogs.json'))).calls.length, dialogsBefore);
    check('dirty-cancel', { draft_before: hash(Buffer.from(draft)), draft_after: hash(Buffer.from(await js('document.getElementById("source-editor").value'))), source_unchanged: fs.readFileSync(sourceFile, 'utf8') === savedSource });
    await click(win, '#openfolder'); await waitFor(win, `document.getElementById('draft-guard').open`);
    await nativeAction('folder', folder, () => click(win, '#draft-guard button[value="save"]'), true); await current('NATIVEDIALOGFOLDER');
    assert.equal(fs.readFileSync(sourceFile, 'utf8'), draft); check('dirty-save', { actual_source_sha256: hash(fs.readFileSync(sourceFile)), expected_draft_sha256: hash(Buffer.from(draft)) });
    await select('NATIVEROUNDTRIP'); const discard = draft + '\n# discard only this unsaved draft\n'; await text('#source-editor', discard);
    await click(win, '#openfolder'); await waitFor(win, `document.getElementById('draft-guard').open`);
    await nativeAction('folder', folder, () => click(win, '#draft-guard button[value="discard"]'), true); await current('NATIVEDIALOGFOLDER');
    assert.equal(fs.readFileSync(sourceFile, 'utf8'), draft); check('dirty-discard', { source_sha256: hash(fs.readFileSync(sourceFile)), saved_sha256: hash(Buffer.from(draft)) });
    const beforeCancel = (await request('http://127.0.0.1:' + workerPort, '/api/status')).json.projects.map((p) => [p.name, p.root]);
    await nativeAction('cancel', null, () => click(win, '#openfile'), false);
    const afterCancel = (await request('http://127.0.0.1:' + workerPort, '/api/status')).json.projects.map((p) => [p.name, p.root]); assert.deepEqual(afterCancel, beforeCancel);
    check('native-cancel', { registry_before: beforeCancel, registry_after: afterCancel });
    assert.deepEqual([...full.checks.map((c) => c.id)].sort(), [...FULL_CHECKS].sort());
    full.native_dialog_selection.status = 'checked'; full.pass = true; full.qualifies_workbench = true;
  } catch (error) { full.error = error.stack || String(error); }
  full.completed_at = Date.now(); write(); return full;
}

async function probe(win, directory, { ownedRoot, projectRoot, projectName = 'WORKBENCHPLATE', workerPort, workerPid, challenge, driver, native_source, preload_identity, resources_path, worker_origin, window_id, web_contents_id }) {
  const { app } = require('electron');
  const bundled = path.join(process.resourcesPath, 'app.asar', 'shell');
  const { click, waitFor, evaluate, bounded, measureFrame } = require(path.join(bundled, 'smoke.cjs'));
  const { recordRendererConsole } = require(path.join(bundled, 'renderer_config.cjs'));
  directory = path.resolve(directory); ownedRoot = path.resolve(ownedRoot); projectRoot = path.resolve(projectRoot);
  // This rejects the installed, currently running user's app before any input or source access.
  assert.ok(app.isPackaged && process.env.CASSQUERY_USER_DATA && process.env.CASSQUERY_DATA_DIR, 'Native workbench probe requires an isolated packaged launch');
  for (const file of [directory, projectRoot, path.resolve(process.env.CASSQUERY_USER_DATA), path.resolve(process.env.CASSQUERY_DATA_DIR)])
    assert.ok(ownedPath(ownedRoot, file), 'Probe input/output/state must remain in its owned temporary root');
  assert.equal(path.resolve(app.getPath('userData')), path.resolve(process.env.CASSQUERY_USER_DATA), 'userData was not isolated before app startup');
  assert.equal(projectName, 'WORKBENCHPLATE'); assert.ok(Number.isInteger(workerPid) && workerPid > 0 && workerPid !== process.pid);
  assert.equal(new URL(win.webContents.getURL()).origin, 'http://127.0.0.1:' + workerPort);
  assert.equal(fs.existsSync(path.join(directory, 'workbench-editor.json')), false, 'Native workbench evidence must be fresh');
  fs.mkdirSync(directory, { recursive: true });
  const report = { schema: 1, platform: 'native-electron-workbench-editor', editor_checks_pass: false,
    qualifies_workbench: false, pass: false, packaged: app.isPackaged, app_pid: process.pid, worker_pid: workerPid,
    started_at: Date.now(), checks: [], images: [], errors: [], challenge, driver, native_source, worker_port: workerPort,
    preload_identity, resources_path, worker_origin, window_id: win.id, web_contents_id: win.webContents.id, project_root: projectRoot,
    initial_project_selection: 'configured startup public fixture; not native OS dialog proof',
    native_dialog_selection: { status: 'blocked', reason: 'Native OS dialog selection was not exercised by this editor probe' },
    not_checked: ['native OS dialog selection/open-file events', 'STEP export/import through native chooser', 'update install/rollback', 'human visual approval'] };
  let end = Date.now() + 240000;
  const origin = 'http://127.0.0.1:' + workerPort;
  const traffic = recordWorkbenchTraffic(workerPort), frames = [], debuggerAPI = win.webContents.debugger;
  report.worker_requests = traffic.trace;
  let ownsDebugger = false;report.renderer_requests=[];
  const rendererRequests=new Map();
  const observeFrame = (_event, method, params) => {
    if(method==='Network.requestWillBeSent'&&report.renderer_requests.length<200){
      const url=new URL(params.request.url);
      if(url.origin===origin&&url.pathname==='/api/rebuild'){
        const row={path:url.pathname,method:params.request.method,status:null,at_ms:Date.now()};
        rendererRequests.set(params.requestId,row);report.renderer_requests.push(row);
      }
    }else if(method==='Network.responseReceived'&&rendererRequests.has(params.requestId))rendererRequests.get(params.requestId).status=params.response.status;
    if (method === 'Network.webSocketFrameReceived' && frames.length < 1000) {
      try { frames.push(JSON.parse(params.response.payloadData)); } catch { /* Non-JSON frames are not certified announcements. */ }
    }
  };
  const js = (code) => evaluate(win, code, Math.max(1, Math.min(10000, end - Date.now())));
  const consoleError = (event) => recordRendererConsole(report.errors, event);
  const rendererGone = (_event, details) => report.errors.push('Renderer gone: ' + details.reason);
  win.webContents.on('console-message', consoleError); win.webContents.on('render-process-gone', rendererGone);
  const key = (keyCode, modifiers = []) => { win.webContents.sendInputEvent({ type: 'keyDown', keyCode, modifiers }); win.webContents.sendInputEvent({ type: 'keyUp', keyCode, modifiers }); };
  const modifier = process.platform === 'darwin' ? 'meta' : 'control';
  const shortcut = (code) => key(code, [modifier]);
  async function selectOption(selector, value) {
    const specification = selector === '#theme-select' ? { key:'theme', allowed:['counterform-dark','counterform-light','graphite','midnight','forest','sand','paper','high-contrast'] }
      : selector === '#tab-size' ? { key:'tabSize', allowed:['2','4'] } : null;
    assert.ok(specification && specification.allowed.includes(value), 'Unsupported assurance select target');
    report.select_diagnostics ||= [];
    const audit={method:'native keyboard closed-select typeahead',selector,expected_value:value,phases:[],pass:false};
    const persist=()=>fs.writeFileSync(path.join(directory,'select-diagnostics.json'),JSON.stringify(report.select_diagnostics,null,2)+'\n');
    report.select_diagnostics.push(audit);
    async function observed(phase) {
      const result=await js(`(() => {const e=document.querySelector(${JSON.stringify(selector)}),r=window.__cqNativeSelectAudit;
        if(r)r.phase=${JSON.stringify(phase)};
        return {phase:${JSON.stringify(phase)},active:document.activeElement?.id,disabled:e.disabled,open:e.matches(':open'),value:e.value,index:e.selectedIndex,
          theme:document.documentElement.dataset.theme,events:r?.events||[],dropped:r?.dropped||0};})()`);
      try { result.persisted=JSON.parse(fs.readFileSync(path.join(process.env.CASSQUERY_USER_DATA,'workbench-settings.json'))); }
      catch(error) { if(error.code!=='ENOENT')throw error;result.persisted=null; }
      audit.phases.push(result);persist();return result;
    }
    await js(`(() => {if(window.__cqNativeSelectAudit)throw new Error('Native select observer already owned');
      const recorder={phase:'focus navigation',events:[],dropped:0};recorder.listener=event=>{if(recorder.events.length>=500){recorder.dropped++;return;}
        const select=document.querySelector(${JSON.stringify(selector)});recorder.events.push({type:event.type,key:event.key,phase:recorder.phase,
          target:event.target?.id,value:select.value,index:select.selectedIndex,trusted:event.isTrusted});};
      for(const type of ['focusin','focusout','keypress','input','change'])document.addEventListener(type,recorder.listener,true);
      window.__cqNativeSelectAudit=recorder;})()`);
    try {
      await waitFor(win,`document.getElementById('settings-dialog').open && !!document.activeElement?.closest('#settings-dialog')`,5000);
      const target=selector.slice(1);
      if(await js(`document.activeElement.id===${JSON.stringify(target)}`)){key('Tab');await waitFor(win,`document.activeElement.id!==${JSON.stringify(target)}`,5000);}
      for(let steps=0;await js(`document.activeElement.id!==${JSON.stringify(target)}`);steps++){
        assert.ok(steps<32,'Native select focus was not reached within bounded keyboard navigation');key('Tab');
        await js('new Promise(resolve=>requestAnimationFrame(resolve))');
      }
      const before=await observed('closed select focused');assert.equal(before.active,target);assert.equal(before.disabled,false);assert.equal(before.open,false);
      const option=await js(`Array.from(document.querySelector(${JSON.stringify(selector)}).options).map((option,index)=>({value:option.value,label:option.text,index})).filter(option=>option.value===${JSON.stringify(value)})`);
      assert.equal(option.length,1);assert.ok(option[0].label.length>0&&option[0].label.length<=80&&/^[\x20-\x7e]+$/.test(option[0].label));
      await js("window.__cqNativeSelectAudit.phase='native typeahead characters'");
      for(const character of option[0].label)win.webContents.sendInputEvent({type:'char',keyCode:character});
      await waitFor(win,`(() => {const e=document.querySelector(${JSON.stringify(selector)}),r=window.__cqNativeSelectAudit;
        return document.activeElement===e&&!e.matches(':open')&&e.value===${JSON.stringify(value)}&&e.selectedIndex===${option[0].index}&&
          ['input','change'].every(type=>r.events.some(event=>event.phase==='native typeahead characters'&&event.type===type&&event.target===e.id&&event.value===${JSON.stringify(value)}&&event.trusted));})()`,5000);
      if(specification.key==='theme')await waitFor(win,`document.documentElement.dataset.theme===${JSON.stringify(value)}`,5000);
      await until('actual selected setting persisted',()=>{
        const stored=JSON.parse(fs.readFileSync(path.join(process.env.CASSQUERY_USER_DATA,'workbench-settings.json')));
        return stored[specification.key]===(specification.key==='tabSize'?Number(value):value);
      });
      await observed('native value/change/persistence acknowledged');key('Tab');
      await waitFor(win,`document.activeElement.id!==${JSON.stringify(target)}`,5000);
      await observed('native blur resets typeahead');audit.pass=true;persist();
    }catch(error){
      try{await observed('failure before throw');}catch(diagnosticError){audit.diagnostic_error=diagnosticError.message;persist();}
      try{await image('select-failure-'+specification.key);}catch(captureError){audit.capture_error=captureError.message;persist();}
      throw error;
    }finally{await js(`(() => {const r=window.__cqNativeSelectAudit;if(r){for(const type of ['focusin','focusout','keypress','input','change'])document.removeEventListener(type,r.listener,true);delete window.__cqNativeSelectAudit;}})()`);}
  }
  async function text(selector, value) {
    assert.ok(Buffer.byteLength(value) <= 16384, 'Probe text exceeds public-fixture input bound');
    report.text_diagnostics ||= [];
    const sequence = report.text_diagnostics.length + 1, name = `text-input-${sequence}.json`;
    const audit = { schema:1, method:'native focused IME text insertion with native Edit selection',
      selection_method:'native webContents.selectAll edit command', selector,
      expected_length:value.length, expected_sha256:hash(Buffer.from(value)), phases:[], sent_events:[], pass:false };
    const persist = () => {
      const bytes=Buffer.from(JSON.stringify(audit,null,2)+'\n'); fs.writeFileSync(path.join(directory,name),bytes);
      report.text_diagnostics[sequence-1]={path:name,bytes:bytes.length,sha256:hash(bytes),pass:audit.pass};
    };
    async function snapshot(phase) {
      const observed=await js(`(() => {
        const e=document.querySelector(${JSON.stringify(selector)}),doc=window.__workbench?.active;
        const recorder=window.__cqNativeInputAudit;if(recorder)recorder.phase=${JSON.stringify(phase)};
        return {phase:${JSON.stringify(phase)},active_element:document.activeElement?.id||document.activeElement?.tagName,
          disabled:!!e?.disabled,read_only:!!e?.readOnly,type:e?.type,selection_start:e?.selectionStart,selection_end:e?.selectionEnd,
          value_length:e?.value?.length,value:e?.value?.slice(0,16384),
          document:doc?{path:doc.path,base_sha256:doc.baseSha,revision:doc.revision,dirty:doc.dirty}:null,
          events:recorder?.events||[],dropped_events:recorder?.dropped||0};
      })()`);
      const encoded=Buffer.from(observed.value||''),bounded=encoded.subarray(0,16384);
      observed.value_truncated=encoded.length>bounded.length||observed.value_length>(observed.value||'').length;
      observed.value=bounded.toString('utf8');observed.value_sha256=observed.value_truncated?null:hash(encoded);
      observed.bounded_value_sha256=hash(bounded);audit.phases.push(observed);persist();return observed;
    }
    await js(`(() => {
      if(window.__cqNativeInputAudit)throw new Error('Input observer already owned');
      const recorder={phase:'before click',events:[],dropped:0};
      recorder.listener=e=>{if(recorder.events.length>=2000){recorder.dropped++;return;}
        recorder.events.push({type:e.type,phase:recorder.phase,key:e.key,input_type:e.inputType,data:e.data?.slice(0,64),
          target:e.target?.id||e.target?.tagName,active:document.activeElement?.id,selection_start:e.target?.selectionStart,
          selection_end:e.target?.selectionEnd,value_length:e.target?.value?.length,value_prefix:e.target?.value?.slice(0,96),trusted:e.isTrusted});};
      for(const type of ['focusin','keydown','keyup','beforeinput','input','select'])document.addEventListener(type,recorder.listener,true);
      window.__cqNativeInputAudit=recorder;
    })()`);
    try {
      const initial=await snapshot('before click');win.focus();await click(win,selector);
      await waitFor(win,`(() => {const e=document.querySelector(${JSON.stringify(selector)});return document.activeElement===e&&!e.readOnly&&!e.disabled;})()`,5000);
      await snapshot('after focused writable click');win.webContents.selectAll();
      await waitFor(win,`(() => {const e=document.querySelector(${JSON.stringify(selector)}),r=window.__cqNativeInputAudit;
        if(document.activeElement!==e||e.readOnly||e.disabled)return false;
        if(e.selectionStart!==null)return e.selectionStart===0&&e.selectionEnd===e.value.length;
        // Number controls do not expose a DOM selection range. Native Edit selection is setup only;
        // exact replacement and trusted IME events below must prove its effect, not a fabricated key acknowledgement.
        return e.type==='number';})()`,5000);
      const selected=await snapshot('after observed select-all');audit.selection_range_supported=selected.selection_start!==null;
      await js("window.__cqNativeInputAudit.phase='native IME commit'");
      audit.sent_events.push({type:'native IME insertText',bytes:Buffer.byteLength(value)});
      await bounded(()=>win.webContents.insertText(value),10000,'Native IME text insertion');
      await waitFor(win,`(() => {const e=document.querySelector(${JSON.stringify(selector)}),r=window.__cqNativeInputAudit,events=r.events.filter(event=>event.phase==='native IME commit'&&event.target===e.id);
        return e.value===${JSON.stringify(value)}&&events.some(event=>event.type==='beforeinput'&&event.trusted)&&events.some(event=>event.type==='input'&&event.trusted);})()`,15000);
      if(selector==='#source-editor')await waitFor(win,`(() => {const d=window.__workbench?.active;return d?.text===${JSON.stringify(value)}&&
        (${initial.value_sha256===hash(Buffer.from(value))?'true':`d.revision>${initial.document?.revision??-1}`});})()`,5000);
      await snapshot('exact native input/document acknowledged');audit.pass=true;persist();
    } catch(error) {
      try {const failed=await snapshot('failure');console.error('PUBLIC_NATIVE_INPUT_DIAGNOSTIC',JSON.stringify({path:name,selector,expected_length:value.length,
        actual_length:failed.value_length,actual_sha256:failed.value_sha256,active_element:failed.active_element,read_only:failed.read_only,
        selection:[failed.selection_start,failed.selection_end],value_prefix:failed.value.slice(0,256)}));}
      catch(diagnosticError){audit.diagnostic_error=diagnosticError.message;persist();}
      try{await image(`text-failure-${sequence}`);}catch(captureError){audit.capture_error=captureError.message;persist();}
      throw error;
    } finally {
      try{await js(`(() => {const r=window.__cqNativeInputAudit;if(r){for(const type of ['focusin','keydown','keyup','beforeinput','input','select'])document.removeEventListener(type,r.listener,true);delete window.__cqNativeInputAudit;}})()`);}
      catch(cleanupError){audit.observer_cleanup_error=cleanupError.message;persist();}
    }
  }
  async function until(label, predicate) {
    while (Date.now() < end - 10000) { const value = await predicate(); if (value) return value; await delay(100); }
    throw new Error('Native workbench deadline: ' + label);
  }
  async function current(name = projectName) {
    return until('fresh certified model', async () => {
      const response = await request(origin, '/api/status?project=' + encodeURIComponent(name));
      if (response.json.state !== 'certified') return false;
      const index = await request(origin, '/payload/' + name + '-certified.json');
      const shown = await js(`window.__cq?.model?.index.project === ${JSON.stringify(name)} && window.__cq.model.tier === 'certified' && window.__cq.model.hash === ${JSON.stringify(index.json.hash)}`);
      return shown ? index.json : false;
    });
  }
  async function geometry(name = projectName) {
    const response = await request(origin, '/api/parts?project=' + encodeURIComponent(name));
    const part = response.json.parts.find((p) => p.name === 'Base'); assert.ok(part);
    return { volume: part.volume, solids: part.solids, min: part.bbox.slice(0, 3), max: part.bbox.slice(3) };
  }
  async function image(name) {
    // Reuse the source gate, but serialize draw eligibility from the actual packaged helper.
    const { sample, settled } = captureScripts(measureFrame);
    const layout = await js(settled);
    assert.deepEqual(win.getContentSize(), layout.viewport);
    if (layout.viewport[0] < 100 || layout.viewport[1] < 100) throw new Error('Native viewport is empty');
    const native = await bounded(() => win.webContents.capturePage(), 15000, 'Native workbench capture'), bytes = native.toPNG(), file = name + '.png';
    const dimensions = pngDimensions(bytes), captureScale = dimensions.width / layout.viewport[0];
    assert.ok(captureScale === 1 || captureScale === layout.dpr); assert.equal(dimensions.height, layout.viewport[1] * captureScale);
    const after = await js(sample);
    assert.deepEqual(after.rendering.canvas_rect, layout.rendering.canvas_rect, 'Native canvas moved during capture');
    assert.deepEqual(after.rendering.buffer, layout.rendering.buffer, 'Native canvas resized during capture');
    assert.equal(after.rendering.model_hash, layout.rendering.model_hash, 'Native model changed during capture');
    fs.writeFileSync(path.join(directory, file), bytes);
    const rect = Object.fromEntries(Object.entries(layout.rendering.canvas_rect).map(([key, value]) => [key, value * captureScale]));
    const record = { path: file, bytes: bytes.length, sha256: hash(bytes), ...dimensions, ...layout, capture_scale: captureScale, canvas_pixels: canvasPixels(bytes, rect) };
    report.images.push(record); validateRendering(record, record.canvas_pixels); return record;
  }
  function check(id, evidence) { if (id !== 'preload-ipc') validateEvidence(id, evidence); report.checks.push({ id, pass: true, evidence }); }
  async function saved(expectedText) {
    const before=await js(`({busy:window.__workbench?.busy,dirty:window.__workbench?.dirty,base_sha256:window.__workbench?.active?.baseSha,status:document.getElementById('workbench-status').textContent})`);
    const start=traffic.trace.length;shortcut('S');
    const response=await until('actual save HTTP response',()=>traffic.trace.slice(start).find(r=>r.action==='save'&&r.status&&r.response));
    if(response.status===200){
      const disk=fs.readFileSync(path.join(projectRoot,'part_entry.py'));
      assert.equal(disk.toString('utf8'),expectedText);assert.equal(hash(disk),response.response.sha256);
      await waitFor(win,`(() => {const w=window.__workbench,d=w?.active;return w?.busy===0&&!w.dirty&&d?.baseSha===${JSON.stringify(response.response.sha256)}&&d.text===${JSON.stringify(expectedText)}&& !['Saving source','Working'].includes(document.getElementById('workbench-status').textContent);})()`,15000);
    }else await waitFor(win,'window.__workbench?.busy===0 && window.__workbench.active?.conflicted && !document.getElementById("conflict-banner").hidden',15000);
    report.save_acknowledgements ||= [];
    report.save_acknowledgements.push({status:response.status,sha256:response.response.sha256||null,before,
      ui:await js(`({busy:window.__workbench?.busy,dirty:window.__workbench?.dirty,base_sha256:window.__workbench?.active?.baseSha,status:document.getElementById('workbench-status').textContent})`)});
    return response;
  }
  async function journalBuild(phase) {
    report.build_diagnostics ||= [];
    const ui=await js(`({busy:window.__workbench?.busy,dirty:window.__workbench?.dirty,base_sha256:window.__workbench?.active?.baseSha,
      status:document.getElementById('workbench-status').textContent,diagnostic:document.getElementById('diag').textContent,
      model:{hash:window.__cq?.model?.hash,tier:window.__cq?.model?.tier,built_at:window.__cq?.model?.index.built_at}})`);
    report.build_diagnostics.push({phase,at_ms:Date.now(),ui,renderer_requests:[...report.renderer_requests]});
    fs.writeFileSync(path.join(directory,'build-diagnostics.json'),JSON.stringify(report.build_diagnostics,null,2)+'\n');
  }
  async function buildKey() {
    await waitFor(win,'window.__workbench?.busy===0',15000);
    const start=report.renderer_requests.length;shortcut('B');
    try {
      await journalBuild('actual native B sent');
      const response=await until('actual keyboard rebuild HTTP response',()=>report.renderer_requests.slice(start).find(r=>r.path==='/api/rebuild'&&r.method==='POST'&&r.status));
      assert.equal(response.status,202,'Actual rebuild shortcut did not queue a build');
      await journalBuild('actual keyboard POST202 acknowledged');
    }catch(error){try{await journalBuild('keyboard rebuild failure');}catch(diagnosticError){report.build_diagnostic_error=diagnosticError.message;}throw error;}
  }
  try {
    assert.equal(debuggerAPI.isAttached(), false, 'Native workbench observer cannot borrow a debugger');
    debuggerAPI.attach('1.3'); ownsDebugger = true; debuggerAPI.on('message', observeFrame);
    await bounded(() => debuggerAPI.sendCommand('Network.enable'), 10000, 'Native workbench network observer');
    const prefs = win.webContents.getLastWebPreferences();
    assert.equal(prefs.nodeIntegration, false); assert.equal(prefs.contextIsolation, true); assert.equal(prefs.sandbox, true);
    // Electron45's preference snapshot intentionally omits preload paths. Verify the constructor-bound identity and real bridge instead.
    assert.equal(resources_path, process.resourcesPath); assert.equal(window_id, win.id); assert.equal(web_contents_id, win.webContents.id);
    assert.equal(worker_origin, origin); assert.equal(new URL(win.webContents.getURL()).origin, worker_origin);
    validatePreloadIdentity(preload_identity, { resourcesPath: process.resourcesPath, webContentsId: win.webContents.id, nativeSource: native_source });
    const status = (await request(origin, '/api/status')).json;
    const selected = status.projects.find((p) => p.name === projectName);
    assert.ok(selected && path.resolve(selected.root) === projectRoot, 'Wrapper must register the owned fixture through the actual startup project path');
    await waitFor(win, `!!window.cassquery?.workbench && !!document.getElementById('source-editor')`);
    const invocationStart = traffic.trace.length;
    const observed = await js(`(async () => {
      const bridge=window.cassquery,keys=Object.keys(bridge).sort();
      return {bridge:{keys,function_types:keys.map(key=>typeof bridge[key]),node_require_type:typeof window.require,node_process_type:typeof window.process},
        settings_result:await bridge.settings('get'),workbench_result:await bridge.workbench('list',{project:'WORKBENCHPLATE'})};
    })()`);
    assert.deepEqual(observed.bridge.keys, BRIDGE_KEYS); assert.deepEqual(observed.bridge.function_types, BRIDGE_KEYS.map(() => 'function'));
    const listedResponse = await until('actual preload worker IPC response', () => traffic.trace.slice(invocationStart).find(record =>
      record.action === 'list' && record.project === projectName && record.status === 200 && record.response && isDeepStrictEqual(record.response, observed.workbench_result)));
    assert.deepEqual(observed.settings_result, require(path.join(bundled, 'desktop.cjs')).settings(observed.settings_result));
    const preloadEvidence = { node_integration: prefs.nodeIntegration, context_isolation: prefs.contextIsolation,
      sandbox: prefs.sandbox, bridge_present: true, project_root_matches: true, preload_identity, bridge: observed.bridge,
      ipc: { method:'actual contextBridge invoke',window_id:win.id,web_contents_id:win.webContents.id,origin:worker_origin,
        settings_result:observed.settings_result,workbench_result:observed.workbench_result,workbench_http_status:listedResponse.status,workbench_http_response:listedResponse.response } };
    validatePreloadIPC(preloadEvidence, report); check('preload-ipc', preloadEvidence);
    const entry = path.join(projectRoot, 'part_entry.py'), original = fs.readFileSync(entry, 'utf8');
    assert.ok(original.includes('.box(40, 30, 6,'), 'Probe requires the unmodified public TESTPLATE geometry');
    await waitFor(win, `document.getElementById('source-editor').value === ${JSON.stringify(original)}`);
    const before = await geometry(), indexBefore = await current(), edited = original.replace('.box(40, 30, 6,', '.box(42, 30, 6,');
    await text('#source-editor', edited); const saveResponse = await saved(edited); assert.equal(saveResponse.status, 200);
    // Both commands are real renderer key input; a disabled auto-build setting is not bypassed.
    await buildKey(); await until('new certified build time', async () => (await current()).built_at > indexBefore.built_at);
    const after = await geometry(), gates = (await request(origin, '/api/gates?project=' + projectName)).json;
    check('edit-save-volume', { before_source: hash(Buffer.from(original)), after_source: hash(fs.readFileSync(entry)), before, after,
      save_status: saveResponse.status, renderer_tier: await js('__cq.model.tier'), gate_ids: gates.checks.map((g) => g.id), gates_pass: gates.pass });
    await image('edit-certified');
    const draft = edited.replace('.box(42, 30, 6,', '.box(43, 30, 6,'), external = edited + '\n# external public native-probe edit\n';
    await text('#source-editor', draft); fs.writeFileSync(entry, external); const conflictResponse = await saved(draft); assert.equal(conflictResponse.status, 409);
    await waitFor(win, `!document.getElementById('conflict-banner').hidden`);
    check('external-conflict', { status: conflictResponse.status, draft_before: hash(Buffer.from(draft)), draft_after: hash(Buffer.from(await js('document.getElementById("source-editor").value'))),
      external_source: hash(Buffer.from(external)), disk_after: hash(fs.readFileSync(entry)), conflict_visible: true });
    await image('external-conflict'); await click(win, '#conflict-reload');
    await waitFor(win, `document.getElementById('draft-guard').open`); await click(win, '#draft-guard button[value="discard"]');
    await waitFor(win, `document.getElementById('source-editor').value === ${JSON.stringify(external)}`);
    await buildKey(); const retained = await current(), frameStart = frames.length;
    await text('#source-editor', 'def build(:\n'); assert.equal((await saved('def build(:\n')).status, 200); await buildKey();
    await waitFor(win, `document.getElementById('diag').textContent.includes('SyntaxError')`);
    check('syntax-retains-certified', { certified_before: retained.hash, certified_after: await js('__cq.model.hash'),
      certified_announcements: frames.slice(frameStart).filter((m) => m.type === 'announce' && m.project === projectName && m.tier === 'certified').length,
      diagnostic: await js('document.getElementById("diag").textContent'), renderer_tier: await js('__cq.model.tier') });
    await image('syntax-retained'); await text('#source-editor', external); assert.equal((await saved(external)).status, 200); await buildKey(); await current();
    const themeIds = ['counterform-dark', 'counterform-light', 'graphite', 'midnight', 'forest', 'sand', 'paper', 'high-contrast'];
    const states = [];
    for (let i = 0; i < themeIds.length; i++) {
      await click(win, '#settingsbtn'); await selectOption('#theme-select', themeIds[i]);
      await click(win, '#settings-close');
      const stored = JSON.parse(fs.readFileSync(path.join(process.env.CASSQUERY_USER_DATA, 'workbench-settings.json'))); assert.equal(stored.theme, themeIds[i]);
      const styling = await js(`(() => {const s=getComputedStyle(document.body);return [s.backgroundColor,s.color,getComputedStyle(document.documentElement).getPropertyValue('--panel')].join('|')})()`);
      const captured = await image('theme-' + themeIds[i]); states.push({ name: THEMES[i], styling, persisted: true, visible: true, image_sha256: captured.sha256 });
    }
    check('eight-themes', { states });
    await click(win, '#settingsbtn'); await text('#editor-font-size', '22'); key('Tab');
    await selectOption('#tab-size', '2'); await click(win, '#settings-close');
    await waitFor(win, `parseFloat(getComputedStyle(document.getElementById('source-editor')).fontSize) === 22`);
    await new Promise((resolve, reject) => {
      const contents = win.webContents;
      const timer = setTimeout(() => { contents.removeListener('did-finish-load', loaded); reject(new Error('Native workbench reload timed out')); }, 30000);
      const loaded = () => { clearTimeout(timer); resolve(); };
      contents.once('did-finish-load', loaded); contents.reload();
    });
    await waitFor(win, `window.__cq?.model?.index.project === 'WORKBENCHPLATE' && document.getElementById('source-editor').value === ${JSON.stringify(external)}`);
    const restoredFont = await js('parseFloat(getComputedStyle(document.getElementById("source-editor")).fontSize)');
    await click(win, '#source-editor'); shortcut('Home'); const oldText = await js('document.getElementById("source-editor").value'); key('Tab');
    const newText = await js('document.getElementById("source-editor").value'); shortcut('Z'); await waitFor(win, `document.getElementById('source-editor').value === ${JSON.stringify(oldText)}`);
    const persisted = JSON.parse(fs.readFileSync(path.join(process.env.CASSQUERY_USER_DATA, 'workbench-settings.json')));
    check('font-tabs', { font_size: persisted.editorFontSize, rendered_font_size: await js('parseFloat(getComputedStyle(document.getElementById("source-editor")).fontSize)'),
      tab_size: persisted.tabSize, inserted_spaces: newText.length - oldText.length, persistence_after_reload: restoredFont === 22 });
    report.editor_checks_pass = true; validateEditorReport(report, directory);
    fs.writeFileSync(path.join(directory, 'editor-ready.json'), JSON.stringify({ schema: 1, app_pid: process.pid, worker_pid: workerPid, worker_port: workerPort, challenge }) + '\n');
    // The wrapper performs standard OS selection; this observer forwards the real native APIs.
    observeNativeDialogs(directory, win, challenge);
    end = Date.now() + 240000;
    await qualifyWorkbench(win, directory, { ownedRoot, projectRoot, challenge, driver, native_source, workerPid, workerPort, click, text, key, js, current, geometry, image, traffic, waitFor });
    const qualified = fs.readFileSync(path.join(directory, 'workbench-qualification.json'));
    report.qualification = { path: 'workbench-qualification.json', bytes: qualified.length, sha256: hash(qualified) };
  } catch (error) {
    report.build_diagnostics ||= [];
    report.build_diagnostics.push({phase:'failure before cleanup',error:error.stack||String(error),renderer_requests:[...report.renderer_requests]});
    fs.writeFileSync(path.join(directory,'build-diagnostics.json'),JSON.stringify(report.build_diagnostics,null,2)+'\n');
    try{await journalBuild('actual failure state');}catch(diagnosticError){report.errors.push('Failure state diagnostic: '+diagnosticError.message);}
    report.errors.push(error.stack || String(error));
  } finally {
    win.webContents.removeListener('console-message', consoleError); win.webContents.removeListener('render-process-gone', rendererGone);
    traffic.stop();
    if (ownsDebugger) { debuggerAPI.removeListener('message', observeFrame); debuggerAPI.detach(); }
    report.worker_requests = traffic.trace;
    report.completed_at = Date.now();
    // Never promote an editor subset into the mandatory OS dialog/full-workbench gate.
    report.pass = false; report.qualifies_workbench = false;
    fs.writeFileSync(path.join(directory, 'workbench-editor.json'), JSON.stringify(report, null, 2) + '\n');
  }
  return report;
}
module.exports = { CHECKS, FULL_CHECKS, ownedPath, validatePreloadRecord, validatePreloadIdentity, validatePreloadIPC, probe, validateEditorReport };
