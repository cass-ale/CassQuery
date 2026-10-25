'use strict';
// Public-fixture real worker/CAD/browser proof. A test dialog bridge is NOT native dialog proof.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const http = require('node:http');
const net = require('node:net');
const { spawn, spawnSync } = require('node:child_process');
const { createHash, randomBytes } = require('node:crypto');
const { GATES, pngDimensions, validateReport: validateBaseline } = require('./integration_suite.cjs');
const { measureFrame } = require('../shell/smoke.cjs');
const { inflateSync } = require('node:zlib');

function analyzeCanvasRaster(bytes, rect) {
  // Decode only the already CRC/size-validated 8-bit RGB/RGBA PNG, including row filters.
  const size = pngDimensions(bytes), channels = bytes[25] === 2 ? 3 : 4, chunks = [];
  for (let offset = 8; offset < bytes.length;) {
    const length = bytes.readUInt32BE(offset), kind = bytes.toString('ascii', offset + 4, offset + 8);
    if (kind === 'IDAT') chunks.push(bytes.subarray(offset + 8, offset + 8 + length));
    offset += 12 + length;
  }
  const stride = size.width * channels, raw = inflateSync(Buffer.concat(chunks), { maxOutputLength: (stride + 1) * size.height });
  let previous = Buffer.alloc(stride), current = Buffer.alloc(stride);
  const histogram = new Map(), raster = createHash('sha256'); let samples = 0;
  const left = Math.max(0, Math.ceil(rect.x + rect.width * .1)), right = Math.min(size.width, Math.floor(rect.x + rect.width * .9));
  const top = Math.max(0, Math.ceil(rect.y + rect.height * .1)), bottom = Math.min(size.height, Math.floor(rect.y + rect.height * .9));
  assert.ok(right > left && bottom > top, 'Canvas pixel region is empty/outside screenshot');
  function paeth(a, b, c) { const p = a + b - c, x = Math.abs(p - a), y = Math.abs(p - b), z = Math.abs(p - c); return x <= y && x <= z ? a : y <= z ? b : c; }
  for (let y = 0; y < size.height; y++) {
    const filter = raw[y * (stride + 1)];
    for (let x = 0; x < stride; x++) {
      const a = x >= channels ? current[x - channels] : 0, b = previous[x], c = x >= channels ? previous[x - channels] : 0;
      const prediction = filter === 0 ? 0 : filter === 1 ? a : filter === 2 ? b : filter === 3 ? Math.floor((a + b) / 2) : paeth(a, b, c);
      current[x] = (raw[y * (stride + 1) + x + 1] + prediction) & 255;
    }
    if (y >= top && y < bottom) {
      const rgb = Buffer.alloc((right - left) * 3);
      for (let x = left; x < right; x++) {
        const index = x * channels, destination = (x - left) * 3;
        rgb[destination] = current[index]; rgb[destination + 1] = current[index + 1]; rgb[destination + 2] = current[index + 2];
      }
      raster.update(rgb);
    }
    if (y >= top && y < bottom) for (let x = left; x < right; x += 2) {
      const offset = x * channels, color = (current[offset] << 16) | (current[offset + 1] << 8) | current[offset + 2];
      histogram.set(color, (histogram.get(color) || 0) + 1); samples++;
    }
    [previous, current] = [current, previous];
  }
  let dominant = 0; for (const count of histogram.values()) dominant = Math.max(dominant, count);
  return { samples, distinct_colors: histogram.size, non_background_samples: samples - dominant, raster_sha256: raster.digest('hex') };
}
function canvasPixels(bytes, rect) {
  const { raster_sha256, ...pixels } = analyzeCanvasRaster(bytes, rect); return pixels;
}
function renderedPixels(pixels) {
  return pixels.samples > 0 && pixels.distinct_colors >= 8 && pixels.non_background_samples > pixels.samples * .001;
}
function validateRenderGeometry(record) {
  const observation = record.rendering;
  assert.ok(observation && observation.model_tier === 'certified');
  assert.match(observation.model_hash, /^[a-f0-9]{16}$/);
  assert.ok(Number.isInteger(observation.vertices) && observation.vertices > 0 && Number.isInteger(observation.meshes) && observation.meshes > 0);
  assert.equal(observation.clipped, 0); assert.equal(observation.nonfinite, 0); assert.equal(observation.context_lost, false);
  assert.ok(observation.canvas_rect.width > 0 && observation.canvas_rect.height > 0);
  const rect = observation.canvas_rect, scale = record.capture_scale || 1;
  assert.ok(['x', 'y', 'width', 'height'].every(key => Number.isFinite(rect[key])) && rect.x >= 0 && rect.y >= 0 &&
    (rect.x + rect.width) * scale <= record.width && (rect.y + rect.height) * scale <= record.height, 'Capture does not cover the measured canvas');
  assert.ok(Math.abs(observation.camera_aspect - observation.canvas_rect.width / observation.canvas_rect.height) < 1e-6);
  assert.deepEqual(observation.buffer, observation.drawing_buffer, 'Canvas and actual WebGL buffer disagree');
  assert.ok(observation.buffer[0] === Math.floor(observation.canvas_rect.width * observation.pixel_ratio) && observation.buffer[1] === Math.floor(observation.canvas_rect.height * observation.pixel_ratio));
}
function validateRendering(record, pixels) {
  validateRenderGeometry(record);
  assert.ok(pixels.samples > 0 && pixels.distinct_colors >= 8 && pixels.non_background_samples > pixels.samples * .001, 'Captured canvas is blank or has no meaningful rendered geometry');
  assert.deepEqual(record.canvas_pixels, pixels, 'Recorded canvas pixels contradict captured PNG');
}

