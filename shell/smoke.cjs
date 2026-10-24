'use strict';
// Opt-in assurance uses native input, production state and the complete rendered frame.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { performance } = require('node:perf_hooks');

const WIDTHS = [450, 700, 1000, 1320];
const THEMES = ['dark', 'light'];
const STATES = ['assembled', 'apart'];
const CHECKS = ['certified demo', 'gate pass', 'assembled step', 'exploded step', 'reset view',
  'save updates rendered model', 'camera preserved on rebuild',
  'failed gate restores certified model', 'no renderer diagnostics'];
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const checkpoint = (name) => ({ name, wall_time_ms: Date.now(), monotonic_ms: Number(process.hrtime.bigint()) / 1e6 });

async function waitFor(win, expression, timeoutMs = 60000) {
  const deadline = performance.now() + timeoutMs;
  while (performance.now() < deadline) {
    if (win.isDestroyed()) throw new Error('Window closed during smoke test');
    if (await win.webContents.executeJavaScript(expression)) return;
    await delay(100);
  }
  throw new Error(`Renderer timed out: ${expression}`);
}

async function click(win, selector) {
  const point = await win.webContents.executeJavaScript(`(() => {
    const e = document.querySelector(${JSON.stringify(selector)});
    if (!e || e.disabled) throw new Error('Missing/disabled control: ' + ${JSON.stringify(selector)});
    e.scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'instant' });
    const r = e.getBoundingClientRect();
    const x = r.left + r.width / 2, y = r.top + r.height / 2;
    if (!r.width || !r.height || x < 0 || y < 0 || x >= innerWidth || y >= innerHeight ||
        !e.contains(document.elementFromPoint(x, y))) throw new Error('Unreachable control: ' + ${JSON.stringify(selector)});
    return {x: Math.round(x), y: Math.round(y)};
  })()`);
  win.webContents.sendInputEvent({ type: 'mouseMove', ...point });
  win.webContents.sendInputEvent({ type: 'mouseDown', button: 'left', clickCount: 1, ...point });
  win.webContents.sendInputEvent({ type: 'mouseUp', button: 'left', clickCount: 1, ...point });
}

