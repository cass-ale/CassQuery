'use strict';
// Standalone helper: only Node built-ins. No privilege escalation, network or project I/O.
// Electron virtualizes .asar as directories. Package custody must hash the actual
// archive bytes in both the GUI importer and copied RunAsNode helper, without
// changing process.noAsar for the rest of the application.
const physicalFs = process.versions.electron ? require('original-fs') : require('node:fs');
const fs = physicalFs.promises;
const { constants, createReadStream } = physicalFs;
const path = require('node:path');
const crypto = require('node:crypto');
const zlib = require('node:zlib');
const { Writable } = require('node:stream');
const { pipeline } = require('node:stream/promises');
const { promisify } = require('node:util');
const { execFile, spawn } = require('node:child_process');
const run = promisify(execFile);
const MAX_ZIP = 500 * 1024 ** 2, MAX_TREE = 2 * 1024 ** 3, MAX_ENTRIES = 10000;
const TOKEN = /^[a-f0-9]{64}$/;
function requireThat(ok, message, code = 'UPDATE_INVALID') {
  if (!ok) throw Object.assign(new Error(message), { code });
}
async function ownedDirectory(directory, { create = false } = {}) {
  const absolute = path.resolve(directory);
  if (create) await fs.mkdir(absolute, { recursive: true, mode: 0o700 });
  const stat = await fs.lstat(absolute);
  requireThat(stat.isDirectory() && stat.uid === process.getuid() && !(stat.mode & 0o022), 'Update directory must be a nonsymlink, user-owned, non-shared directory');
  return fs.realpath(absolute);
}
async function openRegular(file, maxBytes) {
  const handle = await fs.open(file, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  try {
    const stat = await handle.stat();
    requireThat(stat.isFile() && stat.size <= maxBytes, 'Update file is not regular or exceeds its size limit');
    return { handle, stat };
  } catch (error) { await handle.close(); throw error; }
}
async function readJSON(file) {
  const { handle, stat } = await openRegular(file, 128 * 1024);
  try {
    requireThat(stat.uid === process.getuid() && !(stat.mode & 0o022), 'Update record permissions are unsafe');
    const bytes = Buffer.alloc(stat.size); let offset = 0;
    while (offset < bytes.length) {
      const { bytesRead } = await handle.read(bytes, offset, bytes.length - offset, offset);
      requireThat(bytesRead > 0, 'Truncated update record'); offset += bytesRead;
    }
    const value = JSON.parse(bytes.toString('utf8'));
    requireThat(value && typeof value === 'object' && !Array.isArray(value), 'Invalid update record');
    return value;
  } finally { await handle.close(); }
}
async function writeJSON(file, value) {
  const directory = await ownedDirectory(path.dirname(file));
  requireThat(path.basename(file) === file.slice(file.lastIndexOf(path.sep) + 1), 'Invalid update record path');
  try { requireThat((await fs.lstat(file)).isFile(), 'Refusing to replace redirected update record'); }
  catch (error) { if (error.code !== 'ENOENT') throw error; }
  const temporary = path.join(directory, `.record-${crypto.randomUUID()}`);
  const handle = await fs.open(temporary, 'wx', 0o600);
  try { await handle.writeFile(JSON.stringify(value)); await handle.sync(); }
  finally { await handle.close(); }
  try { await fs.rename(temporary, file); }
  finally { await fs.rm(temporary, { force: true }); }
}
async function hashFile(file, maxBytes = MAX_TREE) {
  const { handle, stat } = await openRegular(file, maxBytes);
  const hash = crypto.createHash('sha256'); let bytes = 0;
  try {
    if (stat.size) for await (const chunk of createReadStream(file, { fd: handle.fd, autoClose: false, start: 0, end: stat.size - 1 })) {
      bytes += chunk.length; requireThat(bytes <= stat.size, 'Update file grew while reading'); hash.update(chunk);
    }
    const after = await handle.stat();
    requireThat(bytes === stat.size && ['size', 'mtimeMs', 'ctimeMs', 'ino', 'dev'].every(key => after[key] === stat[key]), 'Update file changed while hashing');
    return { sha256: hash.digest('hex'), bytes };
  } finally { await handle.close(); }
}
function archiveName(bytes, flags) {
  requireThat((flags & 0x800) || bytes.every(b => b < 128), 'ZIP filename has ambiguous encoding');
  const name = new TextDecoder('utf-8', { fatal: true }).decode(bytes);
  requireThat(name.length && name.length <= 4096 && /^[\x20-\x7e]+$/.test(name) && !/[\\:\0]/.test(name) && !name.startsWith('/'), 'Unsafe or non-ASCII ZIP filename');
  const parts = name.replace(/\/$/, '').split('/');
  requireThat(parts.length <= 40 && parts.every(p => p && p !== '.' && p !== '..') && ['CassQuery.app', '__MACOSX'].includes(parts[0]), 'ZIP escapes the application');
  return name;
}
// Validate metadata, local-header agreement, bounds, CRC and symlinks BEFORE ditto.
async function validateZip(file, signal) {
  const { handle, stat } = await openRegular(file, MAX_ZIP);
  const read = async (offset, length) => {
    requireThat(Number.isSafeInteger(offset) && offset >= 0 && length >= 0 && offset + length <= stat.size, 'ZIP range exceeds archive');
    const bytes = Buffer.alloc(length); let n = 0;
    while (n < length) { const result = await handle.read(bytes, n, length - n, offset + n); requireThat(result.bytesRead > 0, 'Truncated ZIP'); n += result.bytesRead; }
    return bytes;
  };
  try {
    requireThat(stat.size >= 22, 'Empty ZIP');
    const tail = await read(Math.max(0, stat.size - 65557), Math.min(stat.size, 65557));
    let end = -1;
    for (let i = tail.length - 22; i >= 0; i--) if (tail.readUInt32LE(i) === 0x06054b50 && i + 22 + tail.readUInt16LE(i + 20) === tail.length) { end = i; break; }
    requireThat(end >= 0 && tail.readUInt16LE(end + 4) === 0 && tail.readUInt16LE(end + 6) === 0, 'Unsupported ZIP layout');
    const count = tail.readUInt16LE(end + 10), length = tail.readUInt32LE(end + 12), start = tail.readUInt32LE(end + 16);
    const endOffset = stat.size - tail.length + end;
    requireThat(count > 0 && count <= MAX_ENTRIES && count === tail.readUInt16LE(end + 8) && length <= 8 * 1024 ** 2 && start + length === endOffset, 'ZIP64/multidisk/oversized ZIP is unsupported');
    const central = await read(start, length), entries = [], seen = new Map(); let cursor = 0, total = 0;
    for (let i = 0; i < count; i++) {
      requireThat(cursor + 46 <= central.length && central.readUInt32LE(cursor) === 0x02014b50, 'Invalid ZIP central directory');
      const flags = central.readUInt16LE(cursor + 8), method = central.readUInt16LE(cursor + 10);
      const crc = central.readUInt32LE(cursor + 16), compressed = central.readUInt32LE(cursor + 20), size = central.readUInt32LE(cursor + 24);
      const nameSize = central.readUInt16LE(cursor + 28), extra = central.readUInt16LE(cursor + 30), comment = central.readUInt16LE(cursor + 32);
      requireThat(cursor + 46 + nameSize + extra + comment <= central.length && !(flags & ~0x80e) && [0, 8].includes(method) && central.readUInt16LE(cursor + 34) === 0, 'Unsupported ZIP metadata');
      const rawName = central.subarray(cursor + 46, cursor + 46 + nameSize), name = archiveName(rawName, flags);
      const key = name.replace(/\/$/, '').toLowerCase(), mode = central.readUInt32LE(cursor + 38) >>> 16, kind = mode & 0o170000;
      requireThat(!seen.has(key) && [0, 0o100000, 0o040000, 0o120000].includes(kind) && !(mode & 0o7000), 'Duplicate/colliding/special ZIP entry');
      const directory = name.endsWith('/'), link = kind === 0o120000;
      requireThat(!kind || directory === (kind === 0o040000), 'ZIP path/type mismatch');
      requireThat(!directory || size === 0, 'ZIP directory has data');
      requireThat(size < 0xffffffff && compressed < 0xffffffff && (!link || (size > 0 && size <= 4096 && name.startsWith('CassQuery.app/'))), 'Unsupported ZIP entry size');
      total += size; requireThat(total <= MAX_TREE, 'ZIP expanded size exceeds limit');
      const entry = { name, key, directory, link, flags, method, crc, compressed, size, offset: central.readUInt32LE(cursor + 42), rawName };
      entries.push(entry); seen.set(key, entry); cursor += 46 + nameSize + extra + comment;
    }
    requireThat(cursor === central.length && seen.has('cassquery.app/contents/info.plist') && seen.has('cassquery.app/contents/macos/cassquery'), 'ZIP lacks required app or has trailing metadata');
    for (const entry of entries) {
      const parts = entry.key.split('/'); parts.pop();
      while (parts.length) { const ancestor = seen.get(parts.join('/')); requireThat(!ancestor || ancestor.directory, 'ZIP writes through symlink/file ancestor'); parts.pop(); }
      const header = await read(entry.offset, 30);
      requireThat(header.readUInt32LE(0) === 0x04034b50 && header.readUInt16LE(6) === entry.flags && header.readUInt16LE(8) === entry.method, 'ZIP local header disagrees');
      const n = header.readUInt16LE(26), x = header.readUInt16LE(28);
      requireThat((await read(entry.offset + 30, n)).equals(entry.rawName), 'ZIP local filename disagrees');
      if (!(entry.flags & 8)) requireThat(header.readUInt32LE(14) === entry.crc && header.readUInt32LE(18) === entry.compressed && header.readUInt32LE(22) === entry.size, 'ZIP local sizes/CRC disagree');
      entry.data = entry.offset + 30 + n + x; entry.end = entry.data + entry.compressed;
      requireThat(entry.end <= start, 'ZIP data overlaps directory');
    }
    const ranges = [...entries].sort((a, b) => a.offset - b.offset);
    for (let i = 1; i < ranges.length; i++) requireThat(ranges[i].offset >= ranges[i - 1].end, 'Overlapping ZIP entries');
    for (const entry of entries) {
      signal?.throwIfAborted(); let bytes = 0, crc = 0; const target = [];
      if (entry.compressed) {
        const input = createReadStream(file, { fd: handle.fd, autoClose: false, start: entry.data, end: entry.end - 1 });
        const sink = new Writable({ write(chunk, _encoding, done) {
          bytes += chunk.length;
          if (bytes > entry.size) return done(new Error('ZIP expands beyond declared size'));
          crc = zlib.crc32(chunk, crc); if (entry.link) target.push(chunk); done();
        } });
        if (entry.method === 8) await pipeline(input, zlib.createInflateRaw(), sink, { signal });
        else await pipeline(input, sink, { signal });
      }
      requireThat(bytes === entry.size && crc === entry.crc, 'ZIP CRC/length mismatch');
      if (entry.link) {
        const value = new TextDecoder('utf-8', { fatal: true }).decode(Buffer.concat(target));
        requireThat(value && /^[\x20-\x7e]+$/.test(value) && !/[\\:\0]/.test(value) && !value.startsWith('/'), 'Unsafe or non-ASCII ZIP symlink');
        entry.target = value;
      }
    }
    // Resolve chained links lexically, including links traversed before '..'.
    const resolve = (parts, expansions = 0) => {
      const resolved = [];
      while (parts.length) {
        const part = parts.shift(); if (!part || part === '.') continue;
        if (part === '..') { requireThat(resolved.length > 1, 'ZIP symlink escapes app'); resolved.pop(); continue; }
        resolved.push(part); const link = seen.get(resolved.join('/').toLowerCase());
        if (link?.link) { requireThat(++expansions <= 64, 'ZIP symlink cycle'); resolved.pop(); parts.unshift(...link.target.split('/')); }
      }
      requireThat(resolved[0] === 'CassQuery.app' && seen.has(resolved.join('/').toLowerCase()), 'ZIP symlink is dangling or escapes app');
    };
    for (const entry of entries.filter(e => e.link)) resolve(entry.name.split('/'));
    return { entries: count, expandedBytes: total };
  } finally { await handle.close(); }
}
async function inventory(app) {
  const rootStat = await fs.lstat(app);
  requireThat(rootStat.isDirectory() && !(rootStat.mode & 0o7022), 'Application root is redirected or has unsafe permissions');
  const root = await fs.realpath(app), records = [{ path: '.', kind: 'directory', mode: rootStat.mode & 0o777 }]; let bytes = 0;
  async function visit(directory) {
    for (const name of (await fs.readdir(directory)).sort()) {
      requireThat(records.length < MAX_ENTRIES, 'Application inventory exceeds entry limit');
      const file = path.join(directory, name), stat = await fs.lstat(file), relative = path.relative(root, file);
      requireThat(!(stat.mode & 0o7000), 'Unsafe application mode');
      if (stat.isSymbolicLink()) {
        const target = await fs.readlink(file), real = await fs.realpath(file);
        requireThat(!/[\0]/.test(target) && !path.isAbsolute(target) && (real === root || real.startsWith(root + path.sep)), 'Application link escapes bundle');
        records.push({ path: relative, kind: 'link', mode: stat.mode & 0o777, target });
      } else if (stat.isDirectory()) { requireThat(!(stat.mode & 0o022), 'Application directory is shared-writable'); records.push({ path: relative, kind: 'directory', mode: stat.mode & 0o777 }); await visit(file); }
      else {
        requireThat(stat.isFile() && !(stat.mode & 0o022), 'Application contains a special or shared-writable file'); bytes += stat.size; requireThat(bytes <= MAX_TREE, 'Application exceeds size limit');
        records.push({ path: relative, kind: 'file', mode: stat.mode & 0o777, ...await hashFile(file, MAX_TREE) });
      }
    }
  }
  await visit(root);
  return { sha256: crypto.createHash('sha256').update(JSON.stringify(records)).digest('hex'), bytes, entries: records.length };
}
async function inspectBundle(app, arch, version) {
  requireThat(process.platform === 'darwin', 'Native Mac installation verification is unavailable', 'UPDATE_PLATFORM');
  requireThat(['arm64', 'x64'].includes(arch), 'Unsupported update architecture');
  const root = await fs.realpath(app);
  requireThat((await fs.lstat(app)).isDirectory(), 'Application root must not be a symlink');
  const plistPath = path.join(root, 'Contents/Info.plist'); await hashFile(plistPath, 128 * 1024);
  const { stdout } = await run('/usr/bin/plutil', ['-convert', 'json', '-o', '-', plistPath], { timeout: 10000, maxBuffer: 256 * 1024 });
  const plist = JSON.parse(stdout);
  requireThat(plist.CFBundleIdentifier === 'com.cassquery.desktop' && plist.CFBundleShortVersionString === version && plist.CFBundleVersion === version && plist.CFBundleExecutable === 'CassQuery', 'Update bundle identity/version mismatch');
  requireThat(typeof plist.LSMinimumSystemVersion === 'string' && /^\d+\.\d+(?:\.\d+)?$/.test(plist.LSMinimumSystemVersion), 'Update bundle lacks a valid minimum macOS version');
  const hostVersion = (await run('/usr/bin/sw_vers', ['-productVersion'], { timeout: 10000, maxBuffer: 1024 })).stdout.trim().split('.').map(Number);
  const minimum = plist.LSMinimumSystemVersion.split('.').map(Number);
  let supported = true;
  for (let i = 0; i < 3; i++) { if ((hostVersion[i] || 0) > (minimum[i] || 0)) break; if ((hostVersion[i] || 0) < (minimum[i] || 0)) { supported = false; break; } }
  requireThat(supported, 'Update requires a newer macOS version', 'UPDATE_PLATFORM');
  const executable = path.join(root, 'Contents/MacOS/CassQuery');
  const { handle } = await openRegular(executable, MAX_TREE);
  try {
    const bytes = Buffer.alloc(32); requireThat((await handle.read(bytes, 0, 32, 0)).bytesRead === 32 && bytes.readUInt32LE(0) === 0xfeedfacf && bytes.readUInt32LE(4) === (arch === 'arm64' ? 0x0100000c : 0x01000007), 'Update executable architecture mismatch');
  } finally { await handle.close(); }
  await fs.access(executable, constants.X_OK);
  await run('/usr/bin/codesign', ['--verify', '--deep', '--strict', root], { timeout: 60000, maxBuffer: 1024 * 1024 });
  const signature = await run('/usr/bin/codesign', ['-dvvv', root], { timeout: 10000, maxBuffer: 256 * 1024 });
  requireThat(/^Identifier=com\.cassquery\.desktop$/m.test(signature.stderr) && /Signature=adhoc(?:\r?\n|$)/.test(signature.stderr) && /TeamIdentifier=not set/.test(signature.stderr), 'Current update channel requires the reviewed ad-hoc signing profile');
  return { version, arch, app: root, executable, minimumOS: plist.LSMinimumSystemVersion, inventory: await inventory(root) };
}
async function runAsNodeEnabled(app) {
  const file = path.join(app, 'Contents/Frameworks/Electron Framework.framework/Versions/A/Electron Framework');
  const { handle, stat } = await openRegular(file, MAX_TREE);
  const sentinel = Buffer.from('dL7pKGdnNz796PbbjQWNKmHXBZaB9tsX');
  const offsets = new Set(); let carry = Buffer.alloc(0), position = 0;
  try {
    for await (const chunk of createReadStream(file, { fd: handle.fd, autoClose: false, start: 0, end: stat.size - 1 })) {
      const data = Buffer.concat([carry, chunk]);
      for (let i = data.indexOf(sentinel); i >= 0; i = data.indexOf(sentinel, i + 1)) {
        if (i + 34 > data.length) break;
        const version = data[i + 32], length = data[i + 33];
        requireThat(version === 1 && length > 0 && length <= 64, 'Unsupported Electron fuse wire');
        if (i + 34 + length > data.length) break;
        const offset = position - carry.length + i;
        if (!offsets.has(offset)) { requireThat(data[i + 34] === 49, 'Electron RunAsNode is disabled; automatic install is unavailable'); offsets.add(offset); }
      }
      position += chunk.length; carry = data.subarray(Math.max(0, data.length - 128));
    }
    for (let i = carry.indexOf(sentinel); i >= 0; i = carry.indexOf(sentinel, i + 1)) {
      const offset = position - carry.length + i;
      if (!offsets.has(offset)) requireThat(false, 'Truncated Electron fuse wire');
    }
    requireThat(offsets.size > 0 && offsets.size <= 4 && (await handle.stat()).size === stat.size, 'Missing/changed Electron fuse wire');
    return true;
  } finally { await handle.close(); }
}
async function copyCandidate(candidate, appPath, arch, currentVersion, version) {
  const app = await fs.realpath(appPath), parent = await ownedDirectory(path.dirname(app));
  const stat = await fs.lstat(appPath);
  requireThat(stat.isDirectory() && stat.uid === process.getuid(), 'Install requires a user-owned application', 'UPDATE_LOCATION');
  await fs.access(parent, constants.W_OK);
  const old = await inspectBundle(app, arch, currentVersion), next = await inspectBundle(candidate, arch, version);
  await runAsNodeEnabled(app); await runAsNodeEnabled(next.app);
  const stage = await fs.mkdtemp(path.join(parent, '.cassquery-update-'));
  const incoming = path.join(stage, 'incoming.app');
  try {
    await fs.cp(next.app, incoming, { recursive: true, dereference: false, verbatimSymlinks: true, errorOnExist: true, force: false });
    requireThat((await inspectBundle(incoming, arch, version)).inventory.sha256 === next.inventory.sha256, 'Copied update inventory mismatch');
    requireThat((await fs.stat(stage)).dev === (await fs.stat(app)).dev, 'Install staging must be on the application filesystem');
    return { app, stage, incoming, oldHash: old.inventory.sha256, newHash: next.inventory.sha256 };
  } catch (error) { await fs.rm(stage, { recursive: true, force: true }); throw error; }
}
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
function alive(pid) { try { process.kill(pid, 0); return true; } catch (error) { if (error.code === 'ESRCH') return false; throw error; } }
async function stopOwnedChild(child) {
  const running = () => child.exitCode === null && child.signalCode === null;
  const groupAlive = () => {
    if (!child.ownedUpdateGroup) return false;
    try { process.kill(-child.pid, 0); return true; }
    catch (error) { if (error.code === 'ESRCH') return false; throw error; }
  };
  const signal = name => {
    if (child.ownedUpdateGroup) {
      // A detached session's PGID stays reserved while its descendants exist.
      // If a reaped leader PID is alive again, ownership cannot be established.
      requireThat(running() || !alive(child.pid), 'Refusing to signal a reused update-process PID');
      try { process.kill(-child.pid, name); } catch (error) { if (error.code !== 'ESRCH') throw error; }
    } else if (running()) child.kill(name);
  };
  if (!running() && !groupAlive()) { child.updateReaped = true; child.updateGroupGone = true; return; }
  signal('SIGTERM');
  let end = Date.now() + 10000;
  while ((running() || groupAlive()) && Date.now() < end) await sleep(50);
  if (running() || groupAlive()) signal('SIGKILL');
  end = Date.now() + 5000;
  while ((running() || groupAlive()) && Date.now() < end) await sleep(50);
  requireThat(!running() && !groupAlive(), 'Update process was not reaped; original backup retained');
  child.updateReaped = true; child.updateGroupGone = true;
}
async function runInstall(requestFile, { inspect = inspectBundle, isAlive = alive, launch, wait = sleep, timeout = 60000 } = {}) {
  const request = await readJSON(requestFile);
  requireThat(request.schema === 1 && TOKEN.test(request.token) && ['arm64', 'x64'].includes(request.arch) && ['oldHash', 'newHash'].every(key => TOKEN.test(request[key])) && ['currentVersion', 'version'].every(key => typeof request[key] === 'string' && /^(0|[1-9]\d{0,8})\.(0|[1-9]\d{0,8})\.(0|[1-9]\d{0,8})$/.test(request[key])) && Number.isInteger(request.oldPid) && request.oldPid > 1 && request.oldPid !== process.pid, 'Invalid install request');
  const beforeVersion = request.currentVersion.split('.').map(Number), afterVersion = request.version.split('.').map(Number);
  let newer = false;
  for (let i = 0; i < 3; i++) { if (afterVersion[i] > beforeVersion[i]) { newer = true; break; } if (afterVersion[i] < beforeVersion[i]) break; }
  requireThat(newer, 'Install helper rejects version downgrade or identical version');
  const root = await ownedDirectory(path.dirname(requestFile)), stage = await ownedDirectory(request.stage);
  const app = path.resolve(request.app), incoming = path.join(stage, 'incoming.app'), backup = path.join(stage, 'previous.app'), failed = path.join(stage, 'failed.app');
  requireThat(path.basename(root) === `stage-${request.token}` && path.basename(stage).startsWith('.cassquery-update-') && path.dirname(stage) === await ownedDirectory(path.dirname(app)) && path.resolve(request.incoming) === incoming && requestFile === path.join(root, 'install.json'), 'Install request escaped owned staging');
  const resultFile = path.join(root, 'result.json'), ackFile = path.join(root, 'ack.json');
  for (const reserved of [backup, failed]) {
    try { await fs.lstat(reserved); requireThat(false, 'Unexpected existing install backup/failed target'); }
    catch (error) { if (error.code !== 'ENOENT') throw error; }
  }
  requireThat((await inspect(app, request.arch, request.currentVersion)).inventory.sha256 === request.oldHash && (await inspect(incoming, request.arch, request.version)).inventory.sha256 === request.newHash, 'Application changed before install');
  process.send?.({ type: 'ready', token: request.token });
  let moved = false, placed = false, oldExited = false, child;
  try {
    const quitDeadline = Date.now() + timeout;
    while (isAlive(request.oldPid)) { requireThat(Date.now() < quitDeadline, 'Old application did not exit; update was not installed', 'UPDATE_QUIT_TIMEOUT'); await wait(100); }
    oldExited = true;
    requireThat((await inspect(app, request.arch, request.currentVersion)).inventory.sha256 === request.oldHash && (await inspect(incoming, request.arch, request.version)).inventory.sha256 === request.newHash, 'Application changed before replacement');
    await writeJSON(path.join(root, 'journal.json'), { token: request.token, phase: 'swapping', app, backup, incoming });
    await fs.rename(app, backup); moved = true;
    await fs.rename(incoming, app); placed = true;
    requireThat((await inspect(app, request.arch, request.version)).inventory.sha256 === request.newHash, 'Installed application inventory mismatch');
    const environment = { ...process.env, CASSQUERY_UPDATE_TOKEN: request.token };
    delete environment.ELECTRON_RUN_AS_NODE; delete environment.NODE_OPTIONS; delete environment.NODE_EXTRA_CA_CERTS;
    await writeJSON(path.join(root, 'journal.json'), { token: request.token, phase: 'launching', app, backup });
    let launchError;
    child = launch ? await launch(app, environment) : spawn(path.join(app, 'Contents/MacOS/CassQuery'), [], { env: environment, stdio: 'ignore', detached: true });
    child.once?.('error', error => { launchError = error; });
    if (!launch && Number.isInteger(child.pid) && child.pid > 1) child.ownedUpdateGroup = true;
    requireThat(Number.isInteger(child.pid) && child.pid > 1, 'Updated application could not be spawned');
    await writeJSON(path.join(root, 'launch.json'), { token: request.token, pid: child.pid, pgid: child.ownedUpdateGroup ? child.pid : null, version: request.version, app });
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      if (launchError) throw launchError;
      requireThat(child.exitCode === null && child.signalCode === null, 'Updated application exited before readiness');
      try {
        const ack = await readJSON(ackFile);
        requireThat(ack.token === request.token && ack.pid === child.pid && ack.version === request.version && ack.arch === request.arch && ack.app === app && ack.rollback !== true, 'Invalid restarted-application acknowledgement');
        await writeJSON(path.join(root, 'journal.json'), { token: request.token, phase: 'complete', app, backup });
        await writeJSON(resultFile, { token: request.token, pass: true, version: request.version, backup, updatedPid: child.pid, pgid: child.ownedUpdateGroup ? child.pid : null, readyAcknowledgement: ack });
        return;
      } catch (error) { if (error.code !== 'ENOENT') throw error; }
      await wait(100);
    }
    throw Object.assign(new Error('Updated worker/renderer did not acknowledge readiness'), { code: 'UPDATE_READY_TIMEOUT' });
  } catch (error) {
    let rollbackError, restoredPid, rollbackAcknowledged = false;
    try {
      if (child) await stopOwnedChild(child);
      if (placed) {
        requireThat((await inventory(app)).sha256 === request.newHash, 'Refusing to overwrite an independently changed application during rollback');
        await fs.rename(app, failed);
      }
      if (moved) await fs.rename(backup, app);
      if (oldExited) {
        requireThat((await inspect(app, request.arch, request.currentVersion)).inventory.sha256 === request.oldHash, 'Restored application differs from original');
        await writeJSON(path.join(root, 'journal.json'), { token: request.token, phase: 'rollback-launching', app, backup });
        const environment = { ...process.env, CASSQUERY_UPDATE_TOKEN: request.token }; delete environment.ELECTRON_RUN_AS_NODE; delete environment.NODE_OPTIONS; delete environment.NODE_EXTRA_CA_CERTS;
        const restored = launch ? await launch(app, environment) : spawn(path.join(app, 'Contents/MacOS/CassQuery'), [], { env: environment, stdio: 'ignore', detached: true });
        let restoredError; restored.once?.('error', failure => { restoredError = failure; });
        if (!launch && Number.isInteger(restored.pid) && restored.pid > 1) restored.ownedUpdateGroup = true;
        restoredPid = restored.pid;
        const recoveryDeadline = Date.now() + timeout;
        while (Date.now() < recoveryDeadline) {
          if (restoredError) throw restoredError;
          requireThat(restored.exitCode === null && restored.signalCode === null, 'Restored application exited before readiness');
          try {
            const ack = await readJSON(path.join(root, 'rollback-ack.json'));
            requireThat(ack.token === request.token && ack.pid === restored.pid && ack.version === request.currentVersion && ack.arch === request.arch && ack.app === app && ack.rollback === true, 'Invalid rollback readiness acknowledgement');
            rollbackAcknowledged = true; break;
          } catch (failure) { if (failure.code !== 'ENOENT') throw failure; }
          await wait(100);
        }
        requireThat(rollbackAcknowledged, 'Original app restored but readiness was not confirmed');
        restored.unref?.();
      }
    } catch (failure) { rollbackError = failure.message; }
    await writeJSON(resultFile, { token: request.token, pass: false, error: error.message, rollbackError, backup, updatedPid: child?.pid, updatedGroup: child?.ownedUpdateGroup ? child.pid : null, updatedExit: child?.exitCode, updatedSignal: child?.signalCode, updatedReaped: !child || child.updateReaped === true, updatedGroupGone: !child || child.updateGroupGone === true, restoredPid, rollbackRestored: oldExited && !rollbackError && rollbackAcknowledged, rollbackAcknowledged });
    throw error;
  }
}
module.exports = { requireThat, ownedDirectory, readJSON, writeJSON, hashFile, validateZip, inventory, inspectBundle, runAsNodeEnabled, copyCandidate, stopOwnedChild, runInstall, MAX_ZIP, MAX_TREE };
if (require.main === module) {
  const file = process.argv[2];
  runInstall(file).catch(error => { if (process.connected) process.send({ type: 'error', error: error.message }); console.error(error.message); process.exitCode = 1; });
}
