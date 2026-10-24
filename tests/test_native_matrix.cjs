'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const { validateMatrix, click, measureFrame, certifiedRebuildComplete, WIDTHS, THEMES, STATES } = require('../shell/smoke.cjs');

function validMatrix() {
  return THEMES.flatMap((theme) => WIDTHS.flatMap((width) => STATES.map((state) => ({
    theme, realized_theme: theme, background: theme, width, state, status: 'checked', viewport: [width, 700],
    full_size: [width, 700], scroll_regions: [], vertical_scroll: false,
    vertices: 30, meshes: 3, triangles: 10, degenerate: 0, controls_checked: 15, clipped: 0, nonfinite: 0, overflow: false,
    bad: [], overlaps: [], errors: [], diag: '', tier: 'certified', gates_pass: true,
    step_pressed: state === 'assembled' ? ['true', 'false'] : ['false', 'true'],
    explode: state === 'assembled' ? '0%' : '100%',
    canvas: { width, height: 600, client_width: width, client_height: 600 },
    image: { path: `${theme}-${width}-${state}.png`, sha256: 'a'.repeat(64), width, height: 700 },
  }))));
}

test('native matrix requires every actual state, both themes, and all realized sizes', () => {
  validateMatrix(validMatrix());
  for (const matrix of [[], validMatrix().slice(1), [...validMatrix().slice(1), validMatrix()[1]]]) {
    assert.throws(() => validateMatrix(matrix));
  }
});

test('zero/skipped/clipped/error/preview/unrealized coverage cannot pass', () => {
  for (const mutation of [{ vertices: 0 }, { meshes: 0 }, { controls_checked: 0 }, { status: 'skipped' },
    { clipped: 1 }, { nonfinite: 1 }, { overflow: true }, { bad: ['clipped text'] }, { overlaps: ['overlap'] },
    { errors: ['pageerror'] }, { diag: 'worker failed' }, { tier: 'preview' }, { gates_pass: false },
    { viewport: [450, 0] }, { realized_theme: 'wrong' }, { step_pressed: ['false', 'false'] },
    { explode: '25%' }, { canvas: { width: 0 } }, { image: { sha256: 'a'.repeat(64), width: 0, height: 0 } }]) {
    const records = validMatrix(); Object.assign(records[0], mutation);
    assert.throws(() => validateMatrix(records), undefined, JSON.stringify(mutation));
  }
  const unchangedTheme = validMatrix().map((r) => ({ ...r, background: 'same' }));
  assert.throws(() => validateMatrix(unchangedTheme));
});

test('scrolling requires full-document dimensions and scrolled-control capture, not clipped or skipped coverage', () => {
  const missingFull = validMatrix(); delete missingFull[0].full_size;
  assert.throws(() => validateMatrix(missingFull));
  const shortImage = validMatrix(); shortImage[0].full_size[1] = 900;
  assert.throws(() => validateMatrix(shortImage));
  const missingControls = validMatrix();
  missingControls[0].scroll_regions = [{ id: 'ASIDE', client_height: 500, scroll_height: 700 }];
  assert.throws(() => validateMatrix(missingControls));
  missingControls[0].controls_image = { path: 'controls.png', sha256: 'b'.repeat(64), width: 450, height: 700 };
  validateMatrix(missingControls);
});

test('controls send native mouse input, not synthetic DOM or semantic control calls', async () => {
  const events = [];
  const win = { isDestroyed: () => false, webContents: {
    executeJavaScript: async (source) => {
      assert.ok(source.includes('elementFromPoint')); assert.ok(source.includes('e.disabled'));
      assert.ok(!source.includes('.click()')); return { ready:true, x: 50, y: 60 };
    },
    sendInputEvent: (event) => events.push(event),
  } };
  await click(win, '#next');
  assert.deepEqual(events.map((e) => e.type), ['mouseMove', 'mouseDown', 'mouseUp']);
  assert.equal(events[1].button, 'left'); assert.equal(events[2].x, 50);
});

