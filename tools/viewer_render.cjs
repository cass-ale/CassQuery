'use strict';
// Real browser + committed public geometry. Not a substitute for live CAD or native app gates.
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium } = require('playwright-core');
const root = path.resolve(__dirname, '..');
const output = path.resolve(process.env.CASSQUERY_RENDER_OUTPUT || path.join(root, 'evidence/browser'));
const executable = process.env.CASSQUERY_BROWSER;
if (!executable) throw new Error('Set CASSQUERY_BROWSER to a Chrome/Chromium executable; no browser is downloaded automatically.');
fs.mkdirSync(output, { recursive: true });
const index = JSON.parse(fs.readFileSync(path.join(root, 'payloads/TESTPLATE-certified.json')));
const status = { name: 'TESTPLATE', root: '/public/demo', state: 'certified', live: true,
  hashes: { certified: index.hash }, gates_pass: true, history: [] };
const server = http.createServer((request, response) => {
  try {
    const url = new URL(request.url, 'http://localhost');
    let data, type = 'application/json';
    if (url.pathname === '/api/projects') data = JSON.stringify([status]);
    else if (url.pathname === '/api/status') data = JSON.stringify({ api_version: 2, projects: [status], load_errors: [] });
    else if (url.pathname === '/api/manifest') data = fs.readFileSync(path.join(root, 'testpart/viewer.json'));
    else if (url.pathname.startsWith('/payload/')) data = fs.readFileSync(path.join(root, 'payloads', path.basename(url.pathname)));
    else {
      const rel = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
      const file = path.resolve(root, 'viewer', rel);
      if (!file.startsWith(path.resolve(root, 'viewer') + '/')) throw new Error('outside fixture');
      data = fs.readFileSync(file);
      type = file.endsWith('.html') ? 'text/html' : file.endsWith('.css') ? 'text/css'
        : file.endsWith('.svg') ? 'image/svg+xml' : 'text/javascript';
    }
    response.writeHead(200, { 'Content-Type': type, 'Cache-Control': 'no-store' }); response.end(data);
  } catch { response.writeHead(404); response.end(); }
});

(async () => {
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({ executablePath: executable, headless: true,
    args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
  const results = [];
  try {
    for (const theme of ['dark', 'light']) for (const width of [450, 700, 1000, 1320]) {
      const context = await browser.newContext({ viewport: { width, height: 900 }, colorScheme: theme,
        reducedMotion: 'reduce', deviceScaleFactor: 1 });
      const page = await context.newPage(), errors = [];
      page.on('pageerror', (error) => errors.push(error.message));
      await page.goto(origin);
      await page.waitForFunction(() => window.__cq?.model?.tier === 'certified', null, { timeout: 30000 });
      const states = [];
      for (const [i, name] of [[0, 'assembled'], [1, 'apart']]) {
        await page.locator('#steps button').nth(i).click();
        await page.waitForFunction((expected) => document.getElementById('explodeval').textContent === expected,
          i ? '100%' : '0%');
        await page.screenshot({ path: path.join(output, `${theme}-${width}-${name}.png`), fullPage: true });
        const layout = await page.evaluate(() => {
          const bad = [...document.querySelectorAll('button, .badge, h1, .caption, label')].filter((element) => {
            const r = element.getBoundingClientRect();
            return r.width > 0 && (r.left < -1 || r.right > innerWidth + 1 || element.scrollWidth > element.clientWidth + 2);
          }).map((element) => ({ id: element.id, text: element.textContent }));
          let clipped = 0, vertices = 0;
          const { model, camera } = window.__cq;
          model.group.updateMatrixWorld(true);
          camera.updateMatrixWorld(true);
          const vector = camera.position.clone();
          model.group.traverse((object) => {
            if (!object.isMesh || !object.visible) return;
            const position = object.geometry.getAttribute('position');
            for (let j = 0; j < position.count; j++) {
              vector.fromBufferAttribute(position, j).applyMatrix4(object.matrixWorld).project(camera);
              vertices++;
              if (Math.abs(vector.x) > 1.001 || Math.abs(vector.y) > 1.001 || Math.abs(vector.z) > 1.001) clipped++;
            }
          });
          return { viewport: [innerWidth, innerHeight], overflow: document.documentElement.scrollWidth > innerWidth,
            bad, clipped, vertices, badge: document.getElementById('tierbadge').textContent };
        });
        states.push({ state: name, ...layout });
      }
      results.push({ theme, width, errors, states });
      await context.close();
    }
  } finally { await browser.close(); server.close(); }
  const pass = results.every((result) => !result.errors.length && result.states.every((state) =>
    !state.overflow && !state.bad.length && !state.clipped && state.vertices > 0));
  const report = { pass, method: 'real headless browser, public fixture, actual DOM step clicks',
    not_checked: ['connected worker', 'native packaged app', 'live CAD rebuild'], results };
  fs.writeFileSync(path.join(output, 'render-results.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report, null, 2));
  if (!pass) process.exitCode = 1;
})().catch((error) => { console.error(error); server.close(); process.exitCode = 1; });
