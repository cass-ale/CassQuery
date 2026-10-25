'use strict';
// Independent actual-Three viewer regressions. DOM/renderer adapters are not native GUI proof.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto');
const ROOT = path.resolve(__dirname, '..');
const app = fs.readFileSync(process.env.CASSQUERY_VIEWER_SOURCE || path.join(ROOT, 'viewer/app.js'), 'utf8');
console.log('viewer_source_sha256=' + crypto.createHash('sha256').update(app).digest('hex'));
const data = (source) => 'data:text/javascript;base64,' + Buffer.from(source).toString('base64');
const actual = (async () => {
  const core = data(fs.readFileSync(path.join(ROOT, 'viewer/vendor/three.core.js'), 'utf8'));
  const THREE = await import(core);
  const load = async (file) => import(data(fs.readFileSync(path.join(ROOT, 'viewer/vendor/addons', file), 'utf8')
    .replaceAll("from 'three'", `from ${JSON.stringify(core)}`)));
  const { OrbitControls } = await load('controls/OrbitControls.js');
  const { toCreasedNormals } = await load('utils/BufferGeometryUtils.js');
  return { THREE, OrbitControls, toCreasedNormals };
})();
const payload = JSON.parse(fs.readFileSync(path.join(ROOT, 'payloads/TESTPLATE-certified-neutral.json'), 'utf8'));
const funcs = app.slice(app.indexOf('function fitDist('), app.indexOf('// ---------- model ----------'))
  + app.slice(app.indexOf('function viewBounds('), app.indexOf('async function reloadManifest('));
function contextFor(THREE, OrbitControls, bounds, reduced = false, aspect = 4 / 3) {
  const camera = new THREE.PerspectiveCamera(40, aspect, 0.5, 4000);
  camera.up.set(0, 0, 1); camera.position.set(60, -70, 40);
  const controls = new OrbitControls(camera, null);
  controls.enableDamping = !reduced; controls.domElement = { clientWidth: 800, clientHeight: 600 };
  const variant = { bounds, parts: new Map() };
  const model = { bounds, variants: new Map([['neutral', variant]]), activeName: 'neutral' };
  const context = vm.createContext({ THREE, camera, controls, model, explodeAmt: 0, explodeTarget: 0,
    camAnim: null, cameraRequest: 0, reduced, performance: { now: () => 0 },
    activeVariant: () => ['neutral', variant], requestAnimationFrame() {},
    renderer: { render() {} }, scene: new THREE.Group() });
  vm.runInContext(funcs, context);
  vm.runInContext('fitModel()', context);
  return { context, camera, controls, model, variant };
}
function settle(context, frames = 180) {
  vm.runInContext(app.slice(app.indexOf('function tick(now)'), app.indexOf('window.__cq =', app.indexOf('function tick(now)'))), context);
  for (let i = 1; i <= frames; i++) vm.runInContext(`tick(${i * 1000 / 60})`, context);
}

test('actual Orbit inertia cannot change a programmatic reframe or reset endpoint', async () => {
  const { THREE, OrbitControls } = await actual;
  for (const reduced of [false, true]) for (const mode of ['orbit', 'pan']) {
    const h = contextFor(THREE, OrbitControls, payload.bounds, reduced);
    h.controls._onMouseDown({ button: mode === 'pan' ? 2 : 0, clientX: 400, clientY: 300 });
    h.controls._onMouseMove({ clientX: 480, clientY: 320 });
    const direction = h.camera.position.clone().sub(h.controls.target).normalize();
    vm.runInContext('fitModel()', h.context);
    for (let i = 0; i < 180; i++) h.controls.update();
    assert.ok(direction.dot(h.camera.position.clone().sub(h.controls.target).normalize()) >= 0.999999,
      `${mode}/reduced=${reduced}: reframe changed direction`);
    assert.ok(h.controls.target.distanceTo(new THREE.Vector3(...payload.bounds.center)) < 1e-9,
      `${mode}/reduced=${reduced}: reframe target drifted`);
    // New real Orbit motion immediately precedes the actual Reset view handler.
    h.controls._onMouseDown({ button: mode === 'pan' ? 2 : 0, clientX: 400, clientY: 300 });
    h.controls._onMouseMove({ clientX: 480, clientY: 320 });
    let reset;
    h.context.document = { getElementById: () => ({ addEventListener: (_type, fn) => { reset = fn; } }) };
    vm.runInContext(app.slice(app.indexOf('document.getElementById("resetview").addEventListener'),
      app.indexOf('window.addEventListener("keydown"')), h.context);
    reset(); settle(h.context);
    const expected = new THREE.Vector3(0.95, -1.35, 0.62).normalize();
    const observed = h.camera.position.clone().sub(h.controls.target).normalize();
    assert.ok(expected.dot(observed) >= 0.999999, `${mode}/reduced=${reduced}: Reset endpoint drifted`);
    assert.ok(h.controls.target.distanceTo(new THREE.Vector3(...payload.bounds.center)) < 1e-9,
      `${mode}/reduced=${reduced}: Reset target drifted`);
  }
});

