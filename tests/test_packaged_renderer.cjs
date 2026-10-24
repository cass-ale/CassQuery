'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');
const { Arch } = require('builder-util');
const afterPack = require('../tools/package_renderer.cjs');

const parts = ['Contents', 'Frameworks', 'Electron Framework.framework', 'Versions', 'A', 'Libraries'];
const driverName = 'libvk_swiftshader.dylib';
const aliasName = 'libvulkan.dylib';
async function fixture(t, cpuType = 0x0100000c) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'cq-renderer-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const app = path.join(root, 'CassQuery.app');
  const libraries = path.join(app, ...parts);
  await fs.mkdir(libraries, { recursive: true });
  const bytes = Buffer.alloc(32);
  bytes.writeUInt32LE(0xfeedfacf, 0); bytes.writeUInt32LE(cpuType, 4);
  const driver = path.join(libraries, driverName), alias = path.join(libraries, aliasName);
  await fs.writeFile(driver, bytes);
  return { root, app, libraries, bytes, driver, alias,
    expected: { cpuType, bytes: bytes.length, sha256: crypto.createHash('sha256').update(bytes).digest('hex') } };
}

for (const cpu of [0x0100000c, 0x01000007]) {
  test(`contained native discovery alias is idempotent: ${cpu.toString(16)}`, async (t) => {
    const f = await fixture(t, cpu);
    await afterPack.installAlias(f.app, f.expected);
    assert.equal(await fs.readlink(f.alias), driverName);
    assert.equal(await fs.realpath(f.alias), await fs.realpath(f.driver));
    assert.deepEqual(await fs.readFile(f.alias), f.bytes);
    assert.equal((await fs.lstat(f.alias)).isSymbolicLink(), true);
    await afterPack.installAlias(f.app, f.expected);
    assert.deepEqual(await fs.readFile(f.driver), f.bytes);
  });
}

test('regular alias is not overwritten, even if its bytes match', async (t) => {
  const f = await fixture(t); await fs.writeFile(f.alias, f.bytes);
  await assert.rejects(afterPack.installAlias(f.app, f.expected), /Refusing to overwrite/);
  assert.equal((await fs.lstat(f.alias)).isFile(), true);
  assert.deepEqual(await fs.readFile(f.alias), f.bytes);
});

for (const target of ['other.dylib', '/outside/libvk_swiftshader.dylib', '../../outside.dylib']) {
  test(`unexpected alias target is not overwritten: ${target}`, async (t) => {
    const f = await fixture(t); await fs.symlink(target, f.alias);
    await assert.rejects(afterPack.installAlias(f.app, f.expected), /Refusing to overwrite/);
    assert.equal(await fs.readlink(f.alias), target);
  });
}

test('driver symlink cannot select an outside file with matching bytes', async (t) => {
  const f = await fixture(t); const outside = path.join(f.root, 'outside.dylib');
  await fs.writeFile(outside, f.bytes); await fs.unlink(f.driver); await fs.symlink(outside, f.driver);
  await assert.rejects(afterPack.installAlias(f.app, f.expected), /ELOOP|symbolic link/);
  await assert.rejects(fs.lstat(f.alias), /ENOENT/);
});

test('library directory symlink cannot escape the bundle', async (t) => {
  const f = await fixture(t); const outside = path.join(f.root, 'outside');
  await fs.rename(f.libraries, outside); await fs.symlink(outside, f.libraries);
  await assert.rejects(afterPack.installAlias(f.app, f.expected), /real directory/);
  await assert.rejects(fs.lstat(path.join(outside, aliasName)), /ENOENT/);
});

test('application symlink cannot mutate another application', async (t) => {
  const f = await fixture(t); const linked = path.join(f.root, 'Linked.app'); await fs.symlink(f.app, linked);
  await assert.rejects(afterPack.installAlias(linked, f.expected), /real directory/);
  await assert.rejects(fs.lstat(f.alias), /ENOENT/);
});

