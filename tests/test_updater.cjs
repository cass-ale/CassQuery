'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');
const zlib = require('node:zlib');
const { EventEmitter } = require('node:events');
const { spawn } = require('node:child_process');
const updater = require('../shell/updater.cjs');
const helper = require('../shell/update_install.cjs');
const sha = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
async function scratch(t) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'cq-update-test-'));
  t.after(() => fs.rm(root, { recursive: true, force: true })); return fs.realpath(root);
}
function release(version = '0.3.0', arch = 'arm64') {
  const asset = name => ({ id: name === 'SHA256SUMS.txt' ? 2 : 1, name, size: 20, digest: `sha256:${'a'.repeat(64)}`, browser_download_url: `${updater.RELEASES}/download/v${version}/${name}` });
  return { tag_name: `v${version}`, html_url: `${updater.RELEASES}/tag/v${version}`, draft: false, prerelease: false, body: 'untrusted instructions ignored',
    assets: [asset(`CassQuery-${version}-mac-${arch}.zip`), asset('SHA256SUMS.txt')] };
}
function zip(entries) {
  const local = [], central = []; let offset = 0;
  for (const entry of entries) {
    const name = Buffer.from(entry.name), data = Buffer.from(entry.data || ''), method = entry.method ?? 8;
    const compressed = method === 8 ? zlib.deflateRawSync(data) : data;
    const crc = zlib.crc32(data), flags = entry.flags || 0;
    const header = Buffer.alloc(30); header.writeUInt32LE(0x04034b50); header.writeUInt16LE(20, 4); header.writeUInt16LE(flags, 6); header.writeUInt16LE(method, 8);
    header.writeUInt32LE(crc, 14); header.writeUInt32LE(compressed.length, 18); header.writeUInt32LE(data.length, 22); header.writeUInt16LE(name.length, 26);
    const cd = Buffer.alloc(46); cd.writeUInt32LE(0x02014b50); cd.writeUInt16LE(0x314, 4); cd.writeUInt16LE(20, 6); cd.writeUInt16LE(flags, 8); cd.writeUInt16LE(method, 10);
    cd.writeUInt32LE(crc, 16); cd.writeUInt32LE(compressed.length, 20); cd.writeUInt32LE(entry.declaredSize ?? data.length, 24); cd.writeUInt16LE(name.length, 28);
    cd.writeUInt32LE(((entry.mode ?? (entry.name.endsWith('/') ? 0o40755 : 0o100644)) << 16) >>> 0, 38); cd.writeUInt32LE(offset, 42);
    local.push(header, name, compressed); central.push(cd, name); offset += header.length + name.length + compressed.length;
  }
  const directory = Buffer.concat(central), end = Buffer.alloc(22); end.writeUInt32LE(0x06054b50); end.writeUInt16LE(entries.length, 8); end.writeUInt16LE(entries.length, 10); end.writeUInt32LE(directory.length, 12); end.writeUInt32LE(offset, 16);
  return Buffer.concat([...local, directory, end]);
}
const appEntries = () => [{ name: 'CassQuery.app/', method: 0 }, { name: 'CassQuery.app/Contents/', method: 0 },
  { name: 'CassQuery.app/Contents/Info.plist', data: 'plist' }, { name: 'CassQuery.app/Contents/MacOS/', method: 0 },
  { name: 'CassQuery.app/Contents/MacOS/CassQuery', data: 'binary', mode: 0o100755 }];