test('same-source certified rebuild needs a new build time, not a changed generation', () => {
  const before = { project: 'TESTPLATE', hash: 'a'.repeat(16), built_at: 10 };
  const model = { tier: 'certified', hash: before.hash, gates: { pass: true },
    index: { project: before.project, built_at: 11, gen: 0 } };
  assert.equal(certifiedRebuildComplete(model, before), true);
  for (const mutation of [
    { index: { ...model.index, built_at: 10 } }, { index: { ...model.index, built_at: '11' } },
    { tier: 'preview' }, { gates: { pass: false } }, { hash: 'b'.repeat(16) },
    { index: { ...model.index, project: 'OTHER' } },
  ]) assert.equal(certifiedRebuildComplete({ ...model, ...mutation }, before), false);
});

test('projection uses every actual visible world-space vertex, excluding hidden ancestors', async () => {
  const data = (source) => 'data:text/javascript;base64,' + Buffer.from(source).toString('base64');
  const base = path.join(__dirname, '../viewer/vendor');
  const core = data(fs.readFileSync(path.join(base, 'three.core.js'), 'utf8'));
  const three = await import(data(fs.readFileSync(path.join(base, 'three.module.js'), 'utf8').replaceAll("'./three.core.js'", JSON.stringify(core))));
  const group = new three.Group(), camera = new three.PerspectiveCamera(40, 1, 0.1, 100);
  camera.position.set(0, 0, 10); camera.lookAt(0, 0, 0);
  const geometry = new three.BufferGeometry();
  geometry.setAttribute('position', new three.Float32BufferAttribute([0, 0, 0, 0.1, 0, 0, 0, 0.1, 0], 3));
  const mesh = new three.Mesh(geometry, new three.MeshBasicMaterial()); group.add(mesh);
  const hidden = new three.Group(); hidden.visible = false;
  const hiddenMesh = mesh.clone(); hiddenMesh.position.x = 1000; hidden.add(hiddenMesh); group.add(hidden);
  const rect = { left: 0, top: 0, right: 100, bottom: 100, width: 100, height: 100 };
  const canvas = { width: 100, height: 100, getBoundingClientRect: () => rect };
  const globals = { window: { __cq: { model: { group, tier: 'certified', gates: { pass: true }, hash: 'hash' }, camera } },
    document: { body: {}, documentElement: { scrollWidth: 100, scrollHeight: 100 },
      querySelectorAll: () => [], querySelector: () => canvas,
      getElementById: (id) => ({ textContent: id === 'explodeval' ? '0%' : '' }) },
    innerWidth: 100, innerHeight: 100, devicePixelRatio: 1, Node: { TEXT_NODE: 3 },
    getComputedStyle: () => ({ backgroundColor: 'dark' }), matchMedia: () => ({ matches: true }) };
  const vm = require('node:vm');
  const measure = () => vm.runInNewContext(`(${measureFrame.toString()})()`, globals);
  assert.equal(measure().vertices, 3); assert.equal(measure().meshes, 1); assert.equal(measure().clipped, 0);
  mesh.position.x = 100;
  assert.equal(measure().clipped, 3, 'must project actual translated geometry, not just local bounding box');
  mesh.visible = false;
  assert.equal(measure().vertices, 0, 'hidden geometry cannot count as coverage');
  geometry.dispose(); mesh.material.dispose();
});

