// Deterministic regression tests execute the actual viewer source. THREE/DOM
// doubles prove state transitions, not WebGL output; native render is separate.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { createHash, webcrypto } = require('node:crypto');

const source = fs.readFileSync(path.join(__dirname, '../viewer/app.js'), 'utf8')
  .replace(/^import .*;\r?\n/gm, '');
const hash = (text) => createHash('sha256').update(text).digest('hex').slice(0, 16);
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((a, b) => { resolve = a; reject = b; });
  return { promise, resolve, reject };
};
const flush = async () => { for (let i = 0; i < 20; i++) await new Promise(setImmediate); };
const gates = { pass: true, checks: ['units', 'watertight', 'volume', 'thin-extent',
  'triangulation', 'payload-roundtrip', 'budget-tris', 'budget-bytes'].map((id) => ({ id, status: 'pass' })), diagnostics: [] };
function fixture(name = 'TESTPLATE', tier = 'certified', gen = 0, variants = ['neutral'], offset = 0) {
  const routes = new Map(), metas = {};
  for (const variant of variants) {
    const value = { bounds: { min: [offset, 0, 0], max: [offset + 2, 2, 2], center: [offset + 1, 1, 1], radius: Math.sqrt(3) },
      parts: [{ name: 'Base', finish: { color: '#96999e', metalness: 1, roughness: 0.38 }, xray: 'ghost', explode: [0, 0, 4],
        solids: [{ positions: Buffer.from(new Uint16Array([0, 0, 0, 65535, 0, 0, 0, 65535, 0]).buffer).toString('base64'),
          indices: Buffer.from(new Uint16Array([0, 1, 2]).buffer).toString('base64'), itype: 'u16', qoffset: [offset, 0, 0], qscale: 2 / 65535 }] }] };
    const text = JSON.stringify(value), url = `/payload/${encodeURIComponent(name)}-${tier}-${encodeURIComponent(variant)}.json`;
    metas[variant] = { hash: hash(text), url, tris: 1, bytes: Buffer.byteLength(text) };
    routes.set(url, text);
  }
  const index = { project: name, tier, gen, hash: hash(Object.keys(metas).sort().map((v) => metas[v].hash).join('')),
    variants: metas, gates: tier === 'certified' ? structuredClone(gates) : null };
  routes.set(`/payload/${encodeURIComponent(name)}-${tier}.json`, JSON.stringify(index));
  return { index, routes };
}
const manifestFor = (name = 'TESTPLATE', variants = ['neutral']) => ({ name, chips: ['test part'], on_demand: false,
  steps: variants.map((variant, i) => ({ id: `step${i}`, label: `Step ${i}`, caption: `Caption ${i}`, variant,
    explode: i ? 1 : 0, xray: !!i, camera: { position: [70, -95, 58], target: [0, 0, 20] } })) });

