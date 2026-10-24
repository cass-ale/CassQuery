const { app, BrowserWindow, Menu, nativeTheme } = require('electron');
// Software rendering is opt-in for headless tests, never forced on a real Mac.
if (process.env.CASSQUERY_SOFTWARE_GL === '1') {
  app.commandLine.appendSwitch('use-angle', 'swiftshader');
  app.commandLine.appendSwitch('enable-unsafe-swiftshader');
}
const fs = require('fs');
const path = require('path');
const { startingPort, workerCommand, startWorker, stopWorker } = require('./runtime.cjs');

const REPO = path.join(__dirname, '..');
const smokeStartedAt = { wall_time_ms: Date.now(), monotonic_ms: Number(process.hrtime.bigint()) / 1e6 };
let smokeStarted = false;
let secondInstances = 0;
let pendingSmokeReport = null;
const rendererErrors = [];
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
    env: app.isPackaged ? { ...process.env, PYTHONDONTWRITEBYTECODE: '1' } : process.env,
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
    win.webContents.on('console-message', (_event, details) => {
      if (details?.level === 'error' || details?.level === 3) rendererErrors.push(details.message);
    });
    win.webContents.on('render-process-gone', (_event, details) => rendererErrors.push(`renderer gone: ${details.reason}`));
    win.webContents.on('did-fail-load', (_event, code, description) => rendererErrors.push(`load ${code}: ${description}`));
    win.webContents.once('did-finish-load', () => {
      if (!smokeStarted) { smokeStarted = true; runNativeSmoke(win).catch((error) => { console.error(error); app.exit(1); }); }
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

async function runNativeSmoke(win) {
  const { spawn } = require('node:child_process');
  const { smoke, waitFor, checkpoint } = require('./smoke.cjs');
  const output = process.env.CASSQUERY_SMOKE_DIR;
  const report = { schema: 1, pass: false, checkpoints: [], errors: [] };
  const ownedWorker = worker;
  try {
    if (!app.isPackaged) throw new Error('Native assurance requires the packaged Electron app');
    await waitFor(win, 'window.__cq?.model?.tier === "certified" && window.__cq.model.gates?.pass');
    const hash = await win.webContents.executeJavaScript('__cq.model.hash');
    const beforeSecond = secondInstances;
    const secondary = spawn(process.execPath, [], { env: process.env, stdio: 'ignore' });
    const secondaryExit = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => { secondary.kill('SIGKILL'); reject(new Error('Second instance did not exit')); }, 15000);
      secondary.once('error', (error) => { clearTimeout(timer); reject(error); });
      secondary.once('exit', (code) => { clearTimeout(timer); resolve(code); });
    });
    if (secondaryExit !== 0 || secondInstances !== beforeSecond + 1 || worker !== ownedWorker)
      throw new Error('Single-instance process/worker ownership failed');
    win.close();
    if (!win.isDestroyed()) throw new Error('Native close did not destroy the window');
    if (worker !== ownedWorker || worker.exitCode !== null) throw new Error('Closing Mac window killed warm worker');
    app.emit('activate');
    await Promise.resolve(opening);
    const reopened = window;
    if (!reopened || reopened === win || reopened.isDestroyed()) throw new Error('Activation did not reopen the window');
    await waitFor(reopened, `window.__cq?.model?.tier === 'certified' && window.__cq.model.hash === ${JSON.stringify(hash)}`);
    if (worker !== ownedWorker) throw new Error('Reopen replaced warm worker');
    const lifecycle = { pass: true, secondary_pid: secondary.pid, secondary_exit: secondaryExit,
      second_instance_events: secondInstances - beforeSecond, close_reopen: true,
      worker_pid_before: ownedWorker.pid, worker_pid_after: worker.pid,
      method: 'real secondary process; BrowserWindow.close; production activate handler; same live worker' };
    Object.assign(report, await smoke(reopened, output, { nativeTheme, errors: rendererErrors, evidence: report }));
    report.lifecycle = lifecycle;
  } catch (error) { report.pass = false; report.error = error.stack || String(error); }
  report.schema = 1;
  report.worker_pid = ownedWorker?.pid;
  report.app_pid = process.pid;
  report.challenge = process.env.CASSQUERY_SMOKE_CHALLENGE;
  report.packaged = app.isPackaged;
  report.bundled_worker = tried.python === path.join(process.resourcesPath, 'worker', 'cassquery-worker');
  report.worker_executable = tried.python;
  report.resources_path = process.resourcesPath;
  report.version = app.getVersion();
  report.architecture = process.arch;
  report.source_sha = process.env.GITHUB_SHA || null;
  report.started = smokeStartedAt;
  report.errors = [...rendererErrors];
  // Exercise normal before-quit shutdown. Successful smoke never calls app.exit.
  pendingSmokeReport = { report, output };
  app.quit();
}

function finishSmokeReport(shutdownError = null) {
  if (!pendingSmokeReport) return;
  const { checkpoint } = require('./smoke.cjs');
  const { report, output } = pendingSmokeReport;
  report.errors = [...rendererErrors];
  if (report.errors.length) { report.pass = false; report.error = 'Renderer diagnostics through shutdown'; }
  report.worker_shutdown = !shutdownError;
  if (shutdownError) { report.pass = false; report.error = String(shutdownError); }
  report.checkpoints.push(checkpoint('worker-shutdown'));
  report.finished = checkpoint('app-complete');
  fs.mkdirSync(output, { recursive: true });
  const file = path.join(output, 'renderer-raw.json'), temporary = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(temporary, JSON.stringify(report, null, 2));
  fs.renameSync(temporary, file);
}

if (process.env.CASSQUERY_USER_DATA) app.setPath('userData', process.env.CASSQUERY_USER_DATA);
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => { secondInstances++; if (window) { window.show(); window.focus(); } });
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
    finishSmokeReport();
    if (pendingSmokeReport?.report.pass === false) app.exit(1);
    else app.quit();
  }).catch((error) => { finishSmokeReport(error); console.error(error); app.exit(1); });
});
