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
    if (canScroll(e) && e.scrollHeight > e.clientHeight + 2) {
      const box=[r.left+e.clientLeft,r.top+e.clientTop,r.left+e.clientLeft+e.clientWidth,r.top+e.clientTop+e.clientHeight];
      const shown=[Math.max(0,box[0]),Math.max(0,box[1]),Math.min(innerWidth,box[2]),Math.min(innerHeight,box[3])];
      for(let ancestor=e.parentElement;ancestor;ancestor=ancestor.parentElement){
        const a=rect(ancestor),s=getComputedStyle(ancestor);
        if(['auto','scroll','hidden','clip'].includes(s.overflowX)){shown[0]=Math.max(shown[0],a.left+ancestor.clientLeft);shown[2]=Math.min(shown[2],a.left+ancestor.clientLeft+ancestor.clientWidth);}
        if(['auto','scroll','hidden','clip'].includes(s.overflowY)){shown[1]=Math.max(shown[1],a.top+ancestor.clientTop);shown[3]=Math.min(shown[3],a.top+ancestor.clientTop+ancestor.clientHeight);}
      }
      scrollRegions.push({ id: e.id || e.tagName, client_height: e.clientHeight, scroll_height: e.scrollHeight, scroll_top: e.scrollTop, viewport_rect:box, visible_rect:shown });
      if(box.some((value,index)=>!Number.isFinite(value)||Math.abs(value-shown[index])>1))bad.push({ id:e.id || e.tagName, category:'scroll-viewport-clipped', text:'' });
    }
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