function harness({ reduced = true, boot = [], routes = new Map() } = {}) {
  const nodes = new Map(), calls = [], timers = new Map(), sockets = [], frames = [], materials = [], geometries = [];
  let timerID = 0, layoutReads = 0, rayHits = [];
  class Element {
    constructor(tag = 'div') { this.tagName = tag; this.children = []; this.style = {}; this.dataset = {}; this.attributes = {}; this.listeners = {}; this.disabled = false; this.hidden = true; this.value = '0'; this._text = ''; }
    get textContent() { return this._text + this.children.map((c) => c.textContent).join(''); }
    set textContent(value) { this._text = String(value); this.children = []; }
    get innerHTML() { return this.textContent; }
    set innerHTML(value) { assert.equal(value, '', 'untrusted HTML must never be inserted'); this.textContent = ''; }
    appendChild(node) { this.children.push(node); return node; }
    append(...nodes) { this.children.push(...nodes); }
    replaceChildren(...nodes) { this._text = ''; this.children = nodes; }
    setAttribute(k, v) { this.attributes[k] = v; }
    addEventListener(k, f) { (this.listeners[k] ||= []).push(f); }
    async fire(k, event = {}) { for (const f of this.listeners[k] || []) await f({ target: this, currentTarget: this, ...event }); }
    closest() { return null; }
    getBoundingClientRect() { return { left: 0, top: 0, width: 800, height: 600 }; }
    get clientWidth() { layoutReads++; return 800; }
    get clientHeight() { layoutReads++; return 600; }
    toDataURL() { return 'data:image/png;base64,AA=='; }
  }
  const node = (id) => { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); };
  class V3 {
    constructor(x = 0, y = 0, z = 0) { this.set(x, y, z); }
    set(x, y, z) { this.x = x; this.y = y; this.z = z; return this; }
    clone() { return new V3(this.x, this.y, this.z); }
    copy(v) { return this.set(v.x, v.y, v.z); }
    add(v) { this.x += v.x; this.y += v.y; this.z += v.z; return this; }
    sub(v) { this.x -= v.x; this.y -= v.y; this.z -= v.z; return this; }
    lengthSq() { return this.x ** 2 + this.y ** 2 + this.z ** 2; }
    length() { return Math.sqrt(this.lengthSq()); }
    normalize() { return this.multiplyScalar(1 / (this.length() || 1)); }
    multiplyScalar(k) { this.x *= k; this.y *= k; this.z *= k; return this; }
    getComponent(k) { return [this.x, this.y, this.z][k]; }
    lerpVectors(a, b, k) { return this.set(a.x + (b.x - a.x) * k, a.y + (b.y - a.y) * k, a.z + (b.z - a.z) * k); }
  }
  class Group {
    constructor() { this.children = []; this.position = new V3(); this.visible = true; this.userData = {}; }
    add(...items) { this.children.push(...items); }
    remove(o) { this.children = this.children.filter((x) => x !== o); }
    traverse(f) { f(this); for (const c of this.children) c.traverse(f); }
  }
  class Material {
    constructor(options) { Object.assign(this, options); this.disposals = 0; materials.push(this); }
    dispose() { this.disposals++; }
  }
  class Geometry {
    constructor() { this.attributes = {}; this.disposals = 0; geometries.push(this); }
    setAttribute(k, v) { this.attributes[k] = v; }
    setIndex(v) { this.index = v; }
    computeVertexNormals() {}
    dispose() { this.disposals++; }
  }
  class Camera extends Group {
    constructor(fov, aspect, near, far) { super(); this.up = new V3(0, 1, 0); Object.assign(this, { fov, aspect, near, far }); }
    updateProjectionMatrix() {}
  }
  class Renderer {
    constructor() { this.domElement = new Element('canvas'); }
    setPixelRatio(v) { this.ratio = v; }
    getPixelRatio() { return this.ratio; }
    setSize(w, h) { this.size = [w, h]; }
    render() {}
  }
  class OrbitControls {
    constructor(camera) { this.camera = camera; this.target = new V3(); this.listeners = {}; }
    update() {}
    addEventListener(k, f) { this.listeners[k] = f; }
  }
  class Mesh extends Group { constructor(geometry, material) { super(); Object.assign(this, { geometry, material }); } }
  const THREE = { Group, Scene: Group, Vector3: V3, Vector2: class {}, Mesh, PerspectiveCamera: Camera,
    WebGLRenderer: Renderer, MeshStandardMaterial: Material, BufferGeometry: Geometry,
    BufferAttribute: class { constructor(array, itemSize) { Object.assign(this, { array, itemSize }); } },
    Color: class {}, DirectionalLight: Group, AmbientLight: Group,
    PMREMGenerator: class { fromScene() { return { texture: {} }; } }, MathUtils: { degToRad: (v) => v * Math.PI / 180 },
    Raycaster: class { setFromCamera() {} intersectObjects(meshes) { this.meshes = meshes; return rayHits.filter((h) => meshes.includes(h.object)); } } };
  const creases = [];
  let creaseError = null;
  const toCreasedNormals = (geometry, angle) => {
    creases.push({ geometry, angle });
    if (creaseError) throw creaseError;
    const replacement = new Geometry();
    replacement.attributes = { ...geometry.attributes };
    return replacement;
  };
  const context = vm.createContext({ THREE, OrbitControls, RoomEnvironment: Group, toCreasedNormals, URL, TextEncoder,
    Uint8Array, Uint16Array, Uint32Array, Float32Array, AbortController, crypto: webcrypto, atob,
    console, devicePixelRatio: 1, performance: { now: () => 0 },
    location: { origin: 'http://127.0.0.1:3939', port: '3939', hostname: '127.0.0.1', protocol: 'http:' },
    document: { documentElement: new Element(), getElementById: node,
      createElement: (tag) => new Element(tag), createTextNode: (text) => { const e = new Element(); e.textContent = text; return e; },
      querySelectorAll: (selector) => node(selector.includes('steps') ? 'steps' : 'projects').children.filter((c) => c.tagName === 'button') },
    window: { addEventListener() {} }, getComputedStyle: () => ({ getPropertyValue: () => '#1c1c1f' }),
    matchMedia: () => ({ matches: reduced }), ResizeObserver: class { constructor(f) { this.f = f; } observe() {} },
    requestAnimationFrame: (f) => { frames.push(f); },
    setTimeout: (f, ms) => { timers.set(++timerID, { f, ms }); return timerID; }, clearTimeout: (id) => timers.delete(id),
    WebSocket: class { constructor(url) { this.url = url; sockets.push(this); } },
    fetch: async (url, options = {}) => {
      calls.push({ url, options });
      let value = url === '/api/projects' ? boot : routes.get(url);
      if (typeof value === 'function') value = value(options);
      if (value && typeof value.then === 'function') {
        value = await Promise.race([value, new Promise((_, reject) => {
          options.signal?.addEventListener('abort', () => reject(new Error('aborted')), { once: true });
        })]);
      }
      if (value instanceof Error) throw value;
      const status = value === undefined ? 404 : value?.status || 200;
      const body = value?.body !== undefined ? value.body : value;
      return { ok: status >= 200 && status < 300, status, text: async () => typeof body === 'string' ? body : JSON.stringify(body) };
    }
  });
  vm.runInContext(source, context, { filename: 'viewer/app.js' });
  return { context, node, calls, routes, timers, sockets, frames, materials, geometries, creases,
    failCrease(error) { creaseError = error; },
    eval: (code) => vm.runInContext(code, context),
    setHits: (hits) => { rayHits = hits; }, layout: () => layoutReads,
    put(f) { for (const [k, v] of f.routes) routes.set(k, v); },
    async prepare(f = fixture(), manifest = manifestFor(f.index.project, Object.keys(f.index.variants))) {
      await flush(); this.put(f);
      routes.set(`/api/manifest?project=${encodeURIComponent(f.index.project)}`, manifest);
      context.testProject = { name: f.index.project, state: f.index.tier, hashes: { [f.index.tier]: f.index.hash } };
      this.eval('projects = [testProject]');
      await this.eval('loadProject(testProject.name)');
      return this.eval('model');
    }
  };
}

