const { app, BrowserWindow } = require('electron');
// software GL where no GPU exists (headless VMs); harmless on real hardware
app.commandLine.appendSwitch('use-angle', 'swiftshader');
app.commandLine.appendSwitch('enable-unsafe-swiftshader');
const fs = require('fs');
const http = require('http');
const os = require('os');
const path = require('path');
const { spawn } = require('child_process');

const HOME = os.homedir();
const REPO = path.join(__dirname, '..');
const WORKER = path.join(REPO, 'worker.py');
let worker = null;
let tried = { python: null, worker: WORKER, exit: null, stderr: '' };

function findPython() {
  if (process.env.CASSQUERY_PYTHON) return process.env.CASSQUERY_PYTHON;
  const venv = process.platform === 'win32'
    ? path.join(REPO, '.venv', 'Scripts', 'python.exe')
    : path.join(REPO, '.venv', 'bin', 'python');
  if (fs.existsSync(venv)) return venv;
  return process.platform === 'win32' ? 'python' : 'python3';
}

function getPort() {
  if (process.env.CASSQUERY_PORT) return parseInt(process.env.CASSQUERY_PORT, 10);
  try {
    const reg = JSON.parse(fs.readFileSync(path.join(HOME, '.cassquery.json'), 'utf8'));
    if (reg.port) return parseInt(reg.port, 10);
  } catch {}
  return 3939;
}

function checkStatus(port, timeoutMs = 1500) {
  return new Promise((resolve) => {
    const req = http.get(`http://127.0.0.1:${port}/api/status`, (res) => {
      res.resume();
      resolve(res.statusCode === 200);
    });
    req.setTimeout(timeoutMs, () => { req.destroy(); resolve(false); });
    req.on('error', () => resolve(false));
  });
}

async function ensureWorker(port) {
  if (await checkStatus(port)) return true;
  const python = findPython();
  tried = { python, worker: WORKER, exit: null, stderr: '' };
  const env = { ...process.env };
  if (!env.CASSQUERY_PROJECTS) env.CASSQUERY_PROJECTS = path.join(REPO, 'testpart');
  let spawnFailed = false;
  worker = spawn(python, [WORKER], { env, detached: false, stdio: ['ignore', 'pipe', 'pipe'] });
  worker.stdout.on('data', (d) => process.stdout.write(d));
  worker.stderr.on('data', (d) => {
    process.stderr.write(d);
    tried.stderr = (tried.stderr + d).slice(-2000);
  });
  worker.on('error', () => { spawnFailed = true; worker = null; });
  // A worker that exits at once (e.g. the dependency preflight) must not
  // leave the shell waiting out the full deadline for a port that will
  // never answer.
  worker.on('exit', (code) => { tried.exit = code; });
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline && !spawnFailed && tried.exit === null) {
    if (await checkStatus(port)) return true;
    await new Promise((r) => setTimeout(r, 500));
  }
  if (spawnFailed || tried.exit !== null) return false;
  return checkStatus(port);
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
<li>Set <code>CASSQUERY_PYTHON</code> to a Python with CadQuery 2.5+ and websockets installed, then relaunch.</li>
<li>Start the worker yourself in a terminal — <code>${esc(tried.python)} ${esc(tried.worker)}</code> — then relaunch.</li>
</ul>
</main></body>`;
  return 'data:text/html;charset=utf-8,' + encodeURIComponent(html);
}

app.whenReady().then(async () => {
  const port = getPort();
  const up = await ensureWorker(port);
  const win = new BrowserWindow({ width: 1320, height: 880 });
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
  win.loadURL(up ? `http://127.0.0.1:${port}/` : errorPage()).catch(() => {});
});

app.on('will-quit', () => {
  if (worker) { try { worker.kill(); } catch {} worker = null; }
});