test('canonical stable newer versions only', () => {
  assert.equal(updater.compareVersions('0.2.0', '0.1.9'), 1); assert.equal(updater.compareVersions('0.1.0', '0.2.0'), -1);
  assert.equal(updater.selectRelease(release('0.1.0'), '0.2.0', 'arm64'), null);
  for (const version of ['01.2.0', '0.2', '0.2.0-beta', '0.2.0+build', '../1.0.0', '99999999999.0.0']) assert.throws(() => updater.compareVersions(version, '0.1.0'));
});
test('release repository, channel, architecture, duplicates and digests are boundaries', () => {
  assert.equal(updater.selectRelease(release(), '0.2.0', 'arm64').version, '0.3.0');
  for (const mutate of [r => { r.draft = true; }, r => { r.prerelease = true; }, r => { r.html_url = 'https://github.com/evil/repo/releases/tag/v0.3.0'; },
    r => { r.assets[0].browser_download_url = 'https://evil.example/update.zip'; }, r => { r.assets[0].digest = null; }, r => { r.assets[0].size = -1; },
    r => { r.assets.push({ ...r.assets[0] }); }]) { const r = release(); mutate(r); assert.throws(() => updater.selectRelease(r, '0.2.0', 'arm64')); }
  assert.throws(() => updater.selectRelease(release('0.3.0', 'x64'), '0.2.0', 'arm64'));
  assert.throws(() => updater.selectRelease(release(), '0.2.0', 'universal'));
});
test('checksum names and records cannot be ambiguous', () => {
  const name = 'CassQuery-0.3.0-mac-arm64.zip'; assert.equal(updater.checksumFor(Buffer.from(`${'a'.repeat(64)}  ${name}\n`), name), 'a'.repeat(64));
  for (const text of [`${'a'.repeat(64)}  ../${name}\n`, `${'a'.repeat(64)}  ${name}\n${'b'.repeat(64)}  ${name}\n`, 'invalid\n']) assert.throws(() => updater.checksumFor(Buffer.from(text), name));
});
test('HTTPS redirects remain repository-scoped and auth-free', async () => {
  const initial = `${updater.RELEASES}/download/v0.3.0/SHA256SUMS.txt`, cdn = 'https://release-assets.githubusercontent.com/github-production-release-asset/1432223312/file';
  updater.allowedURL(cdn, initial);
  for (const target of ['http://github.com/cass-ale/CassQuery/x', 'https://user:token@github.com/x', 'https://api.github.com.evil/x',
    'https://release-assets.githubusercontent.com/github-production-release-asset/99/file', 'https://github.com/evil/repo/x']) assert.throws(() => updater.allowedURL(target, initial));
  const calls = [];
  await updater.smallBody(initial, 10, async (url, options) => { calls.push({ url, options }); return url === initial ? new Response(null, { status: 302, headers: { location: cdn } }) : new Response('ok'); });
  assert.equal(calls.length, 2); assert.ok(calls.every(c => c.options.redirect === 'manual' && !('Authorization' in c.options.headers)));
  assert.equal(calls[0].options.headers.Accept, 'application/octet-stream');
  await assert.rejects(updater.smallBody(updater.API, 100, async () => new Response('unsupported', { status: 415 })), /HTTP 415/);
});
test('bounded download validates actual bytes and digest', async t => {
  const root = await scratch(t), bytes = Buffer.from('verified archive'), asset = { url: `${updater.RELEASES}/download/v0.3.0/archive.zip`, size: bytes.length, sha256: sha(bytes) };
  const file = path.join(root, 'good.zip'); await updater.downloadFile(asset, file, async () => new Response(bytes), undefined);
  assert.deepEqual(await fs.readFile(file), bytes);
  await assert.rejects(updater.downloadFile({ ...asset, sha256: '0'.repeat(64) }, path.join(root, 'bad.zip'), async () => new Response(bytes)), /SHA-256/);
  await assert.rejects(updater.smallBody(updater.API, 2, async () => new Response('oversized')), /limit/);
});
test('HTTP errors retain bounded public diagnostics without guessing or retrying generic403', async () => {
  let calls = 0;
  const response = new Response(JSON.stringify({ message: 'Access denied for 192.0.2.1 https://example.test/x?sig=PRIVATE Bearer SECRETTOKEN', documentation_url: 'https://docs.github.com/rest/rate-limits?token=SECRET', status: '403', token: 'DO_NOT_COPY' }), {
    status: 403, headers: { 'x-ratelimit-remaining': '5', 'x-ratelimit-reset': '1799999999', 'date': 'Sun, 25 Oct 2026 14:00:00 GMT', 'set-cookie': 'private', 'x-github-request-id': 'PUBLIC:1' } });
  await assert.rejects(updater.responseFor(updater.API, async () => { calls++; return response; }), error => {
    assert.equal(error.code, 'UPDATE_HTTP'); assert.equal(error.http.status, 403); assert.equal(error.http.endpoint, updater.API); assert.equal(error.http.retryAt, null);
    assert.equal(error.http.headers['x-ratelimit-remaining'], '5'); assert.equal(error.http.headers['set-cookie'], undefined);
    assert.equal(error.http.body.format, 'json'); assert.equal(error.http.body.token, undefined);
    assert.equal(error.http.body.documentation_url, 'https://docs.github.com/rest/rate-limits');
    assert.equal(/PRIVATE|SECRETTOKEN|DO_NOT_COPY|192\.0\.2\.1/.test(JSON.stringify(error.http)), false); return true;
  });
  assert.equal(calls, 1);
});
test('documentation URLs cannot echo arbitrary token paths, addresses, credentials or ports', async () => {
  for (const url of [
    'https://docs.github.com/rest/github_pat_SYNTHETIC_DIAGNOSTIC_CANARY_123/limits',
    'https://docs.github.com/rest/%67ithub_pat_SYNTHETIC_DIAGNOSTIC_CANARY_123/limits',
    'https://docs.github.com/rest/192.0.2.1/limits', 'https://docs.github.com/rest/%31%39%32.0.2.1/limits',
    'https://user:password@docs.github.com/rest/rate-limits', 'https://docs.github.com:8443/rest/rate-limits',
    'https://evil.example/rest/rate-limits', 'https://docs.github.com/unknown/private-path',
  ]) {
    await assert.rejects(updater.responseFor(updater.API, async () => new Response(JSON.stringify({ message: 'Forbidden', documentation_url: url }), { status: 403 })), error => {
      assert.equal(error.http.body.documentation_url, undefined);
      assert.equal(/CANARY|192\.0\.2\.1|password|private-path/.test(JSON.stringify(error.http)), false); return true;
    });
  }
  await assert.rejects(updater.responseFor(updater.API, async () => new Response(JSON.stringify({ message: 'Limit exceeded', documentation_url: 'https://docs.github.com/rest/overview/resources-in-the-rest-api?sig=SECRET#rate-limiting' }), { status: 403 })), error => {
    assert.equal(error.http.body.documentation_url, 'https://docs.github.com/rest/overview/resources-in-the-rest-api'); return true;
  });
});
test('never-settling error-body cancellation cannot delay the existing caller deadline', async () => {
  let cancelRequested = false;
  const response = new Response(new ReadableStream({ pull() {}, cancel() { cancelRequested = true; return new Promise(() => {}); } }), { status: 403 });
  const guard = setTimeout(() => {}, 1000);
  try {
    const result = await Promise.race([
      updater.responseFor(updater.API, async () => response, AbortSignal.timeout(20)).then(() => null, error => error),
      new Promise(resolve => setTimeout(() => resolve('late'), 300)),
    ]);
    assert.notEqual(result, 'late'); assert.equal(result.http.status, 403); assert.ok(result.http.body.read_error); assert.equal(cancelRequested, true);
  } finally { clearTimeout(guard); }
});