async function pinnedThree() {
  const data = (s) => 'data:text/javascript;base64,' + Buffer.from(s).toString('base64');
  const base = path.join(__dirname, '../viewer/vendor');
  return import(data(fs.readFileSync(path.join(base, 'three.core.js'), 'utf8')));
}
function measureScene(three, geometry, material = new three.MeshBasicMaterial()) {
  const group = new three.Group(), mesh = new three.Mesh(geometry, material);
  group.add(mesh);
  const camera = new three.PerspectiveCamera(40, 1, 0.1, 100); camera.position.set(0, 0, 10); camera.lookAt(0, 0, 0);
  const rect = { left: 0, top: 0, right: 100, bottom: 100, width: 100, height: 100 };
  const globals = { window: { __cq: { model: { group, tier: 'certified', gates: { pass: true }, hash: 'hash' }, camera } },
    document: { body: {}, documentElement: { scrollWidth: 100, scrollHeight: 100 }, querySelectorAll: () => [],
      querySelector: () => ({ width: 100, height: 100, getBoundingClientRect: () => rect }),
      getElementById: (id) => ({ textContent: id === 'explodeval' ? '0%' : '' }) },
    innerWidth: 100, innerHeight: 100, devicePixelRatio: 1, Node: { TEXT_NODE: 3 },
    getComputedStyle: () => ({ backgroundColor: 'dark' }), matchMedia: () => ({ matches: true }) };
  return { mesh, group, camera, measure: () => require('node:vm').runInNewContext(`(${measureFrame.toString()})()`, globals) };
}
test('actual Three draw ranges, materials, camera layers and collapsed triangles cannot pad coverage', async () => {
  const three = await pinnedThree();
  for (const indexed of [false, true]) {
    const geometry = new three.BufferGeometry();
    geometry.setAttribute('position', new three.Float32BufferAttribute([0,0,0, 0.1,0,0, 0,0.1,0],3));
    if (indexed) geometry.setIndex([0,1,2]);
    const fixture = measureScene(three, geometry);
    assert.equal(fixture.measure().triangles,1); assert.equal(fixture.measure().vertices,3);
    geometry.setDrawRange(0,0); assert.equal(fixture.measure().vertices,0); geometry.setDrawRange(0,Infinity);
    fixture.mesh.material.visible=false; assert.equal(fixture.measure().vertices,0); fixture.mesh.material.visible=true;
    fixture.mesh.layers.set(1); assert.equal(fixture.measure().vertices,0); fixture.mesh.layers.set(0);
    fixture.mesh.scale.x=-1; assert.equal(fixture.measure().triangles,1,'reflected front-face winding must remain eligible'); fixture.mesh.scale.x=1;
    fixture.mesh.material.side=three.BackSide; assert.equal(fixture.measure().triangles,0); fixture.mesh.material.side=three.DoubleSide;
    for(let i=0;i<3;i++) geometry.getAttribute('position').setXYZ(i,0,0,0);
    assert.equal(fixture.measure().vertices,0); assert.equal(fixture.measure().degenerate,1);
    geometry.dispose(); fixture.mesh.material.dispose();
  }
});
test('actual grouped/indexed draw intersection excludes unused vertices and rejects invalid draw indices', async () => {
  const three=await pinnedThree(), geometry=new three.BufferGeometry();
  geometry.setAttribute('position',new three.Float32BufferAttribute([0,0,0,.1,0,0,0,.1,0, .1,.1,0,.2,.1,0,.1,.2,0, 10000,10000,0],3));
  geometry.setIndex([0,1,2,3,4,5]); geometry.addGroup(0,3,0); geometry.addGroup(3,3,1);
  const materials=[new three.MeshBasicMaterial(),new three.MeshBasicMaterial()]; materials[0].visible=false;
  const fixture=measureScene(three,geometry,materials);
  assert.equal(fixture.measure().triangles,1); assert.equal(fixture.measure().clipped,0,'unused far position is not drawn coverage');
  geometry.setDrawRange(0,3); assert.equal(fixture.measure().vertices,0);
  geometry.setDrawRange(3,3); assert.equal(fixture.measure().vertices,3);
  geometry.index.setX(3,999); assert.throws(fixture.measure,/Draw index outside/);
  geometry.dispose();materials.forEach(m=>m.dispose());
});
test('scale-relative degeneracy check retains a healthy tiny triangle and matrix rejects degenerate padding', async () => {
  const three=await pinnedThree(), geometry=new three.BufferGeometry();
  geometry.setAttribute('position',new three.Float32BufferAttribute([0,0,0,1e-12,0,0,0,1e-12,0],3));
  const fixture=measureScene(three,geometry);assert.equal(fixture.measure().triangles,1);assert.equal(fixture.measure().degenerate,0);
  for(const mutation of [{triangles:0},{degenerate:1},{vertices:31}]) {const records=validMatrix();Object.assign(records[0],mutation);assert.throws(()=>validateMatrix(records));}
  geometry.dispose();fixture.mesh.material.dispose();
});
