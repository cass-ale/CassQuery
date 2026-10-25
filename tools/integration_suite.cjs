'use strict';
// Production-path Linux source integration, not packaged macOS/Electron evidence.
// Only public demo copies, owned worker processes, and browser input are mutated.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');
const net = require('node:net');
const { spawn } = require('node:child_process');
const { createHash } = require('node:crypto');
const { crc32, inflateSync } = require('node:zlib');

const GATES = ['units', 'watertight', 'volume', 'thin-extent', 'triangulation',
  'payload-roundtrip', 'budget-tris', 'budget-bytes'];
const REQUIRED_CHECKS = Object.freeze({
  'live-truth': ['default-demo', 'eight-cad-gates', 'dom-rebuild-http', 'save-preview-certified',
    'camera-direction', 'failed-gates-retain-bytes-and-model'],
  'interaction-order': ['step-buttons-keyboard', 'rapid-aba-variant', 'xray-materials',
    'explode-transforms', 'pointer-pick-hide-show', 'http-view-screenshot'],
  'isolation-reconnect': ['malformed-manifest-isolation', 'syntax-error-no-model',
    'error-recovery', 'project-switch-stale-response', 'reconnect-hello-snapshot'],
  'boundary': ['malformed-json', 'host-origin', 'static-traversal', 'websocket-origin',
    'mutation-witness'],
});
const REQUIRED_SHOTS = ['01-default-certified.png', '02-live-preview.png', '03-live-certified.png',
  '04-unit-failure-retained.png', '05-xray.png', '06-exploded.png', '07-syntax-no-model.png', '08-reconnect-snapshot.png',
  '09-protocol-shot.png'];
const digest = (bytes) => createHash('sha256').update(bytes).digest('hex');
const readJSON = (file) => JSON.parse(fs.readFileSync(file, 'utf8'));

// Bounded decoding of Chromium's RGB/RGBA, 8-bit, non-interlaced PNG captures.
// A signature/digest alone accepts a truncated or fabricated non-image as evidence.
function pngDimensions(bytes) {
  assert.ok(bytes.length <= 32 * 1024 ** 2 && bytes.subarray(0, 8).toString('hex') === '89504e470d0a1a0a', 'invalid/excessive PNG');
  let offset = 8, header, ended = false, dataEnded = false;
  const packed = [];
  while (offset < bytes.length) {
    assert.ok(offset + 12 <= bytes.length, 'truncated PNG chunk');
    const length = bytes.readUInt32BE(offset), end = offset + 12 + length;
    assert.ok(end <= bytes.length, 'truncated PNG data');
      assert.ok(bytes.subarray(offset + 4, offset + 8).every((b) => b >= 65 && b <= 90 || b >= 97 && b <= 122), 'invalid PNG chunk type');
      const kind = bytes.toString('ascii', offset + 4, offset + 8);
    assert.match(kind, /^[A-Za-z]{4}$/, 'invalid PNG chunk type');
    const content = bytes.subarray(offset + 8, end - 4);
    assert.equal(crc32(bytes.subarray(offset + 4, end - 4)), bytes.readUInt32BE(end - 4), 'PNG checksum mismatch');
    assert.ok(header || kind === 'IHDR', 'PNG missing initial header');
    if (kind === 'IHDR') {
      assert.ok(!header && length === 13, 'invalid PNG header');
      const width = content.readUInt32BE(0), height = content.readUInt32BE(4), channels = { 2: 3, 6: 4 }[content[9]];
      assert.ok(width > 0 && width <= 16384 && height > 0 && height <= 16384 && content[8] === 8 && channels &&
        content[10] === 0 && content[11] === 0 && content[12] === 0, 'unsupported PNG capture format');
      header = { width, height, channels };
    } else if (kind === 'IDAT') {
      assert.equal(dataEnded, false, 'noncontiguous PNG image data'); packed.push(content);
    } else if (kind === 'IEND') {
      assert.ok(length === 0 && end === bytes.length, 'invalid PNG end'); ended = true; break;
    } else {
      if (packed.length) dataEnded = true;
      if (kind === 'PLTE') assert.ok(!packed.length && length > 0 && length <= 768 && length % 3 === 0, 'invalid PNG palette');
      else assert.ok(kind.charCodeAt(0) & 32, 'unsupported critical PNG chunk');
    }
    offset = end;
  }
  const compressed = Buffer.concat(packed);
  assert.ok(header && compressed.length && ended, 'incomplete PNG');
  const stride = header.width * header.channels + 1, expected = stride * header.height;
  assert.ok(expected <= 128 * 1024 ** 2, 'PNG decoded size exceeds bound');
  const decoded = inflateSync(compressed, { maxOutputLength: expected + 1, info: true });
  assert.equal(decoded.buffer.length, expected, 'PNG deflate length mismatch');
  assert.equal(decoded.engine.bytesWritten, compressed.length, 'PNG trailing deflate data');
  for (let row = 0; row < header.height; row++) assert.ok(decoded.buffer[row * stride] <= 4, 'invalid PNG row filter');
  return { width: header.width, height: header.height };
}