test('retry timing is derived only from server guidance, with clock-skew-safe reset', () => {
  const now = 1000000;
  assert.equal(updater.retryTiming(403, { 'x-ratelimit-remaining': '4', 'x-ratelimit-reset': '2000' }, now), null);
  assert.equal(updater.retryTiming(403, { 'x-ratelimit-remaining': '0', 'x-ratelimit-reset': '2000', date: new Date(1900000).toUTCString() }, now), 1100000);
  assert.equal(updater.retryTiming(429, { 'retry-after': '35' }, now), 1035000);
  assert.equal(updater.retryTiming(415, { 'retry-after': '1' }, now), null);
  assert.equal(updater.retryTiming(403, { 'retry-after': 'invalid', 'x-ratelimit-reset': '-1' }, now), null);
});
test('oversized or idle HTTP error body remains bounded and preserves status', async () => {
  await assert.rejects(updater.responseFor(updater.API, async () => new Response('x'.repeat(20000), { status: 403 })), error => {
    assert.equal(error.http.body.bytes, 8192); assert.equal(error.http.body.truncated, true); assert.ok(error.http.body.message.length <= 512); return true;
  });
  let cancelled = false;
  const response = new Response(new ReadableStream({ pull() {}, cancel() { cancelled = true; } }), { status: 429 });
  const hold = setTimeout(() => {}, 1000); // The synthetic idle stream has no socket/event-loop handle.
  try {
    await assert.rejects(updater.responseFor(updater.API, async () => response, AbortSignal.timeout(30)), error => {
      assert.equal(error.http.status, 429); assert.ok(error.http.body.read_error); return true;
    });
    assert.equal(cancelled, true);
  } finally { clearTimeout(hold); }
});

