'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');

async function rendererModules() {
  const base = path.join(__dirname, '../viewer/vendor');
  // Import the exact vendored ESM modules without changing the repository's CJS contract.
  const data = (source) => 'data:text/javascript;base64,' + Buffer.from(source).toString('base64');
  const coreURL = data(fs.readFileSync(path.join(base, 'three.core.js'), 'utf8'));
  const moduleURL = data(fs.readFileSync(path.join(base, 'three.module.js'), 'utf8').replaceAll("'./three.core.js'", JSON.stringify(coreURL)));
  const three = await import(moduleURL);
  const utilsURL = data(fs.readFileSync(path.join(base, 'addons/utils/BufferGeometryUtils.js'), 'utf8').replace("from 'three'", 'from ' + JSON.stringify(moduleURL)));
  return { three, utils: await import(utilsURL) };
}

test('creased CAD normals preserve sharp box planes without faceting a round post', async () => {
  const { three, utils } = await rendererModules();
  const payload = JSON.parse(fs.readFileSync(path.join(__dirname, '../payloads/TESTPLATE-certified-neutral.json')));
  for (const part of payload.parts) {
    const solid = part.solids[0];
    // Decode through an exact byte copy; Buffer backing stores may be offset/aligned.
    const bytes = Uint8Array.from(Buffer.from(solid.positions, 'base64'));
    const quantized = new Uint16Array(bytes.buffer);
    const positions = Float32Array.from(quantized, (value, i) => solid.qoffset[i % 3] + value * solid.qscale);
    const ib = Uint8Array.from(Buffer.from(solid.indices, 'base64'));
    const indices = solid.itype === 'u16' ? new Uint16Array(ib.buffer) : new Uint32Array(ib.buffer);
    const original = new three.BufferGeometry();
    original.setAttribute('position', new three.BufferAttribute(positions, 3));
    original.setIndex(new three.BufferAttribute(indices, 1));
    const geometry = utils.toCreasedNormals(original, Math.PI / 3);
    assert.equal(geometry.index, null);
    const p = geometry.getAttribute('position'), n = geometry.getAttribute('normal');
    let planes = 0, curved = 0;
    for (let i = 0; i < p.count; i += 3) {
      const a = new three.Vector3().fromBufferAttribute(p, i);
      const b = new three.Vector3().fromBufferAttribute(p, i + 1);
      const c = new three.Vector3().fromBufferAttribute(p, i + 2);
      const face = c.clone().sub(b).cross(a.clone().sub(b)).normalize();
      for (let j = 0; j < 3; j++) {
        const normal = new three.Vector3().fromBufferAttribute(n, i + j);
        assert.ok(Math.abs(normal.length() - 1) < 1e-5);
        if (Math.abs(face.z) > 0.99999) {
          assert.ok(normal.dot(face) > 0.99999, `${part.name} planar CAD face was rounded`);
          planes++;
        } else if (part.name === 'Post') {
          const point = new three.Vector3().fromBufferAttribute(p, i + j);
          const radial = new three.Vector3(point.x, point.y, 0).normalize();
          assert.ok(normal.dot(radial) > 0.98, 'cylinder side must remain smoothly radial');
          curved++;
        }
      }
    }
    assert.ok(planes > 0);
    if (part.name === 'Post') assert.ok(curved > 0);
    geometry.dispose(); original.dispose();
  }
});