test('tiny mm geometry stays inside both depth planes after fit and actual Reset, including portrait', async () => {
  const { THREE, OrbitControls } = await actual;
  for (const edge of [0.01, 0.02, 0.2]) for (const aspect of [1, 0.02, 4 / 3]) {
    const radius = Math.sqrt(3) * edge / 2;
    const bounds = { min: [-edge / 2, -edge / 2, -edge / 2], max: [edge / 2, edge / 2, edge / 2], center: [0, 0, 0], radius };
    const h = contextFor(THREE, OrbitControls, bounds, true, aspect);
    const check = () => {
      assert.ok(h.camera.near > 0 && h.camera.far > h.camera.near);
      h.camera.updateMatrixWorld(true);
      for (const x of [-edge / 2, edge / 2]) for (const y of [-edge / 2, edge / 2]) for (const z of [-edge / 2, edge / 2]) {
        const p = new THREE.Vector3(x, y, z).project(h.camera);
        assert.ok([p.x, p.y, p.z].every(Number.isFinite) && Math.max(Math.abs(p.x), Math.abs(p.y), Math.abs(p.z)) <= 1.001,
          `edge=${edge}/aspect=${aspect}: clipped cube corner ${JSON.stringify(p)}`);
      }
    };
    check();
    let reset;
    h.context.document = { getElementById: () => ({ addEventListener: (_type, fn) => { reset = fn; } }) };
    vm.runInContext(app.slice(app.indexOf('document.getElementById("resetview").addEventListener'),
      app.indexOf('window.addEventListener("keydown"')), h.context);
    reset(); check();
  }
});

test('real public geometry/ray: returning long drags do not select; stationary clicks still select', async () => {
  const { THREE, OrbitControls, toCreasedNormals } = await actual;
  for (const excursion of [80, 220]) {
    const h = contextFor(THREE, OrbitControls, payload.bounds);
    const group = new THREE.Group(); h.model.group = group;
    for (const part of payload.parts) {
      const mat = new THREE.MeshStandardMaterial(part.finish), meshes = [];
      for (const solid of part.solids) {
        const q = new Uint16Array(Uint8Array.from(Buffer.from(solid.positions, 'base64')).buffer), p = new Float32Array(q.length);
        for (let i = 0; i < p.length; i++) p[i] = solid.qoffset[i % 3] + q[i] * solid.qscale;
        const bytes = Uint8Array.from(Buffer.from(solid.indices, 'base64'));
        const ix = solid.itype === 'u16' ? new Uint16Array(bytes.buffer) : new Uint32Array(bytes.buffer);
        const g = new THREE.BufferGeometry(); g.setAttribute('position', new THREE.BufferAttribute(p, 3));
        g.setIndex(new THREE.BufferAttribute(ix, 1));
        const m = new THREE.Mesh(toCreasedNormals(g, Math.PI / 3), mat); g.dispose();
        m.userData.part = part.name; meshes.push(m); group.add(m);
      }
      h.variant.parts.set(part.name, { mat, meshes, explode: new THREE.Vector3(...part.explode) });
    }
    const elements = new Map();
    const element = (id) => {
      if (!elements.has(id)) elements.set(id, { style: {}, textContent: '', listeners: {},
        addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); },
        getBoundingClientRect: () => ({ left: 0, top: 0, width: 800, height: 600 }) });
      return elements.get(id);
    };
    const canvas = element('canvas'); h.context.renderer = { domElement: canvas };
    h.context.document = { getElementById: element }; h.context.interactive = () => true; h.context.updateTools = () => {};
    vm.runInContext(app.slice(app.indexOf('const ray = new THREE.Raycaster()'), app.indexOf('// ---------- tools ----------')), h.context);
    const fire = (type, event) => { for (const fn of canvas.listeners[type] || []) fn(event); };
    const press = { button: 0, pointerId: 1, clientX: 400, clientY: 300 };
    h.controls._onMouseDown(press); fire('pointerdown', press);
    for (const x of [400 + excursion, 400]) {
      const move = { ...press, clientX: x }; h.controls._onMouseMove(move); fire('pointermove', move);
    }
    group.updateMatrixWorld(true); h.camera.updateMatrixWorld(true); fire('pointerup', press);
    assert.equal(vm.runInContext('selected', h.context), null, `returning ${excursion}px drag picked a part`);
    // Successful real-ray stationary pick proves this did not merely disable all picking.
    h.controls._onMouseDown(press); fire('pointerdown', press);
    group.updateMatrixWorld(true); h.camera.updateMatrixWorld(true); fire('pointerup', press);
    assert.ok(vm.runInContext('selected', h.context), 'stationary real-ray click was rejected');
  }
});