test('later checks respect server retryAt without sleeping, retrying, or controlling the app', async t => {
  const root = await scratch(t); let requests = 0, quits = 0;
  const client = await updater.createUpdater({ version: '0.2.0', arch: 'arm64', userData: root, appPath: '/never-read.app', autoCheck: false,
    requestQuit: () => { quits++; }, fetch: async () => { requests++; return new Response(JSON.stringify({ message: 'API rate limit exceeded' }), { status: 403,
      headers: { 'x-ratelimit-remaining': '0', 'x-ratelimit-reset': String(Math.floor(Date.now() / 1000) + 3600) } }); } });
  t.after(() => client.dispose()); await assert.rejects(client.check(), /HTTP 403/);
  assert.ok(client.getState().retryAt > Date.now()); assert.equal(client.getState().httpError.status, 403);
  await assert.rejects(client.check(), /server requested backoff/); assert.equal(requests, 2); assert.equal(quits, 0);
});

test('healthy ZIP and internal relative links validate without extraction', async t => {
  const root = await scratch(t), entries = appEntries(); entries.push({ name: 'CassQuery.app/alias', data: 'Contents/Info.plist', mode: 0o120777 });
  const file = path.join(root, 'app.zip'); await fs.writeFile(file, zip(entries)); const proof = await helper.validateZip(file); assert.equal(proof.entries, entries.length);
});
for (const [label, extra] of [
  ['traversal', { name: 'CassQuery.app/../outside', data: 'bad' }], ['absolute', { name: '/outside', data: 'bad' }], ['backslash', { name: 'CassQuery.app\\outside', data: 'bad' }],
  ['case collision', { name: 'cassquery.app/contents/info.plist', data: 'bad' }], ['Unicode casefold', { name: 'CassQuery.app/ΐ', data: 'bad', flags: 0x800 }],
  ['external link', { name: 'CassQuery.app/alias', data: '../../outside', mode: 0o120777 }], ['absolute link', { name: 'CassQuery.app/alias', data: '/outside', mode: 0o120777 }],
  ['special file', { name: 'CassQuery.app/device', mode: 0o20644 }], ['symlink ancestor', { name: 'CassQuery.app/Contents/Info.plist/child', data: 'bad' }],
]) test(`ZIP rejects ${label}`, async t => {
  const root = await scratch(t), file = path.join(root, 'bad.zip'); await fs.writeFile(file, zip([...appEntries(), extra])); await assert.rejects(helper.validateZip(file));
});
test('ZIP rejects chained escapes, cycles, CRC corruption and expansion lies', async t => {
  const root = await scratch(t), file = path.join(root, 'bad.zip');
  for (const entries of [[...appEntries(), { name: 'CassQuery.app/a', data: 'a', mode: 0o120777 }],
    [...appEntries(), { name: 'CassQuery.app/a', data: 'Contents/../..', mode: 0o120777 }],
    [...appEntries(), { name: 'CassQuery.app/bomb', data: 'large data', declaredSize: 1 }]]) { await fs.writeFile(file, zip(entries)); await assert.rejects(helper.validateZip(file)); }
  const stored = zip(appEntries().map(e => ({ ...e, method: 0 }))); stored[stored.indexOf(Buffer.from('binary'))] ^= 1;
  await fs.writeFile(file, stored); await assert.rejects(helper.validateZip(file), /CRC/);
});
test('actual ASAR bytes have identical custody under Node and Electron RunAsNode', async t => {
  const root = await scratch(t), app = path.join(root, 'Custody.app'), source = path.join(root, 'source');
  const resources = path.join(app, 'Contents/Resources');
  await fs.mkdir(resources, { recursive: true }); await fs.mkdir(source);
  await fs.writeFile(path.join(source, 'inside.cjs'), 'module.exports=42;\n');
  const asar = path.join(resources, 'app.asar');
  await require('@electron/asar').createPackage(source, asar);
  const before = await helper.inventory(app), archiveBefore = await helper.hashFile(asar);
  const executable = path.join(path.dirname(require.resolve('electron/package.json')), 'dist',
    process.platform === 'darwin' ? 'Electron.app/Contents/MacOS/Electron' : process.platform === 'win32' ? 'electron.exe' : 'electron');
  const code = `const fs=require('node:fs');const prior=process.noAsar;const virtual=fs.lstatSync(process.argv[2]).isDirectory();
    const h=require(process.argv[1]);h.inventory(process.argv[3]).then(result=>{
      if(process.noAsar!==prior)throw Error('Global ASAR mode changed');
      if(require(process.argv[2]+'/inside.cjs')!==42)throw Error('Application ASAR require disabled');
      console.log(JSON.stringify({result,virtual,archiveRequire:true,noAsarUnchanged:true,node:process.versions.node}));
    }).catch(e=>{console.error(e);process.exitCode=1});`;
  const exec = require('node:util').promisify(require('node:child_process').execFile);
  const { stdout } = await exec(executable, ['-e', code, require.resolve('../shell/update_install.cjs'), asar, app],
    { env: { ...process.env, ELECTRON_RUN_AS_NODE: '1' }, timeout: 15000, maxBuffer: 65536 });
  const observed = JSON.parse(stdout.trim());
  assert.equal(observed.virtual, true); assert.equal(observed.noAsarUnchanged, true); assert.equal(observed.archiveRequire, true);
  assert.deepEqual(observed.result, before); assert.equal(before.bytes, archiveBefore.bytes); assert.equal(before.entries, 4);
  const bytes = await fs.readFile(asar); bytes[bytes.length - 1] ^= 1; await fs.writeFile(asar, bytes);
  assert.notEqual((await helper.inventory(app)).sha256, before.sha256); // Physical archive tampering is not waived.
});

