'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');
const { startingPort, workerCommand, checkStatus, startWorker, stopWorker } = require('../shell/runtime.cjs');
const { spawn } = require('node:child_process');

test('starting port is strict and leaves space for both services', () => {
  assert.equal(startingPort(undefined), 3939);
  assert.equal(startingPort('64476'), 64476);
  for (const bad of ['0', '65535', '3939oops', '-1', '3.9', '64477', 'NaN']) {
    assert.throws(() => startingPort(bad));
  }
});
test('packaged app uses bundled worker without PATH Python', () => {
  assert.deepEqual(workerCommand({ packaged: true, resources: '/bundle', repo: '/repo',
    platform: 'darwin', env: {} }), { executable: '/bundle/worker/cassquery-worker', args: [] });
  assert.deepEqual(workerCommand({ packaged: true, resources: '/bundle', repo: '/repo',
    platform: 'darwin', env: { CASSQUERY_PYTHON: '/custom/python' } }),
  { executable: '/custom/python', args: ['/bundle/source/worker.py'] });
});
test('unrelated HTTP 200 is not a worker status', async (t) => {
  const server = http.createServer((_req, res) => { res.end('{"okay":true}'); });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  t.after(() => server.close());
  assert.equal(await checkStatus(server.address().port), false);
});
test('spawn errors fail promptly and retain diagnosis', async () => {
  const data = fs.mkdtempSync(path.join(os.tmpdir(), 'cq-test-'));
  try {
    const result = await startWorker({ command: { executable: path.join(data, 'missing'), args: [] },
      port: 3939, dataDir: data, timeoutMs: 1000 });
    assert.equal(result.child, null);
    assert.match(result.diagnostic.stderr, /ENOENT/);
  } finally { fs.rmSync(data, { recursive: true, force: true }); }
});
test('worker-owned registry selects scanned port, not initial port', async () => {
  const data = fs.mkdtempSync(path.join(os.tmpdir(), 'cq-owned-'));
  const script = path.join(data, 'fake.cjs');
  fs.writeFileSync(script, `const fs=require('fs'),http=require('http');
    const registry=process.argv[process.argv.indexOf('--registry-file')+1];
    const server=http.createServer((q,r)=>r.end(JSON.stringify({api_version:2,projects:[],load_errors:[]})));
    server.listen(0,'127.0.0.1',()=>fs.writeFileSync(registry,JSON.stringify({pid:process.pid,port:server.address().port})));`);
  let result;
  try {
    // Keep allocated port inside worker scan range by discovering it in advance.
    const probe = http.createServer();
    await new Promise((r) => probe.listen(0, '127.0.0.1', r));
    const port = probe.address().port;
    await new Promise((r) => probe.close(r));
    fs.writeFileSync(script, fs.readFileSync(script, 'utf8').replace("server.listen(0,", `server.listen(${port},`));
    result = await startWorker({ command: { executable: process.execPath, args: [script] },
      port: port - 1, dataDir: data, timeoutMs: 5000 });
    assert.equal(result.port, port);
    assert.ok(result.child);
  } finally {
    if (result?.child) await stopWorker(result.child);
    fs.rmSync(data, { recursive: true, force: true });
  }
});

test('HTTP readiness has an absolute deadline even if bytes trickle', async (t) => {
  const server = http.createServer((_req, res) => {
    const interval = setInterval(() => res.write(' '), 5);
    res.on('close', () => clearInterval(interval));
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  t.after(() => server.close());
  const before = Date.now();
  assert.equal(await checkStatus(server.address().port, 100), false);
  assert.ok(Date.now() - before < 1000);
});

test('shutdown reaps a worker that ignores SIGTERM', async () => {
  const child = spawn(process.execPath, ['-e', "process.on('SIGTERM',()=>{}); console.log('ready'); setInterval(()=>{},1000)"],
    { stdio: ['ignore', 'pipe', 'pipe'] });
  await new Promise((resolve) => child.stdout.once('data', resolve));
  await stopWorker(child, 100);
  assert.equal(child.signalCode, 'SIGKILL');
});

test('worker exit acknowledgement is finite and timers cannot outlive synchronous exit',async()=>{
 const source=fs.readFileSync(path.join(__dirname,'../shell/runtime.cjs'),'utf8');
 const {EventEmitter}=require('node:events'),vm=require('node:vm');
 for(const mode of ['synchronous','late-after-kill','never-exits']){
  let clock=0,next=0;const timers=new Map(),signals=[];
  const child=Object.assign(new EventEmitter(),{pid:123,exitCode:null,signalCode:null,killed:false});
  const finish=()=>{child.signalCode='SIGKILL';child.emit('exit',null,'SIGKILL');};
  child.kill=signal=>{signals.push(signal||'SIGTERM');child.killed=true;if(mode==='synchronous')finish();return true;};
  const setTimer=(fn,ms)=>{const id=++next;timers.set(id,{fn,at:clock+ms});return id;};
  const context=vm.createContext({require,module:{exports:{}},setTimeout:setTimer,clearTimeout:id=>timers.delete(id)});
  vm.runInContext(source,context);const pending=context.module.exports.stopWorker(child,1000);
  if(mode==='synchronous'){await pending;assert.equal(timers.size,0);assert.deepEqual(signals,['SIGTERM']);continue;}
  if(mode==='late-after-kill')setTimer(finish,5500);
  const outcome=pending.then(()=>true,()=>false);
  while(timers.size){const [id,timer]=[...timers].sort((a,b)=>a[1].at-b[1].at)[0];timers.delete(id);clock=timer.at;timer.fn();await Promise.resolve();}
  assert.equal(await outcome,mode==='late-after-kill');assert.deepEqual(signals,['SIGTERM','SIGKILL']);assert.equal(child.listenerCount('exit'),0);assert.ok(clock<=11000);
 }
});

test('cancellation during startup leaves no worker alive', async () => {
  const data = fs.mkdtempSync(path.join(os.tmpdir(), 'cq-cancel-'));
  const script = path.join(data, 'stall.cjs');
  const pidFile = path.join(data, 'pid');
  fs.writeFileSync(script, `require('fs').writeFileSync(${JSON.stringify(pidFile)},String(process.pid)); process.on('SIGTERM',()=>{}); setInterval(()=>{},1000);`);
  const controller = new AbortController();
  try {
    const pending = startWorker({ command: { executable: process.execPath, args: [script] },
      port: 3939, dataDir: data, timeoutMs: 5000, signal: controller.signal });
    while (!fs.existsSync(pidFile)) await new Promise((resolve) => setTimeout(resolve, 10));
    const pid = Number(fs.readFileSync(pidFile));
    controller.abort();
    assert.equal((await pending).child, null);
    assert.throws(() => process.kill(pid, 0), /ESRCH/);
  } finally { fs.rmSync(data, { recursive: true, force: true }); }
});
