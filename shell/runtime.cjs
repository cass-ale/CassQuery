'use strict';

const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');

function startingPort(value) {
  if (value === undefined || value === '') return 3939;
  if (!/^\d+$/.test(String(value))) throw new Error('CASSQUERY_PORT must be an integer');
  const port = Number(value);
  if (port < 1 || port > 64476) throw new Error('CASSQUERY_PORT must be between 1 and 64476');
  return port;
}

function workerCommand({ packaged, resources, repo, platform, env }) {
  if (packaged && !env.CASSQUERY_PYTHON) {
    return { executable: path.join(resources, 'worker', 'cassquery-worker'), args: [] };
  }
  const venv = platform === 'win32'
    ? path.join(repo, '.venv', 'Scripts', 'python.exe')
    : path.join(repo, '.venv', 'bin', 'python');
  const executable = env.CASSQUERY_PYTHON || (fs.existsSync(venv) ? venv
    : platform === 'win32' ? 'python' : 'python3');
  const source = packaged ? path.join(resources, 'source') : repo;
  return { executable, args: [path.join(source, 'worker.py')] };
}

function checkStatus(port, timeoutMs = 1500, signal) {
  return new Promise((resolve) => {
    let req, timer;
    const finish = (value) => {
      clearTimeout(timer);
      signal?.removeEventListener('abort', abort);
      req?.destroy();
      resolve(value);
    };
    const abort = () => finish(false);
    if (signal?.aborted) { resolve(false); return; }
    req = http.get(`http://127.0.0.1:${port}/api/status`, (res) => {
      let body = '';
      res.on('data', (chunk) => {
        body += chunk;
        if (body.length > 65536) finish(false);
      });
      res.on('error', () => finish(false));
      res.on('end', () => {
        try {
          const status = JSON.parse(body);
          finish(res.statusCode === 200 && status.api_version === 2
            && Array.isArray(status.projects) && Array.isArray(status.load_errors));
        } catch { finish(false); }
      });
    });
    // Total request budget, not socket-idle time (trickling bytes must not extend it).
    timer = setTimeout(() => finish(false), Math.max(1, timeoutMs));
    req.on('error', () => finish(false));
    signal?.addEventListener('abort', abort, { once: true });
  });
}

async function stopWorker(child, graceMs = 1000) {
  if (!child || child.exitCode !== null || child.signalCode !== null) return;
  await new Promise((resolve, reject) => {
    let force, watchdog, finished = false;
    const cleanup = () => { clearTimeout(force); clearTimeout(watchdog); child.removeListener('exit', done); };
    const done = () => { if (finished) return; finished = true; cleanup(); resolve(); };
    child.once('exit', done);
    // Cold frozen workers can be in filesystem I/O; still require actual exit after the unchanged force point.
    force = setTimeout(() => { if (!finished) child.kill('SIGKILL'); }, graceMs);
    watchdog = setTimeout(() => {
      if (child.exitCode !== null || child.signalCode !== null) { done(); return; }
      finished = true; cleanup();
      reject(new Error(`Worker ${child.pid} did not exit after SIGKILL (exit=${child.exitCode}, signal=${child.signalCode}, killed=${child.killed})`));
    }, graceMs + 10000);
    child.kill();
  });
}

async function startWorker({ command, port, dataDir, env = process.env,
  timeoutMs = 30000, signal }) {
  const registryDir = fs.mkdtempSync(path.join(os.tmpdir(), 'cassquery-'));
  const registry = path.join(registryDir, 'worker.json');
  const diagnostic = { python: command.executable, worker: command.args[0] || command.executable,
    exit: null, stderr: '' };
  let child, spawnError = false;
  const cleanup = () => fs.rmSync(registryDir, { recursive: true, force: true });
  try {
    if (signal?.aborted) throw new Error('Worker startup cancelled');
    fs.mkdirSync(dataDir, { recursive: true });
    child = spawn(command.executable, [...command.args, '--port', String(port),
      '--registry-file', registry], {
      cwd: dataDir,
      env: { ...env, CASSQUERY_DATA_DIR: dataDir, PYTHONUNBUFFERED: '1' },
      detached: false, stdio: ['ignore', 'pipe', 'pipe'],
    });
    child.stdout.on('data', (chunk) => process.stdout.write(chunk));
    child.stderr.on('data', (chunk) => {
      process.stderr.write(chunk);
      diagnostic.stderr = (diagnostic.stderr + chunk).slice(-4000);
    });
    child.on('error', (error) => {
      spawnError = true; diagnostic.stderr = error.message; diagnostic.exit = -1;
    });
    child.on('exit', (code, exitSignal) => { diagnostic.exit = code ?? exitSignal; cleanup(); });
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline && diagnostic.exit === null && !signal?.aborted) {
      let info;
      try { info = JSON.parse(fs.readFileSync(registry, 'utf8')); } catch { /* not ready yet */ }
      if (info && info.pid === child.pid && Number.isInteger(info.port)
          && info.port >= port && info.port < port + 60
          && await checkStatus(info.port, Math.min(1500, deadline - Date.now()), signal)) {
        if (diagnostic.exit === null && !signal?.aborted && Date.now() < deadline) {
          return { child, port: info.port, diagnostic, cleanup };
        }
      }
      await new Promise((resolve) => setTimeout(resolve, Math.min(100, Math.max(1, deadline - Date.now()))));
    }
    if (diagnostic.exit === null) diagnostic.stderr += signal?.aborted
      ? '\nWorker startup cancelled.' : '\nWorker startup timed out.';
  } catch (error) { diagnostic.stderr = error.message; }
  try {
    if (child && !spawnError) await stopWorker(child);
  } finally { cleanup(); }
  return { child: null, port: null, diagnostic, cleanup };
}

module.exports = { startingPort, workerCommand, checkStatus, startWorker, stopWorker };
