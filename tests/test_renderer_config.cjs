'use strict';
const assert = require('node:assert/strict');
const { test } = require('node:test');
const { configureRenderer, recordRendererConsole } = require('../shell/renderer_config.cjs');
const { waitFor } = require('../shell/smoke.cjs');

test('native macOS CI explicitly selects Metal and never unsafe SwiftShader', () => {
  for (const platform of ['darwin']) {
    const calls = [];
    const backend = configureRenderer({ appendSwitch: (...args) => calls.push(args) }, platform, { CASSQUERY_ANGLE: 'metal' });
    assert.equal(backend, 'metal-requested');
    assert.deepEqual(calls, [['use-angle', 'metal']]);
    for (const env of [{ CASSQUERY_SOFTWARE_GL: '1' }, { CASSQUERY_ANGLE: 'swiftshader' }, { CASSQUERY_ANGLE: 'vulkan' },
      { CASSQUERY_ANGLE: 'metal', CASSQUERY_SOFTWARE_GL: '1' }]) {
      assert.throws(() => configureRenderer({ appendSwitch: () => assert.fail('invalid backend changed switches') }, platform, env));
    }
  }
});

test('Electron 45 console errors are retained and stop readiness immediately', async () => {
  const errors = [];
  recordRendererConsole(errors, { level: 'info', message: 'status' });
  recordRendererConsole(errors, { level: 'warning', message: 'warning' });
  assert.deepEqual(errors, []);
  recordRendererConsole(errors, { level: 'error', message: 'WebGL initialization failed' });
  assert.deepEqual(errors, ['WebGL initialization failed']);
  let reads = 0;
  const win = { isDestroyed: () => false, cassqueryRendererErrors: errors,
    webContents: { executeJavaScript: async () => { reads++; return true; } } };
  await assert.rejects(waitFor(win, 'true'), /Renderer diagnostics.*WebGL initialization failed/);
  assert.equal(reads, 0, 'diagnostic failure cannot be masked by a readiness value');
});

test('ordinary desktops remain platform-default; Linux explicit software stays supported', () => {
  for (const platform of ['darwin', 'linux', 'win32']) {
    const calls = [];
    assert.equal(configureRenderer({ appendSwitch: (...args) => calls.push(args) }, platform, {}), 'desktop-default');
    assert.deepEqual(calls, []);
  }
  const calls = [];
  assert.equal(configureRenderer({ appendSwitch: (...args) => calls.push(args) }, 'linux', { CASSQUERY_SOFTWARE_GL: '1' }), 'swiftshader-requested');
  assert.deepEqual(calls, [['use-angle', 'swiftshader'], ['enable-unsafe-swiftshader']]);
});