// Evidence validation is independent of producer check.pass labels.
function validateEvidence(id, e) {
  const hash = (value) => { assert.equal(typeof value, 'string'); assert.match(value, /^[a-f0-9]{16}$/); };
  const finite = (value) => assert.ok(typeof value === 'number' && Number.isFinite(value));
  const vector = (value, positive = false) => {
    assert.ok(Array.isArray(value) && value.length === 3);
    for (const number of value) { finite(number); if (positive) assert.ok(number > 0); }
  };
  const sameVector = (actual, expected) => {
    vector(actual); vector(expected);
    assert.ok(actual.every((value, i) => Math.abs(value - expected[i]) < 1e-8), 'view command vector mismatch');
  };
  const parts = (value) => {
    assert.ok(Array.isArray(value));
    assert.deepEqual(value.map((part) => part.name).sort(), ['Base', 'Cap', 'Post']);
  };
  const visible = (part) => {
    assert.ok(Array.isArray(part.visible) && part.visible.length > 0);
    assert.ok(part.visible.every((value) => typeof value === 'boolean'));
  };
  const mesh = (value, extent) => {
    assert.ok(Number.isInteger(value.decoded_vertices) && value.decoded_vertices > 0);
    vector(value.decoded_extent, true);
    assert.ok(Math.abs(value.decoded_extent[0] - extent) < 0.01);
  };
  const model = (value, tier, extent) => {
    assert.equal(value.project, 'TESTPLATE'); assert.equal(value.tier, tier); hash(value.hash);
    mesh(value, extent); parts(value.parts);
  };
  switch (id) {
    case 'default-demo':
      assert.equal(e.no_project_arguments, true); assert.equal(e.name, 'TESTPLATE'); assert.equal(e.root_is_temp, true); break;
    case 'eight-cad-gates':
      assert.equal(e.gates.pass, true); assert.equal(e.gates.checks.length, GATES.length);
      for (const gate of GATES) assert.equal(e.gates.checks.filter((g) => g.id === gate && g.status === 'pass').length, 1);
      model(e.model, 'certified', 40); assert.equal(e.model.parts.length, 3); break;
    case 'dom-rebuild-http': {
      assert.equal(e.status, 202); assert.deepEqual(e.body, { project: 'TESTPLATE', tier: 'both' });
      finite(e.prior_built_at); finite(e.rebuilt_at); finite(e.visible_built_at);
      assert.ok(e.rebuilt_at > e.prior_built_at);
      assert.equal(e.visible_built_at, e.rebuilt_at);
      const building = e.worker_frames.findIndex((m) => m.type === 'status' && m.state === 'building-certified');
      const announce = e.worker_frames.findIndex((m, i) => i > building && m.type === 'announce' && m.tier === 'certified');
      assert.ok(building >= 0 && announce > building); break;
    }
    case 'save-preview-certified': {
      model(e.before, 'certified', 40); model(e.preview, 'preview', 42); model(e.after, 'certified', 42);
      assert.notEqual(e.before.hash, e.preview.hash); assert.notEqual(e.before.hash, e.after.hash);
      assert.match(e.preview.badge, /not gate-checked/); assert.equal(e.preview_tools_disabled, true);
      const preview = e.announcements.findIndex((m) => m.tier === 'preview' && m.hash === e.preview.hash);
      const certified = e.announcements.findIndex((m, i) => i > preview && m.tier === 'certified' && m.hash === e.after.hash);
      assert.ok(preview >= 0 && certified > preview); break;
    }
    case 'camera-direction': {
      vector(e.before); vector(e.after); finite(e.direction_error);
      const difference = Math.max(...e.after.map((v, i) => Math.abs(v - e.before[i])));
      assert.ok(difference < 1e-5); assert.ok(Math.abs(difference - e.direction_error) < 1e-12); break;
    }
    case 'failed-gates-retain-bytes-and-model':
      assert.ok(Object.keys(e.files).length >= 3);
      for (const value of Object.values(e.files)) assert.match(value, /^[a-f0-9]{64}$/);
      assert.deepEqual(e.after_files, e.files); model(e.failed, 'certified', 42);
      assert.equal(e.failed.hash, e.retained_hash); assert.match(e.failed.badge, /gate FAILED.*last certified/);
      assert.equal(e.gates.pass, false); assert.equal(e.gates.checks.find((g) => g.id === 'units').status, 'fail');
      assert.equal(e.certified_announcements, 0); break;
    case 'step-buttons-keyboard':
      assert.equal(e.buttons, 3); assert.equal(e.keyboard_step, 'Take it apart'); assert.equal(e.explode, '100%'); break;
    case 'rapid-aba-variant':
      assert.equal(e.delayed_real_variant, true); model(e.final, 'certified', 42);
      assert.equal(e.final.variant, 'neutral'); assert.equal(e.selected_step, 'Assembled');
      assert.equal(e.pending, 0); assert.doesNotMatch(e.loading_line, /loading/); break;
    case 'xray-materials':
      parts(e.parts);
      for (const part of e.parts) {
        assert.equal(part.opacity, part.name === 'Cap' ? 1 : 0.2); assert.equal(part.depthWrite, part.name === 'Cap');
        assert.equal(part.transparent, part.name !== 'Cap'); }
      parts(e.off_parts);
      for (const part of e.off_parts) { assert.equal(part.opacity, 1); assert.equal(part.depthWrite, true); assert.equal(part.transparent, false); }
      break;
    case 'explode-transforms':
      assert.equal(e.keyboard_range_input, 'End'); parts(e.parts);
      for (const part of e.parts) {
        assert.ok(Array.isArray(part.positions) && part.positions.length > 0);
        for (const position of part.positions) {
          vector(position); assert.deepEqual(position, [0, 0, { Base: 0, Post: 22, Cap: 48 }[part.name]]);
        }
      }
      break;
    case 'pointer-pick-hide-show':
      assert.ok(Number.isFinite(e.actual_pointer.x) && Number.isFinite(e.actual_pointer.y)); assert.equal(e.picked, 'Cap');
      parts(e.hidden.parts); parts(e.shown.parts);
      for (const part of [...e.hidden.parts, ...e.shown.parts]) visible(part);
      assert.ok(e.hidden.parts.find((p) => p.name === 'Cap').visible.every((v) => v === false));
      assert.ok(e.shown.parts.every((p) => p.visible.every((v) => v === true)));
      model(e.rebuilt_hidden, 'certified', 42); hash(e.prior_hidden_hash);
      assert.notEqual(e.rebuilt_hidden.hash, e.prior_hidden_hash);
      for (const part of e.rebuilt_hidden.parts) {
        visible(part); assert.ok(part.visible.every((v) => v === (part.name !== 'Cap')));
      }
      assert.equal(e.show_all_reachable, true); break;
    case 'http-view-screenshot': {
      assert.deepEqual(e.statuses, [202, 200, 200]); assert.deepEqual(e.rejected_statuses, [400, 400]);
      assert.equal(e.held_actual_variant, true); assert.equal(e.renderer_posts_before_release, 0);
      const command = { project: 'TESTPLATE', step: 'apart', variant: 'lifted', explode: 0.5, xray: true,
        camera: { position: [150, -170, 110], target: [0, 0, 24] } };
      assert.deepEqual(e.request, command);
      model(e.visible, 'certified', 42); assert.equal(e.visible.variant, 'lifted');
      sameVector(e.visible.camera, command.camera.position); sameVector(e.visible.target, command.camera.target);
      for (const part of e.visible.parts) {
        visible(part); assert.ok(part.visible.every(Boolean));
        assert.equal(part.opacity, part.name === 'Cap' ? 1 : 0.2); assert.equal(part.depthWrite, part.name === 'Cap');
        assert.ok(Array.isArray(part.positions) && part.positions.length > 0);
        for (const position of part.positions) assert.deepEqual(position, [0, 0, { Base: 0, Post: 11, Cap: 24 }[part.name]]);
      }
      assert.ok(e.worker_frames.some((m) => m.type === 'command' && Object.keys(command).every((k) =>
        JSON.stringify(m[k]) === JSON.stringify(command[k]))));
      assert.ok(e.worker_frames.some((m) => m.type === 'screenshot' && m.project === command.project && m.id === e.shot.id));
      assert.equal(e.shot.project, 'TESTPLATE'); assert.equal(e.shot.file, '09-protocol-shot.png');
      assert.equal(e.shot.renderer_id, e.shot.id); assert.equal(e.shot.renderer_sha256, e.shot.sha256);
      assert.match(e.shot.id, /^TESTPLATE-[a-f0-9]{32}$/); assert.match(e.shot.sha256, /^[a-f0-9]{64}$/);
      for (const key of ['width', 'height']) assert.ok(Number.isInteger(e.canvas[key]) && e.canvas[key] > 0 && e.shot[key] === e.canvas[key]);
      break;
    }
    case 'malformed-manifest-isolation':
      assert.equal(e.api_version, 2); assert.deepEqual(e.projects, ['TESTPLATE', 'SECONDPLATE']);
      assert.equal(e.errors.length, 1); assert.match(e.errors[0].error, /JSONDecodeError/); break;
    case 'syntax-error-no-model':
      assert.equal(e.visible.project, null); assert.equal(e.visible.tier, null); assert.equal(e.visible.hash, null);
      assert.equal(e.certified_index_absent, true); assert.match(e.visible.diagnostic, /SyntaxError/);
      assert.match(e.visible.badge, /no certified model/); break;
    case 'error-recovery':
      assert.equal(e.recovered.project, 'SECONDPLATE'); assert.equal(e.recovered.tier, 'certified'); hash(e.recovered.hash);
      mesh(e.recovered, 24); parts(e.recovered.parts); break;
    case 'project-switch-stale-response':
      assert.equal(e.delayed_real_response, true); model(e.visible, 'certified', 42); break;
    case 'reconnect-hello-snapshot':
      hash(e.previous_hash); hash(e.snapshot_hash); assert.notEqual(e.previous_hash, e.snapshot_hash);
      assert.equal(e.missed_announcements, true); assert.equal(e.actual_browser_network_offline, true);
      model(e.reconnected, 'certified', 44); assert.equal(e.reconnected.hash, e.snapshot_hash);
      assert.equal(e.original_port, e.reconnected_port);
      assert.ok(e.hello.projects.some((p) => p.name === 'TESTPLATE' && p.hashes.certified === e.snapshot_hash)); break;
    case 'malformed-json':
      assert.deepEqual(e.bodies, ['syntax', 'array', 'non-finite number']); assert.deepEqual(e.statuses, [400, 400, 400]); break;
    case 'host-origin':
      assert.deepEqual(e.statuses, [403, 403]); assert.ok(Number.isInteger(e.real_listener) && e.real_listener > 0); break;
    case 'static-traversal':
      assert.deepEqual(e.statuses, [404, 404, 404, 404]); assert.equal(e.worker_source_not_served, true); break;
    case 'websocket-origin': assert.match(e.handshake, /^HTTP\/1\.1 403 /); break;
    case 'mutation-witness':
      assert.equal(e.detected, true); assert.equal(e.check, 'host-origin'); assert.equal(e.baseline_status, 403);
      assert.equal(e.actual, 200); assert.equal(e.expected, 403); assert.equal(e.isolated_copy, true);
      assert.equal(e.production_source_unchanged, true);
      assert.match(e.original_sha256, /^[a-f0-9]{64}$/); assert.match(e.mutant_sha256, /^[a-f0-9]{64}$/);
      assert.notEqual(e.original_sha256, e.mutant_sha256); break;
    default: assert.fail(`unknown evidence invariant ${id}`);
  }
}