test('public release discovery preserves archive size and independently published digests on exhausted quota',async()=>{
 const version='0.1.0',arch='arm64',name=`CassQuery-${version}-mac-${arch}.zip`,zipDigest='a'.repeat(64);
 const sums=Buffer.from(`${zipDigest}  ${name}\n`),sumsDigest=sha(sums);
 const candidate={schema:1,repository:'cass-ale/CassQuery',version,architecture:arch,source_sha:'b'.repeat(40),assets:[{path:'dist/mac-app/'+name,bytes:1024,sha256:zipDigest}]};
 const candidateBytes=Buffer.from(JSON.stringify(candidate)),candidateDigest=sha(candidateBytes);
 const row=(n,d)=>`<a href="/cass-ale/CassQuery/releases/download/v${version}/${n}"></a><clipboard-copy aria-label="Copy to clipboard digest for ${n}" value="sha256:${d}"></clipboard-copy>`;
 const html=row(name,zipDigest)+row('SHA256SUMS.txt',sumsDigest)+row('candidate-arm64.json',candidateDigest);
 const release=updater.publicRelease(html,candidate,sums,version,arch);assert.equal(updater.selectRelease(release,'0.0.0',arch).archive.size,1024);
 assert.throws(()=>updater.publicRelease(html,{...candidate,architecture:'x64'},sums,version,arch));
 assert.throws(()=>updater.publicRelease(html+row(name,zipDigest),candidate,sums,version,arch));
 assert.throws(()=>updater.publicRelease(html,candidate,Buffer.from('changed'),version,arch));
 const requests=[];const fetcher=async(url,options)=>{
  requests.push(url);assert.ok(!Object.keys(options.headers).some(k=>/authorization|cookie/i.test(k)));
  if(url===updater.API)return new Response('{"message":"quota"}',{status:403,headers:{'x-ratelimit-remaining':'0'}});
  if(url===updater.RELEASES+'/latest')return new Response(null,{status:302,headers:{location:updater.RELEASES+'/tag/v'+version}});
  if(url.endsWith('/expanded_assets/v'+version))return new Response(html);
  if(url.endsWith('/candidate-arm64.json'))return new Response(candidateBytes);
  if(url.endsWith('/SHA256SUMS.txt'))return new Response(sums);
  throw Error('Unexpected fallback request');
 };
 const found=await updater.releaseMetadata(arch,fetcher,AbortSignal.timeout(1000));assert.deepEqual(found.release,release);assert.equal(found.publicSources.length,3);assert.equal(requests.length,5);
 let attempts=0;await assert.rejects(updater.releaseMetadata(arch,async()=>{attempts++;return new Response('denied',{status:403});},AbortSignal.timeout(1000)),/403/);assert.equal(attempts,1);
});

