const { app, BrowserWindow, Menu, nativeTheme, screen, ipcMain, dialog, shell } = require('electron');
const crypto = require('node:crypto');
const http = require('node:http');
const { createDesktop } = require('./desktop.cjs');
const { configureRenderer } = require('./renderer_config.cjs');
const renderBackendRequested = configureRenderer(app.commandLine, process.platform, process.env);
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
const workbenchToken = crypto.randomBytes(32).toString('hex');
let desktop = null, updater = null, decidingQuit = false, updateProofWatch = null;
const pendingNativePaths = [];
const preloadIdentities = new WeakMap();
function configuredPreloadIdentity(win) {
  if(!win||win!==window||win.isDestroyed()||!preloadIdentities.has(win))throw new Error('Preload identity belongs to another window');
  const identity=preloadIdentities.get(win);
  if(!Number.isSafeInteger(identity.web_contents_id)||identity.web_contents_id<=0||identity.web_contents_id!==win.webContents.id)throw new Error('Preload identity has no current web contents binding');
  return identity;
}

async function ensureWorker() {
  const port = startingPort(process.env.CASSQUERY_PORT);
  const command = workerCommand({ packaged: app.isPackaged, resources: process.resourcesPath,
    repo: REPO, platform: process.platform, env: process.env });
  const result = await startWorker({ command, port,
    dataDir: process.env.CASSQUERY_DATA_DIR || path.join(app.getPath('userData'), 'worker'),
    env: { ...process.env, ...(app.isPackaged ? { PYTHONDONTWRITEBYTECODE: '1' } : {}),
      CASSQUERY_WORKBENCH_TOKEN: workbenchToken },
    timeoutMs: app.isPackaged ? 90000 : 30000,
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
  const esc = (s) => String(s).replaceAll(workbenchToken, '[private capability]').replace(/&/g, '&amp;').replace(/</g, '&lt;');
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
  const preloadPath = path.join(__dirname, 'preload.cjs'), preloadBytes = fs.readFileSync(preloadPath);
  const win = new BrowserWindow({ width: 1320, height: 880, title: 'CassQuery',
    enableLargerThanScreen: !!process.env.CASSQUERY_SMOKE_DIR,
    webPreferences: { nodeIntegration: false, contextIsolation: true, sandbox: true,
      preload: preloadPath } });
  window = win;
  preloadIdentities.set(win,Object.freeze({schema:1,method:'owned BrowserWindow constructor',path:preloadPath,bytes:preloadBytes.length,
    sha256:crypto.createHash('sha256').update(preloadBytes).digest('hex'),web_contents_id:win.webContents.id,resources_path:process.resourcesPath}));
  desktop.attachWindow(win);
  win.on('closed', () => { window = null; });
  const origin = `http://127.0.0.1:${workerPort}`;
  win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  win.webContents.on('will-navigate', (event, url) => {
    if (!url.startsWith(`${origin}/`)) event.preventDefault();
  });
  win.webContents.session.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  win.webContents.once('did-finish-load', () => {
    nativeReady(win).catch(error => { console.error(error); dialog.showErrorBox('Desktop initialization failed', error.message); });
  });
  if (process.env.CASSQUERY_SMOKE_DIR || process.env.CASSQUERY_UPDATE_PROOF_DIR || process.env.CASSQUERY_WORKBENCH_SMOKE_DIR) {
    win.cassqueryRendererErrors = rendererErrors;
    win.webContents.on('console-message', (event) => {
      require('./renderer_config.cjs').recordRendererConsole(rendererErrors, event);
    });
    win.webContents.on('render-process-gone', (_event, details) => rendererErrors.push(`renderer gone: ${details.reason}`));
    win.webContents.on('did-fail-load', (_event, code, description) => rendererErrors.push(`load ${code}: ${description}`));
    win.webContents.once('did-finish-load', () => {
      if (process.env.CASSQUERY_SMOKE_DIR && !process.env.CASSQUERY_WORKBENCH_SMOKE_DIR && !smokeStarted) { smokeStarted = true; runNativeSmoke(win).catch((error) => { console.error(error); app.exit(1); }); }
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
  const { smoke, waitFor, checkpoint, closeWindow, evaluate, bounded } = require('./smoke.cjs');
  const output = process.env.CASSQUERY_SMOKE_DIR;
  const report = { schema: 1, pass: false, checkpoints: [], errors: [] };
  const ownedWorker = worker;
  try {
    if (!app.isPackaged) throw new Error('Native assurance requires the packaged Electron app');
    if (!ownedWorker || workerPort === null) throw new Error('Owned packaged worker startup failed: ' + JSON.stringify(tried));
    const crypto = require('node:crypto');
    const declared = require(path.join(REPO, 'package.json')).cassquerySources?.shell;
    if (!Array.isArray(declared) || !declared.length || !declared.every(name => /^shell\/[a-z_]+\.(?:js|cjs)$/.test(name)))
      throw new Error('Missing/invalid packed shell source contract');
    const files = declared.map((name) => {
      const bytes = fs.readFileSync(path.join(REPO, name));
      return { bytes: bytes.length, path: name, sha256: crypto.createHash('sha256').update(bytes).digest('hex') };
    });
    // Sorted paths and lexically ordered keys: a plain canonical, fresh executed-shell fingerprint.
    report.native_source = { files, fingerprint: crypto.createHash('sha256').update(JSON.stringify({ files })).digest('hex') };
    const display = screen.getDisplayMatching(win.getBounds());
    report.graphics = { runtime: { electron: process.versions.electron, chromium: process.versions.chrome,
      node: process.versions.node, platform: process.platform, architecture: process.arch },
      display: { bounds: display.bounds, work_area: display.workArea, scale_factor: display.scaleFactor,
        window_bounds: win.getBounds() } };
    report.graphics.gpu = await bounded(() => app.getGPUInfo('complete'), 10000, 'Native GPU telemetry');
    await waitFor(win, 'window.__cq?.model?.tier === "certified" && window.__cq.model.gates?.pass');
    const { readRendererContext } = require('./renderer_config.cjs');
    report.graphics.webgl = await evaluate(win, `(${readRendererContext.toString()})()`);
    const hash = await evaluate(win, '__cq.model.hash');
    const beforeSecond = secondInstances;
    const secondary = spawn(process.execPath, [], { env: process.env, stdio: 'ignore' });
    const secondaryExit = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => { secondary.kill('SIGKILL'); reject(new Error('Second instance did not exit')); }, 15000);
      secondary.once('error', (error) => { clearTimeout(timer); reject(error); });
      secondary.once('exit', (code) => { clearTimeout(timer); resolve(code); });
    });
    if (secondaryExit !== 0 || secondInstances !== beforeSecond + 1 || worker !== ownedWorker)
      throw new Error('Single-instance process/worker ownership failed');
    await closeWindow(win);
    if (worker !== ownedWorker || worker.exitCode !== null) throw new Error('Closing Mac window killed warm worker');
    app.emit('activate');
    await bounded(() => Promise.resolve(opening), 30000, 'Native reopen');
    const reopened = window;
    if (!reopened || reopened === win || reopened.isDestroyed()) throw new Error('Activation did not reopen the window');
    await waitFor(reopened, `window.__cq?.model?.tier === 'certified' && window.__cq.model.hash === ${JSON.stringify(hash)}`);
    if (worker !== ownedWorker) throw new Error('Reopen replaced warm worker');
    const lifecycle = { pass: true, secondary_pid: secondary.pid, secondary_exit: secondaryExit,
      second_instance_events: secondInstances - beforeSecond, close_reopen: true,
      worker_pid_before: ownedWorker.pid, worker_pid_after: worker.pid,
      method: 'real secondary process; BrowserWindow.close; production activate handler; same live worker' };
    report.lifecycle = lifecycle;
    Object.assign(report, await smoke(reopened, output, { nativeTheme, errors: rendererErrors, evidence: report }));
  } catch (error) {
    report.pass = false; report.error = (error.stack || String(error)).replaceAll(workbenchToken, '[private capability]');
    if (error.renderer_execution) report.failed_renderer_execution = { ...error.renderer_execution,
      expression: error.renderer_execution.expression.replaceAll(workbenchToken, '[private capability]') };
  }
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
  report.render_backend_requested = renderBackendRequested;
  report.started = smokeStartedAt;
  report.errors = rendererErrors.map(message => String(message).replaceAll(workbenchToken, '[private capability]'));
  // Exercise normal before-quit shutdown. Successful smoke never calls app.exit.
  pendingSmokeReport = { report, output };
  app.quit();
}

function finishSmokeReport(shutdownError = null) {
  if (!pendingSmokeReport) return;
  const { checkpoint } = require('./smoke.cjs');
  const { report, output } = pendingSmokeReport;
  report.errors = rendererErrors.map(message => String(message).replaceAll(workbenchToken, '[private capability]'));
  if (report.errors.length) { report.pass = false; report.error ||= 'Renderer diagnostics through shutdown'; }
  report.worker_shutdown = !shutdownError;
  if (shutdownError) { report.pass = false; report.error ||= String(shutdownError); report.shutdown_error = String(shutdownError); }
  report.checkpoints.push(checkpoint('worker-shutdown'));
  report.finished = checkpoint('app-complete');
  fs.mkdirSync(output, { recursive: true });
  const file = path.join(output, 'renderer-raw.json'), temporary = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(temporary, JSON.stringify(report, null, 2));
  fs.renameSync(temporary, file);
}

function workerStatus() {
  const port = workerPort;
  return new Promise((resolve,reject) => {
    if (!Number.isInteger(port)) { reject(new Error('Worker unavailable')); return; }
    let timer, settled=false;
    const finish=(error,value)=>{if(settled)return;settled=true;clearTimeout(timer);if(error)reject(error);else resolve(value);};
    const request=http.get(`http://127.0.0.1:${port}/api/status`,response=>{
      const chunks=[];let bytes=0;
      response.on('data',chunk=>{bytes+=chunk.length;if(bytes>4*1024*1024){response.destroy();request.destroy();finish(new Error('Worker status exceeds limit'));}else chunks.push(chunk);});
      response.on('error',error=>finish(error));
      response.on('end',()=>{try{const value=JSON.parse(Buffer.concat(chunks).toString('utf8'));if(response.statusCode!==200||value.api_version!==2||!Array.isArray(value.projects)||!Array.isArray(value.load_errors))throw new Error('Invalid worker status');finish(null,value);}catch(error){finish(error);}});
    });
    request.on('error',error=>finish(error));timer=setTimeout(()=>{request.destroy();finish(new Error('Worker status timed out'));},5000);
  });
}
function shellSourceProof() {
  const names=require(path.join(REPO,'package.json')).cassquerySources?.shell;
  if(!Array.isArray(names)||!names.length||new Set(names).size!==names.length||!names.every(name=>/^shell\/[a-z_]+\.(?:js|cjs)$/.test(name)))throw new Error('Invalid packed shell source contract');
  const files=names.map(name=>{const bytes=fs.readFileSync(path.join(REPO,name));return{bytes:bytes.length,path:name,sha256:crypto.createHash('sha256').update(bytes).digest('hex')};});
  return{files,fingerprint:crypto.createHash('sha256').update(JSON.stringify({files})).digest('hex')};
}
async function nativeReady(win) {
  if(window!==win||win.isDestroyed()||workerPort===null||win.webContents.getURL()!==`http://127.0.0.1:${workerPort}/`)return;
  const ownedWorker=worker, port=workerPort;
  const {waitFor,evaluate,bounded,capture,checkpoint}=require('./smoke.cjs');
  const started=checkpoint('desktop-ready-start');
  await waitFor(win,'!!window.__cq?.camera && !!document.querySelector("#stage canvas")',30000);
  const {readRendererContext}=require('./renderer_config.cjs');
  const webgl=await evaluate(win,`(${readRendererContext.toString()})()`);
  const status=await workerStatus();
  if(window!==win||worker!==ownedWorker||workerPort!==port||win.isDestroyed()||win.webContents.getURL()!==`http://127.0.0.1:${port}/`)throw new Error('Owned desktop changed during readiness');
  const proofDir=process.env.CASSQUERY_UPDATE_PROOF_DIR;
  if(proofDir){
    if(!app.isPackaged||!/^[a-f0-9]{64}$/.test(process.env.CASSQUERY_UPDATE_PROOF_CHALLENGE||''))throw new Error('Invalid native update proof context');
    await waitFor(win,'window.__cq?.model?.tier === "certified" && window.__cq.model.gates?.pass',30000);
    const current=await workerStatus();
    if(current.projects.length!==1||current.projects[0].name!=='TESTPLATE'||current.projects[0].state!=='certified'||current.projects[0].gates_pass!==true)throw new Error('Update proof requires the public certified default fixture');
    const directory=path.resolve(proofDir),resources=path.resolve(process.resourcesPath),appRoot=path.dirname(path.dirname(resources));
    if(directory===appRoot||directory.startsWith(appRoot+path.sep))throw new Error('Update proof must be outside the app');
    fs.mkdirSync(directory,{recursive:true});
    const display=screen.getDisplayMatching(win.getBounds());
    const model=await evaluate(win,'({project:__cq.model.index.project,hash:__cq.model.hash,tier:__cq.model.tier,gates:__cq.model.gates})');
    const gateIds=['units','watertight','volume','thin-extent','triangulation','payload-roundtrip','budget-tris','budget-bytes'];
    if(model.project!=='TESTPLATE'||model.gates?.pass!==true||!Array.isArray(model.gates.checks)||model.gates.checks.length!==8||model.gates.checks.some(check=>check.status!=='pass')||gateIds.some(id=>!model.gates.checks.some(check=>check.id===id)))throw new Error('Update proof lacks eight certified gates');
    if(rendererErrors.length)throw new Error('Renderer diagnostics block update readiness');
    const report={schema:1,pass:true,source_sha:process.env.GITHUB_SHA||null,version:app.getVersion(),architecture:process.arch,
      app_pid:process.pid,worker_pid:ownedWorker.pid,challenge:process.env.CASSQUERY_UPDATE_PROOF_CHALLENGE,
      render_backend_requested:renderBackendRequested,
      resources_path:process.resourcesPath,worker_executable:tried.python,packaged:app.isPackaged,
      bundled_worker:tried.python===path.join(process.resourcesPath,'worker','cassquery-worker'),started,
      checkpoints:[checkpoint('desktop-ready')],native_source:shellSourceProof(),worker_status:current,
      model,certified:{project:model.project,hash:model.hash,tier:model.tier},gates:model.gates,errors:[],
      graphics:{runtime:{electron:process.versions.electron,chromium:process.versions.chrome,node:process.versions.node,platform:process.platform,architecture:process.arch},
        display:{bounds:display.bounds,work_area:display.workArea,scale_factor:display.scaleFactor,window_bounds:win.getBounds()},
        gpu:await bounded(()=>app.getGPUInfo('complete'),10000,'Native GPU telemetry'),webgl},
      images:[await capture(win,path.join(directory,`ready-${process.pid}.png`))]};
    if(window!==win||worker!==ownedWorker||workerPort!==port||win.isDestroyed()||rendererErrors.length)throw new Error('Desktop changed before ready proof commit');
    const file=path.join(directory,`ready-${process.pid}.json`),temporary=`${file}.tmp`;fs.writeFileSync(temporary,JSON.stringify(report,null,2));fs.renameSync(temporary,file);
    if(!updateProofWatch){
      const name=`quit-${process.pid}.json`;let requested=false;
      updateProofWatch=fs.watch(directory,(_event,changed)=>{
        if(requested||String(changed)!==name)return;
        const file=path.join(directory,name);
        try{const stat=fs.lstatSync(file);if(!stat.isFile()||stat.isSymbolicLink()||stat.size>4096)return;const value=JSON.parse(fs.readFileSync(file,'utf8'));if(Object.keys(value).length!==1||value.challenge!==process.env.CASSQUERY_UPDATE_PROOF_CHALLENGE)return;requested=true;app.quit();}
        catch(error){if(error.code!=='ENOENT')console.error('Invalid owned quit proof:',error.message);}
      });
      updateProofWatch.on('error',error=>console.error('Owned quit proof watcher:',error.message));
    }
  }
  if(rendererErrors.length)throw new Error('Renderer diagnostics block desktop readiness');
  await evaluate(win,`(${readRendererContext.toString()})()`);
  if(updater)await updater.confirmRestart();
  const workbenchOutput=process.env.CASSQUERY_WORKBENCH_SMOKE_DIR;
  if(workbenchOutput){
    if(!app.isPackaged||process.env.GITHUB_ACTIONS!=='true'||!process.env.CASSQUERY_SMOKE_DIR||!/^[a-f0-9]{64}$/.test(process.env.CASSQUERY_WORKBENCH_SMOKE_CHALLENGE||''))throw new Error('Workbench driver requires an explicit packaged CI launch');
    const driver=process.env.CASSQUERY_WORKBENCH_SMOKE_DRIVER,expected=process.env.CASSQUERY_WORKBENCH_SMOKE_DRIVER_SHA;
    const workspace=process.env.GITHUB_WORKSPACE&&fs.realpathSync(process.env.GITHUB_WORKSPACE);
    if(!workspace||driver!==path.join(workspace,'tools','native_workbench_probe.cjs')||!driver||!path.isAbsolute(driver)||fs.realpathSync(driver)!==driver||fs.lstatSync(driver).isSymbolicLink()||!/^[a-f0-9]{64}$/.test(expected||''))throw new Error('Invalid owned workbench driver');
    const directory=path.resolve(workbenchOutput),resources=path.resolve(process.resourcesPath),appRoot=path.dirname(path.dirname(resources));
    if(directory===appRoot||directory.startsWith(appRoot+path.sep))throw new Error('Workbench proof must be outside the app');
    const bytes=fs.readFileSync(driver);if(bytes.length>1024*1024||crypto.createHash('sha256').update(bytes).digest('hex')!==expected)throw new Error('Workbench driver bytes changed');
    const Module=require('node:module'),loaded=new Module(driver,module);loaded.filename=driver;loaded.paths=Module._nodeModulePaths(path.dirname(driver));loaded._compile(bytes.toString('utf8'),driver);
    if(typeof loaded.exports.probe!=='function')throw new Error('Workbench driver lacks a probe');
    const result=await loaded.exports.probe(win,path.resolve(workbenchOutput),{ownedRoot:process.env.CASSQUERY_USER_DATA,projectRoot:process.env.CASSQUERY_PROJECTS,workerPort:port,workerPid:ownedWorker.pid,
      challenge:process.env.CASSQUERY_WORKBENCH_SMOKE_CHALLENGE,driver:{path:driver,bytes:bytes.length,sha256:expected},
      resources_path:process.resourcesPath,worker_origin:`http://127.0.0.1:${port}`,window_id:win.id,web_contents_id:win.webContents.id,
      native_source:shellSourceProof(),preload_identity:configuredPreloadIdentity(win)});
    const record={schema:1,source_sha:process.env.GITHUB_SHA||null,app_pid:process.pid,worker_pid:ownedWorker.pid,challenge:process.env.CASSQUERY_WORKBENCH_SMOKE_CHALLENGE,
      driver:{path:driver,bytes:bytes.length,sha256:expected},resources_path:process.resourcesPath,worker_origin:`http://127.0.0.1:${port}`,project_root:process.env.CASSQUERY_PROJECTS,
      window_id:win.id,web_contents_id:win.webContents.id,native_source:shellSourceProof(),preload_identity:configuredPreloadIdentity(win),result};
    fs.mkdirSync(directory,{recursive:true});const reportFile=path.join(directory,'native-workbench-main.json');
    fs.writeFileSync(reportFile+'.tmp',JSON.stringify(record,null,2));fs.renameSync(reportFile+'.tmp',reportFile);
  }
  while(pendingNativePaths.length && window===win && !win.isDestroyed())await desktop.openNative(pendingNativePaths.shift());
}
function desktopMenu() {
  const item=(label,accelerator,command)=>({label,accelerator,click:()=>desktop.command(command)});
  const recents=desktop.stores.getSettings().recentProjects;
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    ...(process.platform==='darwin'?[{role:'appMenu'}]:[]),
    {label:'File',submenu:[item('Open File…','CmdOrCtrl+O','openFile'),item('Open Folder…','CmdOrCtrl+Shift+O','openFolder'),
      {label:'Open Recent',submenu:recents.length?recents.map(file=>({label:path.basename(file),click:()=>desktop.openNative(file).catch(error=>dialog.showErrorBox('Project did not open',error.message))})):[{label:'No recent projects',enabled:false}]},
      item('New Project…','CmdOrCtrl+N','new'),{type:'separator'},item('Save','CmdOrCtrl+S','save'),item('Save a Copy…','CmdOrCtrl+Shift+S','saveAs'),
      item('Import STEP…','CmdOrCtrl+I','import'),item('Export…','CmdOrCtrl+E','export'),{type:'separator'},{role:'close'},{role:'quit'}]},
    {label:'Edit',submenu:[item('Undo','CmdOrCtrl+Z','undo'),item('Redo','CmdOrCtrl+Shift+Z','redo'),{type:'separator'},
      {role:'cut'},{role:'copy'},{role:'paste'},{role:'delete'},{role:'selectAll'},item('Find…','CmdOrCtrl+F','find')]},
    {label:'View',submenu:[item('Rebuild','CmdOrCtrl+B','build'),item('Editor / Viewer','CmdOrCtrl+Shift+P','pane'),item('Settings…','CmdOrCtrl+,','settings'),item('Reload','CmdOrCtrl+R','reload')]},
    {role:'windowMenu'},
  ]));
}
if (process.env.CASSQUERY_USER_DATA) app.setPath('userData', process.env.CASSQUERY_USER_DATA);
desktop=createDesktop({ipcMain,dialog,getWindow:()=>window,getPort:()=>workerPort,token:workbenchToken,userData:app.getPath('userData'),
  onSettings:async prefs=>{desktopMenu();if(updater)await updater.setPreferences({autoCheck:prefs.updateCheck==='enabled'&&!process.env.CASSQUERY_SMOKE_DIR,autoDownload:prefs.updateAutoDownload});},
  update:async action=>{
    if(!updater)throw Object.assign(new Error('Updater not ready'),{code:'UPDATE_UNAVAILABLE'});
    if(action==='state')return updater.getState();
    if(action==='install')return updater.install({userInitiated:true});
    return updater[action]();
  }});
