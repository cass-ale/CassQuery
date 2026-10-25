'use strict';
const fs = require('node:fs/promises');
const { createWriteStream } = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { Readable, Transform } = require('node:stream');
const { pipeline } = require('node:stream/promises');
const { fork, execFile } = require('node:child_process');
const { promisify } = require('node:util');
const helper = require('./update_install.cjs');
const REPOSITORY = 'cass-ale/CassQuery', REPOSITORY_ID = '1432223312';
const API = `https://api.github.com/repos/${REPOSITORY}/releases/latest`;
const RELEASES = `https://github.com/${REPOSITORY}/releases`;
const CHECK_INTERVAL = 24 * 60 * 60 * 1000;
const { requireThat: need } = helper;
function versionParts(version) {
  need(typeof version === 'string' && /^(0|[1-9]\d{0,8})\.(0|[1-9]\d{0,8})\.(0|[1-9]\d{0,8})$/.test(version), 'Update version is not canonical stable semver');
  return version.split('.').map(Number);
}
function compareVersions(a, b) {
  const x = versionParts(a), y = versionParts(b);
  for (let i = 0; i < 3; i++) if (x[i] !== y[i]) return x[i] < y[i] ? -1 : 1;
  return 0;
}
function assetRecord(asset, name, version) {
  need(asset && asset.name === name && Number.isSafeInteger(asset.id) && asset.id > 0 && Number.isSafeInteger(asset.size) && asset.size > 0 && typeof asset.digest === 'string' && /^sha256:[a-f0-9]{64}$/.test(asset.digest), 'Release asset lacks verified size/digest metadata');
  const url = `${RELEASES}/download/v${version}/${name}`;
  need(asset.browser_download_url === url, 'Release asset is outside the canonical repository');
  return { name, size: asset.size, sha256: asset.digest.slice(7), url };
}
function selectRelease(release, currentVersion, arch) {
  versionParts(currentVersion); need(['arm64', 'x64'].includes(arch), 'Unsupported update architecture');
  need(release && release.draft === false && release.prerelease === false && typeof release.tag_name === 'string' && release.tag_name.startsWith('v'), 'Release is not published stable');
  const version = release.tag_name.slice(1); versionParts(version);
  need(release.html_url === `${RELEASES}/tag/v${version}` && Array.isArray(release.assets), 'Release repository identity mismatch');
  if (compareVersions(version, currentVersion) <= 0) return null;
  const names = [`CassQuery-${version}-mac-${arch}.zip`, 'SHA256SUMS.txt'];
  const selected = names.map(name => {
    const matches = release.assets.filter(asset => asset.name === name); need(matches.length === 1, 'Missing/duplicate native release asset');
    return assetRecord(matches[0], name, version);
  });
  need(selected[0].size <= helper.MAX_ZIP && selected[1].size <= 64 * 1024, 'Release exceeds update size limits');
  return { version, arch, url: release.html_url, archive: selected[0], checksums: selected[1] };
}
function checksumFor(bytes, name) {
  need(bytes.length <= 64 * 1024, 'Checksum document exceeds limit');
  const text = new TextDecoder('utf-8', { fatal: true }).decode(bytes), records = new Map();
  for (const line of text.split(/\r?\n/).filter(Boolean)) {
    const match = /^([a-f0-9]{64}) [ *]([A-Za-z0-9._-]+)$/.exec(line);
    need(match && !records.has(match[2]), 'Malformed/duplicate release checksum'); records.set(match[2], match[1]);
  }
  need(records.has(name), 'Native archive checksum is missing'); return records.get(name);
}
function allowedURL(url, initial) {
  const value = new URL(url);
  need(value.protocol === 'https:' && !value.username && !value.password && !value.hash && (!value.port || value.port === '443'), 'Unsafe update transport URL');
  if (value.hostname === 'api.github.com') need(value.href === API && initial === API, 'Unexpected API redirect');
  else if (value.hostname === 'github.com') need(value.href === initial && initial.startsWith(RELEASES + '/download/'), 'Unexpected repository redirect');
  else need(['release-assets.githubusercontent.com', 'objects.githubusercontent.com'].includes(value.hostname) && initial.startsWith(RELEASES + '/download/') && value.pathname.startsWith(`/github-production-release-asset/${REPOSITORY_ID}/`), 'Unexpected release download host/path');
}
async function responseFor(url, fetcher, signal) {
  const initial = url;
  for (let redirects = 0; redirects <= 3; redirects++) {
    allowedURL(url, initial);
    const response = await fetcher(url, { redirect: 'manual', signal,
      headers: { Accept: url === API ? 'application/vnd.github+json' : 'application/octet-stream', 'User-Agent': 'CassQuery-Updater', 'Accept-Encoding': 'identity' } });
    if ([301, 302, 303, 307, 308].includes(response.status)) {
      need(redirects < 3 && response.headers.get('location'), 'Update redirect limit exceeded');
      const next = new URL(response.headers.get('location'), url).href; await response.body?.cancel?.(); url = next; continue;
    }
    need(response.status === 200, `Update server returned HTTP ${response.status}`, 'UPDATE_HTTP');
    need(!response.headers.get('content-encoding') || response.headers.get('content-encoding') === 'identity', 'Unexpected update content encoding');
    return response;
  }
  throw new Error('Update redirect limit exceeded');
}
function bodyStream(response) {
  need(response.body, 'Update response has no body');
  return typeof response.body.getReader === 'function' ? Readable.fromWeb(response.body) : Readable.from(response.body);
}
async function smallBody(url, limit, fetcher, signal) {
  const response = await responseFor(url, fetcher, signal), chunks = []; let size = 0;
  const advertised = response.headers.get('content-length');
  need(!advertised || (/^\d+$/.test(advertised) && Number(advertised) <= limit), 'Update response exceeds metadata limit');
  for await (const chunk of bodyStream(response)) { size += chunk.length; need(size <= limit, 'Update response exceeds metadata limit'); chunks.push(chunk); }
  return Buffer.concat(chunks);
}
async function downloadFile(asset, file, fetcher, signal, progress) {
  const response = await responseFor(asset.url, fetcher, signal);
  const advertised = response.headers.get('content-length');
  need(!advertised || (/^\d+$/.test(advertised) && Number(advertised) === asset.size), 'Update asset advertised size mismatch');
  const hash = crypto.createHash('sha256'); let bytes = 0, last = 0;
  const counter = new Transform({ transform(chunk, _encoding, done) {
    bytes += chunk.length;
    if (bytes > asset.size) return done(new Error('Update asset exceeds declared size'));
    hash.update(chunk); if (Date.now() - last > 100) { last = Date.now(); progress?.(bytes / asset.size); } done(null, chunk);
  } });
  await pipeline(bodyStream(response), counter, createWriteStream(file, { flags: 'wx', mode: 0o600 }), { signal });
  need(bytes === asset.size && hash.digest('hex') === asset.sha256, 'Update asset size or SHA-256 mismatch', 'UPDATE_DIGEST'); progress?.(1);
}
async function createUpdater(options) {
  const restartToken = process.env.CASSQUERY_UPDATE_TOKEN;
  delete process.env.CASSQUERY_UPDATE_TOKEN; // Never pass the private install nonce to workers/renderers.
  const { version, arch, appPath, onState = () => {} } = options;
  versionParts(version); need(['arm64', 'x64'].includes(arch), 'Unsupported update architecture');
  const root = await helper.ownedDirectory(path.join(options.userData, 'updates'), { create: true });
  const stateFile = path.join(root, 'state.json'), fetcher = options.fetch || globalThis.fetch;
  let preferences = { autoCheck: options.autoCheck ?? true, autoDownload: options.autoDownload ?? false }, lastCheck = 0, ready = null, selected = null;
  need(typeof preferences.autoCheck === 'boolean' && typeof preferences.autoDownload === 'boolean', 'Updater preferences must be booleans');
  let state = { schema: 1, phase: 'idle', currentVersion: version, version: null, progress: 0, error: null, installMode: 'custom-user-owned-explicit-restart' };
  let busy = false, timer, controller, disposed = false;
  try {
    const saved = await helper.readJSON(stateFile);
    need(saved.schema === 1 && typeof saved.preferences?.autoCheck === 'boolean' && typeof saved.preferences?.autoDownload === 'boolean', 'Invalid persisted updater state');
    preferences = { autoCheck: options.autoCheck ?? saved.preferences.autoCheck, autoDownload: options.autoDownload ?? saved.preferences.autoDownload }; lastCheck = Number.isFinite(saved.lastCheck) ? saved.lastCheck : 0;
    if (saved.ready && /^[a-f0-9]{64}$/.test(saved.ready.token) && compareVersions(saved.ready.version, version) > 0) {
      ready = saved.ready;
      const stage = path.join(root, `stage-${ready.token}`);
      const digest = await helper.hashFile(path.join(stage, 'candidate.zip'), helper.MAX_ZIP);
      need(digest.sha256 === ready.archiveHash, 'Persisted update archive changed');
      need((await helper.inspectBundle(path.join(stage, 'CassQuery.app'), arch, ready.version)).inventory.sha256 === ready.inventoryHash, 'Persisted update bundle changed');
      state = { ...state, phase: 'ready', version: ready.version, progress: 1 };
    }
  } catch (error) { if (error.code !== 'ENOENT') state = { ...state, phase: 'error', error: error.message }; ready = null; }
  function getState() { return { ...state, preferences: { ...preferences }, lastCheck }; }
  async function persist() { await helper.writeJSON(stateFile, { schema: 1, preferences, lastCheck, ready }); }
  function emit(update) { state = { ...state, ...update }; onState(getState()); }
  async function operation(work, timeout = 30000) {
    need(!disposed && !busy, 'Updater is busy or disposed', 'UPDATE_BUSY'); busy = true;
    controller = new AbortController(); const timeoutTimer = setTimeout(() => controller.abort(new Error('Update operation timed out')), timeout);
    try { return await work(controller.signal); }
    catch (error) { emit({ phase: 'error', error: error.message }); throw error; }
    finally { clearTimeout(timeoutTimer); controller = null; busy = false; }
  }
  async function check() {
    await operation(async signal => {
      emit({ phase: 'checking', error: null });
      const bytes = await smallBody(API, 256 * 1024, fetcher, signal);
      selected = selectRelease(JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes)), version, arch); lastCheck = Date.now();
      emit({ phase: ready ? 'ready' : selected ? 'available' : 'idle', version: ready?.version || selected?.version || null }); await persist();
    });
    if (selected && preferences.autoDownload && !ready) await download();
    return getState();
  }
  async function download() {
    need(selected, 'Check for a newer release before downloading');
    await operation(async signal => {
      const release = selected;
      emit({ phase: 'downloading', version: release.version, progress: 0, error: null });
      const sums = await smallBody(release.checksums.url, 64 * 1024, fetcher, signal);
      need(sums.length === release.checksums.size && crypto.createHash('sha256').update(sums).digest('hex') === release.checksums.sha256, 'Checksum document digest mismatch');
      need(checksumFor(sums, release.archive.name) === release.archive.sha256, 'API and published archive checksums disagree');
      const token = crypto.randomBytes(32).toString('hex'), stage = path.join(root, `stage-${token}`);
      await fs.mkdir(stage, { mode: 0o700 });
      try {
        const archive = path.join(stage, 'candidate.zip');
        await downloadFile(release.archive, archive, fetcher, signal, progress => emit({ progress }));
        emit({ phase: 'verifying' });
        const verifyArchive = async () => need((await helper.hashFile(archive, helper.MAX_ZIP)).sha256 === release.archive.sha256, 'Update archive changed during verification');
        await verifyArchive(); await helper.validateZip(archive, signal); await verifyArchive();
        need(process.platform === 'darwin', 'Mac ZIP extraction requires the native platform', 'UPDATE_PLATFORM');
        await promisify(execFile)('/usr/bin/ditto', ['-x', '-k', archive, stage], { timeout: 120000, signal, maxBuffer: 1024 * 1024 });
        await verifyArchive();
        const bundle = await helper.inspectBundle(path.join(stage, 'CassQuery.app'), arch, release.version);
        ready = { token, version: release.version, archiveHash: release.archive.sha256, inventoryHash: bundle.inventory.sha256 };
        await persist(); emit({ phase: 'ready', version: release.version, progress: 1 });
      } catch (error) { await fs.rm(stage, { recursive: true, force: true }); throw error; }
    }, 10 * 60 * 1000);
    return getState();
  }
  async function install({ userInitiated = false } = {}) {
    need(userInitiated === true, 'Restart and install requires explicit user action', 'UPDATE_CONSENT');
    need(ready && typeof options.canInstall === 'function' && typeof options.requestQuit === 'function', 'No verified update or restart/draft guard is available');
    await operation(async signal => {
      need(await options.canInstall() === true, 'Unsaved drafts or active builds block update installation', 'UPDATE_DIRTY'); signal.throwIfAborted();
      const stage = path.join(root, `stage-${ready.token}`), candidate = path.join(stage, 'CassQuery.app');
      need((await helper.hashFile(path.join(stage, 'candidate.zip'), helper.MAX_ZIP)).sha256 === ready.archiveHash, 'Update archive changed before install');
      need((await helper.inspectBundle(candidate, arch, ready.version)).inventory.sha256 === ready.inventoryHash, 'Update bundle changed before install');
      let prepared, child, control;
      try {
        prepared = await helper.copyCandidate(candidate, appPath, arch, version, ready.version); signal.throwIfAborted();
        const token = crypto.randomBytes(32).toString('hex');
        control = path.join(root, `stage-${token}`); await fs.mkdir(control, { mode: 0o700 });
        const request = { schema: 1, token, oldPid: process.pid, arch, currentVersion: version, version: ready.version, ...prepared };
        const requestFile = path.join(control, 'install.json'), helperFile = path.join(control, 'helper.cjs');
        await helper.writeJSON(requestFile, request);
        await fs.writeFile(helperFile, await fs.readFile(path.join(__dirname, 'update_install.cjs')), { flag: 'wx', mode: 0o600 });
        const environment = { ...process.env, ELECTRON_RUN_AS_NODE: '1' }; delete environment.NODE_OPTIONS; delete environment.NODE_EXTRA_CA_CERTS;
        child = fork(helperFile, [requestFile], { execPath: options.execPath || process.execPath, execArgv: [], env: environment, detached: true, stdio: ['ignore', 'ignore', 'ignore', 'ipc'] });
        if (Number.isInteger(child.pid) && child.pid > 1) child.ownedUpdateGroup = true;
        await new Promise((resolve, reject) => {
          const timeout = setTimeout(() => reject(new Error('Install helper did not become ready')), 5 * 60 * 1000);
          const fail = error => { clearTimeout(timeout); reject(error); };
          signal.addEventListener('abort', () => fail(signal.reason), { once: true });
          child.once('error', fail); child.once('exit', () => fail(new Error('Install helper exited before readiness')));
          child.on('message', message => {
            if (message?.type === 'ready' && message.token === token) { clearTimeout(timeout); resolve(); }
            else if (message?.type === 'error') fail(new Error(message.error));
          });
        });
        signal.throwIfAborted();
        need(await options.canInstall() === true, 'Draft/build state changed before restart', 'UPDATE_DIRTY');
        signal.throwIfAborted();
        emit({ phase: 'installing', error: null }); child.disconnect(); child.unref();
        await options.requestQuit();
      } catch (error) {
        if (child) await helper.stopOwnedChild(child);
        if (prepared) await fs.rm(prepared.stage, { recursive: true, force: true });
        if (control) await fs.rm(control, { recursive: true, force: true });
        throw error;
      }
    }, 10 * 60 * 1000);
    return getState();
  }
  async function reveal() {
    need(ready, 'No verified update is ready to reveal');
    const stage = path.join(root, `stage-${ready.token}`), archive = path.join(stage, 'candidate.zip');
    need((await helper.hashFile(archive, helper.MAX_ZIP)).sha256 === ready.archiveHash, 'Update archive changed before reveal');
    const revealFile = options.revealFile || (file => require('electron').shell.showItemInFolder(file));
    await revealFile(archive); return getState();
  }
  async function confirmRestart() {
    const token = restartToken;
    if (!token) return false;
    need(/^[a-f0-9]{64}$/.test(token), 'Invalid restart token');
    const stage = await helper.ownedDirectory(path.join(root, `stage-${token}`));
    const request = await helper.readJSON(path.join(stage, 'install.json'));
    const journal = await helper.readJSON(path.join(stage, 'journal.json'));
    const rollback = journal.phase === 'rollback-launching';
    need(journal.token === token && (rollback || journal.phase === 'launching'), 'Restart journal is not awaiting this acknowledgement');
    const expectedVersion = rollback ? request.currentVersion : request.version;
    const expectedHash = rollback ? request.oldHash : request.newHash;
    const app = await fs.realpath(appPath);
    need(request.token === token && expectedVersion === version && request.arch === arch && request.app === app && request.oldPid !== process.pid, 'Restart acknowledgement does not match installed app');
    need((await helper.inspectBundle(app, arch, version)).inventory.sha256 === expectedHash, 'Restarted application differs from verified update');
    // Caller invokes this only AFTER its owned worker and real renderer are ready.
    await helper.writeJSON(path.join(stage, rollback ? 'rollback-ack.json' : 'ack.json'), { token, pid: process.pid, version, arch, app, rollback });
    const deadline = Date.now() + 20000;
    let receipt;
    while (Date.now() < deadline) {
      try { receipt = await helper.readJSON(path.join(stage, 'result.json')); break; }
      catch (error) { if (error.code !== 'ENOENT') throw error; }
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    need(receipt && receipt.token === token && typeof receipt.pass === 'boolean', 'Install helper did not commit a matching result');
    ready = null; await persist();
    emit({ phase: receipt.pass ? 'idle' : 'error', version: null, progress: 0, error: receipt.pass ? null : receipt.error });
    return true;
  }
  function schedule() {
    clearInterval(timer);
    if (preferences.autoCheck) {
      timer = setInterval(() => { if (!busy && !disposed) check().catch(error => emit({ phase: 'error', error: error.message })); }, CHECK_INTERVAL); timer.unref?.();
      if (Date.now() - lastCheck >= CHECK_INTERVAL) queueMicrotask(() => { if (!busy && !disposed) check().catch(error => emit({ phase: 'error', error: error.message })); });
    }
  }
  async function setPreferences(update) {
    need(update && Object.keys(update).every(k => ['autoCheck', 'autoDownload'].includes(k)) && Object.values(update).every(v => typeof v === 'boolean'), 'Invalid updater preferences');
    preferences = { ...preferences, ...update }; await persist(); schedule(); emit({}); return getState();
  }
  function dispose() { disposed = true; clearInterval(timer); controller?.abort(new Error('Updater disposed')); }
  await persist(); onState(getState()); schedule();
  return { check, download, install, reveal, confirmRestart, setPreferences, getState, dispose };
}
module.exports = { createUpdater, selectRelease, compareVersions, checksumFor, allowedURL, responseFor, smallBody, downloadFile, API, RELEASES };
