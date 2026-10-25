'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const { validateMatrix, click, measureFrame, certifiedRebuildComplete, scrollOffsets, validateScrollCoverage, captureScrollRegions, MAX_SCROLL_CAPTURES, WIDTHS, THEMES, STATES } = require('../shell/smoke.cjs');

function validMatrix() {
  return THEMES.flatMap((theme) => WIDTHS.flatMap((width) => STATES.map((state) => ({
    theme, realized_theme: theme, background: theme, width, state, status: 'checked', viewport: [width, 700],
    full_size: [width, 700], scroll_regions: [], scroll_captures: [], vertical_scroll: false, dpr:1, hash:'a'.repeat(16),
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
  missingControls[0].scroll_regions = [{ id: 'ASIDE', client_height: 500, scroll_height: 700, scroll_top:0 }];
  assert.throws(() => validateMatrix(missingControls));
  const complete = scrollMatrix(500,700);
  validateMatrix(complete);
});

function scrollFrame(row, offset) {
  const keys=['viewport','full_size','theme','background','hash','tier','gates_pass','step_pressed','explode','vertices','meshes','triangles','clipped','nonfinite','degenerate','overflow','bad','overlaps','diag','canvas','dpr','vertical_scroll','scroll_regions','errors'];
  const frame=JSON.parse(JSON.stringify(Object.fromEntries(keys.map(key=>[key,row[key]]))));
  frame.scroll_regions=frame.scroll_regions.map(region=>({...region,scroll_top:offset}));
  return frame;
}
function scrollMatrix(clientHeight=201,scrollHeight=831) {
  const rows=validMatrix(),row=rows[0];row.scroll_regions=[{id:'ASIDE',client_height:clientHeight,scroll_height:scrollHeight,scroll_top:0,viewport_rect:[0,0,row.width,clientHeight],visible_rect:[0,0,row.width,clientHeight]}];
  row.scroll_captures=scrollOffsets(clientHeight,scrollHeight).map((offset,index)=>({id:'ASIDE',scroll_top:offset,client_height:clientHeight,scroll_height:scrollHeight,
    image:index?{path:`${row.theme}-${row.width}-${row.state}-controls-${index}.png`,sha256:'b'.repeat(64),width:row.width,height:700}:row.image,frame:scrollFrame(row,offset)}));
  return rows;
}
test('actual201/831 scrolling needs five ordered gapless captures and rejects fixed top/bottom shortcuts',()=>{
  assert.deepEqual(scrollOffsets(201,831),[0,180,360,540,630]);const rows=scrollMatrix();validateMatrix(rows);
  for(const mutate of [r=>r.scroll_captures.splice(1,3),r=>r.scroll_captures.splice(2,1),r=>r.scroll_captures[2].scroll_top++,r=>r.scroll_captures[0].scroll_top=1,
    r=>r.scroll_captures.at(-1).scroll_top--,r=>r.scroll_captures[2].image.path=r.scroll_captures[1].image.path,r=>r.scroll_captures[2].id='OTHER',
    r=>r.scroll_regions.push({...r.scroll_regions[0]}),r=>r.scroll_captures[2].client_height++,r=>r.scroll_captures[2].scroll_height++,
    r=>r.scroll_captures[1].frame.scroll_regions[0].scroll_top=0,r=>r.scroll_captures[1].frame.clipped=1,r=>r.scroll_captures[1].frame.hash='c'.repeat(16),
    r=>r.scroll_captures[1].frame.theme='light',r=>r.scroll_captures[1].frame.errors=['pageerror'],r=>r.scroll_captures[1].frame.triangles=0,
    r=>r.scroll_captures[1].frame.canvas.width++,r=>r.scroll_captures[1].frame.bad=[{category:'clipped'}],r=>r.scroll_captures[1].image.height=0,
    r=>r.scroll_captures[0].image={...r.image,sha256:'c'.repeat(64)},r=>delete r.scroll_captures[1].frame,
    r=>{delete r.hash;r.scroll_captures.forEach(c=>delete c.frame.hash);},r=>{delete r.dpr;r.scroll_captures.forEach(c=>delete c.frame.dpr);},
    r=>r.scroll_captures[1].frame.dpr=Infinity,r=>r.dpr=0,r=>r.dpr=17,r=>r.hash='not-canonical',
    r=>delete r.scroll_regions[0].viewport_rect,r=>r.scroll_regions[0].visible_rect[3]=100,
    r=>r.scroll_regions[0].viewport_rect[0]=-20,r=>r.scroll_captures[1].frame.scroll_regions[0].visible_rect[0]=50,
    r=>r.scroll_captures[1].frame.scroll_regions[0].viewport_rect[3]=1000,
    r=>{r.scroll_regions[0].viewport_rect=[0,0,.5,201];r.scroll_regions[0].visible_rect=[0,0,0,201];r.scroll_captures.forEach(c=>c.frame.scroll_regions=r.scroll_regions.map(item=>({...item,scroll_top:c.scroll_top})));},
    r=>{r.scroll_regions[0].viewport_rect=[0,0,100,201];r.scroll_regions[0].visible_rect=[0,0,100,0];},
    r=>r.scroll_captures[1].frame.scroll_regions[0].visible_rect=[0,0,0,201],
    r=>r.controls_image=r.image,r=>r.scroll_captures[1].frame.image=r.image,r=>r.scroll_captures[1].frame.scroll_captures=[]]){
    const mutant=scrollMatrix();mutate(mutant[0]);assert.throws(()=>validateMatrix(mutant));
  }
});
test('scroll plans remain bounded and malformed/excessive dimensions cannot be partially covered',()=>{
  assert.equal(MAX_SCROLL_CAPTURES,32);for(const pair of [[0,831],[201,201],[201,100],[-1,831],[201,Infinity],[201.5,831],[1,1000]])assert.throws(()=>scrollOffsets(...pair));
  const row=scrollMatrix()[0];row.scroll_regions[0].scroll_height=100000;assert.throws(()=>validateScrollCoverage(row));
  const blank=validMatrix()[0];delete blank.scroll_captures;assert.throws(()=>validateScrollCoverage(blank));
  const unknown=scrollMatrix()[0];unknown.scroll_regions[0].id='SCROLLER';assert.throws(()=>validateScrollCoverage(unknown));
});
test('scroll capture executes every observed offset, preserves partial images, and restores top on failure',async()=>{
  const os=require('node:os');
  for(const mode of ['healthy','clamped','changed-size','post-capture-change','capture-failure']){
    const directory=fs.mkdtempSync(path.join(os.tmpdir(),'cq-scroll-capture-'));
    const row=scrollMatrix()[0];row.image={...row.image,path:path.join(directory,path.basename(row.image.path))};row.scroll_captures=[];
    let offset=0,commands=0,detached=0;const visited=[],images=[row.image];
    const bytes=Buffer.alloc(1200);Buffer.from('89504e470d0a1a0a','hex').copy(bytes);bytes.writeUInt32BE(row.width,16);bytes.writeUInt32BE(700,20);
    const win={isDestroyed:()=>false,cassqueryImages:images,webContents:{
      executeJavaScript:async expression=>{
        const set=/document\.querySelector\('aside'\)\.scrollTop=(\d+)$/.exec(expression);
        if(set){offset=mode==='clamped'&&Number(set[1])>0?0:Number(set[1]);visited.push(offset);return offset;}
        if(expression.startsWith("document.querySelector('aside').scrollTop==="))return offset===Number(expression.split('===')[1]);
        if(expression.includes('measureFrame')){const frame=scrollFrame(row,offset);if(mode==='changed-size'&&offset>0)frame.scroll_regions[0].scroll_height++;if(mode==='post-capture-change'&&commands>0)frame.clipped=1;return frame;}
        if(expression.includes('width:innerWidth'))return{width:row.width,height:700};return true;
      },debugger:{isAttached:()=>false,attach(){},detach(){detached++;},sendCommand:async()=>{commands++;if(mode==='capture-failure'&&commands===2)throw Error('capture sentinel');return{data:bytes.toString('base64')};}},
    }};
    try{
      if(mode==='healthy'){await captureScrollRegions(win,directory,row,scrollFrame(row,0),[]);assert.deepEqual(row.scroll_captures.map(record=>record.scroll_top),[0,180,360,540,630]);assert.equal(images.length,5);validateScrollCoverage(row);assert.equal(commands,4);}
      else{const pending=captureScrollRegions(win,directory,row,scrollFrame(row,0),[]);if(mode==='clamped')setTimeout(()=>{win.isDestroyed=()=>true;},30);await assert.rejects(pending);assert.ok(row.scroll_captures.length<5);if(mode==='capture-failure')assert.equal(images.length,2,'successful first supplemental frame must survive later failure');}
      assert.equal(offset,0,'scroll must be restored even after failure');assert.equal(detached,commands);assert.equal(visited.at(-1),0);
    }finally{fs.rmSync(directory,{recursive:true,force:true});}
  }
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