desktop.register();
app.on('open-file',(event,file)=>{event.preventDefault();if(pendingNativePaths.length<10)pendingNativePaths.push(file);if(window&&workerPort!==null)nativeReady(window).catch(error=>dialog.showErrorBox('Project did not open',error.message));});
if(app.isPackaged)for(const value of process.argv.slice(1).filter(value=>!value.startsWith('-')&&path.isAbsolute(value)).slice(0,10))pendingNativePaths.push(value);
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => { secondInstances++; if (window) { window.show(); window.focus(); } });
  app.whenReady().then(async () => {
    desktopMenu();
    const prefs=desktop.stores.getSettings();
    const {createUpdater}=require('./updater.cjs');
    updater=await createUpdater({version:app.getVersion(),arch:process.arch,userData:app.getPath('userData'),
      appPath:process.platform==='darwin'&&app.isPackaged?path.dirname(path.dirname(path.dirname(process.execPath))):app.getAppPath(),
      revealFile:file=>shell.showItemInFolder(file),onState:state=>desktop.command('updateState',{state}),
      autoCheck:prefs.updateCheck==='enabled'&&!process.env.CASSQUERY_SMOKE_DIR,autoDownload:prefs.updateAutoDownload,
      canInstall:async()=>{
        if(!worker||worker.exitCode!==null||desktop.isDirty()||desktop.isBusy()||opening||quitting)return false;
        const current=await workerStatus();
        return !desktop.isDirty()&&!desktop.isBusy()&&current.load_errors.length===0&&current.projects.every(project=>['certified','gate-failed','error'].includes(project.state));
      },requestQuit:()=>app.quit()});
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
  if (quitting||decidingQuit) return;
  decidingQuit=true;
  desktop.requestQuit().then(async allowed=>{
    if(!allowed){decidingQuit=false;return;}
    if(window&&!window.isDestroyed())window.setEnabled(false);
    await new Promise(resolve=>setImmediate(resolve));
    if(desktop.isDirty()){decidingQuit=false;if(window&&!window.isDestroyed())window.setEnabled(true);return;}
    quitting=true;decidingQuit=false;startup.abort();
    try{
      await Promise.resolve(opening);await stopWorker(worker);
      shutdownComplete=true;finishSmokeReport();if(updateProofWatch)updateProofWatch.close();if(updater)updater.dispose();
      if(pendingSmokeReport?.report.pass===false)app.exit(1);else app.quit();
    }catch(error){finishSmokeReport(error);console.error(error);app.exit(1);}
  }).catch(error=>{decidingQuit=false;console.error(error);dialog.showErrorBox('Unsaved changes retained',error.message);});
});
process.on('SIGTERM',()=>app.quit());
