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

async function bounded(operation, timeoutMs, label) {
  let timer;
  const deadline = performance.now() + timeoutMs;
  try {
    const result = await Promise.race([Promise.resolve().then(operation), new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(`${label} timed out`)), Math.max(1, timeoutMs));
    })]);
    if (performance.now() > deadline) throw new Error(`${label} exceeded its deadline`);
    return result;
  } finally { clearTimeout(timer); }
}
const evaluate = (win, expression, timeoutMs = 10000) =>
  bounded(() => win.webContents.executeJavaScript(expression), timeoutMs, 'Renderer execution');

async function closeWindow(win, timeoutMs = 15000) {
  if (win.isDestroyed()) throw new Error('Lifecycle window already destroyed');
  let closed;
  try {
    await bounded(() => new Promise((resolve) => {
      closed = resolve;
      win.once('closed', closed);
      win.close();
    }), timeoutMs, 'Native window close');
    assert.equal(win.isDestroyed(), true, 'closed event without destroyed window');
  } finally { if (closed) win.removeListener('closed', closed); }
}

async function waitFor(win, expression, timeoutMs = 60000) {
  const deadline = performance.now() + timeoutMs;
  while (performance.now() < deadline) {
    if (win.isDestroyed()) throw new Error('Window closed during smoke test');
    if (win.cassqueryRendererErrors?.length) throw new Error(`Renderer diagnostics: ${win.cassqueryRendererErrors.join('; ')}`);
    if (await evaluate(win, expression, deadline - performance.now())) return;
    await delay(Math.min(100, Math.max(0, deadline - performance.now())));
  }
  throw new Error(`Renderer timed out: ${expression}`);
}