function validateCaptureIdentity(before, after, expected = null) {
  for (const rendering of [before.rendering, after.rendering]) {
    assert.equal(rendering.model_tier, 'certified', 'Capture must show certified geometry, never a stable preview');
    assert.ok(typeof rendering.model_project === 'string' && rendering.model_project.length > 0);
    assert.match(rendering.model_hash, /^[a-f0-9]{16}$/);
    assert.ok(Number.isInteger(rendering.model_gen) && rendering.model_gen >= 0 && Number.isFinite(rendering.model_built_at) && rendering.model_built_at > 0);
  }
  for (const key of ['model_tier','model_project','model_hash','model_gen','model_built_at'])
    assert.equal(after.rendering[key], before.rendering[key], 'Certified model changed during capture: ' + key);
  if (expected) {
    assert.equal(before.rendering.model_project, expected.project);
    assert.equal(before.rendering.model_hash, expected.hash);
    assert.equal(before.rendering.model_built_at, expected.built_at);
  }
}
function rasterIdentity(layout) {
  const r = layout.rendering;
  return JSON.stringify([layout.viewport,layout.full_size,layout.dpr,layout.horizontal_overflow,
    r.model_tier,r.model_project,r.model_hash,r.model_gen,r.model_built_at,r.canvas_rect,r.stage_size,r.buffer,r.drawing_buffer,
    r.pixel_ratio,r.camera_aspect,r.theme,r.theme_choice,r.panel,r.source_fingerprint]);
}
function validateRasterIdentity(before, after, message) {
  assert.equal(rasterIdentity(before),rasterIdentity(after),message);
  for(const key of ['camera','target']){
    const a=before.rendering[key],b=after.rendering[key];vector(a);vector(b);
    for(let index=0;index<3;index++)assert.ok(Math.abs(a[index]-b[index])<=128*Number.EPSILON*Math.max(1,Math.abs(a[index]),Math.abs(b[index])),
      message+': '+key+' coordinate '+index+' changed beyond machine roundoff');
  }
}
async function captureStableRaster({ observe, take, retain, now = Date.now, readiness_started_at = now(), deadline_at = readiness_started_at + 15000 }, attempts = []) {
  const deadline = deadline_at;
  assert.ok(Number.isFinite(readiness_started_at) && Number.isFinite(deadline) && deadline > readiness_started_at && deadline <= readiness_started_at + 15000,
    'Invalid aggregate physical capture deadline');
  let baseline, previous = null;
  const checkTime = () => assert.ok(now() < deadline, 'Physical raster readiness exceeded its 15-second deadline');
  async function within(work) {
    checkTime(); let timer;
    try { return await Promise.race([Promise.resolve().then(work),new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error('Physical raster readiness exceeded its 15-second deadline')),Math.max(1,deadline-now()));})]); }
    finally { clearTimeout(timer); }
  }
  for (let index = 0; index < 8; index++) {
    checkTime(); const before = await within(()=>observe(deadline - now())); checkTime();
    validateCaptureIdentity(before,before);
    validateRenderGeometry({...before,width:before.full_size[0],height:before.full_size[1]});
    if (!baseline) baseline = before;
    validateRasterIdentity(before,baseline,'Source/camera/theme/layout identity changed during raster readiness');
    const attempt = { index, readiness_started_at, deadline_at:deadline, started_at:now(), before, status:'pending' };
    attempts.push(attempt);
    try {
      const bytes = await within(()=>take(before,deadline - now())); attempt.captured_at = now();
      assert.ok(Buffer.isBuffer(bytes) && bytes.length <= 32 * 1024 ** 2, 'Physical raster exceeds capture byte bound');
      attempt.observed_bytes = bytes.length; attempt.observed_sha256 = digest(bytes);
      attempt.image = await within(()=>retain(bytes,index,before,deadline - now())); checkTime();
      const after = await within(()=>observe(deadline - now())); attempt.after = after; checkTime();
      validateCaptureIdentity(before,after);
      validateRenderGeometry({...after,width:after.full_size[0],height:after.full_size[1]});
      validateRasterIdentity(after,before,'Source/camera/theme/layout identity changed during physical capture');
      const dimensions = pngDimensions(bytes);
      assert.deepEqual([dimensions.width,dimensions.height],before.full_size,'Physical raster does not cover the observed full document');
      const analysis = analyzeCanvasRaster(bytes,before.rendering.canvas_rect);
      const { raster_sha256, ...pixels } = analysis;
      attempt.pixels = pixels; attempt.raster_sha256 = raster_sha256;
      attempt.status = renderedPixels(pixels) ? 'rendered' : 'blank'; attempt.completed_at = now(); checkTime();
      if (attempt.status === 'rendered') {
        validateRendering({ ...attempt.image,...before,canvas_pixels:pixels },pixels);
        if (previous?.status === 'rendered' && previous.raster_sha256 === raster_sha256) return attempt;
      }
      previous = attempt;
    } catch(error) { attempt.status='failed';attempt.error=error.message;attempt.completed_at=now();throw error; }
  }
  assert.fail('Physical raster never produced two consecutive stable rendered samples within eight attempts');
}
function validateRasterAttempts(attempts, directory) {
  assert.ok(Array.isArray(attempts) && attempts.length >= 2 && attempts.length <= 8, 'Incomplete/excessive physical raster samples');
  const baseline=attempts[0].before, start=attempts[0].readiness_started_at;
  let previous=null; const paths=new Set();
  for (const [index,attempt] of attempts.entries()) {
    assert.equal(attempt.index,index);assert.equal(attempt.readiness_started_at,start);
    assert.ok(attempt.deadline_at>start && attempt.deadline_at<=start+15000 && attempt.deadline_at===attempts[0].deadline_at);
    assert.ok([start,attempt.started_at,attempt.captured_at,attempt.completed_at].every(Number.isFinite) && start>0 &&
      attempt.started_at>=start && attempt.captured_at>=attempt.started_at && attempt.completed_at>=attempt.captured_at && attempt.completed_at<attempt.deadline_at);
    validateCaptureIdentity(attempt.before,attempt.after);
    validateRasterIdentity(attempt.before,baseline,'Raster readiness source identity changed');
    validateRasterIdentity(attempt.after,baseline,'Raster readiness post-capture identity changed');
    for (const layout of [attempt.before,attempt.after]) validateRenderGeometry({...layout,width:layout.full_size[0],height:layout.full_size[1]});
    const image=attempt.image;requireHash(image.sha256);assert.ok(!paths.has(image.path),'Duplicate raster sample path');paths.add(image.path);
    const bytes=fs.readFileSync(relativeEvidence(directory,image.path));assert.equal(bytes.length,image.bytes);assert.equal(digest(bytes),image.sha256);
    assert.equal(attempt.observed_bytes,image.bytes);assert.equal(attempt.observed_sha256,image.sha256);
    const size=pngDimensions(bytes);assert.deepEqual([size.width,size.height],attempt.before.full_size);
    const {raster_sha256,...pixels}=analyzeCanvasRaster(bytes,attempt.before.rendering.canvas_rect);
    assert.equal(attempt.raster_sha256,raster_sha256);assert.deepEqual(attempt.pixels,pixels);
    assert.equal(attempt.status,renderedPixels(pixels)?'rendered':'blank','Raster sample classification contradicts physical pixels');
    if(previous)assert.ok(attempt.started_at>=previous.completed_at,'Raster samples must be serial');
    previous=attempt;
  }
  const first=attempts.at(-2),last=attempts.at(-1);
  assert.equal(first.status,'rendered');assert.equal(last.status,'rendered');assert.equal(first.raster_sha256,last.raster_sha256,'Final physical rasters are not consecutive/stable');
  return last;
}
function captureScripts(drawEligibility = measureFrame) {
  const sample = `(() => {
    const measured=(${drawEligibility.toString()})();
    const cq=window.__cq,c=document.querySelector('#stage canvas'),r=c.getBoundingClientRect(),gl=c.getContext('webgl2');
    const stage=document.getElementById('stage');
    return {viewport:[innerWidth,innerHeight],full_size:[innerWidth,Math.max(innerHeight,document.documentElement.scrollHeight)],dpr:devicePixelRatio,
      horizontal_overflow:document.documentElement.scrollWidth>innerWidth,
      rendering:{model_tier:cq.model?.tier,model_hash:cq.model?.hash,model_project:cq.model?.index.project,model_gen:cq.model?.index.gen,model_built_at:cq.model?.index.built_at,
        worker_state:cq.status?.state,rebuilding:!!cq.rebuilding,theme:document.documentElement.dataset.theme,theme_choice:document.documentElement.dataset.themeChoice,
        panel:getComputedStyle(document.documentElement).getPropertyValue('--panel'),source_fingerprint:window.__workbench?.inspection?.source_fingerprint,
        vertices:measured.vertices,meshes:measured.meshes,clipped:measured.clipped,nonfinite:measured.nonfinite,
        canvas_rect:{x:r.left+scrollX,y:r.top+scrollY,width:r.width,height:r.height},stage_size:[stage.clientWidth,stage.clientHeight],buffer:[c.width,c.height],
        drawing_buffer:gl?[gl.drawingBufferWidth,gl.drawingBufferHeight]:[0,0],context_lost:!gl||gl.isContextLost(),pixel_ratio:Math.min(devicePixelRatio,2),
        camera_aspect:cq.camera.aspect,camera:cq.camera.position.toArray(),target:cq.controls.target.toArray()}}
  })()`;
  const settled = `(async () => {
    await Promise.all(Array.from(document.images).filter(e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden';}).map(e=>e.decode()));
    let previous='',stable=0;const recent=[];
    const end=performance.now()+10000;
    while(performance.now()<end){
      await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
      const value=${sample}; const r=value.rendering;
      const key=JSON.stringify([value.full_size,r.canvas_rect,r.buffer,r.camera_aspect,r.camera,r.target,r.model_tier,r.model_project,r.model_hash,r.model_gen,r.model_built_at]);
      const aligned=r.model_tier==='certified'&&typeof r.model_project==='string'&&r.model_project.length>0&&/^[a-f0-9]{16}$/.test(r.model_hash)&&
        Number.isInteger(r.model_gen)&&r.model_gen>=0&&Number.isFinite(r.model_built_at)&&r.model_built_at>0&&!r.rebuilding&&!r.worker_state?.startsWith('building')&&
        [r.camera,r.target].every(vector=>Array.isArray(vector)&&vector.length===3&&vector.every(Number.isFinite))&&
        r.vertices>0&&r.meshes>0&&r.clipped===0&&r.nonfinite===0&&!r.context_lost&&r.buffer[0]===r.drawing_buffer[0]&&r.buffer[1]===r.drawing_buffer[1]&&
        Math.abs(r.camera_aspect-r.canvas_rect.width/r.canvas_rect.height)<1e-6&&r.stage_size[0]===Math.round(r.canvas_rect.width)&&r.stage_size[1]===Math.round(r.canvas_rect.height);
      const key_changed=key!==previous;
      stable=aligned&&key===previous?stable+1:0;previous=key;
      recent.push({observed_at:performance.now(),aligned,stable,key_changed,...value});if(recent.length>2)recent.shift();
      if(stable>=2)return value;
    }
    throw new Error('Actual workbench layout/geometry did not settle before capture; last_readiness='+JSON.stringify(recent));
  })()`;
  return { sample, settled };
}