function validateReport(report, directory) {
  assert.equal(report.schema, 1, 'report schema');
  assert.equal(report.platform, 'linux-source-browser-worker', 'honest platform label');
  assert.equal(report.pass, true, 'suite did not pass');
  assert.equal(report.error, null, 'suite error present');
  assert.equal(report.cleanup.pass, true, 'cleanup not proven');
  assert.equal(report.cleanup.process_groups_gone, true, 'owned process groups remain');
  assert.equal(report.cleanup.ports_closed, true, 'owned listeners remain');
  assert.deepEqual(report.cleanup.errors, [], 'cleanup errors');
  assert.deepEqual(report.browser_errors, [], 'browser errors');
  assert.equal(report.scenarios.length, Object.keys(REQUIRED_CHECKS).length, 'scenario omission');
  for (const [id, checks] of Object.entries(REQUIRED_CHECKS)) {
    const matches = report.scenarios.filter((s) => s.id === id);
    assert.equal(matches.length, 1, `missing/duplicate scenario ${id}`);
    const scenario = matches[0];
    assert.equal(scenario.pass, true, `${id} failed`);
    assert.equal(scenario.checks.length, checks.length, `${id} check omission`);
    for (const check of checks) {
      const found = scenario.checks.filter((c) => c.id === check);
      assert.equal(found.length, 1, `missing/duplicate check ${id}/${check}`);
      assert.equal(found[0].pass, true, `failed check ${id}/${check}`);
      assert.ok(found[0].evidence && typeof found[0].evidence === 'object', 'missing observations');
      validateEvidence(check, found[0].evidence);
    }
  }
  validateEvidence('mutation-witness', report.mutation);
  const mutationCheck = report.scenarios.find((s) => s.id === 'boundary').checks.find((c) => c.id === 'mutation-witness');
  assert.deepEqual(mutationCheck.evidence, report.mutation);
  assert.equal(report.mutation.original_sha256, report.source.worker_sha256);
  assert.equal(report.mutation.detected, true, 'mutation not detected');
  assert.equal(report.mutation.check, 'host-origin', 'unrelated mutant failure');
  assert.match(report.mutation.original_sha256, /^[a-f0-9]{64}$/);
  assert.match(report.mutation.mutant_sha256, /^[a-f0-9]{64}$/);
  assert.notEqual(report.mutation.original_sha256, report.mutation.mutant_sha256);
  for (const filename of REQUIRED_SHOTS)
    assert.equal(report.screenshots.filter((s) => s.file === filename).length, 1, `missing/duplicate screenshot ${filename}`);
  const evidenceRoot = fs.realpathSync(directory);
  for (const shot of report.screenshots) {
    assert.ok(typeof shot.description === 'string' && shot.description.length > 10);
    assert.match(shot.sha256, /^[a-f0-9]{64}$/);
    const file = fs.realpathSync(path.resolve(directory, shot.file));
    assert.ok(file.startsWith(evidenceRoot + path.sep), 'evidence escaped output');
    assert.ok(fs.statSync(file).size <= 32 * 1024 ** 2, 'excessive PNG evidence');
    const bytes = fs.readFileSync(file);
    const dimensions = pngDimensions(bytes);
    assert.equal(digest(bytes), shot.sha256, 'screenshot digest mismatch');
    assert.equal(dimensions.width, shot.width, 'screenshot width mismatch');
    assert.equal(dimensions.height, shot.height, 'screenshot height mismatch');
    if (shot.file === '09-protocol-shot.png') {
      const protocol = report.scenarios.find((s) => s.id === 'interaction-order').checks.find((c) => c.id === 'http-view-screenshot').evidence;
      assert.equal(shot.sha256, protocol.shot.sha256); assert.equal(shot.width, protocol.canvas.width); assert.equal(shot.height, protocol.canvas.height);
    } else assert.ok(shot.width >= 1320 && shot.height >= 900, 'screenshot does not cover source viewport');
  }
  assert.equal(report.source.chromium_sandbox, true);
  assert.match(report.source.worker_sha256, /^[a-f0-9]{64}$/);
  assert.match(report.source.viewer_sha256, /^[a-f0-9]{64}$/);
  const python = report.source.python_sources;
  assert.ok(Array.isArray(python) && python.length >= 3 && python.length <= 10, 'missing Python source closure');
  assert.equal(new Set(python.map((item) => item.path)).size, python.length, 'duplicate Python source record');
  assert.ok(['worker.py', 'workbench.py', 'cad_io.py'].every((name) => python.some((item) => item.path === name)),
    'incomplete workbench Python source closure');
  for (const item of python) {
    assert.match(item.path, /^[A-Za-z_][A-Za-z0-9_]*\.py$/, 'unsafe Python source path');
    assert.ok(Number.isSafeInteger(item.bytes) && item.bytes > 0 && item.bytes <= 2 * 1024 ** 2,
      'invalid Python source size');
    assert.match(item.sha256, /^[a-f0-9]{64}$/);
  }
  assert.equal(python.find((item) => item.path === 'worker.py').sha256, report.source.worker_sha256,
    'worker hash differs from source closure');
  return true;
}

function request(origin, target, { method = 'GET', body, headers = {}, timeoutMs = 5000 } = {}) {
  return new Promise((resolve, reject) => {
    const data = body === undefined ? null : Buffer.from(typeof body === 'string' ? body : JSON.stringify(body));
    const req = http.request(new URL(origin), { path: target, method,
      headers: { ...(data ? { 'Content-Type': 'application/json', 'Content-Length': data.length } : {}), ...headers } },
    (res) => {
      const chunks = [];
      res.on('data', (chunk) => chunks.push(chunk));
      res.on('end', () => {
        const bytes = Buffer.concat(chunks), text = bytes.toString();
        let json = null;
        try { json = JSON.parse(text); } catch { /* Static files and rejected handshakes aren't JSON. */ }
        resolve({ status: res.statusCode, json, bytes });
      });
      res.on('error', reject);
    });
    req.setTimeout(timeoutMs, () => req.destroy(new Error(`HTTP timeout ${target}`)));
    req.on('error', reject);
    req.end(data);
  });
}
function listening(port) {
  return new Promise((resolve) => {
    const socket = net.connect({ host: '127.0.0.1', port });
    socket.setTimeout(500);
    socket.once('connect', () => { socket.destroy(); resolve(true); });
    socket.once('error', () => resolve(false));
    socket.once('timeout', () => { socket.destroy(); resolve(false); });
  });
}
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
function groupExists(pid) {
  try { process.kill(-pid, 0); return true; }
  catch (error) { if (error.code === 'ESRCH') return false; throw error; }
}
function signalGroup(pid, signal) {
  try { process.kill(-pid, signal); }
  catch (error) { if (error.code !== 'ESRCH') throw error; }
}