// Native resize and scroll completion are observed, not assumed from viewport dimensions.
function rendererLayout() {
  const stage = document.getElementById('stage'), canvas = stage?.querySelector('canvas');
  const s = stage?.getBoundingClientRect(), c = canvas?.getBoundingClientRect(), ratio = Math.min(devicePixelRatio, 2);
  const ready = !!(s && c && stage.clientWidth > 0 && stage.clientHeight > 0 &&
    Math.abs(c.width - stage.clientWidth) < 1 && Math.abs(c.height - stage.clientHeight) < 1 &&
    Math.abs(c.left - s.left) < 1 && Math.abs(c.top - s.top) < 1 &&
    canvas.width === Math.floor(stage.clientWidth * ratio) && canvas.height === Math.floor(stage.clientHeight * ratio) &&
    Math.abs(window.__cq?.camera.aspect - stage.clientWidth / stage.clientHeight) < 1e-9);
  return { ready, viewport: [innerWidth, innerHeight], stage: s ? [s.left, s.top, s.width, s.height] : null,
    canvas: c ? [c.left, c.top, c.width, c.height, canvas.width, canvas.height] : null };
}
async function waitRendererLayout(win, timeoutMs = 5000) {
  const deadline = performance.now() + timeoutMs;
  let last;
  while (performance.now() < deadline) {
    if (win.isDestroyed()) throw new Error('Window closed while awaiting renderer layout');
    if (win.cassqueryRendererErrors?.length) throw new Error(`Renderer diagnostics: ${win.cassqueryRendererErrors.join('; ')}`);
    try { last = await evaluate(win, `(${rendererLayout.toString()})()`, deadline - performance.now()); }
    catch (error) { error.message += `; last renderer layout: ${JSON.stringify(last)}`; throw error; }
    if (last.ready) return;
    await delay(Math.min(100, Math.max(0, deadline - performance.now())));
  }
  throw new Error(`Renderer layout timed out: ${JSON.stringify(last)}`);
}
async function click(win, selector, timeoutMs = 5000) {
  const deadline = performance.now() + timeoutMs;
  let point;
  while (performance.now() < deadline) {
    if (win.isDestroyed()) throw new Error('Window closed while awaiting reachable control');
    if (win.cassqueryRendererErrors?.length) throw new Error(`Renderer diagnostics: ${win.cassqueryRendererErrors.join('; ')}`);
    point = await evaluate(win, `(() => {
      const e = document.querySelector(${JSON.stringify(selector)});
      if (!e || e.disabled) return { ready:false, reason:'missing/disabled' };
      e.scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'instant' });
      const r = e.getBoundingClientRect();
      const x = Math.round(r.left + r.width / 2), y = Math.round(r.top + r.height / 2), top = document.elementFromPoint(x,y);
      return {x,y,ready:!!(r.width && r.height && x>=0 && y>=0 && x<innerWidth && y<innerHeight && e.contains(top)),
        rect:[r.left,r.top,r.width,r.height],viewport:[innerWidth,innerHeight],top:top?.tagName || null};
    })()`, deadline - performance.now());
    if (point.ready) break;
    await delay(Math.min(100, Math.max(0, deadline - performance.now())));
  }
  if (!point?.ready) throw new Error(`Unreachable control: ${selector}: ${JSON.stringify(point)}`);
  const position = {x:point.x,y:point.y};
  win.webContents.sendInputEvent({ type: 'mouseMove', ...position });
  win.webContents.sendInputEvent({ type: 'mouseDown', button: 'left', clickCount: 1, ...position });
  win.webContents.sendInputEvent({ type: 'mouseUp', button: 'left', clickCount: 1, ...position });
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
  let vertices = 0, meshes = 0, triangles = 0, degenerate = 0, clipped = 0, nonfinite = 0;
  const points = Array.from({ length: 3 }, () => camera.position.clone());
  // This is draw eligibility, not pixel/occlusion proof. The captured frame still needs review.
  model.group.traverseVisible((o) => {
    if (!o.isMesh || !o.layers.test(camera.layers)) return;
    const geometry = o.geometry, positions = geometry.getAttribute('position'), index = geometry.index;
    if (!positions || positions.itemSize !== 3 || !Number.isInteger(positions.count) || !positions.count)
      throw new Error('Missing/invalid render positions');
    // shortcut: the viewer uses plain triangle meshes; extend before enabling deformation or wireframe.
    if (o.isInstancedMesh || o.isSkinnedMesh || o.morphTargetInfluences?.some((n) => n !== 0))
      throw new Error('Unsupported deformed/instanced coverage');
    const total = index ? index.count : positions.count, range = geometry.drawRange;
    const validRange = (r) => r && Number.isInteger(r.start) && r.start >= 0 &&
      (r.count === Infinity || (Number.isInteger(r.count) && r.count >= 0));
    if (!Number.isInteger(total) || !validRange(range)) throw new Error('Invalid render draw range');
    const draws = Array.isArray(o.material) ? geometry.groups.map((g) => ({ group: g, material: o.material[g.materialIndex] }))
      : [{ group: null, material: o.material }];
    let meshTriangles = 0;
    for (const { group, material } of draws) {
      if (!material || !material.visible || !material.colorWrite || (material.transparent && material.opacity === 0)) continue;
      if (material.wireframe) throw new Error('Unsupported wireframe coverage');
      if (group && !validRange(group)) throw new Error('Invalid render material group');
      const start = Math.max(0, range.start, group?.start ?? 0);
      const end = Math.min(total, range.start + range.count, group ? group.start + group.count : total);
      for (let i = start; i + 2 < end; i += 3) {
        const ids = [i, i + 1, i + 2].map((n) => index ? index.getX(n) : n);
        if (ids.some((n) => !Number.isInteger(n) || n < 0 || n >= positions.count)) throw new Error('Draw index outside positions');
        points.forEach((p, n) => p.fromBufferAttribute(positions, ids[n]).applyMatrix4(o.matrixWorld));
        if (points.some((p) => ![p.x, p.y, p.z].every(Number.isFinite))) { nonfinite++; continue; }
        const [a, b, c] = points, ab = b.clone().sub(a), ac = c.clone().sub(a);
        const scale = Math.max(...ab.toArray().map(Math.abs), ...ac.toArray().map(Math.abs));
        if (!scale || !Number.isFinite(scale) || !ab.divideScalar(scale).cross(ac.divideScalar(scale)).lengthSq()) { degenerate++; continue; }
        points.forEach((p) => p.project(camera));
        if (points.some((p) => ![p.x, p.y, p.z].every(Number.isFinite))) { nonfinite++; continue; }
        const area = (b.x - a.x) * (c.y - a.y) - (b.y - a.y) * (c.x - a.x);
        if (!area) continue; // Edge-on triangles cover no pixels, without an absolute-size epsilon.
        // WebGL front-face winding is reversed for reflected model transforms (Three r186).
        const front = (o.matrixWorld.determinant() < 0 ? -area : area) > 0;
        if ((material.side === 0 && !front) || (material.side === 1 && front)) continue;
        meshTriangles++; triangles++; vertices += 3;
        for (const p of points) if (Math.abs(p.x) > 1.001 || Math.abs(p.y) > 1.001 || Math.abs(p.z) > 1.001) clipped++;
      }
    }
    if (meshTriangles) meshes++;
  });
  const canvas = document.querySelector('#stage canvas'), c = rect(canvas);
  return { viewport: [innerWidth, innerHeight], dpr: devicePixelRatio,
    theme: matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light',
    background: getComputedStyle(document.body).backgroundColor,
    full_size: [innerWidth, Math.max(innerHeight, document.documentElement.scrollHeight)],
    vertical_scroll: document.documentElement.scrollHeight > innerHeight, scroll_regions: scrollRegions,
    overflow: document.documentElement.scrollWidth > innerWidth ||
      (document.documentElement.scrollHeight > innerHeight && !canScroll(document.body)),
    bad, overlaps, vertices, meshes, triangles, degenerate, clipped, nonfinite,
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
    assert.ok(Number.isInteger(r.triangles) && r.triangles > 0 && r.vertices === r.triangles * 3, 'zero/inconsistent drawn triangle coverage');
    assert.equal(r.degenerate, 0, 'degenerate eligible triangles');
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

// Missing/undecodable visible assets cannot be hidden by successful geometry measurements.
function decodeVisibleImages() {
  const images = [...document.images].filter((image) => {
    const r = image.getBoundingClientRect();
    for (let node = image; node; node = node.parentElement) {
      const s = getComputedStyle(node);
      if (s.visibility === 'hidden' || s.display === 'none') return false;
    }
    return r.width > 0 && r.height > 0;
  });
  return Promise.all(images.map(async (image) => {
    if (image.complete && (!image.naturalWidth || !image.naturalHeight))
      throw new Error(`Image has no decoded pixels: ${image.currentSrc || image.src}`);
    try { await image.decode(); }
    catch (error) { throw new Error(`Image decode failed: ${image.currentSrc || image.src}: ${error.message}`); }
    if (!image.complete || image.naturalWidth <= 0 || image.naturalHeight <= 0)
      throw new Error(`Image has no decoded pixels: ${image.currentSrc || image.src}`);
  }));
}
async function capture(win, file, timeoutMs = 15000) {
  const deadline = performance.now() + timeoutMs;
  const debuggerAPI = win.webContents.debugger;
  assert.equal(debuggerAPI.isAttached(), false, 'native capture debugger already owned');
  debuggerAPI.attach('1.3');
  let bytes;
  try {
    await evaluate(win, `(${decodeVisibleImages.toString()})()`, deadline - performance.now());
    const dimensions = await evaluate(win, '({width:innerWidth,height:Math.max(innerHeight,document.documentElement.scrollHeight)})', Math.min(10000, deadline - performance.now()));
    const result = await bounded(() => debuggerAPI.sendCommand('Page.captureScreenshot', { format: 'png', fromSurface: true,
      captureBeyondViewport: true, clip: { x: 0, y: 0, ...dimensions, scale: 1 } }), deadline - performance.now(), 'Native screenshot capture');
    bytes = Buffer.from(result.data, 'base64');
  } finally { debuggerAPI.detach(); }
  assert.ok(bytes.length > 1000 && bytes.subarray(0, 8).equals(Buffer.from('89504e470d0a1a0a', 'hex')), 'empty/invalid native capture');
  const width = bytes.readUInt32BE(16), height = bytes.readUInt32BE(20);
  assert.ok(width > 0 && height > 0, 'zero native capture');
  if (performance.now() > deadline) throw new Error('Native screenshot capture exceeded its deadline before file commit');
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
        await waitRendererLayout(win);
        for (const [index, state] of STATES.entries()) {
          await click(win, `#steps button:nth-child(${index + 1})`);
          await waitFor(win, `document.getElementById('explodeval').textContent === '${index ? '100%' : '0%'}'`);
          await delay(700);
          await evaluate(win, 'scrollTo(0,0); document.querySelector("aside").scrollTop=0');
          await delay(100);
          const measured = await evaluate(win, `(${measureFrame.toString()})()`);
          const controls = await evaluate(win, 'document.querySelectorAll("button, input, label").length');
          const image = await capture(win, path.join(directory, `${theme}-${width}-${state}.png`));
          const row = { ...measured, realized_theme: measured.theme, theme, width, state, status: 'checked',
            controls_checked: controls, errors: [...errors], image };
          if (win.cassqueryImages) win.cassqueryImages.push(image);
          records.push(row);
          if (measured.scroll_regions.length) {
            assert.ok(measured.scroll_regions.every((r) => r.id === 'ASIDE' && r.scroll_height <= r.client_height * 2), 'scroll content exceeds complete two-frame coverage');
            await evaluate(win, 'document.querySelector("aside").scrollTop=document.querySelector("aside").scrollHeight');
            await delay(100);
            row.controls_image = await capture(win, path.join(directory, `${theme}-${width}-${state}-controls.png`));
            if (win.cassqueryImages) win.cassqueryImages.push(row.controls_image);
            await evaluate(win, 'document.querySelector("aside").scrollTop=0');
          }

        }
      }
    }
    validateMatrix(records);
    return records;
  } finally { nativeTheme.themeSource = previousTheme; }
}