const THEMES = ['Counterform Dark', 'Counterform Light', 'Graphite', 'Midnight', 'Forest', 'Sand', 'Paper', 'High Contrast'];
const REQUIRED_CHECKS = {
  'source-edit': ['capability-boundary', 'open-folder-file', 'edit-save-volume', 'external-conflict', 'syntax-retains-certified', 'draft-recovery'],
  'cad-roundtrip': ['source-transform', 'step-export-import'],
  'workbench-ui': ['eight-themes', 'font-tabs', 'shortcut-focus'],
};
const digest = (bytes) => createHash('sha256').update(bytes).digest('hex');
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
function requireHash(value) { assert.equal(typeof value, 'string'); assert.match(value, /^[a-f0-9]{64}$/); }
function positive(value) { assert.ok(typeof value === 'number' && Number.isFinite(value) && value > 0); }
function vector(value) { assert.ok(Array.isArray(value) && value.length === 3 && value.every(Number.isFinite)); }
function geometry(value) {
  positive(value.volume); assert.ok(Number.isInteger(value.solids) && value.solids > 0);
  vector(value.min); vector(value.max); assert.ok(value.max.every((n, i) => n > value.min[i]));
}
function sameGeometry(a, b) {
  geometry(a); geometry(b); assert.ok(Math.abs(a.volume - b.volume) <= Math.max(a.volume, b.volume) * 1e-6);
  for (const key of ['min', 'max']) assert.ok(a[key].every((n, i) => Math.abs(n - b[key][i]) < 1e-5));
}
function validateEvidence(id, e) {
  assert.ok(e && typeof e === 'object' && !Array.isArray(e));
  switch (id) {
    case 'capability-boundary':
      assert.equal(e.without_capability, 403); assert.equal(e.wrong_capability, 403);
      assert.equal(e.status_contains_token, false); break;
    case 'open-folder-file':
      assert.equal(e.folder.project, 'WORKBENCHPLATE'); assert.ok(e.folder.entry.endsWith('.py'));
      assert.ok(e.folder.files.some((f) => f.path === e.folder.entry && f.bytes > 0));
      assert.notEqual(e.file.project, e.folder.project); assert.ok(e.file.entry.endsWith('.py'));
      requireHash(e.original_file_before); assert.equal(e.original_file_after, e.original_file_before);
      assert.equal(e.editor_follows_selected_file, true); break;
    case 'edit-save-volume':
      requireHash(e.before_source); requireHash(e.after_source); assert.notEqual(e.before_source, e.after_source);
      geometry(e.before); geometry(e.after); assert.ok(Math.abs(e.after.volume - e.before.volume - 360) < 1e-4);
      assert.equal(e.save_status, 200); assert.equal(e.renderer_tier, 'certified');
      assert.deepEqual(e.gate_ids, GATES); assert.equal(e.gates_pass, true); break;
    case 'external-conflict':
      assert.equal(e.status, 409); requireHash(e.draft_before); assert.equal(e.draft_after, e.draft_before);
      requireHash(e.external_source); assert.equal(e.disk_after, e.external_source);
      assert.notEqual(e.draft_after, e.disk_after); assert.equal(e.conflict_visible, true); break;
    case 'syntax-retains-certified':
      assert.match(e.certified_before, /^[a-f0-9]{16}$/); assert.equal(e.certified_after, e.certified_before);
      assert.equal(e.certified_announcements, 0); assert.match(e.diagnostic, /SyntaxError/);
      assert.equal(e.renderer_tier, 'certified'); break;
    case 'draft-recovery':
      requireHash(e.stored_draft); assert.equal(e.restored_draft, e.stored_draft);
      assert.equal(e.disk_before, e.disk_after); requireHash(e.disk_before);
      assert.notEqual(e.stored_draft, e.disk_after); assert.equal(e.production_store, true); break;
    case 'source-transform':
      assert.deepEqual(e.transform, { translation: [3, -2, 5], rotation: [0, 0, 90], scale: 1.5 });
      requireHash(e.manifest_before); requireHash(e.manifest_after); assert.notEqual(e.manifest_before, e.manifest_after);
      assert.deepEqual(e.persisted, e.transform); geometry(e.before); geometry(e.after);
      assert.ok(Math.abs(e.after.volume / e.before.volume - 3.375) < 1e-5);
      assert.ok(e.after.min.every((v, i) => Math.abs(v - [-19.5, -33.5, 5][i]) < 1e-5));
      assert.ok(e.after.max.every((v, i) => Math.abs(v - [25.5, 29.5, 14][i]) < 1e-5));
      assert.equal(e.renderer_tier, 'certified'); break;
    case 'step-export-import':
      sameGeometry(e.exported, e.imported); requireHash(e.export_sha256); assert.equal(e.original_export_after_import, e.export_sha256);
      assert.ok(Number.isInteger(e.build_calls_before_export) && e.build_calls_before_export > 0);
      assert.equal(e.build_calls_after_export, e.build_calls_before_export);
      assert.equal(e.dirty_export_requests, 0); assert.equal(e.stale_export_rejected, true); break;
    case 'eight-themes':
      assert.ok(Array.isArray(e.states) && e.states.length === THEMES.length);
      assert.deepEqual(e.states.map((s) => s.name).sort(), [...THEMES].sort());
      assert.equal(new Set(e.states.map((s) => s.name)).size, THEMES.length);
      assert.equal(new Set(e.states.map((s) => s.styling)).size, THEMES.length);
      for (const state of e.states) { assert.equal(state.persisted, true); assert.equal(state.visible, true); requireHash(state.image_sha256); }
      break;
    case 'font-tabs':
      assert.equal(e.font_size, 22); assert.equal(e.rendered_font_size, 22);
      assert.equal(e.tab_size, 2); assert.equal(e.inserted_spaces, 2); assert.equal(e.persistence_after_reload, true); break;
    case 'shortcut-focus':
      assert.equal(e.text_input_received_digit, true); assert.equal(e.focused_editor_camera_unchanged, true);
      assert.equal(e.outside_axis_camera_changed, true); assert.equal(e.rebuild_status, 202);
      assert.equal(e.find_visible, true); assert.equal(e.undo_restored_text, true); break;
    default: assert.fail('unknown workbench check: ' + id);
  }
}
function relativeEvidence(directory, name) {
  assert.ok(typeof name === 'string' && name && !path.isAbsolute(name) && !name.includes('\\') &&
    name.split('/').every((part) => part && part !== '.' && part !== '..'));
  const base = fs.realpathSync(directory), file = path.join(base, name);
  for (let cursor = file; cursor !== base; cursor = path.dirname(cursor)) assert.equal(fs.lstatSync(cursor).isSymbolicLink(), false);
  assert.ok(fs.realpathSync(file).startsWith(base + path.sep)); return file;
}
function validateReport(report, directory) {
  assert.equal(report.schema, 1); assert.equal(report.platform, 'linux-source-browser-workbench');
  assert.equal(report.pass, true); assert.equal(report.error, null);
  assert.equal(report.method, 'real worker/CAD/files; DOM keyboard/pointer; production stores; simulated desktop chooser');
  assert.equal(report.native_dialog_selection, 'not checked'); assert.deepEqual(report.viewport, [1600, 1000]);
  assert.deepEqual(report.browser_errors, []);
  assert.deepEqual(report.cleanup.errors, []); assert.equal(report.cleanup.process_groups_gone, true); assert.equal(report.cleanup.ports_closed, true);
  requireHash(report.source.before_fingerprint); assert.equal(report.source.after_fingerprint, report.source.before_fingerprint);
  requireHash(report.source.worker_sha256); requireHash(report.source.viewer_sha256); requireHash(report.source.suite_sha256);
  const closure = report.source.files;
  assert.ok(Array.isArray(closure) && closure.length >= 10 && closure.length <= 10000);
  assert.equal(new Set(closure.map((file) => file.path)).size, closure.length);
  for (const file of closure) { assert.ok(typeof file.path === 'string' && !path.isAbsolute(file.path) && !file.path.split('/').includes('..')); requireHash(file.sha256); assert.ok(Number.isInteger(file.bytes) && file.bytes >= 0); }
  const canonical = (value) => JSON.stringify(value && typeof value === 'object' && !Array.isArray(value)
    ? Object.fromEntries(Object.keys(value).sort().map((key) => [key, JSON.parse(canonical(value[key]))]))
    : Array.isArray(value) ? value.map((item) => JSON.parse(canonical(item))) : value);
  assert.equal(digest(Buffer.from(canonical({ source_sha: report.source.source_sha, files: closure }))), report.source.before_fingerprint);
  for (const [field, name] of [['worker_sha256', 'worker.py'], ['viewer_sha256', 'viewer/app.js'], ['suite_sha256', 'tools/workbench_suite.cjs']])
    assert.equal(closure.find((file) => file.path === name)?.sha256, report.source[field], 'Source closure does not bind ' + name);
  assert.ok(Number.isInteger(report.worker_pid) && report.worker_pid > 0); positive(report.started_at); positive(report.completed_at);
  assert.ok(report.completed_at > report.started_at);
  assert.equal(report.scenarios.length, Object.keys(REQUIRED_CHECKS).length);
  for (const [name, checks] of Object.entries(REQUIRED_CHECKS)) {
    const scenarios = report.scenarios.filter((s) => s.id === name); assert.equal(scenarios.length, 1);
    const scenario = scenarios[0]; assert.equal(scenario.pass, true); assert.equal(scenario.checks.length, checks.length);
    for (const id of checks) {
      const found = scenario.checks.filter((c) => c.id === id); assert.equal(found.length, 1);
      assert.equal(found[0].pass, true); validateEvidence(id, found[0].evidence);
    }
  }
  const allChecks = report.scenarios.flatMap((scenario) => scenario.checks);
  sameGeometry(allChecks.find((check) => check.id === 'source-transform').evidence.after,
    allChecks.find((check) => check.id === 'step-export-import').evidence.exported);
  assert.ok(Array.isArray(report.files) && report.files.length >= 4);
  const names = new Set();
  for (const record of [...report.files, ...report.screenshots]) {
    assert.ok(!names.has(record.path)); names.add(record.path); requireHash(record.sha256);
    const file = relativeEvidence(directory, record.path), bytes = fs.readFileSync(file);
    assert.equal(digest(bytes), record.sha256); assert.equal(bytes.length, record.bytes);
    if (record.path.endsWith('.png')) {
      const size = pngDimensions(bytes); assert.equal(size.width, record.width); assert.equal(size.height, record.height);
      assert.deepEqual(record.viewport, report.viewport); assert.equal(record.dpr, 1); assert.equal(record.horizontal_overflow, false);
      assert.ok(Array.isArray(record.full_size) && record.full_size[0] === 1600 && record.full_size[1] >= 1000);
      assert.equal(size.width, 1600); assert.equal(size.height, record.full_size[1]);
      const pixels = canvasPixels(bytes, record.rendering?.canvas_rect || {}); validateRendering(record, pixels);
      const selected=validateRasterAttempts(record.raster_attempts,directory);
      assert.equal(record.sha256,selected.image.sha256,'Final screenshot differs from its selected physical sample');
      assert.equal(record.raster_sha256,selected.raster_sha256);
      validateRasterIdentity(record,selected.before,'Final screenshot metadata differs from its physical raster sample');
    }
  }
  assert.ok(Array.isArray(report.capture_attempts)&&report.capture_attempts.length===report.screenshots.length,'Missing/excessive raster readiness journal');
  const journal=JSON.parse(fs.readFileSync(relativeEvidence(directory,'capture-attempts.json')));
  assert.deepEqual(journal,report.capture_attempts,'Retained capture journal differs from report');
  for (const shot of report.screenshots) {
    const entries=report.capture_attempts.filter(entry=>entry.description===shot.description);
    assert.equal(entries.length,1);assert.equal(entries[0].status,'stable');assert.equal(entries[0].method,'bounded serial physical raster readiness');
    assert.ok(Number.isFinite(entries[0].started_at)&&entries[0].started_at>0&&entries[0].deadline_at===entries[0].started_at+15000&&
      Number.isFinite(entries[0].completed_at)&&entries[0].completed_at>=entries[0].started_at&&entries[0].completed_at<entries[0].deadline_at,'Capture action exceeded its aggregate deadline');
    assert.deepEqual(entries[0].attempts,shot.raster_attempts);
    assert.ok(entries[0].completed_at>=shot.raster_attempts.at(-1).completed_at,'Capture action completed before its final physical sample');
    for(const attempt of shot.raster_attempts){
      assert.equal(attempt.readiness_started_at,entries[0].started_at,'Capture sample origin differs from the action origin');
      assert.equal(attempt.deadline_at,entries[0].deadline_at-1000,'Capture sample budget differs from the reserved action budget');
    }
  }
  for (const name of ['before-edit.py', 'after-edit.py', 'transformed-manifest.json', 'recovery.json', 'settings.json', 'transformed.step', 'roundtrip.step'])
    assert.ok(report.files.some((record) => record.path === name), 'missing workbench file evidence: ' + name);
  for (const name of ['transformed.step', 'roundtrip.step']) {
    const text = fs.readFileSync(relativeEvidence(directory, name), 'utf8');
    assert.ok(text.startsWith('ISO-10303-21;') && text.includes('DATA;') && text.trimEnd().endsWith('END-ISO-10303-21;'), 'incomplete STEP evidence');
  }
  assert.equal(report.files.find((record) => record.path === 'transformed.step').sha256,
    allChecks.find((check) => check.id === 'step-export-import').evidence.export_sha256);
  for (const [checkId, key, file] of [['edit-save-volume', 'before_source', 'before-edit.py'], ['edit-save-volume', 'after_source', 'after-edit.py'],
    ['source-transform', 'manifest_after', 'transformed-manifest.json'], ['open-folder-file', 'original_file_before', 'plain-original.py'],
    ['external-conflict', 'draft_before', 'conflict-draft.py'], ['external-conflict', 'external_source', 'external-source.py']])
    assert.equal(allChecks.find((check) => check.id === checkId).evidence[key], report.files.find((record) => record.path === file)?.sha256, 'observation not bound to file: ' + file);
  const recovery = JSON.parse(fs.readFileSync(relativeEvidence(directory, 'recovery.json')));
  const draft = recovery.drafts.find((item) => item.project === 'WORKBENCHPLATE' && item.path === 'part_entry.py'); assert.ok(draft);
  assert.equal(digest(Buffer.from(draft.text)), allChecks.find((check) => check.id === 'draft-recovery').evidence.stored_draft);
  const recordedTransform = JSON.parse(fs.readFileSync(relativeEvidence(directory, 'transformed-manifest.json'))).workbench.transforms.Base;
  assert.deepEqual(recordedTransform, allChecks.find((check) => check.id === 'source-transform').evidence.persisted);
  for (const theme of THEMES) {
    const image = report.screenshots.find((image) => image.description === theme); assert.ok(image);
    assert.equal(image.sha256, allChecks.find((check) => check.id === 'eight-themes').evidence.states.find((state) => state.name === theme).image_sha256);
  }
  for (const name of ['edit-certified', 'external-conflict', 'syntax-retained', 'step-roundtrip'])
    assert.ok(report.screenshots.some((image) => image.description === name));
  const baseline = report.baseline;
  requireHash(baseline.sha256); const baselineFile = relativeEvidence(directory, baseline.path);
  assert.equal(digest(fs.readFileSync(baselineFile)), baseline.sha256);
  const old = JSON.parse(fs.readFileSync(baselineFile)); validateBaseline(old, path.dirname(baselineFile));
  assert.equal(old.source.worker_sha256, report.source.worker_sha256); assert.equal(old.source.viewer_sha256, report.source.viewer_sha256);
  assert.ok(Array.isArray(report.source.python_sources) && report.source.python_sources.length === 3);
  for (const record of report.source.python_sources) assert.deepEqual(old.source.python_sources.find((p) => p.path === record.path), record);
  return true;
}
function request(origin, target, { body, headers = {}, timeoutMs = 10000 } = {}) {
  return new Promise((resolve, reject) => {
    const bytes = body === undefined ? null : Buffer.from(JSON.stringify(body));
    if (bytes) assert.ok(bytes.length <= 3 * 1024 ** 2);
    let timer;
    const req = http.request(new URL(origin), { path: target, method: bytes ? 'POST' : 'GET',
      headers: { ...headers, ...(bytes ? { 'Content-Type': 'application/json', 'Content-Length': bytes.length } : {}) } }, (res) => {
      const chunks = []; let size = 0;
      res.on('data', (chunk) => { size += chunk.length; if (size > 16 * 1024 ** 2) req.destroy(new Error('workbench response exceeds bound')); else chunks.push(chunk); });
      res.on('error', reject); res.on('end', () => { clearTimeout(timer); const data = Buffer.concat(chunks); let json;
        try { json = JSON.parse(data.toString()); } catch (error) { reject(new Error('non-JSON worker response')); return; }
        resolve({ status: res.statusCode, json }); });
    });
    timer = setTimeout(() => req.destroy(new Error('absolute workbench HTTP deadline')), timeoutMs);
    req.on('error', (error) => { clearTimeout(timer); reject(error); }); req.end(bytes);
  });
}
function recordWorkbenchTraffic(port, trace = []) {
  // Observe actual production HTTP calls without replacing their request, listener or response.
  const original = http.request;
  http.request = function (...args) {
    const req = original.apply(this, args), options = args[0], requestOptions = options instanceof URL ? args[1] : options;
    const targetPort = options instanceof URL ? Number(options.port) : Number(options.port);
    if (targetPort === port && requestOptions?.path === '/api/workbench') {
      const chunks = [], end = req.end.bind(req), write = req.write.bind(req), record = { action: null, status: null };
      req.write = (chunk, ...rest) => { if (chunk) chunks.push(Buffer.from(chunk)); return write(chunk, ...rest); };
      req.end = (chunk, ...rest) => {
        if (chunk) chunks.push(Buffer.from(chunk));
        try { const body = JSON.parse(Buffer.concat(chunks)); record.action = body.action; record.project = body.project; if (body.action === 'open') record.selected_path = body.path; }
        catch { /* Invalid input is still sent unchanged, not substituted. */ }
        trace.push(record); return end(chunk, ...rest);
      };
      req.on('response', (res) => {
        record.status = res.statusCode;
        const responses = []; let size = 0;
        res.on('data', (chunk) => { size += chunk.length; if (size <= 4 * 1024 ** 2) responses.push(chunk); });
        res.on('end', () => {
          if (size <= 4 * 1024 ** 2) {
            try { const value = JSON.parse(Buffer.concat(responses)); record.response = value; } catch { /* Original consumer still validates JSON. */ }
          }
        });
      });
    }
    return req;
  };
  return { trace, stop: () => { http.request = original; } };
}
function sourceSnapshot(root, python) {
  const code = 'import sys,json;sys.path.insert(0,sys.argv[1]+"/tools");from release_evidence import source_snapshot;print(json.dumps(source_snapshot(sys.argv[1])))';
  const result = spawnSync(python, ['-c', code, root], { encoding: 'utf8', timeout: 30000, maxBuffer: 16 * 1024 ** 2 });
  assert.equal(result.status, 0, result.stderr || result.error?.message); return JSON.parse(result.stdout);
}
function groupExists(pid) {
  try { process.kill(-pid, 0); return true; } catch (error) { if (error.code === 'ESRCH') return false; throw error; }
}
function signalGroup(pid, signal) {
  try { process.kill(-pid, signal); } catch (error) { if (error.code !== 'ESRCH') throw error; }
}
function listening(port) {
  return new Promise((resolve) => { const socket = net.connect({ host: '127.0.0.1', port });
    socket.setTimeout(500); socket.once('connect', () => { socket.destroy(); resolve(true); });
    socket.once('error', () => resolve(false)); socket.once('timeout', () => { socket.destroy(); resolve(false); }); });
}
function measureStep(file, python) {
  const code = 'import sys,json,cadquery as cq;from OCP.Bnd import Bnd_Box;from OCP.BRepBndLib import BRepBndLib;s=cq.importers.importStep(sys.argv[1],unit="MM").val();b=Bnd_Box();BRepBndLib.AddOptimal_s(s.wrapped,b,False,False);v=b.Get();print(json.dumps({"volume":s.Volume(),"solids":len(s.Solids()),"min":list(v[:3]),"max":list(v[3:])}))';
  const result = spawnSync(python, ['-c', code, file], { encoding: 'utf8', timeout: 30000, maxBuffer: 1024 * 1024 });
  assert.equal(result.status, 0, result.stderr || result.error?.message); const value = JSON.parse(result.stdout); geometry(value); return value;
}