const MAX_SCROLL_CAPTURES = 32;
function scrollOffsets(clientHeight, scrollHeight) {
  assert.ok(Number.isSafeInteger(clientHeight) && clientHeight > 0 && Number.isSafeInteger(scrollHeight) && scrollHeight > clientHeight, 'invalid scroll dimensions');
  const step = Math.max(1, Math.floor(clientHeight * 0.9)), bottom = scrollHeight - clientHeight;
  assert.ok(Math.ceil(bottom / step) + 1 <= MAX_SCROLL_CAPTURES, 'scroll coverage exceeds bounded capture limit');
  const offsets = [0];
  while (offsets.at(-1) < bottom) offsets.push(Math.min(bottom, offsets.at(-1) + step));
  return offsets;
}
function validateScrollRegion(region, viewport) {
  assert.ok(Number.isSafeInteger(region.client_height) && region.client_height > 0 && Number.isSafeInteger(region.scroll_height) && region.scroll_height > region.client_height, 'invalid scroll dimensions');
  for(const key of ['viewport_rect','visible_rect'])assert.ok(Array.isArray(region[key])&&region[key].length===4&&region[key].every(Number.isFinite),'missing scroll visibility geometry');
  const box=region.viewport_rect,shown=region.visible_rect;
  assert.ok(box[0]>=-1&&box[1]>=-1&&box[2]<=viewport[0]+1&&box[3]<=viewport[1]+1&&box[2]>box[0]&&box[3]>box[1], 'scroll region is not inside viewport');
  assert.ok(shown[2]>shown[0]&&shown[3]>shown[1]&&shown[0]>=-1&&shown[1]>=-1&&shown[2]<=viewport[0]+1&&shown[3]<=viewport[1]+1,'scroll region has no positive visible area');
  assert.ok(Math.abs(box[3]-box[1]-region.client_height)<=1&&box.every((value,index)=>Math.abs(value-shown[index])<=1), 'scroll region client area is clipped');
}
function validateScrollFrame(frame, row, id, offset) {
  assert.ok(frame && typeof frame === 'object' && !Object.hasOwn(frame,'image') && !Object.hasOwn(frame,'scroll_captures'), 'missing/recursive scroll frame evidence');
  assert.ok(typeof frame.hash==='string'&&/^[a-f0-9]{16}$/.test(frame.hash),'missing certified scroll frame hash');
  assert.ok(Number.isFinite(frame.dpr)&&frame.dpr>0&&frame.dpr<=16,'missing/invalid scroll frame DPR');
  assert.deepEqual(frame.viewport, row.viewport); assert.deepEqual(frame.full_size, row.full_size);
  assert.equal(frame.theme, row.theme); assert.equal(frame.background, row.background);
  assert.equal(frame.hash, row.hash); assert.equal(frame.tier, 'certified'); assert.equal(frame.gates_pass, true);
  assert.deepEqual(frame.step_pressed, row.step_pressed); assert.equal(frame.explode, row.explode);
  assert.ok(Number.isInteger(frame.vertices) && frame.vertices > 0 && Number.isInteger(frame.meshes) && frame.meshes > 0 &&
    Number.isInteger(frame.triangles) && frame.triangles > 0 && frame.vertices === frame.triangles * 3, 'empty/inconsistent scroll frame geometry');
  assert.equal(frame.clipped, 0); assert.equal(frame.nonfinite, 0); assert.equal(frame.degenerate, 0);
  assert.equal(frame.overflow, false); assert.deepEqual(frame.bad, []); assert.deepEqual(frame.overlaps, []); assert.deepEqual(frame.errors, []); assert.equal(frame.diag, '');
  assert.deepEqual(frame.canvas, row.canvas); assert.equal(frame.dpr, row.dpr);
  const expected = row.scroll_regions.find(region => region.id === id);
  assert.ok(expected, 'unknown scroll frame region');
  assert.equal(frame.scroll_regions.length, row.scroll_regions.length, 'scroll frame region inventory changed');
  for (const region of frame.scroll_regions) {
    const original = row.scroll_regions.find(item => item.id === region.id);
    assert.ok(original, 'scroll region changed');
    assert.equal(region.client_height, original.client_height); assert.equal(region.scroll_height, original.scroll_height);
    validateScrollRegion(region,frame.viewport);assert.deepEqual(region.viewport_rect,original.viewport_rect);assert.deepEqual(region.visible_rect,original.visible_rect);
    assert.equal(region.scroll_top, region.id === id ? offset : original.scroll_top, 'scroll offset not realized');
  }
}
function validateScrollCoverage(row) {
  assert.ok(Array.isArray(row.scroll_regions) && row.scroll_regions.length<=1 && Array.isArray(row.scroll_captures) && row.scroll_captures.length<=MAX_SCROLL_CAPTURES && !Object.hasOwn(row,'controls_image'), 'missing/legacy scroll coverage inventory');
  assert.ok(typeof row.hash==='string'&&/^[a-f0-9]{16}$/.test(row.hash),'missing certified matrix hash');
  assert.ok(Number.isFinite(row.dpr)&&row.dpr>0&&row.dpr<=16,'missing/invalid matrix DPR');
  const seen = new Set();
  for (const region of row.scroll_regions) {
    assert.equal(region.id, 'ASIDE', 'unsupported scroll region'); assert.ok(!seen.has(region.id), 'duplicate scroll region'); seen.add(region.id);
    assert.equal(region.scroll_top, 0, 'baseline scroll region was not at the top');validateScrollRegion(region,row.viewport);
    const offsets = scrollOffsets(region.client_height, region.scroll_height);
    const records = row.scroll_captures.filter(record => record.id === region.id);
    assert.equal(records.length, offsets.length, 'incomplete scroll capture coverage');
    let covered = 0;
    for (const [index, record] of records.entries()) {
      assert.equal(record.scroll_top, offsets[index], 'missing/reordered scroll interval');
      assert.equal(record.client_height, region.client_height); assert.equal(record.scroll_height, region.scroll_height);
      assert.ok(record.scroll_top <= covered, 'gap in scroll coverage'); covered = record.scroll_top + record.client_height;
      assert.ok(record.image && /^[a-f0-9]{64}$/.test(record.image.sha256) && Number.isSafeInteger(record.image.width) && Number.isSafeInteger(record.image.height) && record.image.width >= row.full_size[0] && record.image.height >= row.full_size[1], 'missing complete scroll frame image');
      if (index === 0) assert.deepEqual(record.image, row.image, 'scroll top frame does not match baseline image');
      else assert.equal(path.basename(record.image.path), `${row.theme}-${row.width}-${row.state}-controls-${index}.png`, 'scroll image identity mismatch');
      validateScrollFrame(record.frame, row, region.id, record.scroll_top);
    }
    assert.equal(covered, region.scroll_height, 'scroll bottom not covered');
    assert.equal(new Set(records.map(record => record.image.path)).size, records.length, 'duplicate scroll image path');
  }
  assert.equal(row.scroll_captures.length, [...seen].reduce((total,id) => total + row.scroll_captures.filter(record => record.id === id).length, 0), 'foreign scroll capture region');
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
    validateScrollCoverage(r);
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

async function captureScrollRegions(win, directory, row, baselineFrame, errors) {
  // Model-matrix coverage currently owns one ASIDE; unexpected/nested regions fail closed.
  assert.ok(row.scroll_regions.length <= 1 && row.scroll_regions.every(region => region.id === 'ASIDE'), 'unsupported scroll region inventory');
  if (!row.scroll_regions.length) return;
  const region = row.scroll_regions[0], offsets = scrollOffsets(region.client_height, region.scroll_height);
  assert.equal(region.scroll_top, 0, 'baseline scroll position was not at the top');
  let firstError;
  try {
    validateScrollFrame(baselineFrame, row, region.id, 0);
    const initial = { id:region.id, scroll_top:0, client_height:region.client_height, scroll_height:region.scroll_height,
      image:row.image, frame:baselineFrame };
    row.scroll_captures.push(initial);
    const afterTop = { ...await evaluate(win, `(${measureFrame.toString()})()`), errors:[...errors] };
    validateScrollFrame(afterTop, row, region.id, 0);
    for (const [index, offset] of offsets.entries()) {
      if (!index) continue;
      await evaluate(win, `document.querySelector('aside').scrollTop=${offset}`);
      await waitFor(win, `document.querySelector('aside').scrollTop===${offset}`, 5000);
      // Observe a naturally painted frame after the scroll; no renderer resize/control shortcut.
      await evaluate(win, 'new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))', 5000);
      const frame = { ...await evaluate(win, `(${measureFrame.toString()})()`), errors:[...errors] };
      validateScrollFrame(frame, row, region.id, offset); assert.deepEqual(errors, [], 'renderer diagnostics during scroll coverage');
      const image = await capture(win, path.join(directory, `${row.theme}-${row.width}-${row.state}-controls-${index}.png`));
      if (win.cassqueryImages) win.cassqueryImages.push(image);
      const record = { id:region.id, scroll_top:offset, client_height:region.client_height, scroll_height:region.scroll_height, image, frame };
      row.scroll_captures.push(record);
      const after = { ...await evaluate(win, `(${measureFrame.toString()})()`), errors:[...errors] };
      validateScrollFrame(after, row, region.id, offset); assert.deepEqual(errors, [], 'renderer diagnostics after scroll capture');
    }
    validateScrollCoverage(row);
  } catch (error) { firstError = error; throw error; }
  finally {
    try {
      await evaluate(win, `document.querySelector('aside').scrollTop=${region.scroll_top}`);
      await waitFor(win, `document.querySelector('aside').scrollTop===${region.scroll_top}`, 5000);
    } catch (cleanup) { if (firstError) firstError.message += `; scroll restore failed: ${cleanup.message}`; else throw cleanup; }
  }
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
          const row = { ...measured, realized_theme: measured.theme, theme, width, state, status: 'pending',
            controls_checked: controls, errors: [...errors], image, scroll_captures: [] };
          if (win.cassqueryImages) win.cassqueryImages.push(image);
          records.push(row);
          await captureScrollRegions(win, directory, row, {...measured,errors:[...errors]}, errors);
          row.status = 'checked';

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
  assert.ok(button === 'left' || button === 'right', 'Unsupported native camera button');
  const gestureBudget=20000,phaseBudget=2000,deadline=performance.now()+gestureBudget;
  const remaining=()=>{const left=deadline-performance.now();if(left<=0)throw new Error('Native gesture exceeded its deadline');return Math.min(phaseBudget,left);};
  const trace = {button,method:'main-process paced native whole gesture',status:'pending',budget_ms:gestureBudget,intermediate_ack_required:false,moves_sent:0,sent:[],frames:[],observations:[]};
  const point = await evaluate(win, `(() => { const canvas=document.querySelector('#stage canvas'),r=canvas.getBoundingClientRect();
    window.__cqSmokePointer = null;window.__cqSmokeDown=null;window.__cqSmokeUp=null;window.__cqSmokePointerEvents=[];window.__cqSmokePointerDropped=0;
    window.__cqSmokeMove=e=>{window.__cqSmokePointer={x:e.clientX,y:e.clientY,buttons:e.buttons};};
    window.__cqSmokeEvent=e=>{
      const samples=e.type==='pointermove'&&typeof e.getCoalescedEvents==='function'?e.getCoalescedEvents():[];
      for(const sample of [e,...samples]){
        const record={type:e.type,x:sample.clientX,y:sample.clientY,button:sample.button,buttons:sample.buttons,pointer_id:sample.pointerId,
          trusted:sample.isTrusted,target:e.target===canvas?'canvas':e.target?.id||e.target?.tagName,captured:Number.isInteger(sample.pointerId)&&canvas.hasPointerCapture(sample.pointerId),
          stamp:sample.timeStamp,observed_at:performance.now(),coalesced:sample!==e,movement_x:sample.movementX,movement_y:sample.movementY};
        if(window.__cqSmokePointerEvents.length<80)window.__cqSmokePointerEvents.push(record);else window.__cqSmokePointerDropped++;
        if(sample===e&&e.type==='pointerdown'&&e.target===canvas)window.__cqSmokeDown=record;
        if(sample===e&&e.type==='pointerup'&&e.target===canvas)window.__cqSmokeUp=record;
      }
    };
    canvas.addEventListener('pointermove',window.__cqSmokeMove);
    for(const type of ['pointerdown','pointermove','pointerup','pointercancel','gotpointercapture','lostpointercapture','contextmenu'])document.addEventListener(type,window.__cqSmokeEvent,true);
    const x=Math.round(r.left+r.width/2),y=Math.round(r.top+r.height/2);
    return{x,y,rect:[r.left,r.top,r.right,r.bottom],viewport:[innerWidth,innerHeight],scroll:[scrollX,scrollY],hit:document.elementFromPoint(x,y)===canvas}; })()`,remaining());
  trace.start=point;
  const mask=button==='left'?1:2,number=button==='left'?0:2,modifiers=[button==='left'?'leftButtonDown':'rightButtonDown'];
  let error,pressed=false,released=false,last={x:point.x,y:point.y};
  const send=input=>{trace.sent.push(input);win.webContents.sendInputEvent(input);};
  async function observe(phase,timeoutMs=remaining()){const state=await evaluate(win,`(() => {const c=document.querySelector('#stage canvas'),r=c.getBoundingClientRect();return{phase:${JSON.stringify(phase)},pointer:window.__cqSmokePointer,down:window.__cqSmokeDown,up:window.__cqSmokeUp,events:window.__cqSmokePointerEvents,dropped_events:window.__cqSmokePointerDropped,
    rect:[r.left,r.top,r.right,r.bottom],viewport:[innerWidth,innerHeight],scroll:[scrollX,scrollY],document_focused:document.hasFocus(),
    hit:document.elementFromPoint(${last.x},${last.y})===c,capture:window.__cqSmokeDown?c.hasPointerCapture(window.__cqSmokeDown.pointer_id):false};})()`,timeoutMs);
    state.window_focused=typeof win.isFocused==='function'?win.isFocused():null;trace.observations.push(state);return state;}
  try {
    assert.ok(point.hit&&point.x>=0&&point.y>=0&&point.x+80<Math.min(point.rect[2],point.viewport[0])&&point.y+30<Math.min(point.rect[3],point.viewport[1]),'Native camera drag is outside its visible canvas');
    win.focus();await waitFor(win,'document.hasFocus()',remaining());assert.equal(win.isFocused(),true,'Native camera window is not focused');
    const ready=await observe('before native press');assert.ok(ready.hit&&JSON.stringify(ready.rect)===JSON.stringify(point.rect)&&JSON.stringify(ready.viewport)===JSON.stringify(point.viewport),'Native camera canvas moved before press');
    send({type:'mouseMove',button,x:point.x,y:point.y});
    pressed=true;send({type:'mouseDown',button,clickCount:1,modifiers,x:point.x,y:point.y});
    await waitFor(win,`window.__cqSmokeDown?.trusted===true&&window.__cqSmokeDown?.x===${point.x}&&window.__cqSmokeDown?.y===${point.y}&&window.__cqSmokeDown?.button===${number}&&window.__cqSmokeDown?.buttons===${mask}&&document.hasFocus()&&document.querySelector('#stage canvas').hasPointerCapture(window.__cqSmokeDown.pointer_id)`,remaining());
    await observe('pointerdown acknowledged');
    for(let step=1;step<=10;step++){
      // A renderer RAF can itself wait on pending continuous input; native release must not depend on it.
      await delay(Math.min(16,remaining()));remaining();
      last={x:point.x+step*8,y:point.y+step*3};send({type:'mouseMove',button,...last,modifiers});trace.moves_sent++;
      trace.frames.push({stamp:performance.now(),pacing:'main-process timer; not a renderer frame acknowledgement'});
    }
    remaining();released=true;send({type:'mouseUp',button,clickCount:1,...last});
    await waitFor(win,`(() => {const c=document.querySelector('#stage canvas'),d=window.__cqSmokeDown,events=window.__cqSmokePointerEvents;
      if(!d||window.__cqSmokePointerDropped!==0||events.some(e=>e.type==='pointercancel'&&e.pointer_id===d.pointer_id))return false;
      const move=events.findIndex(e=>e.type==='pointermove'&&e.trusted&&e.target==='canvas'&&e.pointer_id===d.pointer_id&&e.x===${last.x}&&e.y===${last.y}&&e.buttons===${mask});
      const up=events.findIndex(e=>e.type==='pointerup'&&e.trusted&&e.target==='canvas'&&e.pointer_id===d.pointer_id&&e.x===${last.x}&&e.y===${last.y}&&e.buttons===0&&e.button===${number});
      const lost=events.findIndex(e=>e.type==='lostpointercapture'&&e.trusted&&e.target==='canvas'&&e.pointer_id===d.pointer_id);
      return move>=0&&up>move&&lost>up&&!c.hasPointerCapture(d.pointer_id);})()`,remaining());
    const done=await observe('whole gesture endpoint and release acknowledged');
    assert.ok(done.hit&&done.document_focused&&done.window_focused&&JSON.stringify(done.rect)===JSON.stringify(point.rect)&&JSON.stringify(done.viewport)===JSON.stringify(point.viewport)&&JSON.stringify(done.scroll)===JSON.stringify(point.scroll),'Native camera layout changed before gesture completion');
  }catch(cause){error=cause;trace.status='failed';try{await observe(released?'failure after native release':'failure before native release',phaseBudget);}catch(diagnostic){trace.diagnostic_error=diagnostic.message;}throw cause;}
  finally {
    let cleanupError;
    try{
      if(win.isDestroyed())throw new Error('Native window closed before pointer release');
      if(pressed&&!released){released=true;send({type:'mouseUp',button,clickCount:1,...last});await waitFor(win,`window.__cqSmokeUp?.trusted===true&&window.__cqSmokeUp?.buttons===0&&window.__cqSmokeUp?.button===${number}&&!document.querySelector('#stage canvas').hasPointerCapture(window.__cqSmokeDown.pointer_id)&&window.__cqSmokePointerEvents.some(e=>e.type==='lostpointercapture'&&e.pointer_id===window.__cqSmokeDown.pointer_id)`,phaseBudget);await observe('failure cleanup pointerup acknowledged',phaseBudget);}
    }catch(cleanup){cleanupError=cleanup;}
    try{if(!win.isDestroyed())await evaluate(win,`(() => {const c=document.querySelector('#stage canvas');c.removeEventListener('pointermove',window.__cqSmokeMove);for(const type of ['pointerdown','pointermove','pointerup','pointercancel','gotpointercapture','lostpointercapture','contextmenu'])document.removeEventListener(type,window.__cqSmokeEvent,true);delete window.__cqSmokeMove;delete window.__cqSmokeEvent;delete window.__cqSmokePointer;delete window.__cqSmokePointerEvents;delete window.__cqSmokePointerDropped;delete window.__cqSmokeDown;delete window.__cqSmokeUp;})()`,phaseBudget);}
    catch(cleanup){if(cleanupError)cleanupError.message+=`; listener cleanup: ${cleanup.message}`;else cleanupError=cleanup;}
    if(cleanupError){trace.status='failed';trace.cleanup_error=cleanupError.message;if(error)error.message+=`; pointer cleanup: ${cleanupError.message}`;else error=cleanupError;}
    if(error){error.pointer_trace=trace;throw error;}
  }
  trace.elapsed_ms=gestureBudget-(deadline-performance.now());
  if(trace.elapsed_ms>gestureBudget){trace.status='failed';const late=new Error('Native gesture exceeded its deadline');late.pointer_trace=trace;throw late;}
  trace.status='checked';return trace;
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
    try{row.pointer_drag=await dragCamera(win,button);}catch(error){row.pointer_drag=error.pointer_trace;throw error;}
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
    try{report.camera_save.pointer_drag=await dragCamera(win,'left');}catch(error){report.camera_save.pointer_drag=error.pointer_trace;throw error;}
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
module.exports = { smoke, waitFor, evaluate, bounded, closeWindow, capture, waitRendererLayout, checkpoint, click, measureFrame, validateMatrix, scrollOffsets, validateScrollCoverage, captureScrollRegions, MAX_SCROLL_CAPTURES, certifiedRebuildComplete, WIDTHS, THEMES, STATES, CHECKS };
