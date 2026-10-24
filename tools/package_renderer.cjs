'use strict';

const fs = require('node:fs/promises');
const { constants } = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { Arch } = require('builder-util');

// Electron 45.0.0 release-archive bytes, before electron-builder signs them.
// Chromium 156 / ANGLE 3b89d84 uses a shared Vulkan loader on macOS but its
// archive ships only SwiftShader's ICD. SwiftShader 1e80438 documents direct
// ICD loading; its VulkanTester also exercises that route on macOS.
const DRIVERS = Object.freeze({
  [Arch.arm64]: Object.freeze({ cpuType: 0x0100000c, bytes: 16475184,
    sha256: '320be4faf44f8568e3cad217bd3fd1bc775dc3fb525ea182913e311e64c6bd13' }),
  [Arch.x64]: Object.freeze({ cpuType: 0x01000007, bytes: 4481584,
    sha256: '578ab57f7213c9c08bf48bb11be9fabbb6920ce4e18f3d0bc4785ed858684fd6' }),
});
const LIBRARIES = ['Contents', 'Frameworks', 'Electron Framework.framework', 'Versions', 'A', 'Libraries'];
const DRIVER = 'libvk_swiftshader.dylib';
const ALIAS = 'libvulkan.dylib';

async function installAlias(app, expected) {
  // Do not follow directory/driver symlinks into another bundle or build input.
  if (!(await fs.lstat(app)).isDirectory())
    throw new Error(`Renderer application is not a real directory: ${app}`);
  let directory = await fs.realpath(app);
  for (const component of LIBRARIES) {
    directory = path.join(directory, component);
    if (!(await fs.lstat(directory)).isDirectory())
      throw new Error(`Renderer library directory is not a real directory: ${directory}`);
  }
  const driver = path.join(directory, DRIVER);
  const handle = await fs.open(driver, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  let bytes;
  try {
    const stat = await handle.stat();
    if (!stat.isFile()) throw new Error('SwiftShader driver is not a regular file');
    if (stat.size !== expected.bytes) throw new Error('SwiftShader driver differs from the pinned archive byte size');
    bytes = Buffer.alloc(expected.bytes);
    let offset = 0;
    while (offset < bytes.length) {
      const { bytesRead } = await handle.read(bytes, offset, bytes.length - offset, offset);
      if (!bytesRead) throw new Error('SwiftShader driver was truncated while reading');
      offset += bytesRead;
    }
    if ((await handle.read(Buffer.alloc(1), 0, 1, bytes.length)).bytesRead)
      throw new Error('SwiftShader driver grew beyond the pinned archive byte size');
  } finally { await handle.close(); }
  if (bytes.length < 32 || bytes.readUInt32LE(0) !== 0xfeedfacf || bytes.readUInt32LE(4) !== expected.cpuType)
    throw new Error('SwiftShader driver is not the expected native Mach-O architecture');
  if (crypto.createHash('sha256').update(bytes).digest('hex') !== expected.sha256)
    throw new Error('SwiftShader driver differs from the pinned Electron 45.0.0 archive');

  const alias = path.join(directory, ALIAS);
  let existing;
  try { existing = await fs.lstat(alias); }
  catch (error) { if (error.code !== 'ENOENT') throw error; }
  if (existing) {
    if (!existing.isSymbolicLink() || await fs.readlink(alias) !== DRIVER || await fs.realpath(alias) !== driver)
      throw new Error('Refusing to overwrite an unexpected Vulkan discovery alias');
    return;
  }
  // ModuleDir is Electron Framework/Versions/A in this static ANGLE build.
  // OpenLibVulkan explicitly searches Libraries/libvulkan.dylib. Keep the ICD
  // filename intact; this relative alias adds no new binary or global loader path.
  await fs.symlink(DRIVER, alias);
}

async function afterPack(context) {
  if (context.electronPlatformName === 'linux' || context.electronPlatformName === 'win32') return;
  if (context.electronPlatformName !== 'darwin') throw new Error('Unsupported renderer packaging platform');
  const expected = DRIVERS[context.arch];
  if (!expected) throw new Error('Renderer packaging requires native arm64 or x64');
  if (require('../package.json').devDependencies.electron !== '45.0.0')
    throw new Error('Refresh the reviewed SwiftShader archive pins before changing Electron');
  const name = context.packager.appInfo.productFilename;
  if (typeof name !== 'string' || !name || name === '.' || name === '..' || path.basename(name) !== name || name.includes('\\'))
    throw new Error('Invalid packaged application filename');
  await installAlias(path.join(context.appOutDir, `${name}.app`), expected);
}

module.exports = afterPack;
// The filesystem boundary is independently exercised with synthetic Mach-O bytes;
// the production hook supplies only the fixed, reviewed archive pins above.
module.exports.installAlias = installAlias;