test('certified load decodes full uint16 range and preview disables semantic controls', async () => {
  const h = harness(), good = await h.prepare();
  assert.ok(good);
  assert.equal(good.variants.get('neutral').parts.get('Base').meshes[0].geometry.attributes.position.array[3], 2);
  assert.equal(h.node('xraybtn').disabled, false);
  const preview = fixture('TESTPLATE', 'preview', 1, ['neutral'], 2); h.put(preview);
  h.context.nextHash = preview.index.hash;
  await h.eval('fetchPayload(current, "preview", nextHash)');
  assert.equal(h.eval('model.tier'), 'preview');
  for (const id of ['explode', 'xraybtn', 'selisolate', 'prev', 'next']) assert.equal(h.node(id).disabled, true);
  assert.equal(await h.eval('goStep(0, true)'), false);
  assert.equal(h.eval('certifiedModel'), good);
  assert.equal(good.disposed, false);
});

test('gate failure restores exact certified model and view, cancels pending previews', async () => {
  const h = harness(), good = await h.prepare(fixture('TESTPLATE', 'certified', 0, ['neutral', 'pose']));
  await h.eval('goStep(1, false)');
  const preview = fixture('TESTPLATE', 'preview', 1, ['neutral'], 2); h.put(preview);
  h.context.nextHash = preview.index.hash;
  await h.eval('fetchPayload(current, "preview", nextHash)');
  const pending = fixture('TESTPLATE', 'preview', 2, ['neutral'], 3), wait = deferred(); h.put(pending);
  h.routes.set('/payload/TESTPLATE-preview.json', wait.promise); h.context.nextHash = pending.index.hash;
  const load = h.eval('fetchPayload(current, "preview", nextHash)');
  h.eval('gateFailed({ diagnostics: ["units failed"] })');
  wait.resolve(JSON.stringify(pending.index)); await load;
  assert.equal(h.eval('model'), good);
  assert.equal(h.eval('model.activeName'), 'pose');
  assert.equal(h.eval('currentStep'), 1);
  assert.equal(h.eval('explodeTarget'), 1);
  assert.equal(h.eval('xrayOn'), true);
  assert.equal(h.node('tierbadge').textContent, 'gate FAILED — showing last certified');
  assert.equal(h.node('diag').textContent, 'units failed');
});

test('malformed, failed-gate, wrong-project and overwritten variant loads retain last good model and retry', async () => {
  const h = harness(), good = await h.prepare();
  const bad = fixture('TESTPLATE', 'certified', 1, ['neutral'], 4); h.put(bad); h.context.nextHash = bad.index.hash;
  bad.index.gates.pass = false; h.routes.set('/payload/TESTPLATE-certified.json', JSON.stringify(bad.index));
  await h.eval('fetchPayload(current, "certified", nextHash)'); assert.equal(h.eval('model'), good);
  assert.match(h.node('diag').textContent, /gates did not pass/);
  bad.index.gates.pass = true; bad.index.project = 'OTHER';
  h.routes.set('/payload/TESTPLATE-certified.json', JSON.stringify(bad.index));
  await h.eval('fetchPayload(current, "certified", nextHash)'); assert.equal(h.eval('model'), good);
  bad.index.project = 'TESTPLATE'; h.routes.set('/payload/TESTPLATE-certified.json', JSON.stringify(bad.index));
  h.routes.set(bad.index.variants.neutral.url, '{}');
  await h.eval('fetchPayload(current, "certified", nextHash)'); assert.equal(h.eval('model'), good);
  assert.match(h.node('diag').textContent, /payload changed/);
  h.put(fixture('TESTPLATE', 'certified', 1, ['neutral'], 4));
  await h.eval('fetchPayload(current, "certified", nextHash)');
  assert.notEqual(h.eval('model'), good);
  assert.equal(good.disposed, true);
});

test('new previews display after certification, same-generation preview cannot beat certified', async () => {
  const h = harness(); await h.prepare();
  const preview = fixture('TESTPLATE', 'preview', 1, ['neutral'], 1), cert = fixture('TESTPLATE', 'certified', 1, ['neutral'], 1);
  h.put(preview); h.put(cert); h.context.pHash = preview.index.hash; h.context.cHash = cert.index.hash;
  await h.eval('fetchPayload(current, "preview", pHash)'); assert.equal(h.eval('model.tier'), 'preview');
  await h.eval('fetchPayload(current, "certified", cHash)'); assert.equal(h.eval('model.tier'), 'certified');
  await h.eval('fetchPayload(current, "preview", pHash)'); assert.equal(h.eval('model.tier'), 'certified');
  assert.equal(h.eval('model.index.gen'), 1);
});

test('same hash rebuild advances generation without decoding again', async () => {
  const h = harness(), good = await h.prepare();
  const again = fixture('TESTPLATE', 'certified', 5); h.put(again); h.context.nextHash = again.index.hash;
  const count = h.geometries.length;
  await h.eval('fetchPayload(current, "certified", nextHash)');
  assert.equal(h.eval('model'), good); assert.equal(good.index.gen, 5); assert.equal(h.geometries.length, count);
  const preview = fixture('TESTPLATE', 'preview', 5); h.put(preview); h.context.nextHash = preview.index.hash;
  await h.eval('fetchPayload(current, "preview", nextHash)'); assert.equal(h.eval('model'), good);
});