test('inventory binds root mode and refuses escapes', async t => {
  const root = await scratch(t), app = path.join(root, 'App.app'); await fs.mkdir(app, { mode: 0o755 }); await fs.writeFile(path.join(app, 'file'), 'safe');
  const first = await helper.inventory(app); await fs.chmod(app, 0o700); assert.notEqual((await helper.inventory(app)).sha256, first.sha256);
  await fs.symlink(root, path.join(app, 'escape')); await assert.rejects(helper.inventory(app), /escapes/);
});
test('metadata check failure is visible but does not control the app', async t => {
  const root = await scratch(t), states = []; let quits = 0;
  const client = await updater.createUpdater({ version: '0.2.0', arch: 'arm64', userData: root, appPath: '/never-read.app', autoCheck: false,
    fetch: async () => new Response('failure', { status: 503 }), requestQuit: () => { quits++; }, onState: s => states.push(s.phase) });
  t.after(() => client.dispose()); await assert.rejects(client.check(), /HTTP 503/); assert.equal(client.getState().phase, 'error'); assert.equal(quits, 0);
  assert.ok(states.includes('checking')); assert.ok(states.includes('error')); await assert.rejects(client.install(), /explicit user/);
});
test('check/notify/prefs and early private nonce consumption are stable', async t => {
  const root = await scratch(t), prior = process.env.CASSQUERY_UPDATE_TOKEN; process.env.CASSQUERY_UPDATE_TOKEN = 'b'.repeat(64);
  t.after(() => { if (prior === undefined) delete process.env.CASSQUERY_UPDATE_TOKEN; else process.env.CASSQUERY_UPDATE_TOKEN = prior; });
  const client = await updater.createUpdater({ version: '0.2.0', arch: 'arm64', userData: root, appPath: '/never-read.app', autoCheck: false, fetch: async () => new Response(JSON.stringify(release())) });
  t.after(() => client.dispose()); assert.equal(process.env.CASSQUERY_UPDATE_TOKEN, undefined); assert.equal(typeof client.reveal, 'function');
  assert.equal((await client.check()).phase, 'available'); await assert.rejects(client.setPreferences({ autoCheck: 'yes' }));
  assert.equal((await client.setPreferences({ autoDownload: false })).preferences.autoDownload, false);
});
async function installFixture(t) {
  const root = await scratch(t), parent = path.join(root, 'apps'); await fs.mkdir(parent, { mode: 0o700 });
  const app = path.join(parent, 'CassQuery.app'), stage = await fs.mkdtemp(path.join(parent, '.cassquery-update-')), incoming = path.join(stage, 'incoming.app');
  await fs.mkdir(app, { mode: 0o755 }); await fs.mkdir(incoming, { mode: 0o755 }); await fs.writeFile(path.join(app, 'value'), 'old'); await fs.writeFile(path.join(incoming, 'value'), 'new');
  const token = 'c'.repeat(64), control = path.join(root, 'userData/updates', `stage-${token}`); await fs.mkdir(control, { recursive: true, mode: 0o700 });
  const request = { schema: 1, token, oldPid: 12345, arch: 'arm64', currentVersion: '1.0.0', version: '1.1.0', app, stage, incoming,
    oldHash: (await helper.inventory(app)).sha256, newHash: (await helper.inventory(incoming)).sha256 };
  const file = path.join(control, 'install.json'); await helper.writeJSON(file, request);
  const inspect = async target => ({ inventory: await helper.inventory(target) });
  return { root, app, stage, control, request, file, inspect };
}
function fakeChild(pid) {
  const child = new EventEmitter(); Object.assign(child, { pid, exitCode: null, signalCode: null, unref() {}, kill(signal) { this.signalCode = signal; this.emit('exit', null, signal); } }); return child;
}
test('installer only commits after new-version/PID/nonce readiness', async t => {
  const f = await installFixture(t), child = fakeChild(100123); let unreferenced = false;
  child.unref = () => { assert.equal(JSON.parse(require('node:fs').readFileSync(path.join(f.control, 'result.json'))).pass, true); unreferenced = true; };
  await helper.runInstall(f.file, { inspect: f.inspect, isAlive: () => false, timeout: 500, launch: async app => {
    assert.equal(await fs.readFile(path.join(app, 'value'), 'utf8'), 'new');
    await helper.writeJSON(path.join(f.control, 'ack.json'), { token: f.request.token, pid: child.pid, version: f.request.version, arch: 'arm64', app }); return child;
  } });
  assert.equal(unreferenced, true);
  assert.equal((await helper.readJSON(path.join(f.control, 'result.json'))).pass, true); assert.equal(await fs.readFile(path.join(f.stage, 'previous.app/value'), 'utf8'), 'old');
});
test('completed install helper exits while its acknowledged app remains running',async t=>{
  // Real process lifetime regression; the small app/inspection fixture is not native installation proof.
  const f=await installFixture(t), pidFile=path.join(f.root,'new-pid');
  const code=`const {spawn}=require('node:child_process'),fs=require('node:fs'),path=require('node:path');
    const h=require(process.argv[1]),file=process.argv[2],pidFile=process.argv[3];
    (async()=>{const r=await h.readJSON(file);await h.runInstall(file,{inspect:async app=>({inventory:await h.inventory(app)}),isAlive:()=>false,launch:async app=>{
      const child=spawn(process.execPath,['-e','setInterval(()=>{},1000)'],{stdio:'ignore'});fs.writeFileSync(pidFile,String(child.pid));
      await h.writeJSON(path.join(path.dirname(file),'ack.json'),{token:r.token,pid:child.pid,version:r.version,arch:r.arch,app});return child;
    }});console.log('completed');})().catch(e=>{console.error(e);process.exitCode=1});`;
  try{
    const exec=require('node:util').promisify(require('node:child_process').execFile);
    const result=await exec(process.execPath,['-e',code,require.resolve('../shell/update_install.cjs'),f.file,pidFile],{timeout:3000});
    assert.match(result.stdout,/completed/);const pid=Number(await fs.readFile(pidFile,'utf8'));assert.ok(pid>1);process.kill(pid,0);
    assert.equal((await helper.readJSON(path.join(f.control,'result.json'))).pass,true);
  }finally{
    if(require('node:fs').existsSync(pidFile)){const pid=Number(await fs.readFile(pidFile,'utf8'));try{process.kill(pid,'SIGTERM');}catch(error){if(error.code!=='ESRCH')throw error;}}
  }
});
test('failed new app is stopped and original startup acknowledged before rollback passes', async t => {
  const f = await installFixture(t); let launches = 0;
  await assert.rejects(helper.runInstall(f.file, { inspect: f.inspect, isAlive: () => false, timeout: 500, launch: async app => {
    const child = fakeChild(100123 + launches++);
    if (launches === 1) child.exitCode = 1;
    else { assert.equal(await fs.readFile(path.join(app, 'value'), 'utf8'), 'old'); await helper.writeJSON(path.join(f.control, 'rollback-ack.json'), {
      token: f.request.token, pid: child.pid, version: f.request.currentVersion, arch: 'arm64', app, rollback: true }); }
    return child;
  } }), /exited before readiness/);
  const result = await helper.readJSON(path.join(f.control, 'result.json')); assert.equal(result.pass, false); assert.equal(result.rollbackAcknowledged, true); assert.equal(result.rollbackRestored, true);
  assert.equal((await helper.inventory(f.app)).sha256, f.request.oldHash);
});
test('alive old PID prevents all application writes', async t => {
  const f = await installFixture(t);
  await assert.rejects(helper.runInstall(f.file, { inspect: f.inspect, isAlive: () => true, timeout: 20, wait: ms => new Promise(r => setTimeout(r, Math.min(ms, 5))), launch: () => { throw new Error('must not launch'); } }), /did not exit/);
  assert.equal((await helper.inventory(f.app)).sha256, f.request.oldHash); await assert.rejects(fs.lstat(path.join(f.stage, 'previous.app')), /ENOENT/);
});
for (const late of [false, true]) test(`final install guard respects aggregate deadline: ${late ? 'expired' : 'healthy'}`, async t => {
  const f = await installFixture(t), userData = path.join(f.root, 'client-data'), updates = path.join(userData, 'updates');
  const downloadToken = 'd'.repeat(64), downloaded = path.join(updates, `stage-${downloadToken}`), archive = Buffer.from('owned test archive');
  await fs.mkdir(downloaded, { recursive: true, mode: 0o700 }); await fs.writeFile(path.join(downloaded, 'candidate.zip'), archive);
  await fs.mkdir(path.join(downloaded, 'CassQuery.app'), { mode: 0o755 });
  await helper.writeJSON(path.join(updates, 'state.json'), { schema: 1, preferences: { autoCheck: false, autoDownload: false }, lastCheck: 0,
    ready: { token: downloadToken, version: '0.3.0', archiveHash: sha(archive), inventoryHash: f.request.newHash } });
  t.mock.method(helper, 'inspectBundle', async () => ({ inventory: { sha256: f.request.newHash } }));
  t.mock.method(helper, 'copyCandidate', async () => ({ app: f.app, stage: f.stage, incoming: f.request.incoming, oldHash: f.request.oldHash, newHash: f.request.newHash }));
  let stops = 0, quits = 0, guards = 0;
  t.mock.method(helper, 'stopOwnedChild', async child => { stops++; child.exitCode = 1; });
  const childProcess = require('node:child_process');
  t.mock.method(childProcess, 'fork', (_file, args) => {
    const child = fakeChild(100987); child.disconnect = () => {};
    fs.readFile(args[0], 'utf8').then(text => child.emit('message', { type: 'ready', token: JSON.parse(text).token }));
    return child;
  });
  const originalTimeout = globalThis.setTimeout;
  t.mock.method(globalThis, 'setTimeout', (callback, ms, ...args) => originalTimeout(callback, ms === 10 * 60 * 1000 ? 500 : ms, ...args));
  const moduleFile = require.resolve('../shell/updater.cjs'), cached = require.cache[moduleFile]; delete require.cache[moduleFile];
  const fresh = require(moduleFile); t.after(() => { require.cache[moduleFile] = cached; });
  const client = await fresh.createUpdater({ version: '0.2.0', arch: 'arm64', userData, appPath: f.app, autoCheck: false, autoDownload: false,
    canInstall: async () => { guards++; if (guards === 2) await new Promise(resolve => originalTimeout(resolve, late ? 700 : 5)); return true; },
    requestQuit: () => { quits++; } });
  t.after(() => client.dispose());
  if (late) { await assert.rejects(client.install({ userInitiated: true }), /timed out/); assert.equal(quits, 0); assert.equal(stops, 1); assert.equal(client.getState().phase, 'error'); }
  else { await client.install({ userInitiated: true }); assert.equal(quits, 1); assert.equal(stops, 0); }
  assert.equal(guards, 2); // Source-only platform/helper seams; no app process or installation.
});