function certifiedRebuildComplete(model, before) {
  return !!(model && model.tier === 'certified' && model.gates?.pass === true
    && model.index?.project === before.project && model.hash === before.hash
    && Number.isFinite(model.index.built_at) && model.index.built_at > before.built_at);
}

async function cameraSnapshot(win, timeoutMs = 10000) {
  return evaluate(win, `(() => { const {camera,controls,model}=__cq, bounds=model.variants.get(model.activeName).bounds;
    return {direction:camera.position.clone().sub(controls.target).normalize().toArray(),
      target:controls.target.toArray(),center:bounds.center,radius:bounds.radius}; })()`, timeoutMs);
}

async function settledCamera(win, timeoutMs = 10000) {
  const deadline = performance.now() + timeoutMs;
  let previous = await cameraSnapshot(win, timeoutMs), quiet = 0;
  while (performance.now() < deadline) {
    await delay(Math.min(100, Math.max(0, deadline - performance.now())));
    const current = await cameraSnapshot(win, deadline - performance.now());
    const direction = Math.hypot(...current.direction.map((v, i) => v - previous.direction[i]));
    const target = Math.hypot(...current.target.map((v, i) => v - previous.target[i]));
    quiet = direction < 1e-6 && target < current.radius * 1e-6 ? quiet + 1 : 0;
    if (quiet === 3) return current;
    previous = current;
  }
  throw new Error('Camera motion did not settle');
}

