const { app, BrowserWindow, Menu } = require('electron');
// Software rendering is opt-in for headless tests, never forced on a real Mac.
if (process.env.CASSQUERY_SOFTWARE_GL === '1') {
  app.commandLine.appendSwitch('use-angle', 'swiftshader');
  app.commandLine.appendSwitch('enable-unsafe-swiftshader');
}
const fs = require('fs');
const path = require('path');
const { startingPort, workerCommand, startWorker, stopWorker } = require('./runtime.cjs');

const REPO = path.join(__dirname, '..');
let worker = null;
let window = null;
let workerPort = null;
let tried = { python: null, worker: null, exit: null, stderr: '' };
let opening = null;
let quitting = false;
let shutdownComplete = false;
const startup = new AbortController();

async function ensureWorker() {
  const port = startingPort(process.env.CASSQUERY_PORT);
  const command = workerCommand({ packaged: app.isPackaged, resources: process.resourcesPath,
    repo: REPO, platform: process.platform, env: process.env });
  const result = await startWorker({ command, port,
    dataDir: process.env.CASSQUERY_DATA_DIR || path.join(app.getPath('userData'), 'worker'),
    signal: startup.signal });
  if (quitting) {
    await stopWorker(result.child);
    return false;
  }
  worker = result.child;
  workerPort = result.port;
  tried = result.diagnostic;
  if (worker) worker.on('exit', () => {
    worker = null;
    workerPort = null;
    if (!quitting && window && !window.isDestroyed()) window.loadURL(errorPage()).catch(console.error);
  });
  return workerPort !== null;
}

function errorPage() {
  const esc = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;');
  const exit = tried.exit !== null && tried.exit !== undefined
    ? `<p>The worker exited with code ${tried.exit} before answering.</p>` : '';
  const stderr = tried.stderr
    ? `<p>Last of its stderr:</p><pre>${esc(tried.stderr)}</pre>` : '';
  // Same tokens as viewer/index.html, declared here because this page is a
  // self-contained data URL with no stylesheet of its own.
  const html = `<!doctype html><meta charset="utf-8"><title>CassQuery — worker did not start</title>
<style>
  :root {
    --bg: #131315; --panel: #1c1c1f; --panel-2: #232327; --ink: #f0ede6;
    --muted: #a7a49a; --accent: #c9a86a; --line: #2e2e33; --radius: 10px;
    --font-ui: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    --font-display: "Iowan Old Style", Palatino, "Palatino Linotype", Georgia, serif;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; }
  body {
    background: var(--bg); color: var(--ink); font-family: var(--font-ui);
    line-height: 1.55; padding: 48px 24px;
  }
  main { max-width: 640px; margin: 0 auto; display: flex; flex-direction: column; gap: 12px; }
  h1 { font-family: var(--font-display); font-weight: 600; font-size: 22px; margin: 0; }
  p { margin: 0; }
  ul { margin: 0; padding-left: 20px; }
  code {
    background: var(--panel-2); border: 1px solid var(--line); border-radius: 6px;
    padding: 1px 6px; font-size: 13px; overflow-wrap: break-word;
  }
  pre {
    background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius);
    padding: 12px; font-size: 12px; white-space: pre-wrap; overflow-wrap: break-word; margin: 0;
  }
  .muted { color: var(--muted); }
</style>
<body><main>
  <h1>The CassQuery worker did not start</h1>
<p class="muted">Tried Python: <code>${esc(tried.python)}</code><br>Worker: <code>${esc(tried.worker)}</code></p>
${exit}${stderr}
<p>Fix it one of two ways:</p>
<ul>
<li>Set <code>CASSQUERY_PYTHON</code> to a Python with CadQuery 2.5+ and websockets 13+ installed, then relaunch.</li>
<li>Check that the packaged app is complete, or run <code>npm run app</code> from a development checkout with its virtualenv.</li>
</ul>
</main></body>`;
  return 'data:text/html;charset=utf-8,' + encodeURIComponent(html);
}

function openWindow() {
  if (opening) return opening;
  opening = createWindow().finally(() => { opening = null; });
  return opening;
}

async function createWindow() {
  if (window && !window.isDestroyed()) { window.show(); return; }
  if (quitting) return;
  const up = workerPort !== null || await ensureWorker();
  if (quitting) return;
  const win = new BrowserWindow({ width: 1320, height: 880, title: 'CassQuery',
    webPreferences: { nodeIntegration: false, contextIsolation: true, sandbox: true } });
  window = win;
  win.on('closed', () => { window = null; });
  const origin = `http://127.0.0.1:${workerPort}`;
  win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  win.webContents.on('will-navigate', (event, url) => {
    if (!url.startsWith(`${origin}/`)) event.preventDefault();
  });
  win.webContents.session.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  if (process.env.CASSQUERY_SMOKE_DIR) {
    win.webContents.once('did-finish-load', async () => {
      const output = process.env.CASSQUERY_SMOKE_DIR;
      let report;
      try { report = await require('./smoke.cjs').smoke(win, output); }
      catch (error) { report = { pass: false, error: error.stack || String(error) }; }
      fs.mkdirSync(output, { recursive: true });
      report.worker_pid = worker?.pid;
      report.app_pid = process.pid;
      report.challenge = process.env.CASSQUERY_SMOKE_CHALLENGE;
      fs.writeFileSync(path.join(output, 'renderer.json'), JSON.stringify(report, null, 2));
      quitting = true;
      startup.abort();
      await stopWorker(worker);
      app.exit(report.pass ? 0 : 1);
    });
  }
  const shot = process.env.CASSQUERY_SHOT;
  if (shot) {
    let scheduled = false;
    const schedule = () => {
      if (scheduled) return;
      scheduled = true;
      const delay = parseInt(process.env.CASSQUERY_SHOT_DELAY || '6000', 10);
      setTimeout(async () => {
        try {
          const img = await win.webContents.capturePage();
          fs.mkdirSync(path.dirname(shot), { recursive: true });
          fs.writeFileSync(shot, img.toPNG());
        } finally {
          app.quit();
        }
      }, delay);
    };
    win.webContents.once('did-finish-load', schedule);
    win.webContents.once('did-fail-load', schedule);
    setTimeout(schedule, 15000); // shortcut: error pages may fire neither reliably, screenshot whatever is shown
  }
  win.loadURL(up ? `${origin}/` : errorPage()).catch((error) => {
    tried.stderr = error.message;
    win.loadURL(errorPage()).catch(console.error);
  });
}

if (process.env.CASSQUERY_USER_DATA) app.setPath('userData', process.env.CASSQUERY_USER_DATA);
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => { if (window) { window.show(); window.focus(); } });
  app.whenReady().then(() => {
    Menu.setApplicationMenu(Menu.buildFromTemplate([
      ...(process.platform === 'darwin' ? [{ role: 'appMenu' }] : []),
      { role: 'editMenu' }, { role: 'viewMenu' }, { role: 'windowMenu' },
    ]));
    return openWindow();
  }).catch((error) => {
    tried.stderr = error.message;
    console.error(error);
    app.quit();
  });
  app.on('activate', () => openWindow().catch(console.error));
}
app.on('window-all-closed', () => { if (process.platform !== 'darwin') app.quit(); });
app.on('before-quit', (event) => {
  if (shutdownComplete) return;
  event.preventDefault();
  if (quitting) return;
  quitting = true;
  startup.abort();
  Promise.resolve(opening).then(() => stopWorker(worker)).then(() => {
    shutdownComplete = true;
    app.quit();
  }).catch((error) => { console.error(error); app.exit(1); });
});