function verifyStepEvidence(report, directory, python = process.env.CASSQUERY_PYTHON || 'python3') {
  // The executable is selected by the caller environment, never by untrusted report data.
  const checks = report.scenarios.flatMap((scenario) => scenario.checks);
  const evidence = checks.find((check) => check.id === 'step-export-import').evidence;
  sameGeometry(measureStep(relativeEvidence(directory, 'transformed.step'), python), evidence.exported);
  sameGeometry(measureStep(relativeEvidence(directory, 'roundtrip.step'), python), evidence.imported);
  return true;
}

async function run() {
  const root = path.resolve(__dirname, '..'), python = process.env.CASSQUERY_PYTHON || 'python3';
  const output = path.resolve(process.env.CASSQUERY_WORKBENCH_OUTPUT || path.join(root, 'evidence/workbench'));
  assert.equal(fs.existsSync(output), false, 'Workbench evidence requires a new directory'); fs.mkdirSync(output, { recursive: true });
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'cassquery-workbench-'));
  const source = path.join(temporary, 'source'), data = path.join(temporary, 'data');
  const report = { schema: 1, platform: 'linux-source-browser-workbench', pass: false, error: null,
    method: 'real worker/CAD/files; DOM keyboard/pointer; production stores; simulated desktop chooser',
    native_dialog_selection: 'not checked', not_checked: ['native preload/IPC and OS dialog selection', 'update install/rollback', 'human visual approval'],
    started_at: Date.now(), viewport: [1600, 1000], scenarios: [], files: [], screenshots: [], browser_errors: [], cleanup: { errors: [] }, source: {} };
  const deadline = Date.now() + 480000, token = randomBytes(32).toString('hex');
  const groups = [], ports = [], bridgeTrace = [], wireTrace = [];
  let worker, browser, page, traffic, updater, before, currentScenario;
  const browserPID = path.join(temporary, 'browser-pid');
  const reportFile = path.join(output, 'workbench-results.json');
  function writeReport() { fs.writeFileSync(reportFile, JSON.stringify(report, null, 2) + '\n'); }
  writeReport();
  function discoverBrowser() {
    if (fs.existsSync(browserPID)) { const pid = Number(fs.readFileSync(browserPID)); assert.ok(Number.isInteger(pid) && pid > 1); if (!groups.includes(pid)) groups.push(pid); }
  }
  const watchdog = setTimeout(() => {
    report.pass = false; report.error = 'absolute workbench deadline exceeded'; discoverBrowser();
    for (const pid of groups) signalGroup(pid, 'SIGKILL');
    report.cleanup = { errors: ['deadline cleanup unverified'], process_groups_gone: false, ports_closed: false };
    report.completed_at = Date.now(); writeReport(); process.exit(1);
  }, 480000);
  async function until(label, predicate, limit = 45000) {
    const end = Math.min(deadline - 12000, Date.now() + limit);
    while (Date.now() < end) { const value = await predicate(); if (value) return value; await pause(80); }
    throw new Error('Timed out: ' + label);
  }
  async function api(action, payload) { return request(worker.origin, '/api/workbench', { body: { action, ...payload }, headers: { 'X-CassQuery-Workbench': token }, timeoutMs: 30000 }); }
  async function inspect(name) { const value = await api('inspect', { project: name }); assert.equal(value.status, 200); return value.json; }
  function part(value, name = 'Base') { const p = value.parts.find((p) => p.name === name); assert.ok(p); const g = { volume: p.volume, solids: p.solids, min: p.bbox.slice(0, 3), max: p.bbox.slice(3) }; geometry(g); return g; }
  async function certified(name) {
    return until(name + ' current certificate', async () => {
      const status = await request(worker.origin, '/api/status?project=' + encodeURIComponent(name));
      if (status.json.state !== 'certified') return false;
      const value = await inspect(name); return !value.stale && value.certified_hash ? value : false;
    }, 60000);
  }
  async function shown(name, hash) {
    await page.waitForFunction(({ name, hash }) => window.__cq?.model?.index.project === name && window.__cq.model.tier === 'certified' && (!hash || window.__cq.model.hash === hash), { name, hash }, { timeout: 45000 });
  }
  async function selectProject(name) { await page.locator('#projects button').filter({ hasText: name }).click(); await shown(name); }
  const editor = () => page.locator('#source-editor');
  async function setText(text) { await editor().focus(); await editor().press('Control+a'); await page.keyboard.insertText(text); assert.equal(await editor().inputValue(), text); }
  async function save() {
    const start = wireTrace.length; await editor().press('Control+s');
    return until('actual save HTTP response', () => wireTrace.slice(start).find((r) => r.action === 'save' && r.status));
  }
  async function capture(description, name = description, expected = null) {
    const started_at=Date.now(),deadline_at=started_at+15000;
    const attempts=[];report.capture_attempts ||= [];
    const journal={method:'bounded serial physical raster readiness',description,started_at,deadline_at,attempts,status:'pending'};report.capture_attempts.push(journal);
    let cdp,record,error;
    async function withinCapture(work, deadline=deadline_at) {
      assert.ok(Date.now()<deadline,'Physical capture action exceeded its 15-second deadline');let timer;
      try { const result=await Promise.race([Promise.resolve().then(work),new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error('Physical capture action exceeded its 15-second deadline')),Math.max(1,deadline-Date.now()));})]);
        assert.ok(Date.now()<deadline,'Physical capture action exceeded its 15-second deadline');return result;
      } finally {clearTimeout(timer);}
    }
    try {
      const { settled } = captureScripts();
      cdp=await withinCapture(()=>page.context().newCDPSession(page));
      const selected=await captureStableRaster({readiness_started_at:started_at,deadline_at:deadline_at-1000,
        observe:async()=>{const layout=await page.evaluate(settled);validateCaptureIdentity(layout,layout,expected);
          assert.deepEqual(layout.viewport,report.viewport);assert.equal(layout.horizontal_overflow,false);return layout;},
        take:async(layout)=>{const shot=await cdp.send('Page.captureScreenshot',{format:'png',captureBeyondViewport:true,fromSurface:true,
          clip:{x:0,y:0,width:layout.full_size[0],height:layout.full_size[1],scale:1}});return Buffer.from(shot.data,'base64');},
        retain:async(bytes,index)=>{
          const file=`${name}-raster-${index}.png`;fs.writeFileSync(path.join(output,file),bytes);
          assert.ok(fs.readFileSync(path.join(output,file)).equals(bytes),'Retained physical raster differs from captured bytes');
          return {path:file,bytes:bytes.length,sha256:digest(bytes),width:bytes.length>=24?bytes.readUInt32BE(16):null,height:bytes.length>=24?bytes.readUInt32BE(20):null};
        },
      },attempts);
      await withinCapture(()=>validateRasterAttempts(attempts,output),deadline_at-500);
      record={...selected.image,path:name+'.png',description,...selected.before,canvas_pixels:selected.pixels,
        raster_sha256:selected.raster_sha256,raster_attempts:attempts};
      await withinCapture(()=>validateRendering(record,record.canvas_pixels),deadline_at-500);
    }catch(failure){error=failure;journal.status='failed';journal.error=failure.message;}
    if(cdp){try{await withinCapture(()=>cdp.detach());}catch(cleanup){error ||= cleanup;journal.status='failed';journal.cleanup_error=cleanup.message;}}
    if(!error)journal.status='stable';journal.completed_at=Date.now();
    try {await withinCapture(()=>{const bytes=Buffer.from(JSON.stringify(report.capture_attempts,null,2)+'\n');
      fs.writeFileSync(path.join(output,'capture-attempts.json'),bytes);assert.ok(fs.readFileSync(path.join(output,'capture-attempts.json')).equals(bytes));});}
    catch(failure){error ||= failure;journal.status='failed';journal.journal_error=failure.message;}
    if(error)throw error;
    try {
      await withinCapture(()=>{const bytes=fs.readFileSync(path.join(output,attempts.at(-1).image.path));
        fs.writeFileSync(path.join(output,record.path),bytes);assert.ok(fs.readFileSync(path.join(output,record.path)).equals(bytes));});
      journal.completed_at=Date.now();
      await withinCapture(()=>{const bytes=Buffer.from(JSON.stringify(report.capture_attempts,null,2)+'\n');
        fs.writeFileSync(path.join(output,'capture-attempts.json'),bytes);assert.ok(fs.readFileSync(path.join(output,'capture-attempts.json')).equals(bytes));});
    }catch(failure){journal.status='failed';journal.error=failure.message;throw failure;}
    report.screenshots.push(record);
  }
  function retain(file, name) { const bytes = fs.readFileSync(file); fs.writeFileSync(path.join(output, name), bytes); report.files.push({ path: name, bytes: bytes.length, sha256: digest(bytes) }); return digest(bytes); }
  async function scenario(id, work) { currentScenario = { id, pass: false, checks: [] }; report.scenarios.push(currentScenario); await work(); assert.deepEqual(currentScenario.checks.map((c) => c.id), REQUIRED_CHECKS[id]); currentScenario.pass = true; }
  function check(id, evidence) { validateEvidence(id, evidence); currentScenario.checks.push({ id, pass: true, evidence }); }
  async function traceControls(label) {
    const observed = await page.evaluate(() => {
      const workbench = window.__workbench;
      const manifest = [...(workbench?.documents.values() || [])].find((doc) => doc.project === workbench.project && doc.root === workbench.root && doc.path === 'viewer.json');
      const fields = Object.fromEntries(['translation-x', 'translation-y', 'translation-z', 'rotation-x', 'rotation-y', 'rotation-z', 'uniform-scale'].map((id) => {
        const input = document.getElementById(id); return [id, { value: input.value, disabled: input.disabled, valid: input.checkValidity() }];
      }));
      const pending = workbench?.pendingTransform;
      return { fields, pending: pending ? { project: pending.project, part: pending.part, expected: pending.expected, translation: pending.translation, rotation: pending.rotation, scale: pending.scale } : null,
        source_dirty: workbench?.sourceDirty, dirty: workbench?.dirty, inspector_revision: workbench?.inspectorRevision,
        inspection_manifest_sha: workbench?.inspection?.manifest_sha256 || null,
        manifest_draft: manifest ? { model_derived: !!manifest.modelDerived, dirty: manifest.dirty, revision: manifest.revision, base_sha256: manifest.baseSha } : null,
        model: { hash: window.__cq.model?.hash, built_at: window.__cq.model?.index.built_at } };
    });
    report.control_trace ||= []; report.control_trace.push({ label, at_ms: Date.now(), ...observed });
  }
  const picker = [];
  function choose(kind, value) { assert.ok(value.startsWith(temporary + path.sep)); picker.push({ kind, value }); }
  function take(kind) { const choice = picker.shift(); assert.ok(choice && choice.kind === kind, 'unexpected simulated chooser'); return choice.value; }
  try {
    assert.equal(process.platform, 'linux', 'Workbench suite is Linux source/browser evidence only');
    before = sourceSnapshot(root, python); report.source.before_fingerprint = before.fingerprint;
    report.source.source_sha = before.source_sha; report.source.files = before.files;
    assert.ok(before.files.length <= 10000 && before.files.reduce((n, f) => n + f.bytes, 0) <= 128 * 1024 ** 2, 'source copy exceeds reviewed bound');
    fs.mkdirSync(source);
    for (const record of before.files) {
      if (record.path.startsWith('payloads/')) continue; // No committed mesh can stand in for an actual build.
      const destination = path.join(source, record.path); fs.mkdirSync(path.dirname(destination), { recursive: true }); fs.copyFileSync(path.join(root, record.path), destination);
      assert.equal(digest(fs.readFileSync(destination)), record.sha256, 'Source mutated during workbench copy');
    }
    for (const [file, key] of [['worker.py', 'worker_sha256'], ['viewer/app.js', 'viewer_sha256'], ['tools/workbench_suite.cjs', 'suite_sha256']]) report.source[key] = digest(fs.readFileSync(path.join(source, file)));
    const pythonClosure = ['worker.py', 'workbench.py', 'cad_io.py'].map((name) => ({ path: name, bytes: fs.statSync(path.join(source, name)).size, sha256: digest(fs.readFileSync(path.join(source, name))) }));
    report.source.python_sources = pythonClosure;
    const baselinePath = process.env.CASSQUERY_BASELINE_REPORT;
    assert.ok(baselinePath, 'Set CASSQUERY_BASELINE_REPORT to the exact-source existing full integration result; do not duplicate its actor');
    const old = JSON.parse(fs.readFileSync(baselinePath)); validateBaseline(old, path.dirname(baselinePath));
    assert.equal(old.source.worker_sha256, report.source.worker_sha256); assert.equal(old.source.viewer_sha256, report.source.viewer_sha256);
    for (const record of pythonClosure) assert.deepEqual(old.source.python_sources.find((p) => p.path === record.path), record, 'legacy Python source closure is stale');
    const legacy = path.join(output, 'baseline'); fs.mkdirSync(legacy);
    for (const image of old.screenshots) fs.copyFileSync(path.join(path.dirname(baselinePath), image.file), path.join(legacy, image.file));
    fs.copyFileSync(baselinePath, path.join(legacy, 'integration-results.json'));
    report.baseline = { path: 'baseline/integration-results.json', sha256: digest(fs.readFileSync(baselinePath)) };
    const projectRoot = path.join(temporary, 'projects/WORKBENCHPLATE'); fs.mkdirSync(path.dirname(projectRoot)); fs.cpSync(path.join(source, 'testpart'), projectRoot, { recursive: true });
    const manifest = JSON.parse(fs.readFileSync(path.join(projectRoot, 'viewer.json'))); manifest.name = 'WORKBENCHPLATE'; fs.writeFileSync(path.join(projectRoot, 'viewer.json'), JSON.stringify(manifest));
    const counter = path.join(temporary, 'build-calls'); fs.writeFileSync(counter, '0');
    const entry = path.join(projectRoot, 'part_entry.py'); let code = fs.readFileSync(entry, 'utf8');
    code = code.replace('def build():', `def build():\n    from pathlib import Path as _Path\n    _count = _Path(${JSON.stringify(counter)})\n    _count.write_text(str(int(_count.read_text()) + 1))`); fs.writeFileSync(entry, code);
    const plainRoot = path.join(temporary, 'plain'); fs.mkdirSync(plainRoot);
    const plainFile = path.join(plainRoot, 'PLAINWORKBENCH.py'); fs.writeFileSync(plainFile, "import cadquery as cq\ndef build():\n    return {'names':['Block'],'parts':{'Block':cq.Workplane('XY').box(12,14,16).val()}}\n");
    const originalPlain = retain(plainFile, 'plain-original.py'), registry = path.join(temporary, 'registry.json');
    const log = fs.openSync(path.join(output, 'worker.log'), 'w');
    worker = spawn(python, ['-u', path.join(source, 'worker.py'), '--port', '42000', '--registry-file', registry], {
      cwd: source, detached: true, stdio: ['ignore', log, log], env: { ...process.env, CASSQUERY_WORKBENCH_TOKEN: token, CASSQUERY_PROJECTS: '', CASSQUERY_DATA_DIR: data, PYTHONDONTWRITEBYTECODE: '1', PYTHONNOUSERSITE: '1', HOME: temporary } }); fs.closeSync(log);
    worker.on('error', (error) => { worker.startError = error; });
    if (Number.isInteger(worker.pid) && worker.pid > 0) groups.push(worker.pid);
    const ready = await until('owned worker registry', () => { if (worker.startError) throw worker.startError; if (worker.exitCode !== null) throw new Error('worker exited before readiness'); return fs.existsSync(registry) ? JSON.parse(fs.readFileSync(registry)) : false; });
    assert.equal(ready.pid, worker.pid); worker.port = ready.port; worker.origin = 'http://127.0.0.1:' + ready.port;
    ports.push(ready.port, ready.port + 1000); report.worker_pid = worker.pid;
    traffic = recordWorkbenchTraffic(worker.port, wireTrace);
    const { chromium } = require(process.env.CASSQUERY_PLAYWRIGHT || 'playwright-core');
    assert.ok(process.env.CASSQUERY_BROWSER && fs.existsSync(process.env.CASSQUERY_BROWSER), 'Set CASSQUERY_BROWSER to an installed sandbox-capable Linux browser');
    const launcher = path.join(temporary, 'browser-launcher'); fs.writeFileSync(launcher, '#!/bin/sh\nexec setsid sh -c \'printf "%s" "$$" > "$CASSQUERY_BROWSER_PID_FILE"; exec "$CASSQUERY_BROWSER" "$@"\' sh "$@"\n', { mode: 0o700 });
    browser = await chromium.launch({ executablePath: launcher, headless: true, chromiumSandbox: true, env: { ...process.env, CASSQUERY_BROWSER_PID_FILE: browserPID }, args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader'], timeout: 30000 }); discoverBrowser();
    const context = await browser.newContext({ viewport: { width: 1600, height: 1000 }, reducedMotion: 'reduce' }); page = await context.newPage(); page.setDefaultTimeout(15000);
    page.on('pageerror', (error) => report.browser_errors.push(error.message)); page.on('dialog', (dialog) => dialog.accept());
    const frames = []; page.on('websocket', (ws) => ws.on('framereceived', ({ payload }) => { if (frames.length < 1000) frames.push(JSON.parse(String(payload))); }));
    const { createDesktop } = require(path.join(source, 'shell/desktop.cjs'));
    const { createUpdater } = require(path.join(source, 'shell/updater.cjs'));
    updater = await createUpdater({ version: JSON.parse(fs.readFileSync(path.join(source, 'package.json'))).version, arch: 'x64',
      appPath: path.join(temporary, 'not-an-installed-application.app'), userData: path.join(temporary, 'desktop-state'), autoCheck: false, autoDownload: false,
      canInstall: async () => false, requestQuit: async () => false });
    await updater.setPreferences({ autoCheck: false, autoDownload: false });
    const handlers = new Map(), frame = { url: worker.origin }, win = { isDestroyed: () => false, webContents: { mainFrame: frame, getURL: () => worker.origin, send: (_name, message) => { page.evaluate((message) => window.dispatchEvent(new CustomEvent('cassquery-source-command', { detail: message })), message).catch((error) => report.browser_errors.push(error.message)); } } };
    const desktop = createDesktop({ ipcMain: { handle: (name, handler) => handlers.set(name, handler) }, getWindow: () => win, getPort: () => worker.port, token, userData: path.join(temporary, 'desktop-state'), dialog: {
      showOpenDialog: async (_win, options) => ({ canceled: false, filePaths: [take(options.properties.includes('openFile') ? 'file' : 'folder')] }),
      showSaveDialog: async () => ({ canceled: false, filePath: take('save') }),
      showMessageBox: async (_win, options) => ({ response: options.buttons.includes('Trust and Open') || options.buttons.includes('Create project sidecars') ? 0 : 2 }),
    }, update: async (action) => {
      assert.equal(action, 'state', 'This source UI gate does not authorize network/update/install actions'); return updater.getState();
    } }); desktop.register(); desktop.stores.patchSettings({ autoBuild: true, reducedMotion: 'on' });
    await page.exposeBinding('__cassquerySourceBridge', async (source, channel, ...args) => {
      assert.equal(source.frame, page.mainFrame()); assert.equal(new URL(source.frame.url()).origin, worker.origin);
      const handler = handlers.get(channel); assert.ok(handler); const value = await handler({ sender: win.webContents, senderFrame: frame }, ...args);
      bridgeTrace.push({ channel, action: args[0], result: value }); return value;
    });
    await page.addInitScript(() => {
      window.cassquery = Object.freeze({
        workbench: (action, payload = {}) => window.__cassquerySourceBridge('cassquery:workbench', action, payload),
        settings: (action, patch) => window.__cassquerySourceBridge('cassquery:settings', action, patch),
        recovery: (action, record) => window.__cassquerySourceBridge('cassquery:recovery', action, record),
        setDirty: (value) => window.__cassquerySourceBridge('cassquery:dirty', value),
        update: (action) => window.__cassquerySourceBridge('cassquery:update', action),
        onCommand: (callback) => { const listener = (event) => callback(event.detail); window.addEventListener('cassquery-source-command', listener); return () => window.removeEventListener('cassquery-source-command', listener); },
      });
    });
    await page.goto(worker.origin); await shown('TESTPLATE');
    let edited, beforeGeometry, afterGeometry;
    await scenario('source-edit', async () => {
      const no = await request(worker.origin, '/api/workbench', { body: { action: 'list', project: 'TESTPLATE' } });
      const bad = await request(worker.origin, '/api/workbench', { body: { action: 'list', project: 'TESTPLATE' }, headers: { 'X-CassQuery-Workbench': '0'.repeat(64) } });
      const status = await request(worker.origin, '/api/status'); check('capability-boundary', { without_capability: no.status, wrong_capability: bad.status, status_contains_token: JSON.stringify(status.json).includes(token) });
      choose('folder', projectRoot); const start = bridgeTrace.length; await page.locator('#openfolder').click();
      const opened = await until('folder open response', () => bridgeTrace.slice(start).find((r) => r.action === 'openFolder' && !r.result.error));
      await certified('WORKBENCHPLATE'); await shown('WORKBENCHPLATE'); await until('real folder source editor', async () => await editor().inputValue() === code);
      choose('file', plainFile); const fileStart = bridgeTrace.length; await page.locator('#openfile').click();
      const fileOpen = await until('plain Python open response', () => bridgeTrace.slice(fileStart).find((r) => r.action === 'openFile' && !r.result.error));
      await certified('PLAINWORKBENCH'); await shown('PLAINWORKBENCH');
      const plainText = fs.readFileSync(plainFile, 'utf8'); await until('selected plain source', async () => await editor().inputValue() === plainText);
      check('open-folder-file', { folder: opened.result, file: fileOpen.result, original_file_before: originalPlain, original_file_after: digest(fs.readFileSync(plainFile)), editor_follows_selected_file: true });
      await selectProject('WORKBENCHPLATE'); await until('folder source returns', async () => await editor().inputValue() === code);
      beforeGeometry = part(await certified('WORKBENCHPLATE')); const beforeHash = retain(entry, 'before-edit.py');
      edited = code.replace('.box(40, 30, 6,', '.box(42, 30, 6,'); assert.notEqual(edited, code); await setText(edited); const saveRecord = await save(); assert.equal(saveRecord.status, 200);
      const fresh = await certified('WORKBENCHPLATE'); await shown('WORKBENCHPLATE', fresh.certified_hash); afterGeometry = part(fresh);
      const gates = (await request(worker.origin, '/api/gates?project=WORKBENCHPLATE')).json;
      check('edit-save-volume', { before_source: beforeHash, after_source: retain(entry, 'after-edit.py'), before: beforeGeometry, after: afterGeometry,
        save_status: saveRecord.status, renderer_tier: await page.evaluate(() => window.__cq.model.tier), gate_ids: gates.checks.map((g) => g.id), gates_pass: gates.pass });
      await capture('edit-certified');
      const draft = edited.replace('.box(42, 30, 6,', '.box(43, 30, 6,'); await setText(draft);
      const external = edited + '\n# external public-fixture change\n'; fs.writeFileSync(entry, external);
      fs.writeFileSync(path.join(output, 'conflict-draft.py'), draft); report.files.push({ path: 'conflict-draft.py', bytes: Buffer.byteLength(draft), sha256: digest(Buffer.from(draft)) });
      retain(entry, 'external-source.py'); const failedSave = await save();
      await until('conflict visible', () => page.locator('#conflict-banner').isVisible());
      check('external-conflict', { status: failedSave.status, draft_before: digest(Buffer.from(draft)), draft_after: digest(Buffer.from(await editor().inputValue())),
        external_source: digest(Buffer.from(external)), disk_after: digest(fs.readFileSync(entry)), conflict_visible: true });
      const conflictCertificate = await certified('WORKBENCHPLATE'); await shown('WORKBENCHPLATE', conflictCertificate.certified_hash);
      assert.equal(await editor().inputValue(), draft, 'Waiting for external certification must not alter the conflict draft');
      assert.equal(digest(fs.readFileSync(entry)), digest(Buffer.from(external)));
      assert.equal(await page.locator('#conflict-banner').isVisible(), true);
      await capture('external-conflict', 'external-conflict', { project:'WORKBENCHPLATE',hash:conflictCertificate.certified_hash,built_at:conflictCertificate.built_at });
      await page.locator('#conflict-reload').click();
      await page.locator('#draft-guard').waitFor({ state: 'visible' }); await page.locator('#draft-guard button[value="discard"]').click();
      await until('reload external bytes', async () => await editor().inputValue() === external);
      edited = external; await certified('WORKBENCHPLATE');
      const certBefore = (await inspect('WORKBENCHPLATE')).certified_hash, frameStart = frames.length;
      await setText('def build(:\n'); assert.equal((await save()).status, 200);
      await until('syntax error status', async () => (await request(worker.origin, '/api/status?project=WORKBENCHPLATE')).json.state === 'error');
      await page.waitForFunction(() => document.getElementById('diag').textContent.includes('SyntaxError'));
      check('syntax-retains-certified', { certified_before: certBefore, certified_after: await page.evaluate(() => window.__cq.model.hash),
        certified_announcements: frames.slice(frameStart).filter((m) => m.project === 'WORKBENCHPLATE' && m.type === 'announce' && m.tier === 'certified').length,
        diagnostic: await page.locator('#diag').textContent(), renderer_tier: await page.evaluate(() => window.__cq.model.tier) }); await capture('syntax-retained');
      await setText(edited); await save(); await certified('WORKBENCHPLATE'); await shown('WORKBENCHPLATE');
      const recoverText = edited + '\n# recover this unsaved public draft\n', diskBefore = digest(fs.readFileSync(entry)); await setText(recoverText);
      await until('production persisted draft', () => desktop.stores.getRecovery().drafts.some((d) => d.project === 'WORKBENCHPLATE' && d.text === recoverText));
      retain(path.join(temporary, 'desktop-state/workbench-recovery.json'), 'recovery.json'); await page.reload(); await selectProject('WORKBENCHPLATE');
      await page.locator('#recovery-restore').click();
      await until('draft recovered after explicit renderer restore', async () => await editor().inputValue() === recoverText);
      check('draft-recovery', { stored_draft: digest(Buffer.from(recoverText)), restored_draft: digest(Buffer.from(await editor().inputValue())), disk_before: diskBefore, disk_after: digest(fs.readFileSync(entry)), production_store: true });
      await setText(edited); await until('draft no longer dirty', () => !desktop.isDirty());
    });
    await scenario('cad-roundtrip', async () => {
      const manifestPath = path.join(projectRoot, 'viewer.json'), manifestBefore = digest(fs.readFileSync(manifestPath));
      await page.locator('#part-tree button[data-part="Base"]').click(); await traceControls('part selected');
      await page.waitForFunction(() => {
        const workbench = window.__workbench, model = window.__cq?.model;
        return workbench?.inspection?.stale === false && workbench.inspection.certified_hash === model?.hash &&
          ['translation-x', 'translation-y', 'translation-z', 'rotation-z', 'uniform-scale'].every(id => !document.getElementById(id).disabled);
      });
      await traceControls('inputs actionable');
      for (const [selector, value] of [['#translation-x', 3], ['#translation-y', -2], ['#translation-z', 5], ['#rotation-z', 90], ['#uniform-scale', 1.5]]) {
        await page.locator(selector).click();
        assert.equal(await page.locator(selector).evaluate((element) => document.activeElement === element), true, 'Inspector input was not the actual focused control');
        await page.locator(selector).press('Control+a'); await page.keyboard.insertText(String(value)); await traceControls(selector + ' input');
        assert.equal(await page.locator(selector).inputValue(), String(value), 'Inspector input did not receive the keyboard edit');
        await page.locator(selector).press('Tab'); await traceControls(selector + ' blurred');
      }
      await traceControls('before certificate observation');
      const previousCertificate = await certified('WORKBENCHPLATE'), transformStart = wireTrace.length;
      await traceControls('after certificate observation');
      await page.locator('#transform-apply').click();
      const transformResponse = await until('actual transform HTTP response', () => wireTrace.slice(transformStart).find((record) => record.action === 'transform' && record.status && record.response));
      assert.equal(transformResponse.status, 200);
      const committedSha = transformResponse.response.manifest_sha256 || transformResponse.response.sha256; requireHash(committedSha);
      await until('actual transform manifest committed', () => {
        const value = JSON.parse(fs.readFileSync(manifestPath));
        return value.workbench?.transforms?.Base && digest(fs.readFileSync(manifestPath)) === committedSha && committedSha !== manifestBefore;
      });
      const transformed = await until('fresh transformed certificate', async () => {
        const value = await certified('WORKBENCHPLATE'); return value.built_at > previousCertificate.built_at && value.certified_hash !== previousCertificate.certified_hash
          && value.manifest_sha256 === committedSha ? value : false;
      });
      await shown('WORKBENCHPLATE', transformed.certified_hash);
      const transform = { translation: [3, -2, 5], rotation: [0, 0, 90], scale: 1.5 };
      check('source-transform', { transform, persisted: JSON.parse(fs.readFileSync(manifestPath)).workbench.transforms.Base,
        manifest_before: manifestBefore, manifest_after: retain(manifestPath, 'transformed-manifest.json'), before: afterGeometry, after: part(transformed), renderer_tier: await page.evaluate(() => window.__cq.model.tier) });
      const dirty = edited + '\n# export must not bypass this draft\n'; await setText(dirty); await until('dirty broker state', () => desktop.isDirty());
      const wireStart = wireTrace.length;
      if (!await page.locator('#exportbtn').isDisabled()) {
        await page.locator('#exportbtn').click();
        if (await page.locator('#export-confirm').isVisible() && !await page.locator('#export-confirm').isDisabled()) await page.locator('#export-confirm').click();
      }
      await pause(150);
      const dirtyRequests = wireTrace.slice(wireStart).filter((r) => r.action === 'export').length; assert.equal(dirtyRequests, 0);
      assert.equal(await editor().inputValue(), dirty, 'dirty export lost the draft');
      await page.keyboard.press('Escape'); await setText(edited); await until('clean export state', () => !desktop.isDirty());
      // The chooser is simulated; production desktop broker, worker BREP and filesystem are real.
      const exported = path.join(temporary, 'transformed.step'), callsBefore = Number(fs.readFileSync(counter));
      choose('save', exported); const exportStart = wireTrace.length;
      await page.locator('#part-tree button[data-part="Base"]').click();
      await page.waitForFunction(() => window.__cq.selected === 'Base' && window.__cq.model.activeName === 'neutral' &&
        document.querySelector('#part-tree button[data-part="Base"]').getAttribute('aria-pressed') === 'true');
      await page.locator('#exportbtn').click(); await page.locator('#export-format').selectOption('step');
      await page.locator('#export-variant').selectOption('neutral'); await page.locator('#export-selected').check(); await page.locator('#export-confirm').click();
      const result = await until('actual export HTTP response', () => wireTrace.slice(exportStart).find((r) => r.action === 'export' && r.status)); assert.equal(result.status, 200);
      const callsAfter = Number(fs.readFileSync(counter)), measuredExport = measureStep(exported, python), exportHash = retain(exported, 'transformed.step');
      const staleText = edited + '\n# unsaved-on-disk stale export negative\n'; fs.writeFileSync(entry, staleText);
      const stale = await api('export', { project: 'WORKBENCHPLATE', path: path.join(temporary, 'stale.step'), format: 'step', parts: ['Base'] }); assert.equal(stale.status, 409);
      assert.equal(fs.existsSync(path.join(temporary, 'stale.step')), false); fs.writeFileSync(entry, edited); await certified('WORKBENCHPLATE');
      const imported = await api('import', { path: exported, directory: path.join(temporary, 'projects'), name: 'ROUNDTRIPPLATE' }); assert.equal(imported.status, 200);
      await certified('ROUNDTRIPPLATE'); await selectProject('ROUNDTRIPPLATE');
      const importedExport = path.join(temporary, 'roundtrip.step'); const reexport = await api('export', { project: 'ROUNDTRIPPLATE', path: importedExport, format: 'step' }); assert.equal(reexport.status, 200);
      const measuredImport = measureStep(importedExport, python); retain(importedExport, 'roundtrip.step');
      check('step-export-import', { exported: measuredExport, imported: measuredImport, export_sha256: exportHash,
        original_export_after_import: digest(fs.readFileSync(exported)), build_calls_before_export: callsBefore, build_calls_after_export: callsAfter,
        dirty_export_requests: dirtyRequests, stale_export_rejected: stale.status === 409 }); await capture('step-roundtrip');
    });
    await scenario('workbench-ui', async () => {
      const states = [], ids = ['counterform-dark', 'counterform-light', 'graphite', 'midnight', 'forest', 'sand', 'paper', 'high-contrast'];
      for (let i = 0; i < ids.length; i++) {
        await page.locator('#settingsbtn').click(); await page.locator('#theme-select').selectOption(ids[i]);
        await until('persisted theme', () => desktop.stores.getSettings().theme === ids[i]); await page.keyboard.press('Escape');
        await page.waitForFunction((id) => document.documentElement.dataset.theme === id, ids[i]);
        const styling = await page.evaluate(() => { const s = getComputedStyle(document.body); return [s.backgroundColor, s.color, getComputedStyle(document.documentElement).getPropertyValue('--panel')].join('|'); });
        await capture(THEMES[i], 'theme-' + ids[i]); states.push({ name: THEMES[i], styling, persisted: true, visible: true, image_sha256: report.screenshots.at(-1).sha256 });
      }
      check('eight-themes', { states });
      await page.locator('#settingsbtn').click(); await page.locator('#editor-font-size').fill('22'); await page.locator('#editor-font-size').press('Tab'); await page.locator('#tab-size').selectOption('2');
      await until('font and tabs persisted', () => desktop.stores.getSettings().editorFontSize === 22 && desktop.stores.getSettings().tabSize === 2); await page.keyboard.press('Escape');
      const oldText = await editor().inputValue(); await editor().press('Control+Home'); await editor().press('Tab'); const indented = await editor().inputValue();
      await editor().press('Control+z'); assert.equal(await editor().inputValue(), oldText);
      const font = await editor().evaluate((e) => parseFloat(getComputedStyle(e).fontSize)); await page.reload(); await selectProject('ROUNDTRIPPLATE');
      check('font-tabs', { font_size: desktop.stores.getSettings().editorFontSize, rendered_font_size: font, tab_size: desktop.stores.getSettings().tabSize,
        inserted_spaces: indented.length - oldText.length, persistence_after_reload: await editor().evaluate((e) => parseFloat(getComputedStyle(e).fontSize)) === 22 });
      const direction = () => page.evaluate(() => window.__cq.camera.position.clone().sub(window.__cq.controls.target).normalize().toArray());
      const originalText = await editor().inputValue(), beforeDirection = await direction(); await editor().press('Control+End'); await page.keyboard.insertText('\n# focus guard: '); await editor().press('1');
      const gotDigit = (await editor().inputValue()).endsWith('1'), afterDirection = await direction();
      await editor().press('Control+z'); await editor().press('Control+z'); const undoRestored = await editor().inputValue() === originalText;
      await editor().press('Control+f'); const find = await page.locator('#find-query').isVisible(); await page.keyboard.press('Escape');
      await page.locator('#stage canvas').click(); await page.keyboard.press('1'); await pause(300); const outsideDirection = await direction();
      const rebuilt = page.waitForResponse((r) => r.url().endsWith('/api/rebuild') && r.request().method() === 'POST'); await page.keyboard.press('Control+b'); const rebuild = await rebuilt;
      check('shortcut-focus', { text_input_received_digit: gotDigit, focused_editor_camera_unchanged: beforeDirection.every((v, i) => Math.abs(v - afterDirection[i]) < 1e-6),
        outside_axis_camera_changed: afterDirection.some((v, i) => Math.abs(v - outsideDirection[i]) > 0.01), rebuild_status: rebuild.status(), find_visible: find, undo_restored_text: undoRestored });
      retain(path.join(temporary, 'desktop-state/workbench-settings.json'), 'settings.json');
    });
    assert.deepEqual(report.browser_errors, []); assert.equal(picker.length, 0); report.bridge_actions = bridgeTrace.map((r) => ({ channel: r.channel, action: r.action, error: r.result?.error || null })); report.worker_requests = wireTrace;
  } catch (error) {
    report.error = error.stack || String(error);
    if (page && !page.isClosed()) {
      try { await traceControls('failure'); } catch { /* The primary error stays authoritative if the page is unavailable. */ }
      try { await capture('failure'); } catch (captureError) { report.failure_capture_error = captureError.message; }
    }
  }
  finally {
    report.worker_requests = wireTrace;
    report.bridge_actions = bridgeTrace.map((r) => ({ channel: r.channel, action: r.action, error: r.result?.error || null }));
    if (traffic) traffic.stop(); if (updater) updater.dispose();
    discoverBrowser();
    try { if (browser) await browser.close(); } catch (error) { report.cleanup.errors.push(error.message); }
    for (const pid of groups) {
      try { if (groupExists(pid)) { signalGroup(pid, 'SIGTERM'); const end = Date.now() + 4000; while (groupExists(pid) && Date.now() < end) await pause(50); if (groupExists(pid)) signalGroup(pid, 'SIGKILL'); } }
      catch (error) { report.cleanup.errors.push(error.message); }
    }
    for (let i = 0; i < 50 && groups.some(groupExists); i++) await pause(50);
    report.cleanup.process_groups_gone = groups.every((pid) => !groupExists(pid)); report.cleanup.ports_closed = (await Promise.all(ports.map(listening))).every((open) => !open);
    try { const after = sourceSnapshot(root, python); report.source.after_fingerprint = after.fingerprint; }
    catch (error) { report.cleanup.errors.push(error.message); }
    report.completed_at = Date.now(); report.pass = !report.error && report.cleanup.errors.length === 0 && report.cleanup.process_groups_gone && report.cleanup.ports_closed;
    if (report.pass) { try { validateReport(report, output); verifyStepEvidence(report, output, python); } catch (error) { report.pass = false; report.error = error.stack || String(error); } }
    writeReport(); fs.rmSync(temporary, { recursive: true, force: true }); clearTimeout(watchdog);
  }
  console.log(JSON.stringify({ pass: report.pass, report: reportFile, error: report.error })); if (!report.pass) process.exitCode = 1; return report;
}
module.exports = { THEMES, REQUIRED_CHECKS, validateRasterIdentity, validateRasterAttempts, validateRenderGeometry, captureStableRaster, rasterIdentity, analyzeCanvasRaster, validateCaptureIdentity, captureScripts, canvasPixels, validateRendering, validateEvidence, validateReport, verifyStepEvidence, request, recordWorkbenchTraffic, sourceSnapshot, measureStep, run };
if (require.main === module) {
  const args = process.argv.slice(2);
  if (args.length === 2 && args[0] === '--validate-report') {
    try { const report = JSON.parse(fs.readFileSync(args[1])), directory = path.dirname(path.resolve(args[1])); validateReport(report, directory); verifyStepEvidence(report, directory); }
    catch (error) { console.error(error.message); process.exitCode = 1; }
  } else if (!args.length) run().catch((error) => { console.error(error.message); process.exitCode = 1; });
  else { console.error('Usage: node tools/workbench_suite.cjs [--validate-report FILE]'); process.exitCode = 2; }
}