async function dragCamera(win, button) {
  const point = await evaluate(win, `(() => { const canvas=document.querySelector('#stage canvas'), r=canvas.getBoundingClientRect();
    window.__cqSmokePointer = null;
    window.__cqSmokeMove = e => { window.__cqSmokePointer={x:e.clientX,y:e.clientY,buttons:e.buttons}; };
    canvas.addEventListener('pointermove',window.__cqSmokeMove);
    return {x:Math.round(r.left+r.width/2),y:Math.round(r.top+r.height/2)}; })()`);
  const modifiers = [button === 'left' ? 'leftButtonDown' : 'rightButtonDown'];
  let error;
  try {
    win.webContents.sendInputEvent({ type: 'mouseMove', ...point });
    win.webContents.sendInputEvent({ type: 'mouseDown', button, clickCount: 1, ...point });
    for (let step = 1; step <= 10; step++) {
      const x = point.x + step * 8, y = point.y + step * 3;
      win.webContents.sendInputEvent({ type: 'mouseMove', x, y, modifiers });
      await waitFor(win, `window.__cqSmokePointer?.x===${x} && window.__cqSmokePointer?.y===${y} && window.__cqSmokePointer?.buttons===${button === 'left' ? 1 : 2}`, 2000);
    }
  } catch (cause) { error = cause; throw cause; }
  finally {
    try {
      if (!win.isDestroyed()) {
        win.webContents.sendInputEvent({ type: 'mouseUp', button, clickCount: 1, x: point.x + 80, y: point.y + 30 });
        await evaluate(win, 'document.querySelector("#stage canvas").removeEventListener("pointermove",window.__cqSmokeMove);delete window.__cqSmokeMove;delete window.__cqSmokePointer');
      }
    } catch (cleanup) { if (error) error.message += `; pointer cleanup: ${cleanup.message}`; else throw cleanup; }
  }
}