test('latest project wins out-of-order manifests and payload responses; project names are encoded', async () => {
  const h = harness(); await flush();
  const a = fixture('TESTPLATE'), b = fixture('SCRATCH & +?'); h.put(a); h.put(b);
  const wait = deferred(); h.routes.set('/api/manifest?project=TESTPLATE', wait.promise);
  h.routes.set('/api/manifest?project=SCRATCH%20%26%20%2B%3F', manifestFor(b.index.project));
  h.context.projectList = [a, b].map((f) => ({ name: f.index.project, hashes: { certified: f.index.hash } }));
  h.eval('projects = projectList');
  const first = h.eval('loadProject("TESTPLATE")');
  await h.eval('loadProject("SCRATCH & +?")');
  wait.resolve(manifestFor('TESTPLATE')); await first;
  assert.equal(h.eval('current'), b.index.project); assert.equal(h.eval('manifest.name'), b.index.project);
  assert.equal(h.eval('model.index.project'), b.index.project);
  const pending = deferred(); h.routes.set('/payload/TESTPLATE-certified.json', pending.promise);
  h.routes.set('/api/manifest?project=TESTPLATE', manifestFor('TESTPLATE'));
  const loading = h.eval('loadProject("TESTPLATE")'); await flush();
  await h.eval('loadProject("SCRATCH & +?")'); pending.resolve(JSON.stringify(a.index)); await loading;
  assert.equal(h.eval('model.index.project'), b.index.project);
});

test('step variants are deduplicated and only latest A-B-A transition changes view', async () => {
  const h = harness(); await h.prepare(fixture('TESTPLATE', 'certified', 0, ['neutral', 'A', 'B']));
  const a = deferred(), b = deferred();
  const aText = h.routes.get('/payload/TESTPLATE-certified-A.json'), bText = h.routes.get('/payload/TESTPLATE-certified-B.json');
  h.routes.set('/payload/TESTPLATE-certified-A.json', a.promise); h.routes.set('/payload/TESTPLATE-certified-B.json', b.promise);
  const firstA = h.eval('goStep(1, true)'), onlyB = h.eval('goStep(2, true)'), lastA = h.eval('goStep(1, false)');
  b.resolve(bText); await onlyB; a.resolve(aText); await Promise.all([firstA, lastA]);
  assert.equal(h.eval('currentStep'), 1); assert.equal(h.eval('model.activeName'), 'A');
  assert.equal(h.calls.filter((c) => c.url.endsWith('-A.json')).length, 1);
  assert.equal(h.eval('model.variants.get("B").group.visible'), false);
});

test('missing variant restores step and does not apply camera, xray or explode', async () => {
  const h = harness(); await h.prepare();
  h.eval('manifest.steps.push({ id: "bad", label: "Bad", caption: "Bad", variant: "missing", explode: 1, xray: true })');
  assert.equal(await h.eval('goStep(1, true)'), false);
  assert.equal(h.eval('currentStep'), 0); assert.equal(h.eval('model.activeName'), 'neutral');
  assert.equal(h.eval('xrayOn'), false); assert.equal(h.eval('explodeTarget'), 0);
  assert.match(h.node('diag').textContent, /unknown variant/);
});

test('rebuild preserves latest camera direction and cancels authored tween', async () => {
  const h = harness({ reduced: false }); await h.prepare();
  h.eval('camera.position.set(7, 11, 13); controls.target.set(1, 2, 3); tweenCamera(new THREE.Vector3(70, -95, 58), null)');
  const dir = h.eval('camera.position.clone().sub(controls.target).normalize()');
  const update = fixture('TESTPLATE', 'certified', 1, ['neutral'], 4); h.put(update); h.context.nextHash = update.index.hash;
  await h.eval('fetchPayload(current, "certified", nextHash)');
  const after = h.eval('camera.position.clone().sub(controls.target).normalize()');
  assert.ok(Math.abs(after.x - dir.x) < 1e-12 && Math.abs(after.y - dir.y) < 1e-12 && Math.abs(after.z - dir.z) < 1e-12);
  assert.equal(h.eval('camAnim'), null);
  const reads = h.layout(); h.eval('tick(600)'); assert.equal(h.layout(), reads, 'animation does not read layout');
});

test('manifest reload preserves step by id through reorder, rejects stale reload across switch', async () => {
  const h = harness(); await h.prepare(fixture('TESTPLATE', 'certified', 0, ['neutral', 'pose']));
  await h.eval('goStep(1, false)');
  const changed = manifestFor('TESTPLATE', ['neutral', 'pose']); changed.steps.reverse();
  h.routes.set('/api/manifest?project=TESTPLATE', changed);
  await h.eval('handleMessage({ type: "manifest", project: current })');
  assert.equal(h.eval('currentStep'), 0); assert.equal(h.eval('model.activeName'), 'pose');
  const wait = deferred(); h.routes.set('/api/manifest?project=TESTPLATE', wait.promise);
  const reload = h.eval('reloadManifest()');
  h.routes.set('/api/manifest?project=SCRATCH', manifestFor('SCRATCH'));
  await h.eval('loadProject("SCRATCH")'); wait.resolve(changed); await reload;
  assert.equal(h.eval('manifest.name'), 'SCRATCH');
});

test('HTTP errors and timeouts are visible, controls unavailable until manifest succeeds', async () => {
  const h = harness(); await flush();
  h.routes.set('/api/manifest?project=TESTPLATE', { status: 500, body: { error: 'bad' } });
  await h.eval('loadProject("TESTPLATE")');
  assert.equal(h.eval('manifest'), null); assert.equal(h.node('buildbtn').disabled, true);
  assert.match(h.node('diag').textContent, /worker returned 500/);
  const wait = deferred(); h.routes.set('/api/manifest?project=TESTPLATE', wait.promise);
  const pending = h.eval('loadProject("TESTPLATE")');
  const timeout = [...h.timers.values()].find((t) => t.ms === 15000); assert.ok(timeout); timeout.f();
  await pending; assert.match(h.node('diag').textContent, /aborted/);
});