test('FIFO driver is rejected without a blocking open', async (t) => {
  const f = await fixture(t); await fs.unlink(f.driver);
  const execFile = require('node:util').promisify(require('node:child_process').execFile);
  await execFile('mkfifo', [f.driver], { timeout: 3000 });
  const code = `const hook=require(${JSON.stringify(path.join(__dirname, '../tools/package_renderer.cjs'))});
    hook.installAlias(${JSON.stringify(f.app)},${JSON.stringify(f.expected)}).then(
      ()=>{console.error('FIFO unexpectedly accepted');process.exitCode=1},
      error=>{if(!/not a regular file/.test(error.message)){console.error(error);process.exitCode=1}});`;
  // A regression must fail this bounded subprocess, not hang the whole test runner.
  await execFile(process.execPath, ['-e', code], { timeout: 3000 });
  await assert.rejects(fs.lstat(f.alias), /ENOENT/);
});

test('missing driver fails closed without creating an alias', async (t) => {
  const f = await fixture(t); await fs.unlink(f.driver);
  await assert.rejects(afterPack.installAlias(f.app, f.expected), /ENOENT/);
  await assert.rejects(fs.lstat(f.alias), /ENOENT/);
});

for (const [label, bytes] of [['empty', Buffer.alloc(0)], ['not Mach-O', Buffer.alloc(32)],
  ['wrong architecture', (() => { const b = Buffer.alloc(32); b.writeUInt32LE(0xfeedfacf, 0); b.writeUInt32LE(0x01000007, 4); return b; })()]]) {
  test(`invalid native driver fails before alias creation: ${label}`, async (t) => {
    const f = await fixture(t); await fs.writeFile(f.driver, bytes);
    await assert.rejects(afterPack.installAlias(f.app, f.expected), /Mach-O architecture|archive byte size/);
    await assert.rejects(fs.lstat(f.alias), /ENOENT/);
  });
}

test('wrong byte size fails before reading or alias creation', async (t) => {
  const f = await fixture(t); await fs.truncate(f.driver, 1024);
  await assert.rejects(afterPack.installAlias(f.app, f.expected), /archive byte size/);
  await assert.rejects(fs.lstat(f.alias), /ENOENT/);
});

test('modified driver bytes fail archive identity validation', async (t) => {
  const f = await fixture(t); const changed = Buffer.from(f.bytes); changed[31] = 1; await fs.writeFile(f.driver, changed);
  await assert.rejects(afterPack.installAlias(f.app, f.expected), /pinned Electron/);
  await assert.rejects(fs.lstat(f.alias), /ENOENT/);
});

test('hook does nothing on Linux/Windows and rejects unsupported targets', async () => {
  await afterPack({ electronPlatformName: 'linux' }); await afterPack({ electronPlatformName: 'win32' });
  await assert.rejects(afterPack({ electronPlatformName: 'mas' }), /Unsupported/);
  await assert.rejects(afterPack({}), /Unsupported/);
  await assert.rejects(afterPack({ electronPlatformName: 'darwin', arch: Arch.universal }), /native arm64 or x64/);
});

test('application filename traversal is rejected before touching output', async () => {
  for (const productFilename of ['../outside', 'nested/app', 'nested\\app', '..', '.', '']) {
    await assert.rejects(afterPack({ electronPlatformName: 'darwin', arch: Arch.arm64,
      appOutDir: '/unused', packager: { appInfo: { productFilename } } }), /application filename/);
  }
});

test('production hook never trusts synthetic test driver bytes', async (t) => {
  const f = await fixture(t);
  await assert.rejects(afterPack({ electronPlatformName: 'darwin', arch: Arch.arm64,
    appOutDir: f.root, packager: { appInfo: { productFilename: 'CassQuery' } } }), /pinned (Electron|archive)/);
  await assert.rejects(fs.lstat(f.alias), /ENOENT/);
});

test('builder registers hook before sealing, without environment changes', async () => {
  const config = JSON.parse(await fs.readFile(path.join(__dirname, '../packaging/electron-builder.json'), 'utf8'));
  assert.equal(config.afterPack, './tools/package_renderer.cjs');
  assert.equal(config.mac.identity, '-'); assert.equal(config.mac.hardenedRuntime, false);
  assert.equal(config.mac.notarize, false);
});
