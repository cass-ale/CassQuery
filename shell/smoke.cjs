'use strict';
// Opt-in, deterministic checks against the actual Electron renderer, not mocked DOM.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

async function waitFor(win, expression, timeoutMs = 60000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (win.isDestroyed()) throw new Error('Window closed during smoke test');
    if (await win.webContents.executeJavaScript(expression)) return;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`Renderer timed out: ${expression}`);
}

async function smoke(win, directory) {
  fs.mkdirSync(directory, { recursive: true });
  const js = (code) => win.webContents.executeJavaScript(code);
  await waitFor(win, '!!(window.__cq?.model?.tier === "certified" && window.__cq.model.gates?.pass && window.__cq.model.activeName)');
  const initial = await js('JSON.stringify({hash:__cq.model.hash, tier:__cq.model.tier, variants:[...__cq.model.variants.keys()], badge:document.getElementById("tierbadge").textContent})');
  assert.equal(JSON.parse(initial).tier, 'certified');
  const shots = [];
  for (const [index, name] of [[0, 'assembled'], [1, 'apart']]) {
    await js(`document.querySelectorAll('#steps button')[${index}].click()`);
    await waitFor(win, index ? 'document.getElementById("explodeval").textContent === "100%"'
      : 'document.getElementById("explodeval").textContent === "0%"');
    await new Promise((resolve) => setTimeout(resolve, 700));
    const file = path.join(directory, `${name}.png`);
    fs.writeFileSync(file, (await win.webContents.capturePage()).toPNG());
    shots.push(file);
  }
  // User viewpoint survives a rebuild; save-triggered preview is tested by Python driver.
  await js('document.getElementById("resetview").click()');
  await new Promise((resolve) => setTimeout(resolve, 700));
  const diag = await js('document.getElementById("diag").textContent');
  assert.equal(diag, '');
  const project = process.env.CASSQUERY_PROJECTS;
  assert.ok(project && !project.includes(path.delimiter), 'smoke requires one scratch demo project');
  const beforeDirection = await js('JSON.stringify(__cq.camera.position.clone().sub(__cq.controls.target).normalize().toArray())');
  const entry = path.join(project, 'part_entry.py');
  const beforeHash = await js('__cq.model.hash');
  fs.writeFileSync(entry, fs.readFileSync(entry, 'utf8').replace('.circle(4)', '.circle(5)'));
  await waitFor(win, `__cq.model?.tier === 'certified' && __cq.model.hash !== ${JSON.stringify(beforeHash)}`);
  const afterDirection = await js('JSON.stringify(__cq.camera.position.clone().sub(__cq.controls.target).normalize().toArray())');
  const a = JSON.parse(beforeDirection), b = JSON.parse(afterDirection);
  assert.ok(a.reduce((sum, value, i) => sum + value * b[i], 0) > 0.9999, 'rebuild stole camera direction');
  const goodHash = await js('__cq.model.hash');
  const manifestPath = path.join(project, 'viewer.json');
  const config = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
  config.unit_scale = 2;
  fs.writeFileSync(manifestPath, JSON.stringify(config));
  await waitFor(win, `document.getElementById('tierbadge').textContent.includes('gate FAILED') && __cq.model?.hash === ${JSON.stringify(goodHash)} && __cq.model.tier === 'certified'`);
  return { pass: true, initial: JSON.parse(initial), screenshots: shots,
    checks: ['certified demo', 'gate pass', 'assembled step', 'exploded step', 'reset view',
      'save updates rendered model', 'camera preserved on rebuild', 'failed gate restores certified model', 'no renderer diagnostics'] };
}
module.exports = { smoke };