test('status text is inert, malformed WS data contained, first connection failure visible', async () => {
  const h = harness(); await h.prepare();
  h.context.untrusted = '<img src=x onerror=alert(1)>';
  h.eval('setStale(untrusted)'); assert.ok(h.node('buildline').textContent.includes(h.context.untrusted));
  await h.sockets[0].onmessage({ data: '{' }); assert.match(h.node('diag').textContent, /Worker message failed/);
  h.sockets[0].onclose(); assert.equal(h.node('buildline').textContent, 'disconnected — reconnecting');
});

test('hello refreshes missed hashes and noncurrent project updates are remembered', async () => {
  const h = harness(); await h.prepare();
  const next = fixture('TESTPLATE', 'certified', 1, ['neutral'], 4); h.put(next); h.context.nextHash = next.index.hash;
  await h.eval('handleMessage({ type: "hello", projects: [{name: current, state: "certified", hashes: {certified: nextHash}}, {name: "SCRATCH", hashes: {}}] })');
  assert.equal(h.eval('model.hash'), next.index.hash);
  await h.eval('handleMessage({type: "announce", project: "SCRATCH", tier: "preview", hash: "aaaaaaaaaaaaaaaa"})');
  assert.equal(h.eval('projects[1].hashes.preview'), 'aaaaaaaaaaaaaaaa');
});

test('commands wait for variant and screenshot waits for settled command; failures contained', async () => {
  const h = harness({ reduced: false }); await h.prepare(fixture('TESTPLATE', 'certified', 0, ['neutral', 'pose']));
  const wait = deferred(), text = h.routes.get('/payload/TESTPLATE-certified-pose.json');
  h.routes.set('/payload/TESTPLATE-certified-pose.json', wait.promise);
  h.routes.set('/api/shot', { saved: 'scratch.png' });
  const command = h.eval('handleMessage({type: "command", project: current, step: "step1", xray: false, explode: 0.5, camera: {position: [1,2,3], target: [0,0,0]}})');
  const shot = h.eval('handleMessage({type: "screenshot", project: current, id: "scratch-shot"})');
  assert.equal(h.calls.filter((c) => c.url === '/api/shot').length, 0);
  wait.resolve(text); await Promise.all([command, shot]);
  assert.equal(h.eval('model.activeName'), 'pose'); assert.equal(h.eval('xrayOn'), false); assert.equal(h.eval('explodeAmt'), 0.5);
  assert.equal(h.eval('camera.position.x'), 1); assert.equal(h.eval('camAnim'), null);
  h.routes.set('/api/shot', { status: 500, body: {} });
  await h.sockets[0].onmessage({ data: JSON.stringify({ type: 'screenshot', project: 'TESTPLATE', id: 'scratch-shot2' }) });
  assert.match(h.node('diag').textContent, /worker returned 500/);
});

test('hidden meshes cannot be picked and cancelled/right-button gestures do not select', async () => {
  const h = harness(); await h.prepare();
  const mesh = h.eval('activeVariant()[1].parts.get("Base").meshes[0]'); h.setHits([{ object: mesh }]);
  const canvas = h.eval('renderer.domElement');
  await canvas.fire('pointerdown', { button: 0, pointerId: 1, clientX: 20, clientY: 20 });
  await canvas.fire('pointerup', { button: 0, pointerId: 1, clientX: 20, clientY: 20 }); assert.equal(h.eval('selected'), 'Base');
  h.eval('clearSelection()'); mesh.visible = false;
  await canvas.fire('pointerdown', { button: 0, pointerId: 1, clientX: 20, clientY: 20 });
  await canvas.fire('pointerup', { button: 0, pointerId: 1, clientX: 20, clientY: 20 }); assert.equal(h.eval('selected'), null);
  mesh.visible = true;
  await canvas.fire('pointerdown', { button: 0, pointerId: 1, clientX: 20, clientY: 20 }); await canvas.fire('pointercancel');
  await canvas.fire('pointerup', { pointerId: 1, clientX: 20, clientY: 20 }); assert.equal(h.eval('selected'), null);
  await canvas.fire('pointerdown', { button: 2, pointerId: 1, clientX: 20, clientY: 20 });
  await canvas.fire('pointerup', { button: 2, pointerId: 1, clientX: 20, clientY: 20 }); assert.equal(h.eval('selected'), null);
});

test('bundled TESTPLATE payload uses exact index hashes and valid encoded geometry', async () => {
  const h = harness(), root = path.join(__dirname, '..'); await flush();
  const index = JSON.parse(fs.readFileSync(path.join(root, 'payloads/TESTPLATE-certified.json'), 'utf8'));
  const routes = new Map([[`/payload/TESTPLATE-certified.json`, JSON.stringify(index)]]);
  for (const meta of Object.values(index.variants)) routes.set(meta.url, fs.readFileSync(path.join(root, 'payloads', path.basename(meta.url)), 'utf8'));
  const manifest = JSON.parse(fs.readFileSync(path.join(root, 'testpart/viewer.json'), 'utf8'));
  await h.prepare({ index, routes }, manifest);
  assert.ok(h.eval('model')); assert.equal(h.eval('model.variants.get("neutral").parts.size'), 3);
});