function assertResetCamera(state) {
  assert.ok(state.radius > 0 && state.direction.every(Number.isFinite) && state.target.every(Number.isFinite));
  const defaultDirection = [0.95, -1.35, 0.62], length = Math.hypot(...defaultDirection);
  assert.ok(state.direction.reduce((n, v, i) => n + v * defaultDirection[i] / length, 0) > 0.9999, 'Reset retained orbit motion');
  assert.ok(Math.hypot(...state.target.map((v, i) => v - state.center[i])) <= state.radius * 1e-6, 'Reset retained pan motion');
}

async function productionControls(win, directory, evidence) {
  await click(win, '#steps button:first-child'); await delay(700);
  await click(win, '#next'); await waitFor(win, 'document.getElementById("explodeval").textContent === "100%"');
  await click(win, '#prev'); await waitFor(win, 'document.getElementById("explodeval").textContent === "0%"');
  await click(win, '#xraybtn');
  await waitFor(win, 'document.getElementById("xraybtn").getAttribute("aria-pressed") === "true"');
  await waitFor(win, `(() => { const v=__cq.model.variants.get(__cq.model.activeName); return [...v.parts].every(([name,p])=> p.mat.transparent === (name !== 'Cap') && Math.abs(p.mat.opacity - (name === 'Cap' ? 1 : 0.2)) < 1e-6 && p.mat.depthWrite === (name === 'Cap')); })()`);
  await click(win, '#xraybtn');
  await click(win, '#explode');
  win.webContents.sendInputEvent({ type: 'keyDown', keyCode: 'End' });
  win.webContents.sendInputEvent({ type: 'keyUp', keyCode: 'End' });
  await waitFor(win, 'document.getElementById("explodeval").textContent === "100%"');
  await click(win, '#steps button:first-child'); await delay(700);
  // Pick a real triangle centroid with native pointer events, then exercise selection controls.
  const points = await evaluate(win, `(() => {
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
    if (await evaluate(win, '!!document.getElementById("selname").textContent')) { picked = true; break; }
  }
  assert.ok(picked, 'native mesh pick did not select a part');
  const selected = await evaluate(win, 'document.getElementById("selname").textContent');
  await click(win, '#selisolate');
  await waitFor(win, `(() => {const v=__cq.model.variants.get(__cq.model.activeName);return v.parts.size===3 && [...v.parts].every(([name,p])=>p.meshes.length>0 && p.meshes.every(m=>m.visible===(name===${JSON.stringify(selected)})));})()`);
  await click(win, '#selhide');
  await waitFor(win, `(() => {const v=__cq.model.variants.get(__cq.model.activeName);return v.parts.size===3 && [...v.parts.values()].every(p=>p.meshes.length>0 && p.meshes.every(m=>!m.visible));})()`);
  await click(win, '#selshow');
  await waitFor(win, `(() => {const v=__cq.model.variants.get(__cq.model.activeName);return v.parts.size===3 && [...v.parts.values()].every(p=>p.meshes.length>0 && p.meshes.every(m=>m.visible));})()`);
  await click(win, '#resetview'); await delay(700);
  evidence.camera_checks = [];
  for (const [mode, button] of [['orbit', 'left'], ['pan', 'right']]) {
    const row = { mode, status: 'pending', method: 'native pointer drag then Reset; observed at 700ms and 3s' };
    evidence.camera_checks.push(row);
    row.before = await cameraSnapshot(win);
    await dragCamera(win, button);
    row.after_drag = await cameraSnapshot(win);
    if (mode === 'orbit') assert.ok(row.before.direction.reduce((n, v, i) => n + v * row.after_drag.direction[i], 0) < 0.999, 'Orbit input did not move camera');
    else assert.ok(Math.hypot(...row.after_drag.target.map((v, i) => v - row.before.target[i])) > row.before.radius * 0.001, 'Pan input did not move target');
    row.drag_image = await capture(win, path.join(directory, `${mode}-drag.png`)); evidence.images.push(row.drag_image);
    await click(win, '#resetview'); await delay(700);
    row.after_reset_700 = await cameraSnapshot(win); assertResetCamera(row.after_reset_700);
    await delay(2300);
    row.after_reset_settled = await cameraSnapshot(win); assertResetCamera(row.after_reset_settled);
    row.reset_image = await capture(win, path.join(directory, `${mode}-reset.png`)); evidence.images.push(row.reset_image);
    row.status = 'checked';
  }
  const beforeBuild = await evaluate(win, '({project:__cq.model.index.project, hash:__cq.model.hash, built_at:__cq.model.index.built_at})');
  assert.ok(Number.isFinite(beforeBuild.built_at), 'missing original certified build time');
  await click(win, '#buildbtn');
  await waitFor(win, `(${certifiedRebuildComplete.toString()})(window.__cq?.model, ${JSON.stringify(beforeBuild)})`);
  await waitFor(win, 'document.getElementById("diag").textContent === ""');
  return ['next/previous', 'xray rendered material', 'range keyboard input', 'mesh pointer selection',
    'isolate/hide/show all', 'reset view', 'rebuild certified button'];
}

async function smoke(win, directory, { nativeTheme, errors = [], evidence = {} }) {
  fs.mkdirSync(directory, { recursive: true });
  const js = (code) => evaluate(win, code);
  const report = evidence;
  Object.assign(report, { schema: 1, pass: false, checkpoints: [checkpoint('renderer-start')], matrix: [], images: [], checks: [] });
  await waitFor(win, '!!(window.__cq?.model?.tier === "certified" && window.__cq.model.gates?.pass && window.__cq.model.activeName)');
  report.initial = await js('({hash:__cq.model.hash, tier:__cq.model.tier, variants:[...__cq.model.variants.keys()], badge:document.getElementById("tierbadge").textContent})');
  report.worker_status = await js('fetch("/api/status",{cache:"no-store"}).then(r=>r.json())');
  assert.equal(report.worker_status.api_version, 2); assert.deepEqual(report.worker_status.load_errors, []);
  assert.equal(report.worker_status.projects.length, 1); assert.equal(report.worker_status.projects[0].name, 'TESTPLATE');
  report.checkpoints.push(checkpoint('certified-ready'));
  report.control_checks = await productionControls(win, directory, report);
  report.checkpoints.push(checkpoint('production-controls'));
  win.cassqueryImages = report.images;
  await matrix(win, directory, nativeTheme, errors, report.matrix);
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
    report.camera_save = { method: 'native orbit to non-default direction; observe settled camera before saving' };
    report.camera_save.before_drag = await cameraSnapshot(win);
    await dragCamera(win, 'left');
    report.camera_save.before_save = await settledCamera(win);
    assert.ok(report.camera_save.before_drag.direction.reduce((n, v, i) => n + v * report.camera_save.before_save.direction[i], 0) < 0.999, 'Save camera check used unchanged default view');
    report.camera_save.image = await capture(win, path.join(directory, 'save-camera.png')); report.images.push(report.camera_save.image);
    const beforeDirection = report.camera_save.before_save.direction;
    const entry = path.join(project, 'part_entry.py'), beforeHash = await js('__cq.model.hash');
    const source = fs.readFileSync(entry, 'utf8'); assert.ok(source.includes('.circle(4)'), 'save mutation would be a no-op');
    fs.writeFileSync(entry, source.replace('.circle(4)', '.circle(5)'));
    await waitFor(win, `__cq.model?.tier === 'certified' && __cq.model.hash !== ${JSON.stringify(beforeHash)}`);
    const direction = await js('__cq.camera.position.clone().sub(__cq.controls.target).normalize().toArray()');
    report.camera_save.after_save = await cameraSnapshot(win);
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
module.exports = { smoke, waitFor, evaluate, bounded, closeWindow, capture, waitRendererLayout, checkpoint, click, measureFrame, validateMatrix, certifiedRebuildComplete, WIDTHS, THEMES, STATES, CHECKS };