test('RunAsNode fuse gate checks every wire and rejects missing/truncated/disabled', async t => {
  const root = await scratch(t), app = path.join(root, 'App.app'), directory = path.join(app, 'Contents/Frameworks/Electron Framework.framework/Versions/A');
  await fs.mkdir(directory, { recursive: true }); const file = path.join(directory, 'Electron Framework');
  const sentinel = Buffer.from('dL7pKGdnNz796PbbjQWNKmHXBZaB9tsX');
  const wire = enabled => Buffer.concat([sentinel, Buffer.from([1, 9, enabled ? 49 : 48, 48, 49, 49, 48, 48, 48, 49, 49])]);
  await fs.writeFile(file, wire(true)); assert.equal(await helper.runAsNodeEnabled(app), true);
  await fs.writeFile(file, Buffer.concat([wire(true), Buffer.alloc(200), wire(true)])); assert.equal(await helper.runAsNodeEnabled(app), true);
  for (const bytes of [Buffer.alloc(20), wire(false), wire(true).subarray(0, 36), Buffer.concat([wire(true), wire(false)])]) {
    await fs.writeFile(file, bytes); await assert.rejects(helper.runAsNodeEnabled(app));
  }
});
test('explicit disabled auto-check overrides persisted preferences', async t => {
  const root = await scratch(t), directory = path.join(root, 'updates'); await fs.mkdir(directory, { mode: 0o700 });
  await helper.writeJSON(path.join(directory, 'state.json'), { schema: 1, preferences: { autoCheck: true, autoDownload: true }, lastCheck: Date.now(), ready: null });
  const client = await updater.createUpdater({ version: '0.2.0', arch: 'arm64', userData: root, appPath: '/never-read.app', autoCheck: false, autoDownload: false });
  t.after(() => client.dispose()); assert.deepEqual(client.getState().preferences, { autoCheck: false, autoDownload: false });
});
test('ZIP rejects Unicode link targets and symlink descendants', async t => {
  const root = await scratch(t), file = path.join(root, 'bad.zip');
  for (const extra of [[{ name: 'CassQuery.app/a', data: 'ΐ', mode: 0o120777 }],
    [{ name: 'CassQuery.app/a', data: 'Contents', mode: 0o120777 }, { name: 'CassQuery.app/a/child', data: 'bad' }]]) {
    await fs.writeFile(file, zip([...appEntries(), ...extra])); await assert.rejects(helper.validateZip(file));
  }
});
test('only a created child is terminated and reaped', async t => {
  const child = spawn(process.execPath, ['-e', 'setInterval(()=>{},1000)'], { stdio: 'ignore' });
  t.after(async () => { if (child.exitCode === null && child.signalCode === null) { child.kill('SIGKILL'); await new Promise(r => child.once('exit', r)); } });
  await new Promise((resolve, reject) => { child.once('spawn', resolve); child.once('error', reject); });
  await helper.stopOwnedChild(child); assert.ok(child.exitCode !== null || child.signalCode !== null);
});