test('immutable hash-qualified URL accepted; arbitrary origin, path, query and wrong suffix rejected', async () => {
  const h = harness(); await h.prepare();
  const f = fixture('TESTPLATE', 'certified', 1, ['neutral'], 5);
  const meta = f.index.variants.neutral, fixed = meta.url;
  meta.url = fixed.replace('.json', `-${meta.hash}.json`);
  f.routes.set(meta.url, f.routes.get(fixed));
  f.routes.set('/payload/TESTPLATE-certified.json', JSON.stringify(f.index)); h.put(f); h.context.nextHash = f.index.hash;
  assert.equal(await h.eval('fetchPayload(current, "certified", nextHash)'), true);
  const good = h.eval('model');
  for (const url of ['https://example.com/payload/x.json', '/api/projects', fixed + '?x=1', fixed.replace('.json', '-0000000000000000.json')]) {
    f.index.variants.neutral.url = url; h.routes.set('/payload/TESTPLATE-certified.json', JSON.stringify(f.index));
    assert.equal(await h.eval('fetchPayload(current, "certified", nextHash)'), false);
    assert.equal(h.eval('model'), good); assert.match(h.node('diag').textContent, /invalid variant metadata/);
  }
  assert.ok(h.calls.every((c) => c.options.redirect === 'error'));
});

test('late same-generation preview cannot replace certification or corrupt the fallback reference', async () => {
  const h = harness(); await h.prepare();
  const p = fixture('TESTPLATE', 'preview', 1, ['neutral'], 2), c = fixture('TESTPLATE', 'certified', 1, ['neutral'], 2);
  h.put(p); h.put(c); h.context.pHash = p.index.hash; h.context.cHash = c.index.hash;
  const wait = deferred(), body = h.routes.get(p.index.variants.neutral.url);
  h.routes.set(p.index.variants.neutral.url, wait.promise);
  const preview = h.eval('fetchPayload(current, "preview", pHash)'); await flush();
  await h.eval('fetchPayload(current, "certified", cHash)'); const certified = h.eval('model');
  wait.resolve(body); await preview;
  assert.equal(h.eval('model'), certified); assert.equal(h.eval('certifiedModel'), certified);
  assert.equal(certified.tier, 'certified');
});

test('new certified fallback remains semantically correct behind a newer preview', async () => {
  const h = harness(); await h.prepare(fixture('TESTPLATE', 'certified', 0, ['neutral', 'pose']));
  await h.eval('goStep(1, false)');
  const p = fixture('TESTPLATE', 'preview', 2, ['neutral'], 2), c = fixture('TESTPLATE', 'certified', 1, ['neutral', 'pose'], 1);
  h.put(p); h.put(c); h.context.pHash = p.index.hash; h.context.cHash = c.index.hash;
  await h.eval('fetchPayload(current, "preview", pHash)');
  await h.eval('fetchPayload(current, "certified", cHash)');
  assert.equal(h.eval('model.tier'), 'preview');
  assert.equal(h.eval('certifiedModel.activeName'), 'pose');
  h.eval('gateFailed({ diagnostics: [] })');
  assert.equal(h.eval('model.index.gen'), 1); assert.equal(h.eval('model.activeName'), 'pose'); assert.equal(h.eval('currentStep'), 1);
});

test('hello resets generation ordering after a worker restart', async () => {
  const h = harness(); await h.prepare(fixture('TESTPLATE', 'certified', 99));
  const p = fixture('TESTPLATE', 'preview', 0, ['neutral'], 4); h.put(p); h.context.pHash = p.index.hash;
  await h.eval('handleMessage({type: "hello", projects: [{name: current, state: "preview", hashes: {preview: pHash}}]})');
  assert.equal(h.eval('model.tier'), 'preview'); assert.equal(h.eval('model.index.gen'), 0);
});

test('overlapping failed steps return to displayed step, not another failed pending step', async () => {
  const h = harness(); await h.prepare(fixture('TESTPLATE', 'certified', 0, ['neutral', 'A', 'B']));
  const a = deferred(), b = deferred();
  h.routes.set('/payload/TESTPLATE-certified-A.json', a.promise); h.routes.set('/payload/TESTPLATE-certified-B.json', b.promise);
  const first = h.eval('goStep(1, true)'), second = h.eval('goStep(2, true)');
  b.reject(new Error('B failed')); await second; a.reject(new Error('A failed')); await first;
  assert.equal(h.eval('currentStep'), 0); assert.equal(h.eval('displayedStep'), 0); assert.equal(h.node('caption').textContent, 'Caption 0');
});

test('pending step does not overwrite newer user camera, xray and explode choices', async () => {
  const h = harness({ reduced: false }); await h.prepare(fixture('TESTPLATE', 'certified', 0, ['neutral', 'pose']));
  const wait = deferred(), body = h.routes.get('/payload/TESTPLATE-certified-pose.json');
  h.routes.set('/payload/TESTPLATE-certified-pose.json', wait.promise);
  const step = h.eval('goStep(1, true)');
  h.eval('controls.listeners.start(); camera.position.set(10,20,30)');
  h.node('explode').value = '25'; await h.node('explode').fire('input');
  await h.node('xraybtn').fire('click'); await h.node('xraybtn').fire('click');
  wait.resolve(body); await step;
  assert.equal(h.eval('camera.position.x'), 10); assert.equal(h.eval('camAnim'), null);
  assert.equal(h.eval('explodeTarget'), 0.25); assert.equal(h.eval('xrayOn'), false);
});