// Serialized into the actual renderer. No DOM/layout mocks, bounding-sphere shortcuts,
// hidden-ancestor vertices, or empty geometry can count as projection coverage.
function measureFrame() {
  const bad = [], overlaps = [];
  const rect = (e) => e.getBoundingClientRect();
  const visible = (e) => {
    const r = rect(e), s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const elements = [...document.querySelectorAll('header, main, aside, nav, button, input, label, h1, .badge, .caption, #chips li, #diag, #buildline, #selection')].filter(visible);
  const scrollRegions = [];
  const canScroll = (e) => ['auto', 'scroll'].includes(getComputedStyle(e).overflowY);
  for (const e of elements) {
    const r = rect(e), style = getComputedStyle(e);
    if (canScroll(e) && e.scrollHeight > e.clientHeight + 2)
      scrollRegions.push({ id: e.id || e.tagName, client_height: e.clientHeight, scroll_height: e.scrollHeight });
    let scrollParent = null;
    for (let p = e.parentElement; p && p !== document.body; p = p.parentElement)
      if (canScroll(p) && p.scrollHeight > p.clientHeight + 2) { scrollParent = p; break; }
    const documentScroll = document.documentElement.scrollHeight > innerHeight && canScroll(document.body);
    const unreachableY = !scrollParent && !documentScroll && (r.top < -1 || r.bottom > innerHeight + 1);
    if (r.left < -1 || r.right > innerWidth + 1 || unreachableY || e.scrollWidth > e.clientWidth + 2 ||
        (e.scrollHeight > e.clientHeight + 2 && ['hidden', 'clip'].includes(style.overflowY)))
      bad.push({ id: e.id || e.tagName, category: 'clipped', text: e.textContent.slice(0, 120) });
    for (const node of e.childNodes) {
      if (node.nodeType !== Node.TEXT_NODE || !node.textContent.trim()) continue;
      const range = document.createRange(); range.selectNodeContents(node);
      for (const t of range.getClientRects()) if (t.left < r.left - 2 || t.right > r.right + 2 ||
          (['hidden', 'clip'].includes(style.overflowY) && (t.top < r.top - 2 || t.bottom > r.bottom + 2)))
        bad.push({ id: e.id || e.tagName, category: 'text-clipped', text: node.textContent.slice(0, 120) });
    }
  }
  for (const parent of document.querySelectorAll('header, main, aside, nav, .navrow, .tools, .identity, .state, #selection')) {
    const children = [...parent.children].filter(visible);
    for (let i = 0; i < children.length; i++) for (let j = i + 1; j < children.length; j++) {
      const a = rect(children[i]), b = rect(children[j]);
      if (Math.min(a.right, b.right) - Math.max(a.left, b.left) > 2 &&
          Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 2)
        overlaps.push([children[i].id || children[i].tagName, children[j].id || children[j].tagName]);
    }
  }
  const { model, camera } = window.__cq;
  model.group.updateMatrixWorld(true); camera.updateMatrixWorld(true);
  let vertices = 0, meshes = 0, clipped = 0, nonfinite = 0;
  const v = camera.position.clone();
  model.group.traverseVisible((o) => {
    if (!o.isMesh) return;
    meshes++;
    const positions = o.geometry.getAttribute('position');
    if (!positions || !positions.count) { nonfinite++; return; }
    for (let i = 0; i < positions.count; i++) {
      v.fromBufferAttribute(positions, i).applyMatrix4(o.matrixWorld).project(camera); vertices++;
      if (![v.x, v.y, v.z].every(Number.isFinite)) nonfinite++;
      else if (Math.abs(v.x) > 1.001 || Math.abs(v.y) > 1.001 || Math.abs(v.z) > 1.001) clipped++;
    }
  });
  const canvas = document.querySelector('#stage canvas'), c = rect(canvas);
  return { viewport: [innerWidth, innerHeight], dpr: devicePixelRatio,
    theme: matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light',
    background: getComputedStyle(document.body).backgroundColor,
    full_size: [innerWidth, Math.max(innerHeight, document.documentElement.scrollHeight)],
    vertical_scroll: document.documentElement.scrollHeight > innerHeight, scroll_regions: scrollRegions,
    overflow: document.documentElement.scrollWidth > innerWidth ||
      (document.documentElement.scrollHeight > innerHeight && !canScroll(document.body)),
    bad, overlaps, vertices, meshes, clipped, nonfinite,
    canvas: { width: canvas.width, height: canvas.height, client_width: c.width, client_height: c.height },
    diag: document.getElementById('diag').textContent,
    badge: document.getElementById('tierbadge').textContent,
    step_pressed: [...document.querySelectorAll('#steps button')].map((b) => b.getAttribute('aria-pressed')),
    explode: document.getElementById('explodeval').textContent,
    tier: model.tier, gates_pass: model.gates?.pass === true, hash: model.hash };
}

function validateMatrix(records) {
  assert.equal(records.length, THEMES.length * WIDTHS.length * STATES.length, 'incomplete native matrix');
  const seen = new Set();
  for (const r of records) {
    const key = `${r.theme}/${r.width}/${r.state}`;
    assert.ok(THEMES.includes(r.theme) && WIDTHS.includes(r.width) && STATES.includes(r.state) && !seen.has(key), 'invalid/duplicate matrix cell');
    seen.add(key);
    assert.equal(r.status, 'checked', 'skipped/failed matrix cell');
    assert.deepEqual(r.viewport, [r.width, 700], 'native size was not realized');
    assert.ok(Array.isArray(r.full_size) && r.full_size.length === 2 && r.full_size[0] === r.width && r.full_size[1] >= 700, 'missing full-document dimensions');
    assert.ok(Array.isArray(r.scroll_regions) && typeof r.vertical_scroll === 'boolean', 'missing scroll layout coverage');
    assert.equal(r.realized_theme, r.theme, 'native theme was not realized');
    assert.ok(Number.isInteger(r.vertices) && r.vertices > 0 && Number.isInteger(r.meshes) && r.meshes > 0
      && r.canvas?.width > 0 && r.canvas?.height > 0, 'zero rendered coverage');
    assert.ok(r.canvas.client_width > 0 && r.canvas.client_height > 0);
    assert.ok(Number.isInteger(r.controls_checked) && r.controls_checked > 0, 'zero layout coverage');
    assert.equal(r.clipped, 0); assert.equal(r.nonfinite, 0); assert.equal(r.overflow, false);
    assert.deepEqual(r.bad, []); assert.deepEqual(r.overlaps, []); assert.deepEqual(r.errors, []);
    assert.equal(r.diag, ''); assert.equal(r.tier, 'certified'); assert.equal(r.gates_pass, true);
    assert.deepEqual(r.step_pressed, r.state === 'assembled' ? ['true', 'false'] : ['false', 'true']);
    assert.equal(r.explode, r.state === 'assembled' ? '0%' : '100%');
    assert.ok(r.image && /^[a-f0-9]{64}$/.test(r.image.sha256) && r.image.width >= r.width && r.image.height >= 700);
    if (r.scroll_regions?.length) assert.ok(r.controls_image && /^[a-f0-9]{64}$/.test(r.controls_image.sha256)
      && r.controls_image.width >= r.width && r.controls_image.height >= 700, 'missing scrolled controls capture');
    if (r.full_size) assert.ok(r.image.width >= r.full_size[0] && r.image.height >= r.full_size[1], 'capture does not cover complete document');
  }
  for (const width of WIDTHS) {
    const dark = records.find((r) => r.theme === 'dark' && r.width === width);
    const light = records.find((r) => r.theme === 'light' && r.width === width);
    assert.notEqual(dark.background, light.background, 'theme did not change rendered styling');
  }
}

async function capture(win, file) {
  const debuggerAPI = win.webContents.debugger;
  assert.equal(debuggerAPI.isAttached(), false, 'native capture debugger already owned');
  debuggerAPI.attach('1.3');
  let bytes;
  try {
    const dimensions = await win.webContents.executeJavaScript('({width:innerWidth,height:Math.max(innerHeight,document.documentElement.scrollHeight)})');
    const result = await debuggerAPI.sendCommand('Page.captureScreenshot', { format: 'png', fromSurface: true,
      captureBeyondViewport: true, clip: { x: 0, y: 0, ...dimensions, scale: 1 } });
    bytes = Buffer.from(result.data, 'base64');
  } finally { debuggerAPI.detach(); }
  assert.ok(bytes.length > 1000 && bytes.subarray(0, 8).equals(Buffer.from('89504e470d0a1a0a', 'hex')), 'empty/invalid native capture');
  const width = bytes.readUInt32BE(16), height = bytes.readUInt32BE(20);
  assert.ok(width > 0 && height > 0, 'zero native capture');
  fs.writeFileSync(file, bytes);
  return { path: file, sha256: crypto.createHash('sha256').update(bytes).digest('hex'), width, height };
}

async function matrix(win, directory, nativeTheme, errors, records = []) {
  const previousTheme = nativeTheme.themeSource;
  try {
    for (const theme of THEMES) {
      nativeTheme.themeSource = theme;
      await new Promise((resolve, reject) => {
        const contents = win.webContents;
        const cleanup = () => { clearTimeout(timer); contents.removeListener('did-finish-load', loaded); contents.removeListener('did-fail-load', failed); };
        const loaded = () => { cleanup(); resolve(); };
        const failed = (_e, code, description) => { cleanup(); reject(new Error(`${code}: ${description}`)); };
        const timer = setTimeout(() => { cleanup(); reject(new Error('Native theme reload timed out')); }, 30000);
        contents.once('did-finish-load', loaded); contents.once('did-fail-load', failed);
        contents.reload();
      });
      await waitFor(win, 'window.__cq?.model?.tier === "certified" && window.__cq.model.gates?.pass');
      for (const width of WIDTHS) {
        win.setContentSize(width, 700);
        await delay(200);
        assert.deepEqual(win.getContentSize(), [width, 700], `display cannot realize ${width}x700`);
        await waitFor(win, `innerWidth === ${width} && innerHeight === 700`, 5000);
        for (const [index, state] of STATES.entries()) {
          await click(win, `#steps button:nth-child(${index + 1})`);
          await waitFor(win, `document.getElementById('explodeval').textContent === '${index ? '100%' : '0%'}'`);
          await delay(700);
          await win.webContents.executeJavaScript('scrollTo(0,0); document.querySelector("aside").scrollTop=0');
          await delay(100);
          const measured = await win.webContents.executeJavaScript(`(${measureFrame.toString()})()`);
          const controls = await win.webContents.executeJavaScript('document.querySelectorAll("button, input, label").length');
          const image = await capture(win, path.join(directory, `${theme}-${width}-${state}.png`));
          const row = { ...measured, realized_theme: measured.theme, theme, width, state, status: 'checked',
            controls_checked: controls, errors: [...errors], image };
          if (measured.scroll_regions.length) {
            assert.ok(measured.scroll_regions.every((r) => r.id === 'ASIDE' && r.scroll_height <= r.client_height * 2), 'scroll content exceeds complete two-frame coverage');
            await win.webContents.executeJavaScript('document.querySelector("aside").scrollTop=document.querySelector("aside").scrollHeight');
            await delay(100);
            row.controls_image = await capture(win, path.join(directory, `${theme}-${width}-${state}-controls.png`));
            await win.webContents.executeJavaScript('document.querySelector("aside").scrollTop=0');
          }
          records.push(row);
        }
      }
    }
    validateMatrix(records);
    return records;
  } finally { nativeTheme.themeSource = previousTheme; }
}

async function productionControls(win) {
  await click(win, '#steps button:first-child'); await delay(700);
  await click(win, '#next'); await waitFor(win, 'document.getElementById("explodeval").textContent === "100%"');
  await click(win, '#prev'); await waitFor(win, 'document.getElementById("explodeval").textContent === "0%"');
  await click(win, '#xraybtn');
  await waitFor(win, 'document.getElementById("xraybtn").getAttribute("aria-pressed") === "true"');
  assert.ok(await win.webContents.executeJavaScript(`(() => { let n=0; __cq.model.group.traverseVisible(o=>{if(o.isMesh && o.material.transparent && o.material.opacity===0.2)n++;}); return n>0; })()`), 'xray did not affect rendered materials');
  await click(win, '#xraybtn');
  await click(win, '#explode');
  win.webContents.sendInputEvent({ type: 'keyDown', keyCode: 'End' });
  win.webContents.sendInputEvent({ type: 'keyUp', keyCode: 'End' });
  await waitFor(win, 'document.getElementById("explodeval").textContent === "100%"');
  await click(win, '#steps button:first-child'); await delay(700);
  // Pick a real triangle centroid with native pointer events, then exercise selection controls.
  const points = await win.webContents.executeJavaScript(`(() => {
    const {model,camera}=__cq, r=document.querySelector('#stage canvas').getBoundingClientRect(), out=[];
    model.group.updateMatrixWorld(true); camera.updateMatrixWorld(true);
    model.group.traverseVisible(o=>{if(!o.isMesh)return; const p=o.geometry.getAttribute('position'), ix=o.geometry.index;
      for(let i=0;i<Math.min(ix?ix.count:p.count,60);i+=3){const v=camera.position.clone().set(0,0,0);
        for(let j=0;j<3;j++)v.add(camera.position.clone().fromBufferAttribute(p,ix?ix.getX(i+j):i+j));
        v.multiplyScalar(1/3).applyMatrix4(o.matrixWorld).project(camera);
        if(Math.abs(v.x)<1&&Math.abs(v.y)<1)out.push({x:Math.round(r.left+(v.x+1)*r.width/2),y:Math.round(r.top+(1-v.y)*r.height/2)});
      }}); return out; })()`);
  let picked = false;
  for (const point of points) {
    win.webContents.sendInputEvent({ type: 'mouseDown', button: 'left', clickCount: 1, ...point });
    win.webContents.sendInputEvent({ type: 'mouseUp', button: 'left', clickCount: 1, ...point });
    await delay(30);
    if (await win.webContents.executeJavaScript('!!document.getElementById("selname").textContent')) { picked = true; break; }
  }
  assert.ok(picked, 'native mesh pick did not select a part');
  await click(win, '#selisolate');
  assert.ok(await win.webContents.executeJavaScript(`(() => {let hidden=0; __cq.model.group.traverse(o=>{if(o.isMesh&&!o.visible)hidden++;});return hidden>0;})()`));
  await click(win, '#selhide');
  assert.ok(await win.webContents.executeJavaScript(`(() => {let visible=0;__cq.model.group.traverseVisible(o=>{if(o.isMesh)visible++;});return visible===0;})()`));
  await click(win, '#selshow');
  assert.ok(await win.webContents.executeJavaScript(`(() => {let n=0;__cq.model.group.traverseVisible(o=>{if(o.isMesh)n++;});return n>1;})()`));
  await click(win, '#resetview'); await delay(700);
  const gen = await win.webContents.executeJavaScript('__cq.model.index.gen');
  await click(win, '#buildbtn');
  await waitFor(win, `__cq.model?.tier === 'certified' && __cq.model.index.gen > ${gen}`);
  await waitFor(win, 'document.getElementById("diag").textContent === ""');
  return ['next/previous', 'xray rendered material', 'range keyboard input', 'mesh pointer selection',
    'isolate/hide/show all', 'reset view', 'rebuild certified button'];
}

async function smoke(win, directory, { nativeTheme, errors = [], evidence = {} }) {
  fs.mkdirSync(directory, { recursive: true });
  const js = (code) => win.webContents.executeJavaScript(code);
  const report = { schema: 1, pass: false, checkpoints: [checkpoint('renderer-start')], matrix: [], images: [], checks: [] };
  Object.assign(evidence, report);
  await waitFor(win, '!!(window.__cq?.model?.tier === "certified" && window.__cq.model.gates?.pass && window.__cq.model.activeName)');
  report.initial = await js('({hash:__cq.model.hash, tier:__cq.model.tier, variants:[...__cq.model.variants.keys()], badge:document.getElementById("tierbadge").textContent})');
  report.worker_status = await js('fetch("/api/status",{cache:"no-store"}).then(r=>r.json())');
  assert.equal(report.worker_status.api_version, 2); assert.deepEqual(report.worker_status.load_errors, []);
  assert.equal(report.worker_status.projects.length, 1); assert.equal(report.worker_status.projects[0].name, 'TESTPLATE');
  report.checkpoints.push(checkpoint('certified-ready'));
  report.control_checks = await productionControls(win);
  report.checkpoints.push(checkpoint('production-controls'));
  await matrix(win, directory, nativeTheme, errors, report.matrix);
  report.images = report.matrix.flatMap((r) => r.controls_image ? [r.image, r.controls_image] : [r.image]);
  report.checkpoints.push(checkpoint('native-matrix'));
  report.screenshots = [];
  for (const state of STATES) {
    const source = report.matrix.find((r) => r.theme === 'light' && r.width === 1320 && r.state === state).image.path;
    const file = path.join(directory, `${state}.png`); fs.copyFileSync(source, file);
    report.screenshots.push(file);
    report.images.push({ ...report.matrix.find((r) => r.image.path === source).image, path: file });
  }
  assert.deepEqual(errors, []); assert.equal(await js('document.getElementById("diag").textContent'), '');
  const project = process.env.CASSQUERY_PROJECTS;
  if (project) {
    assert.ok(!project.includes(path.delimiter), 'smoke requires one scratch demo project');
    await click(win, '#resetview'); await delay(700);
    const beforeDirection = await js('__cq.camera.position.clone().sub(__cq.controls.target).normalize().toArray()');
    const entry = path.join(project, 'part_entry.py'), beforeHash = await js('__cq.model.hash');
    const source = fs.readFileSync(entry, 'utf8'); assert.ok(source.includes('.circle(4)'), 'save mutation would be a no-op');
    fs.writeFileSync(entry, source.replace('.circle(4)', '.circle(5)'));
    await waitFor(win, `__cq.model?.tier === 'certified' && __cq.model.hash !== ${JSON.stringify(beforeHash)}`);
    const direction = await js('__cq.camera.position.clone().sub(__cq.controls.target).normalize().toArray()');
    assert.ok(beforeDirection.reduce((sum, v, i) => sum + v * direction[i], 0) > 0.9999, 'rebuild stole camera direction');
    report.checkpoints.push(checkpoint('save-certified'));
    report.save_state = await js(`(${measureFrame.toString()})()`);
    assert.ok(report.save_state.vertices > 0 && report.save_state.clipped === 0 && report.save_state.nonfinite === 0);
    assert.equal(report.save_state.diag, '');
    report.images.push(await capture(win, path.join(directory, 'save-certified.png')));
    const goodHash = await js('__cq.model.hash'), configPath = path.join(project, 'viewer.json');
    const config = JSON.parse(fs.readFileSync(configPath, 'utf8')); config.unit_scale = 2;
    fs.writeFileSync(configPath, JSON.stringify(config));
    await waitFor(win, `document.getElementById('tierbadge').textContent.includes('gate FAILED') && __cq.model?.hash === ${JSON.stringify(goodHash)} && __cq.model.tier === 'certified'`);
    await js('document.querySelector("aside").scrollTop=document.querySelector("aside").scrollHeight');
    report.failure_state = await js(`(${measureFrame.toString()})()`);
    assert.ok(report.failure_state.vertices > 0 && report.failure_state.clipped === 0);
    report.images.push(await capture(win, path.join(directory, 'gate-failed.png')));
    report.checkpoints.push(checkpoint('failed-gate-retained'));
    report.checks = [...CHECKS];
  } else {
    report.checks = ['bundled default certified demo', 'gate pass', 'production controls', 'native rendered matrix', 'no renderer diagnostics'];
  }
  assert.deepEqual(errors, [], 'renderer diagnostics after mutation');
  report.checkpoints.push(checkpoint('renderer-complete')); report.pass = true;
  Object.assign(evidence, report);
  return report;
}
module.exports = { smoke, waitFor, checkpoint, click, measureFrame, validateMatrix, WIDTHS, THEMES, STATES, CHECKS };
