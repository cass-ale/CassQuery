const { app, BrowserWindow } = require('electron');
const fs = require('fs');
const http = require('http');
const os = require('os');
const path = require('path');
const { spawn } = require('child_process');

const HOME = os.homedir();
let worker = null;

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
  if (await checkStatus(port)) return;
  const python = process.env.CASSQUERY_PYTHON || path.join(HOME, 'workspace/.venv-cadquery/bin/python');
  const projects = [
    path.join(HOME, 'workspace/goals/bard-ring-design'),
    path.join(HOME, 'workspace/goals/ifrit-ring-design/repo'),
    path.join(HOME, 'workspace/cassquery/testpart'),
  ].join(path.delimiter);
  worker = spawn(python, [path.join(HOME, 'workspace/cassquery/worker.py')], {
    env: { ...process.env, CASSQUERY_PROJECTS: projects },
    detached: false,
    stdio: 'inherit',
  });
  worker.on('error', () => { worker = null; });
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    if (await checkStatus(port)) return;
    await new Promise((r) => setTimeout(r, 500));
  }
}

app.whenReady().then(async () => {
  const port = getPort();
  await ensureWorker(port);
  const win = new BrowserWindow({ width: 1320, height: 880 });
  const shot = process.env.CASSQUERY_SHOT;
  if (shot) {
    let scheduled = false;
    const schedule = () => {
      if (scheduled) return;
      scheduled = true;
      setTimeout(async () => {
        try {
          const img = await win.webContents.capturePage();
          fs.mkdirSync(path.dirname(shot), { recursive: true });
          fs.writeFileSync(shot, img.toPNG());
        } finally {
          app.quit();
        }
      }, 6000);
    };
    win.webContents.once('did-finish-load', schedule);
    win.webContents.once('did-fail-load', schedule);
    setTimeout(schedule, 15000); // shortcut: error pages may fire neither reliably, screenshot whatever is shown
  }
  win.loadURL(`http://127.0.0.1:${port}/`).catch(() => {});
});

app.on('will-quit', () => {
  if (worker) { try { worker.kill(); } catch {} worker = null; }
});