test('failed command cannot poison later screenshot requests', async () => {
  const h = harness(); await h.prepare(); h.routes.set('/api/shot', { saved: 'scratch.png' });
  await h.sockets[0].onmessage({ data: JSON.stringify({ type: 'command', project: 'TESTPLATE', explode: -1 }) });
  assert.match(h.node('diag').textContent, /invalid view command/);
  await h.sockets[0].onmessage({ data: JSON.stringify({ type: 'screenshot', project: 'TESTPLATE', id: 'after-error' }) });
  assert.equal(h.calls.filter((c) => c.url === '/api/shot').length, 1);
});

test('rebuild button stays queued through tool updates and old errors cannot affect another project', async () => {
  const h = harness(); await h.prepare(); const wait = deferred(); h.routes.set('/api/rebuild', wait.promise);
  const queued = h.node('buildbtn').fire('click');
  h.eval('updateTools()'); assert.equal(h.node('buildbtn').disabled, true);
  await h.node('buildbtn').fire('click'); assert.equal(h.calls.filter((c) => c.url === '/api/rebuild').length, 1);
  h.routes.set('/api/manifest?project=SCRATCH', manifestFor('SCRATCH'));
  await h.eval('loadProject("SCRATCH")'); const diag = h.node('diag').textContent;
  wait.reject(new Error('old project failed')); await queued;
  assert.equal(h.node('diag').textContent, diag); assert.equal(h.node('buildbtn').disabled, false);
});

test('malformed packed indices are rejected and provisional GPU objects are disposed once', async () => {
  const h = harness(), good = await h.prepare();
  const f = fixture('TESTPLATE', 'certified', 1, ['neutral'], 4), meta = f.index.variants.neutral;
  const value = JSON.parse(f.routes.get(meta.url));
  value.parts[0].solids.push({ ...value.parts[0].solids[0], indices: Buffer.from(new Uint16Array([0,1,99]).buffer).toString('base64') });
  const text = JSON.stringify(value); meta.hash = hash(text); f.index.hash = hash(meta.hash);
  f.routes.set(meta.url, text); f.routes.set('/payload/TESTPLATE-certified.json', JSON.stringify(f.index)); h.put(f); h.context.nextHash = f.index.hash;
  const materialCount = h.materials.length, geometryCount = h.geometries.length;
  await h.eval('fetchPayload(current, "certified", nextHash)');
  assert.equal(h.eval('model'), good); assert.match(h.node('diag').textContent, /outside vertices/);
  assert.ok(h.materials.slice(materialCount).every((m) => m.disposals === 1));
  assert.ok(h.geometries.slice(geometryCount).every((g) => g.disposals === 1));
});

test('GPU materials shared across solids and empty parts are released once', async () => {
  const h = harness(); await flush();
  const f = fixture(), meta = f.index.variants.neutral, value = JSON.parse(f.routes.get(meta.url));
  value.parts[0].solids.push(value.parts[0].solids[0]); value.parts.push({ ...value.parts[0], name: 'Empty', solids: [] });
  const text = JSON.stringify(value); meta.hash = hash(text); f.index.hash = hash(meta.hash);
  f.routes.set(meta.url, text); f.routes.set('/payload/TESTPLATE-certified.json', JSON.stringify(f.index));
  await h.prepare(f); h.eval('disposeModel()');
  assert.ok(h.materials.every((m) => m.disposals === 1)); assert.ok(h.geometries.every((g) => g.disposals === 1));
});

test('gate failure without any certified model is truthful and build error restores certification', async () => {
  const h = harness(); await h.prepare(fixture('TESTPLATE', 'preview'));
  h.eval('gateFailed({ diagnostics: [] })'); assert.equal(h.node('tierbadge').textContent, 'gate FAILED — no certified model');
  const c = fixture('TESTPLATE', 'certified', 1); h.put(c); h.context.cHash = c.index.hash;
  await h.eval('fetchPayload(current, "certified", cHash)'); const good = h.eval('model');
  const p = fixture('TESTPLATE', 'preview', 2, ['neutral'], 5); h.put(p); h.context.pHash = p.index.hash;
  await h.eval('fetchPayload(current, "preview", pHash)');
  await h.eval('handleMessage({type: "build_failed", project: current, tier: "certified", detail: "scratch error"})');
  assert.equal(h.eval('model'), good); assert.equal(h.node('diag').textContent, 'Build error: scratch error');
});

test('Show all remains reachable when the last mesh is hidden and empty canvas is clicked', async () => {
  const h = harness(); await h.prepare();
  const canvas = h.eval('renderer.domElement'), mesh = h.eval('activeVariant()[1].parts.get("Base").meshes[0]');
  h.setHits([{ object: mesh }]);
  await canvas.fire('pointerdown', { button: 0, pointerId: 1, clientX: 20, clientY: 20 });
  await canvas.fire('pointerup', { button: 0, pointerId: 1, clientX: 20, clientY: 20 });
  assert.equal(h.node('selhide').disabled, false); await h.node('selhide').fire('click');
  await canvas.fire('pointerdown', { button: 0, pointerId: 1, clientX: 20, clientY: 20 });
  await canvas.fire('pointerup', { button: 0, pointerId: 1, clientX: 20, clientY: 20 });
  assert.equal(h.node('selection').style.display, 'flex'); assert.equal(h.node('selshow').disabled, false);
  await h.node('selshow').fire('click'); assert.equal(mesh.visible, true);
});