async function run(options = {}) {
  const root = path.resolve(__dirname, '..');
  const output = path.resolve(options.output || process.env.CASSQUERY_INTEGRATION_OUTPUT || path.join(root, 'evidence/integration'));
  fs.mkdirSync(output, { recursive: true });
  assert.equal(fs.existsSync(path.join(output, 'integration-results.json')), false,
    'Use a new output directory; evidence must not overwrite a previous run');
  const started = Date.now(), deadline = started + (options.timeoutMs || 240000);
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'cassquery-integration-'));
  const source = path.join(temporary, 'source');
  let originalWorker;
  const mutationAnchor = 'error = _boundary_error(self.headers, self.server.server_port, self.server.server_port)';
  function mutateWorker(destination) {
    assert.equal(originalWorker.split(mutationAnchor).length, 2, 'mutation anchor drift');
    const mutant = originalWorker.replace(mutationAnchor, 'error = None  # isolated integration mutation: omit HTTP boundary');
    fs.writeFileSync(destination, mutant);
    return { original_sha256: digest(originalWorker), mutant_sha256: digest(mutant) };
  }
  const report = { schema: 1, platform: 'linux-source-browser-worker', pass: false, error: null,
    source: { python_executable: process.env.CASSQUERY_PYTHON || 'python3', revision: process.env.GITHUB_SHA || null },
    method: 'real CadQuery/OCP worker, HTTP/WS, Chromium WebGL, DOM/pointer/keyboard actions',
    not_checked: ['packaged macOS/Electron, native menu/dialogs, signing/notarization',
      'private models/preferences', 'human visual review of every screenshot'],
    scenarios: [], screenshots: [], mutation: null, cleanup: { pass: false },
    browser_errors: [], websocket_frames: [], elapsed_ms: 0 };
  const workers = [], browserGroups = [], ports = new Set();
  let browser, context, page, activeScenario, hardExpired = false;
  const browserPIDFile = path.join(temporary, 'browser-pid');
  function discoverBrowserGroup() {
    if (fs.existsSync(browserPIDFile)) {
      const pid = Number(fs.readFileSync(browserPIDFile, 'utf8'));
      assert.ok(Number.isInteger(pid) && pid > 1, 'invalid owned browser PID');
      if (!browserGroups.includes(pid)) browserGroups.push(pid);
    }
  }
  // Absolute watchdog: unlike per-action timeouts it also covers imports, CAD hangs and cleanup.
  // The browser is launched as an owned Linux session, so its descendants cannot escape with it.
  const watchdog = setTimeout(() => {
    hardExpired = true;
    report.error = { message: 'absolute integration deadline exceeded', scenario: activeScenario?.id || null };
    for (const child of workers) if (child.pid) signalGroup(child.pid, 'SIGKILL');
    discoverBrowserGroup();
    for (const pid of browserGroups) signalGroup(pid, 'SIGKILL');
    report.cleanup = { pass: false, process_groups_gone: false, ports_closed: false,
      owned_worker_pids: workers.map((c) => c.pid).filter(Boolean), owned_browser_groups: browserGroups,
      errors: ['deadline termination; cleanup verification incomplete'] };
    report.elapsed_ms = Date.now() - started;
    fs.writeFileSync(path.join(output, 'integration-results.json'), JSON.stringify(report, null, 2));
    process.exit(1);
  }, deadline - Date.now());
  // Begin graceful cancellation before the absolute kill deadline so ordinary hangs
  // still reach the finally block and get a verified cleanup ledger.
  const softWatchdog = setTimeout(() => {
    report.error = { message: 'integration soft deadline exceeded', scenario: activeScenario?.id || null };
    if (browser) browser.close().catch((error) => { report.browser_errors.push(`deadline close: ${error.message}`); });
    for (const child of workers) if (child.pid) signalGroup(child.pid, 'SIGINT');
  }, Math.max(1, deadline - Date.now() - 10000));
  async function until(label, predicate, limit = 30000, cleaning = false) {
    const end = Math.min(deadline - (cleaning ? 500 : 12000), Date.now() + limit);
    while (Date.now() < end) {
      const value = await predicate();
      if (value) return value;
      await pause(60);
    }
    throw new Error(`Timed out: ${label}`);
  }
  function check(id, evidence) {
    assert.ok(REQUIRED_CHECKS[activeScenario.id].includes(id), `undeclared check ${id}`);
    activeScenario.checks.push({ id, pass: true, evidence });
  }
  async function scenario(id, work) {
    activeScenario = { id, pass: false, checks: [] };
    report.scenarios.push(activeScenario);
    await work();
    assert.deepEqual(activeScenario.checks.map((c) => c.id).sort(), [...REQUIRED_CHECKS[id]].sort());
    activeScenario.pass = true;
  }
  async function startWorker(label, projects = [], workerRoot = source, port = 41000 + Math.floor(Math.random() * 5000)) {
    const registry = path.join(temporary, `${label}-registry.json`);
    fs.rmSync(registry, { force: true });
    const log = path.join(output, `${label}-worker.log`), fd = fs.openSync(log, 'w');
    const environment = { ...process.env, CASSQUERY_DATA_DIR: path.join(temporary, `${label}-data`),
      CASSQUERY_PROJECTS: '', CASSQUERY_PORT: String(port), PYTHONUNBUFFERED: '1',
      PYTHONDONTWRITEBYTECODE: '1', PYTHONNOUSERSITE: '1', HOME: path.join(temporary, 'home') };
    const child = spawn(process.env.CASSQUERY_PYTHON || 'python3',
      ['-u', path.join(workerRoot, 'worker.py'), ...projects, '--port', String(port), '--registry-file', registry],
      { cwd: workerRoot, env: environment, detached: true, stdio: ['ignore', fd, fd] });
    fs.closeSync(fd);
    workers.push(child);
    child.on('error', (error) => { child.startError = error; });
    const ready = await until(`${label} worker readiness`, () => {
      if (child.startError) throw child.startError;
      if (child.exitCode !== null) throw new Error(`${label} worker exited ${child.exitCode}; see ${log}`);
      return fs.existsSync(registry) ? readJSON(registry) : false;
    }, 45000);
    assert.equal(ready.pid, child.pid, 'registry not owned');
    child.port = ready.port;
    ports.add(ready.port); ports.add(ready.port + 1000);
    child.origin = `http://127.0.0.1:${ready.port}`;
    child.data = environment.CASSQUERY_DATA_DIR;
    assert.equal((await request(child.origin, '/api/status')).status, 200);
    return child;
  }
  async function stopWorker(child) {
    signalGroup(child.pid, 'SIGINT');
    try { await until('worker group graceful exit', () => !groupExists(child.pid), 4000, true); }
    catch { signalGroup(child.pid, 'SIGKILL'); await until('worker group killed', () => !groupExists(child.pid), 4000, true); }
  }
  async function status(worker, name) {
    const response = await request(worker.origin, `/api/status?project=${encodeURIComponent(name)}`);
    assert.equal(response.status, 200);
    return response.json;
  }
  async function certified(worker, name) {
    return until(`${name} certified`, async () => {
      const value = await status(worker, name);
      return value.state === 'certified' ? value : false;
    }, 45000);
  }
  async function shown(name, tier = 'certified', hash = null) {
    await page.waitForFunction(({ name, tier, hash }) => {
      const model = window.__cq?.model;
      return model?.index.project === name && model.tier === tier && (!hash || model.hash === hash);
    }, { name, tier, hash }, { timeout: 30000 });
  }
  async function observe() {
    return page.evaluate(() => {
      const { model, camera, controls } = window.__cq;
      const variant = model?.variants.get(model.activeName);
      const decodedMin = [Infinity, Infinity, Infinity], decodedMax = [-Infinity, -Infinity, -Infinity];
      let decodedVertices = 0;
      if (variant) for (const [, part] of variant.parts) for (const mesh of part.meshes) {
        const positions = mesh.geometry.getAttribute('position');
        for (let i = 0; i < positions.count; i++) for (let axis = 0; axis < 3; axis++) {
          const value = positions.array[i * 3 + axis] + mesh.position.getComponent(axis);
          decodedMin[axis] = Math.min(decodedMin[axis], value);
          decodedMax[axis] = Math.max(decodedMax[axis], value);
        }
        decodedVertices += positions.count;
      }
      return { project: model?.index.project || null, tier: model?.tier || null, hash: model?.hash || null,
        decoded_extent: decodedVertices ? decodedMax.map((v, i) => v - decodedMin[i]) : null, decoded_vertices: decodedVertices,
        gen: model?.index.gen, variant: model?.activeName,
        direction: camera.position.clone().sub(controls.target).normalize().toArray(),
        camera: camera.position.toArray(), target: controls.target.toArray(),
        badge: document.getElementById('tierbadge').textContent,
        diagnostic: document.getElementById('diag').textContent,
        parts: variant ? [...variant.parts].map(([name, part]) => ({ name,
          opacity: part.mat.opacity, transparent: part.mat.transparent, depthWrite: part.mat.depthWrite,
          visible: part.meshes.map((m) => m.visible), positions: part.meshes.map((m) => m.position.toArray()) })) : [],
        bounds: variant?.bounds || null };
    });
  }
  async function screenshot(file, description) {
    await page.screenshot({ path: path.join(output, file), fullPage: true });
    const bytes = fs.readFileSync(path.join(output, file));
    report.screenshots.push({ file, description, sha256: digest(bytes), ...pngDimensions(bytes) });
  }
  function retainedFiles(worker, name) {
    const directory = path.join(worker.data, 'payloads');
    const indexFile = `${name}-certified.json`, indexBytes = fs.readFileSync(path.join(directory, indexFile));
    const index = JSON.parse(indexBytes);
    const files = { [indexFile]: digest(indexBytes) };
    for (const item of Object.values(index.variants)) {
      const filename = decodeURIComponent(item.url.split('/').pop());
      const bytes = fs.readFileSync(path.join(directory, filename));
      assert.equal(digest(bytes).slice(0, 16), item.hash, 'variant bytes do not match certified index');
      files[filename] = digest(bytes);
    }
    return files;
  }
  function edit(file, old, replacement) {
    const text = fs.readFileSync(file, 'utf8');
    assert.equal(text.split(old).length, 2, `fixture edit anchor drift: ${old}`);
    fs.writeFileSync(file, text.replace(old, replacement));
  }
  async function expectHostOrigin(worker) {
    const statuses = [];
    for (const headers of [{ Host: 'outside.invalid' }, { Origin: 'https://outside.invalid' }]) {
      const result = await request(worker.origin, '/api/status', { headers });
      statuses.push(result.status);
      assert.equal(result.status, 403, 'host-origin');
    }
    return { statuses, real_listener: worker.port };
  }
  try {
    assert.equal(process.platform, 'linux', 'This suite proves Linux source behavior only');
    // Preparation is guarded too: missing source must leave a red report and no temp fixture.
    fs.mkdirSync(source);
    // Deliberately do not copy checked-in payloads, private projects, home preferences, or packages.
    const pythonSources = readJSON(path.join(root, 'package.json')).cassquerySources.python;
    assert.ok(Array.isArray(pythonSources) && pythonSources.length >= 3 && pythonSources.length <= 10,
      'missing or excessive declared Python source closure');
    assert.equal(new Set(pythonSources).size, pythonSources.length, 'duplicate Python source');
    for (const name of pythonSources) {
      assert.match(name, /^[A-Za-z_][A-Za-z0-9_]*\.py$/, 'unsafe Python source path');
      fs.copyFileSync(path.join(root, name), path.join(source, name));
    }
    assert.ok(['worker.py', 'workbench.py', 'cad_io.py'].every((name) => pythonSources.includes(name)),
      'incomplete workbench Python source closure');
    report.source.python_sources = pythonSources.map((name) => {
      const bytes = fs.readFileSync(path.join(source, name));
      return { path: name, bytes: bytes.length, sha256: digest(bytes) };
    });
    fs.cpSync(path.join(root, 'viewer'), path.join(source, 'viewer'), { recursive: true });
    fs.cpSync(path.join(root, 'testpart'), path.join(source, 'testpart'), { recursive: true });
    originalWorker = fs.readFileSync(path.join(source, 'worker.py'), 'utf8');
    if (options.mutation) mutateWorker(path.join(source, 'worker.py'));
    Object.assign(report.source, { worker_sha256: digest(fs.readFileSync(path.join(source, 'worker.py'))),
      viewer_sha256: digest(fs.readFileSync(path.join(source, 'viewer/app.js'))),
      suite_sha256: digest(fs.readFileSync(__filename)),
      public_demo_sha256: digest(fs.readFileSync(path.join(root, 'testpart/part_entry.py'))),
      manifest_sha256: digest(fs.readFileSync(path.join(root, 'testpart/viewer.json'))) });
    const executable = process.env.CASSQUERY_BROWSER;
    assert.ok(executable && fs.existsSync(executable), 'Set CASSQUERY_BROWSER to an existing Linux Chrome executable');
    const { chromium } = require(process.env.CASSQUERY_PLAYWRIGHT || 'playwright-core');
    // setsid + exec creates a process group covering Chromium and all of its descendants.
    const launcher = path.join(temporary, 'browser-launcher');
    fs.writeFileSync(launcher, '#!/bin/sh\nexec setsid sh -c \'printf "%s" "$$" > "$CASSQUERY_BROWSER_PID_FILE"; exec "$CASSQUERY_BROWSER" "$@"\' sh "$@"\n', { mode: 0o700 });
    browser = await chromium.launch({ executablePath: launcher, headless: true, chromiumSandbox: true,
      env: { ...process.env, CASSQUERY_BROWSER_PID_FILE: browserPIDFile },
      args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader'], timeout: 20000 });
    discoverBrowserGroup();
    report.source.browser_version = browser.version();
    report.source.chromium_sandbox = true;
    context = await browser.newContext({ viewport: { width: 1320, height: 900 },
      deviceScaleFactor: 1, reducedMotion: 'reduce' });
    page = await context.newPage();
    page.setDefaultTimeout(10000);
    page.on('pageerror', (error) => report.browser_errors.push(error.message));
    const cdp = await context.newCDPSession(page);
    const browserCDP = await browser.newBrowserCDPSession();
    const info = await browserCDP.send('SystemInfo.getProcessInfo');
    assert.equal(Number(info.processInfo.find((p) => p.type === 'browser').id), browserGroups[0], 'browser ownership mismatch');
    await browserCDP.detach();
    page.on('websocket', (ws) => ws.on('framereceived', ({ payload }) => {
      const frame = JSON.parse(String(payload));
      if (report.websocket_frames.length < 500) report.websocket_frames.push({ t_ms: Date.now() - started, ...frame });
    }));
    let worker = await startWorker('default');
    const demo = path.join(source, 'testpart'), entry = path.join(demo, 'part_entry.py');
    const manifestFile = path.join(demo, 'viewer.json');
    await page.goto(worker.origin);
    await scenario('live-truth', async () => {
      const st = await certified(worker, 'TESTPLATE');
      await shown('TESTPLATE', 'certified', st.hashes.certified);
      check('default-demo', { no_project_arguments: true, name: st.name, root_is_temp: st.root === demo });
      assert.equal(st.root, demo);
      const gates = (await request(worker.origin, '/api/gates?project=TESTPLATE')).json;
      assert.equal(gates.pass, true);
      for (const id of GATES) assert.equal(gates.checks.find((g) => g.id === id)?.status, 'pass', `CAD gate ${id}`);
      const initial = await observe();
      assert.equal(initial.parts.length, 3);
      assert.ok(Math.abs(initial.bounds.max[0] - initial.bounds.min[0] - 40) < 0.01);
      assert.ok(initial.decoded_vertices > 0 && Math.abs(initial.decoded_extent[0] - 40) < 0.01, 'decoded CAD extent');
      check('eight-cad-gates', { gates, actual_mesh_bounds: initial.bounds, model: initial });
      await screenshot('01-default-certified.png', 'Unmodified public TESTPLATE built by real CadQuery/OCP; three assembled parts and certified badge.');
      const priorIndex = (await request(worker.origin, '/payload/TESTPLATE-certified.json')).json;
      const rebuildFrameStart = report.websocket_frames.length;
      const rebuildResponse = page.waitForResponse((r) => r.url().endsWith('/api/rebuild') && r.request().method() === 'POST');
      await page.locator('#buildbtn').click();
      const response = await rebuildResponse;
      assert.equal(response.status(), 202);
      assert.equal(response.request().postDataJSON().project, 'TESTPLATE');
      await until('DOM rebuild genuinely completes', async () => {
        const frames = report.websocket_frames.slice(rebuildFrameStart).filter((m) => m.project === 'TESTPLATE');
        if (!frames.some((m) => m.type === 'status' && m.state === 'building-certified') ||
            !frames.some((m) => m.type === 'announce' && m.tier === 'certified')) return false;
        const index = (await request(worker.origin, '/payload/TESTPLATE-certified.json')).json;
        return index.built_at > priorIndex.built_at ? index : false;
      }, 45000);
      await page.waitForFunction((prior) => window.__cq.model?.tier === 'certified' &&
        window.__cq.model.index.built_at > prior, priorIndex.built_at);
      const rebuiltIndex = (await request(worker.origin, '/payload/TESTPLATE-certified.json')).json;
      check('dom-rebuild-http', { status: response.status(), body: response.request().postDataJSON(),
        prior_built_at: priorIndex.built_at, rebuilt_at: rebuiltIndex.built_at,
        visible_built_at: await page.evaluate(() => window.__cq.model.index.built_at),
        worker_frames: report.websocket_frames.slice(rebuildFrameStart).filter((m) => m.project === 'TESTPLATE') });
      // Add a public pose so gate failure proves retention of every variant, not only neutral.
      fs.writeFileSync(path.join(demo, 'poses.py'), 'POSES = {"lifted": lambda result: {name: shape.translate((0, 0, 3)) for name, shape in result["parts"].items()}}\n');
      const manifest = readJSON(manifestFile);
      manifest.poses = 'poses.py';
      manifest.steps.push({ id: 'lifted', label: 'Lifted pose', variant: 'lifted', explode: 0, xray: false });
      fs.writeFileSync(manifestFile, JSON.stringify(manifest));
      await until('pose-certified manifest', async () => {
        const result = await request(worker.origin, '/payload/TESTPLATE-certified.json');
        return result.json?.variants?.lifted;
      });
      await certified(worker, 'TESTPLATE');
      await page.locator('#steps button').first().click();
      // Orbit via actual pointer drag; observations below never call application actions.
      const canvas = await page.locator('#stage canvas').boundingBox();
      await page.mouse.move(canvas.x + canvas.width * 0.7, canvas.y + canvas.height * 0.55);
      await page.mouse.down();
      await page.mouse.move(canvas.x + canvas.width * 0.78, canvas.y + canvas.height * 0.6, { steps: 8 });
      await page.mouse.up();
      const before = await observe(), frameStart = report.websocket_frames.length;
      // A real slow build-only fixture gives the browser time to observe preview before certification.
      edit(entry, 'def build():', 'def build():\n    import time\n    time.sleep(1.0)');
      edit(entry, '.box(40, 30, 6,', '.box(42, 30, 6,');
      await shown('TESTPLATE', 'preview');
      const preview = await observe();
      assert.notEqual(preview.hash, before.hash);
      assert.match(preview.badge, /not gate-checked/);
      assert.ok(Math.abs(preview.bounds.max[0] - preview.bounds.min[0] - 42) < 0.01);
      assert.ok(Math.abs(preview.decoded_extent[0] - 42) < 0.01, 'saved preview decoded geometry');
      assert.equal(await page.locator('#xraybtn').isDisabled(), true);
      await screenshot('02-live-preview.png', 'Saved public CAD base grows from 40 to 42 mm; real watcher preview is visibly labelled not gate-checked.');
      const fresh = await certified(worker, 'TESTPLATE');
      await shown('TESTPLATE', 'certified', fresh.hashes.certified);
      const after = await observe();
      assert.notEqual(after.hash, before.hash);
      assert.ok(Math.abs(after.decoded_extent[0] - 42) < 0.01, 'fresh certified decoded geometry');
      const announcements = report.websocket_frames.slice(frameStart).filter((m) => m.type === 'announce' && m.project === 'TESTPLATE');
      assert.ok(announcements.some((m) => m.tier === 'preview' && m.hash === preview.hash));
      assert.ok(announcements.some((m) => m.tier === 'certified' && m.hash === after.hash));
      check('save-preview-certified', { before, preview, after, announcements, preview_tools_disabled: true });
      const directionError = Math.max(...after.direction.map((n, i) => Math.abs(n - before.direction[i])));
      assert.ok(directionError < 1e-5, `camera confiscated ${directionError}`);
      check('camera-direction', { direction_error: directionError, before: before.direction, after: after.direction });
      await screenshot('03-live-certified.png', 'Fresh certified 42 mm public CAD model replaces preview without changing the user-orbited camera direction.');
      const retained = retainedFiles(worker, 'TESTPLATE'), failedStart = report.websocket_frames.length;
      manifest.unit_scale = 2;
      fs.writeFileSync(manifestFile, JSON.stringify(manifest));
      await until('unit mismatch fails actual gates', async () => (await status(worker, 'TESTPLATE')).state === 'gate-failed', 45000);
      await page.waitForFunction(() => document.getElementById('tierbadge').textContent.includes('gate FAILED'));
      const failed = await observe();
      assert.equal(failed.tier, 'certified'); assert.equal(failed.hash, after.hash);
      assert.deepEqual(failed.decoded_extent, after.decoded_extent, 'failed build replaced visible geometry');
      assert.deepEqual(retainedFiles(worker, 'TESTPLATE'), retained);
      const failedGates = (await request(worker.origin, '/api/gates?project=TESTPLATE')).json;
      assert.equal(failedGates.pass, false);
      assert.equal(failedGates.checks.find((g) => g.id === 'units').status, 'fail');
      assert.ok(!report.websocket_frames.slice(failedStart).some((m) => m.type === 'announce' && m.tier === 'certified'));
      check('failed-gates-retain-bytes-and-model', { files: retained, after_files: retainedFiles(worker, 'TESTPLATE'),
        retained_hash: after.hash, failed, gates: failedGates, certified_announcements: 0 });
      await screenshot('04-unit-failure-retained.png', 'Deliberate unit_scale=2 fails units gate; badge reports failure while the last certified model remains visible.');
      manifest.unit_scale = 1;
      fs.writeFileSync(manifestFile, JSON.stringify(manifest));
      await certified(worker, 'TESTPLATE');
      await shown('TESTPLATE');
    });
    await scenario('interaction-order', async () => {
      const steps = page.locator('#steps button');
      assert.equal(await steps.count(), 3);
      assert.deepEqual(await steps.evaluateAll((buttons) => buttons.map((b) => [b.tagName, b.type])),
        [['BUTTON', 'button'], ['BUTTON', 'button'], ['BUTTON', 'button']]);
      await steps.nth(0).click();
      await steps.nth(0).press('ArrowRight');
      await page.waitForFunction(() => document.getElementById('explodeval').textContent === '100%');
      check('step-buttons-keyboard', { buttons: 3, keyboard_step: 'Take it apart', explode: '100%' });
      let releaseVariant, variantRequested;
      const requested = new Promise((resolve) => { variantRequested = resolve; });
      const held = new Promise((resolve) => { releaseVariant = resolve; });
      await page.route('**/payload/*-certified-lifted-*.json', async (route) => {
        variantRequested(); await held; await route.continue(); // delay actual HTTP, never substitute payloads
      });
      await steps.nth(0).click();
      await steps.nth(2).click();
      await Promise.race([requested, pause(10000).then(() => { throw new Error('real lazy variant request not observed'); })]);
      await steps.nth(0).click();
      releaseVariant();
      await until('lazy variant HTTP completes', async () => page.evaluate(() => window.__cq.model.pending.size === 0));
      await page.unroute('**/payload/*-certified-lifted-*.json');
      const aba = await observe();
      assert.equal(aba.variant, 'neutral');
      assert.equal(await steps.nth(0).getAttribute('aria-pressed'), 'true');
      assert.equal(await steps.nth(2).getAttribute('aria-pressed'), 'false');
      assert.equal(await page.locator('#explode').inputValue(), '0');
      const loadingLine = await page.locator('#buildline').textContent();
      assert.doesNotMatch(loadingLine, /loading/, 'superseded variant loading indicator stayed visible');
      check('rapid-aba-variant', { delayed_real_variant: true, final: aba,
        selected_step: await steps.nth(0).textContent(), pending: 0, loading_line: loadingLine });
      await page.locator('#xraybtn').click();
      const xray = await observe();
      assert.equal(await page.locator('#xraybtn').getAttribute('aria-pressed'), 'true');
      for (const part of xray.parts) {
        assert.equal(part.opacity, part.name === 'Cap' ? 1 : 0.2);
        assert.equal(part.depthWrite, part.name === 'Cap');
      }
      await screenshot('05-xray.png', 'Real X-ray button makes Base/Post ghost materials while Cap stays opaque in the public CAD assembly.');
      await page.locator('#xraybtn').click();
      const xrayOff = await observe();
      assert.ok(xrayOff.parts.every((p) => p.opacity === 1 && p.depthWrite && !p.transparent));
      check('xray-materials', { parts: xray.parts, off_parts: xrayOff.parts });
      await page.locator('#explode').press('End');
      await page.waitForFunction(() => document.getElementById('explodeval').textContent === '100%');
      const exploded = await observe();
      for (const part of exploded.parts) assert.deepEqual(part.positions[0], [0, 0, { Base: 0, Post: 22, Cap: 48 }[part.name]]);
      check('explode-transforms', { keyboard_range_input: 'End', parts: exploded.parts });
      await page.locator('#resetview').click();
      await screenshot('06-exploded.png', 'Actual range keyboard input and Reset view produce separated Base/Post/Cap with full assembly framed.');
      // Project a geometry triangle to a pointer coordinate; actual production raycaster still decides the pick.
      const point = await page.evaluate(() => {
        const { model, camera } = window.__cq;
        const mesh = model.variants.get(model.activeName).parts.get('Cap').meshes[0];
        model.group.updateMatrixWorld(true); camera.updateMatrixWorld(true);
        const position = mesh.geometry.getAttribute('position');
        const v = camera.position.clone().set(0, 0, 0);
        for (let i = 0; i < position.count; i++) v.add(camera.position.clone().fromBufferAttribute(position, i));
        v.multiplyScalar(1 / position.count).applyMatrix4(mesh.matrixWorld).project(camera);
        const r = document.querySelector('#stage canvas').getBoundingClientRect();
        return { x: r.left + (v.x + 1) * r.width / 2, y: r.top + (1 - v.y) * r.height / 2 };
      });
      await page.mouse.click(point.x, point.y);
      assert.equal(await page.locator('#selname').textContent(), 'Cap');
      await page.locator('#selhide').click();
      const hidden = await observe();
      assert.ok(hidden.parts.find((p) => p.name === 'Cap').visible.every((v) => !v));
      // A changed CAD hash must not discard an inspection choice that a same-hash rebuild keeps.
      edit(entry, '.circle(4)', '.circle(4.25)');
      const hiddenStatus = await until('hidden part rebuilt with fresh geometry', async () => {
        const st = await status(worker, 'TESTPLATE');
        return st.state === 'certified' && st.hashes.certified !== hidden.hash ? st : false;
      }, 45000);
      await shown('TESTPLATE', 'certified', hiddenStatus.hashes.certified);
      const rebuiltHidden = await observe();
      assert.ok(rebuiltHidden.parts.find((p) => p.name === 'Cap').visible.every((v) => !v));
      const showAllReachable = await page.locator('#selshow').isVisible() && await page.locator('#selshow').isEnabled();
      assert.equal(showAllReachable, true);
      await page.locator('#selshow').click();
      const shownAgain = await observe();
      assert.ok(shownAgain.parts.every((p) => p.visible.every(Boolean)));
      check('pointer-pick-hide-show', { actual_pointer: point, picked: 'Cap', hidden, shown: shownAgain,
        prior_hidden_hash: hidden.hash, rebuilt_hidden: rebuiltHidden, show_all_reachable: showAllReachable });
      await steps.nth(0).click();
      // Fresh renderer forces a real lazy-variant HTTP request. The command and shot travel
      // through the worker, not __cq actions or a forged WebSocket/PNG fixture.
      await page.reload(); await shown('TESTPLATE');
      let releaseCommandVariant, requestedCommandVariant;
      const heldCommand = new Promise((resolve) => { releaseCommandVariant = resolve; });
      const requestedCommand = new Promise((resolve) => { requestedCommandVariant = resolve; });
      await page.route('**/payload/*-certified-lifted-*.json', async (route) => {
        requestedCommandVariant(); await heldCommand; await route.continue();
      });
      const rendererPosts = [];
      const recordShot = (r) => {
        if (r.url().endsWith('/api/shot') && r.method() === 'POST') {
          const body = r.postDataJSON();
          rendererPosts.push({ id: body.id, project: body.project,
            sha256: digest(Buffer.from(body.data.split(',')[1], 'base64')) });
        }
      };
      page.on('request', recordShot);
      try {
        const wireStart = report.websocket_frames.length;
        const command = { project: 'TESTPLATE', step: 'apart', variant: 'lifted', explode: 0.5, xray: true,
          camera: { position: [150, -170, 110], target: [0, 0, 24] } };
        const commanded = await request(worker.origin, '/api/view', { method: 'POST', body: command });
        assert.equal(commanded.status, 202); assert.deepEqual(commanded.json, { accepted: true });
        await Promise.race([requestedCommand, pause(10000).then(() => { throw new Error('wire command did not request real lazy variant'); })]);
        const rendererResponse = page.waitForResponse((r) => r.url().endsWith('/api/shot') && r.request().method() === 'POST');
        rendererResponse.catch(() => {}); // Preserve the primary error if the outer scenario fails first.
        const screenshotResponse = request(worker.origin, '/api/screenshot', { method: 'POST', body: { project: 'TESTPLATE' }, timeoutMs: 30000 });
        screenshotResponse.catch(() => {});
        await until('worker screenshot request reached renderer while command is pending', () =>
          report.websocket_frames.slice(wireStart).some((m) => m.type === 'screenshot' && m.project === 'TESTPLATE'));
        assert.equal(rendererPosts.length, 0, 'screenshot captured before lazy command completed');
        releaseCommandVariant();
        const shotResponse = await screenshotResponse, rendered = await rendererResponse;
        assert.equal(shotResponse.status, 200); assert.equal(rendered.status(), 200);
        const shotPath = fs.realpathSync(shotResponse.json.path), shotRoot = fs.realpathSync(path.join(worker.data, 'evidence/shots'));
        assert.ok(shotPath.startsWith(shotRoot + path.sep)); assert.equal(shotResponse.json.project, 'TESTPLATE');
        const bytes = fs.readFileSync(shotPath), dimensions = pngDimensions(bytes), file = '09-protocol-shot.png';
        fs.writeFileSync(path.join(output, file), bytes);
        const shot = { file, project: shotResponse.json.project, id: path.basename(shotPath, '.png'), sha256: digest(bytes),
          ...dimensions, renderer_id: rendererPosts[0].id, renderer_sha256: rendererPosts[0].sha256 };
        assert.equal(rendererPosts.length, 1); assert.equal(shot.renderer_sha256, shot.sha256);
        const canvas = await page.locator('#stage canvas').evaluate((c) => ({ width: c.width, height: c.height }));
        assert.deepEqual(dimensions, canvas);
        const visible = await observe();
        assert.equal(visible.variant, 'lifted');
        for (const key of ['camera', 'target']) assert.ok(visible[key].every((v, i) =>
          Math.abs(v - command.camera[key === 'camera' ? 'position' : 'target'][i]) < 1e-8), 'settled wire camera differs from command');
        const rejected = [
          await request(worker.origin, '/api/view', { method: 'POST', body: { project: 'TESTPLATE', camera: [] } }),
          await request(worker.origin, '/api/shot', { method: 'POST', body: { id: 'unrequested-shot', project: 'TESTPLATE', data: 'data:image/png;base64,' + bytes.toString('base64') } }),
        ];
        assert.deepEqual(rejected.map((r) => r.status), [400, 400]);
        report.screenshots.push({ file, description: 'Actual HTTP view command waits for lazy pose; worker screenshot receives the renderer PNG through /api/shot at the settled camera/X-ray/explode state.', sha256: shot.sha256, ...dimensions });
        check('http-view-screenshot', { statuses: [commanded.status, shotResponse.status, rendered.status()],
          rejected_statuses: rejected.map((r) => r.status), held_actual_variant: true, renderer_posts_before_release: 0,
          request: command, visible, canvas, shot, worker_frames: report.websocket_frames.slice(wireStart) });
      } finally {
        releaseCommandVariant(); page.off('request', recordShot);
        await page.unroute('**/payload/*-certified-lifted-*.json');
      }
      await page.locator('#steps button').first().click();
    });
    await stopWorker(worker);
    const second = path.join(temporary, 'second'), bad = path.join(temporary, 'malformed');
    fs.cpSync(path.join(root, 'testpart'), second, { recursive: true });
    fs.mkdirSync(bad);
    fs.writeFileSync(path.join(bad, 'viewer.json'), '{invalid JSON');
    const secondManifest = readJSON(path.join(second, 'viewer.json'));
    secondManifest.name = 'SECONDPLATE'; secondManifest.live = false; secondManifest.on_demand = true;
    fs.writeFileSync(path.join(second, 'viewer.json'), JSON.stringify(secondManifest));
    const secondEntry = path.join(second, 'part_entry.py');
    const goodSecond = fs.readFileSync(secondEntry, 'utf8').replace('.box(40, 30, 6,', '.box(24, 30, 6,');
    fs.writeFileSync(secondEntry, 'def build(:\n');
    worker = await startWorker('multi', [demo, second, bad]);
    await page.goto(worker.origin);
    await scenario('isolation-reconnect', async () => {
      await certified(worker, 'TESTPLATE'); await shown('TESTPLATE');
      const all = (await request(worker.origin, '/api/status')).json;
      assert.deepEqual(all.projects.map((p) => p.name), ['TESTPLATE', 'SECONDPLATE']);
      assert.equal(all.load_errors.length, 1); assert.equal(all.load_errors[0].path, bad);
      check('malformed-manifest-isolation', { api_version: all.api_version, projects: all.projects.map((p) => p.name), errors: all.load_errors });
      await page.locator('#projects button').nth(1).click();
      assert.equal((await observe()).project, null);
      await page.locator('#buildbtn').click();
      await until('syntax failure', async () => (await status(worker, 'SECONDPLATE')).state === 'error');
      await page.waitForFunction(() => document.getElementById('tierbadge').textContent.includes('no certified model'));
      const syntax = await observe();
      assert.equal(syntax.project, null); assert.match(syntax.diagnostic, /SyntaxError/);
      assert.equal(fs.existsSync(path.join(worker.data, 'payloads/SECONDPLATE-certified.json')), false);
      check('syntax-error-no-model', { visible: syntax, certified_index_absent: true });
      await screenshot('07-syntax-no-model.png', 'Second project with deliberate Python syntax error shows no certified model and a clear diagnostic, never the first project geometry.');
      fs.writeFileSync(secondEntry, goodSecond);
      await page.locator('#buildbtn').click();
      const secondStatus = await certified(worker, 'SECONDPLATE');
      await shown('SECONDPLATE', 'certified', secondStatus.hashes.certified);
      const recovered = await observe();
      assert.ok(Math.abs(recovered.bounds.max[0] - recovered.bounds.min[0] - 24) < 0.01);
      assert.ok(Math.abs(recovered.decoded_extent[0] - 24) < 0.01, 'recovered project decoded geometry');
      check('error-recovery', { recovered });
      await page.locator('#projects button').nth(0).click(); await shown('TESTPLATE');
      let releaseProject, projectRequested;
      const requested = new Promise((resolve) => { projectRequested = resolve; });
      const held = new Promise((resolve) => { releaseProject = resolve; });
      await page.route('**/payload/SECONDPLATE-certified.json', async (route) => {
        projectRequested(); await held; await route.continue();
      });
      await page.locator('#projects button').nth(1).click();
      await Promise.race([requested, pause(10000).then(() => { throw new Error('real project payload request not observed'); })]);
      await page.locator('#projects button').nth(0).click(); await shown('TESTPLATE');
      const staleResponse = page.waitForResponse((r) => r.url().endsWith('/SECONDPLATE-certified.json'));
      releaseProject();
      await (await staleResponse).finished();
      await page.unroute('**/payload/SECONDPLATE-certified.json');
      await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      const switched = await observe();
      assert.equal(switched.project, 'TESTPLATE');
      assert.equal(await page.locator('#projects button').nth(0).getAttribute('aria-pressed'), 'true');
      check('project-switch-stale-response', { delayed_real_response: true, visible: switched });
      const old = await observe(), reconnectStart = report.websocket_frames.length, port = worker.port;
      await cdp.send('Network.enable');
      await cdp.send('Network.emulateNetworkConditions', { offline: true, latency: 0, downloadThroughput: -1, uploadThroughput: -1 });
      await stopWorker(worker);
      await page.waitForFunction(() => document.getElementById('buildline').textContent.includes('disconnected'));
      edit(entry, '.box(42, 30, 6,', '.box(44, 30, 6,');
      worker = await startWorker('reconnect', [demo, second, bad], source, port);
      assert.equal(worker.port, port);
      const rebuilt = await certified(worker, 'TESTPLATE');
      assert.notEqual(rebuilt.hashes.certified, old.hash);
      await cdp.send('Network.emulateNetworkConditions', { offline: false, latency: 0, downloadThroughput: -1, uploadThroughput: -1 });
      await shown('TESTPLATE', 'certified', rebuilt.hashes.certified);
      const snapshotFrames = report.websocket_frames.slice(reconnectStart);
      assert.ok(snapshotFrames.some((m) => m.type === 'hello' && m.projects.some((p) => p.name === 'TESTPLATE' && p.hashes.certified === rebuilt.hashes.certified)));
      const reconnected = await observe();
      assert.ok(Math.abs(reconnected.bounds.max[0] - reconnected.bounds.min[0] - 44) < 0.01);
      assert.ok(Math.abs(reconnected.decoded_extent[0] - 44) < 0.01, 'reconnect decoded geometry');
      check('reconnect-hello-snapshot', { previous_hash: old.hash, snapshot_hash: rebuilt.hashes.certified,
        missed_announcements: true, actual_browser_network_offline: true, reconnected,
        original_port: port, reconnected_port: worker.port,
        hello: snapshotFrames.find((m) => m.type === 'hello' && m.projects.some((p) => p.name === 'TESTPLATE' && p.hashes.certified === rebuilt.hashes.certified)) });
      await screenshot('08-reconnect-snapshot.png', 'After real worker restart and missed announcements, reconnect hello snapshot replaces old geometry with newly certified 44 mm public model.');
    });
    await scenario('boundary', async () => {
      const jsonStatuses = [];
      for (const body of ['{broken', '[]', '{"project":"TESTPLATE","explode":NaN}']) {
        const result = await request(worker.origin, '/api/rebuild', { method: 'POST', body });
        jsonStatuses.push(result.status); assert.equal(result.status, 400);
      }
      check('malformed-json', { bodies: ['syntax', 'array', 'non-finite number'], statuses: jsonStatuses });
      check('host-origin', await expectHostOrigin(worker));
      const traversalStatuses = [];
      for (const target of ['/..%2fworker.py', '/payload/..%2fworker.py', '/%2e%2e/worker.py', '/%00']) {
        const result = await request(worker.origin, target);
        traversalStatuses.push(result.status); assert.equal(result.status, 404, `traversal ${target}`);
      }
      check('static-traversal', { statuses: traversalStatuses, worker_source_not_served: true });
      const handshake = await new Promise((resolve, reject) => {
        const socket = net.connect({ host: '127.0.0.1', port: worker.port + 1000 });
        let text = '';
        socket.setTimeout(5000, () => socket.destroy(new Error('WS boundary timeout')));
        socket.on('error', reject);
        socket.on('connect', () => socket.write(`GET / HTTP/1.1\r\nHost: 127.0.0.1:${worker.port + 1000}\r\nOrigin: https://outside.invalid\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n`));
        socket.on('data', (data) => { text += data; if (text.includes('\r\n')) { socket.destroy(); resolve(text.split('\r\n')[0]); } });
      });
      assert.match(handshake, / 403 /); check('websocket-origin', { handshake });
      // Isolated copied-source witness uses the SAME live production HTTP assertion as baseline.
      const mutantRoot = path.join(temporary, 'mutant'); fs.mkdirSync(mutantRoot);
      const hashes = mutateWorker(path.join(mutantRoot, 'worker.py'));
      for (const name of pythonSources.filter((name) => name !== 'worker.py'))
        fs.copyFileSync(path.join(source, name), path.join(mutantRoot, name));
      fs.cpSync(path.join(root, 'testpart'), path.join(mutantRoot, 'testpart'), { recursive: true });
      const mutant = await startWorker('mutant', [], mutantRoot);
      let detected = false;
      try { await expectHostOrigin(mutant); }
      catch (error) {
        assert.match(error.message, /^host-origin/);
        assert.equal(error.actual, 200); assert.equal(error.expected, 403);
        detected = true;
      }
      assert.equal(detected, true, 'mutated guard escaped production HTTP assertion');
      await stopWorker(mutant);
      report.mutation = { detected, check: 'host-origin', ...hashes, baseline_status: 403, actual: 200, expected: 403,
        isolated_copy: true, production_source_unchanged: digest(fs.readFileSync(path.join(root, 'worker.py'))) === hashes.original_sha256 };
      assert.equal(report.mutation.production_source_unchanged, true);
      check('mutation-witness', report.mutation);
    });
    assert.deepEqual(report.browser_errors, [], 'uncaught browser errors');
  } catch (error) {
    if (!report.error) report.error = { message: error.message, stack: error.stack, scenario: activeScenario?.id || null };
    if (page && !page.isClosed()) {
      try { await screenshot('failure.png', `Failure evidence in ${activeScenario?.id || 'startup'}: ${error.message}`); }
      catch (shotError) { report.error.screenshot_error = shotError.message; }
    }
  } finally {
    const cleanupErrors = [];
    discoverBrowserGroup();
    try { if (browser) await browser.close(); } catch (error) { cleanupErrors.push(error.message); }
    for (const child of workers) {
      try { if (child.pid && groupExists(child.pid)) await stopWorker(child); }
      catch (error) { cleanupErrors.push(error.message); }
    }
    for (const pid of browserGroups) {
      try { if (groupExists(pid)) { signalGroup(pid, 'SIGKILL'); await until('browser process group exit', () => !groupExists(pid), 3000, true); } }
      catch (error) { cleanupErrors.push(error.message); }
    }
    const groupsGone = [...workers.map((c) => c.pid).filter(Boolean), ...browserGroups].every((pid) => !groupExists(pid));
    const portsClosed = (await Promise.all([...ports].map(listening))).every((open) => !open);
    report.cleanup = { pass: groupsGone && portsClosed && !cleanupErrors.length, process_groups_gone: groupsGone,
      ports_closed: portsClosed, owned_worker_pids: workers.map((c) => c.pid), owned_browser_groups: browserGroups,
      errors: cleanupErrors };
    report.elapsed_ms = Date.now() - started;
    report.pass = !report.error && report.cleanup.pass && !hardExpired;
    if (report.pass) {
      try { validateReport(report, output); }
      catch (error) { report.pass = false; report.error = { message: error.message, stack: error.stack, scenario: 'report-contract' }; }
    }
    fs.writeFileSync(path.join(output, 'integration-results.json'), JSON.stringify(report, null, 2));
    fs.rmSync(temporary, { recursive: true, force: true });
    clearTimeout(watchdog);
    clearTimeout(softWatchdog);
  }
  console.log(JSON.stringify({ pass: report.pass, report: path.join(output, 'integration-results.json'),
    scenarios: report.scenarios.map((s) => ({ id: s.id, pass: s.pass })), error: report.error?.message || null }));
  if (!report.pass) process.exitCode = 1;
  return report;
}

module.exports = { GATES, REQUIRED_CHECKS, pngDimensions, validateEvidence, validateReport, run };
if (require.main === module) {
  const args = process.argv.slice(2);
  if (args[0] === '--validate-report' && args.length === 2) {
    try { validateReport(readJSON(args[1]), path.dirname(path.resolve(args[1]))); }
    catch (error) { console.error(error.message); process.exitCode = 1; }
  } else if (args.length === 0 || (args.length === 1 && args[0] === '--mutation')) {
    run({ mutation: args[0] === '--mutation' }).catch((error) => { console.error(error.stack); process.exitCode = 1; });
  } else { console.error('Usage: node tools/integration_suite.cjs [--mutation | --validate-report FILE]'); process.exitCode = 2; }
}