test('initial aspect is set before loading; rebuilt exploded view fits current bounds', async () => {
  const h = harness(); assert.equal(h.eval('camera.aspect'), 800 / 600);
  await h.prepare(fixture('TESTPLATE', 'certified', 0, ['neutral', 'pose'])); await h.eval('goStep(1, false)');
  const f = fixture('TESTPLATE', 'certified', 1, ['neutral', 'pose'], 3); h.put(f); h.context.nextHash = f.index.hash;
  await h.eval('fetchPayload(current, "certified", nextHash)');
  const bounds = h.eval('viewBounds()'), distance = h.eval('camera.position.clone().sub(controls.target).length()');
  assert.ok(bounds.radius > Math.sqrt(3)); assert.ok(distance > bounds.radius);
  assert.equal(h.eval('controls.target.z'), bounds.center.z);
  assert.equal(h.eval('controls.enableDamping'), false);
});

test('disconnect notice survives completed payload and reconnect hello beats cold API snapshot', async () => {
  const h = harness(); await h.prepare(); h.sockets[0].onclose();
  const f = fixture('TESTPLATE', 'certified', 1, ['neutral'], 3); h.put(f); h.context.nextHash = f.index.hash;
  await h.eval('fetchPayload(current, "certified", nextHash)');
  assert.equal(h.node('buildline').textContent, 'disconnected — reconnecting');
  const boot = deferred(), cold = harness({ boot: boot.promise });
  cold.routes.set('/api/manifest?project=SCRATCH', manifestFor('SCRATCH'));
  await cold.eval('handleMessage({type: "hello", projects: [{name: "SCRATCH", state: "idle", hashes: {}}]})');
  boot.resolve([{name: 'OLD', state: 'idle'}]); await flush();
  assert.equal(cold.eval('current'), 'SCRATCH'); assert.equal(cold.eval('projects[0].name'), 'SCRATCH');
});

test('minimal manifest defaults optional steps/chips, and project switch clears stale tool values', async () => {
  const h = harness(); await h.prepare(); h.eval('applyExplode(1)');
  h.routes.set('/api/manifest?project=SCRATCH', { name: 'SCRATCH' });
  await h.eval('loadProject("SCRATCH")');
  assert.equal(h.eval('manifest.steps.length'), 0); assert.equal(h.eval('manifest.chips.length'), 0);
  assert.equal(h.node('explode').value, 0); assert.equal(h.node('explodeval').textContent, '0%');
});

test('project entry loads newer preview while retaining certified fallback, but not after gate failure', async () => {
  const h = harness(); await flush();
  const c = fixture('TESTPLATE', 'certified', 0), p = fixture('TESTPLATE', 'preview', 1, ['neutral'], 2);
  h.put(c); h.put(p); h.routes.set('/api/manifest?project=TESTPLATE', manifestFor());
  h.context.entry = { name: 'TESTPLATE', state: 'preview', hashes: { certified: c.index.hash, preview: p.index.hash } };
  h.eval('projects = [entry]'); await h.eval('loadProject(entry.name)');
  assert.equal(h.eval('model.tier'), 'preview'); assert.equal(h.eval('certifiedModel.hash'), c.index.hash);
  h.routes.set('/api/manifest?project=SCRATCH', manifestFor('SCRATCH')); await h.eval('loadProject("SCRATCH")');
  h.eval('entry.state = "gate-failed"'); await h.eval('loadProject(entry.name)');
  assert.equal(h.eval('model.tier'), 'certified'); assert.equal(h.node('tierbadge').textContent, 'gate FAILED — showing last certified');
});

test('hello cancelling a pending step keeps its label aligned with the actual displayed variant', async () => {
  const h = harness(); await h.prepare(fixture('TESTPLATE', 'certified', 0, ['neutral', 'pose']));
  const wait = deferred(), text = h.routes.get('/payload/TESTPLATE-certified-pose.json');
  h.routes.set('/payload/TESTPLATE-certified-pose.json', wait.promise);
  const step = h.eval('goStep(1, true)');
  await h.eval('handleMessage({type: "hello", projects: [{name: current, state: "certified", hashes: {certified: model.hash}}]})');
  wait.resolve(text); await step;
  assert.equal(h.eval('currentStep'), 0); assert.equal(h.eval('displayedStep'), 0);
  assert.equal(h.eval('model.activeName'), 'neutral'); assert.equal(h.node('caption').textContent, 'Caption 0');
});

test('CAD Z-up configured before controls; crease converter replaces and releases indexed source once', async () => {
  const h = harness(); await h.prepare();
  assert.equal(h.eval('camera.up.x'), 0); assert.equal(h.eval('camera.up.y'), 0); assert.equal(h.eval('camera.up.z'), 1);
  assert.equal(h.creases.length, 1); assert.equal(h.creases[0].angle, Math.PI / 3);
  const original = h.creases[0].geometry, displayed = h.eval('activeVariant()[1].parts.get("Base").meshes[0].geometry');
  assert.notEqual(displayed, original); assert.equal(original.disposals, 1); assert.equal(displayed.disposals, 0);
  h.eval('disposeModel()'); assert.equal(original.disposals, 1); assert.equal(displayed.disposals, 1);
});

test('crease conversion failure disposes provisional resources and retains displayed certification', async () => {
  const h = harness(), good = await h.prepare();
  const next = fixture('TESTPLATE', 'certified', 1, ['neutral'], 8); h.put(next); h.context.nextHash = next.index.hash;
  const geometryCount = h.geometries.length, materialCount = h.materials.length;
  h.failCrease(new Error('conversion failed'));
  assert.equal(await h.eval('fetchPayload(current, "certified", nextHash)'), false);
  assert.equal(h.eval('model'), good); assert.match(h.node('diag').textContent, /conversion failed/);
  assert.ok(h.geometries.slice(geometryCount).every((g) => g.disposals === 1));
  assert.ok(h.materials.slice(materialCount).every((m) => m.disposals === 1));
});
