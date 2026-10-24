'use strict';
const assert = require('node:assert/strict');
const { test } = require('node:test');
const { configureRenderer, recordRendererConsole, readRendererContext } = require('../shell/renderer_config.cjs');
const vm = require('node:vm');
const { waitFor } = require('../shell/smoke.cjs');

test('explicit Mac Metal and SwiftShader are supported without backend retry', () => {
  for (const requested of ['metal', 'swiftshader']) {
    const calls = [];
    const backend = configureRenderer({ appendSwitch: (...args) => calls.push(args) }, 'darwin', { CASSQUERY_ANGLE: requested });
    assert.equal(backend, `${requested}-requested`);
    assert.deepEqual(calls, requested === 'metal' ? [['use-angle', 'metal']] : [['use-angle', 'swiftshader'], ['enable-unsafe-swiftshader']]);
  }
  for (const [platform, env] of [['darwin', { CASSQUERY_ANGLE: 'vulkan' }], ['linux', { CASSQUERY_ANGLE: 'metal' }],
    ['darwin', { CASSQUERY_ANGLE: 'metal', CASSQUERY_SOFTWARE_GL: '1' }], ['darwin', { CASSQUERY_ANGLE: 'swiftshader', CASSQUERY_SOFTWARE_GL: '1' }]]) {
    assert.throws(() => configureRenderer({ appendSwitch: () => assert.fail('invalid backend changed switches') }, platform, env));
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

test('telemetry reads existing WebGL2 and never invents missing/lost/backend evidence', () => {
  for (const mode of ['complete', 'extension-missing', 'context-missing', 'context-lost']) {
    const contexts = [];
    const gl = { VENDOR: 1, RENDERER: 2, VERSION: 3, SHADING_LANGUAGE_VERSION: 4,
      drawingBufferWidth: 1280, drawingBufferHeight: 800,
      isContextLost: () => mode === 'context-lost',
      getExtension: () => mode === 'extension-missing' ? null : { UNMASKED_VENDOR_WEBGL: 5, UNMASKED_RENDERER_WEBGL: 6 },
      getParameter: (name) => ({ 1: 'generic', 2: 'WebKit WebGL', 3: 'WebGL 2', 4: 'GLSL', 5: 'Google', 6: 'ANGLE SwiftShader' })[name] };
    const globals = { innerWidth: 640, innerHeight: 400, devicePixelRatio: 2,
      document: { querySelector: () => ({ getContext: (kind) => { contexts.push(kind); return mode === 'context-missing' ? null : gl; } }) } };
    const read = () => vm.runInNewContext(`(${readRendererContext.toString()})()`, globals);
    if (mode === 'context-missing' || mode === 'context-lost') assert.throws(read, /Missing\/lost/);
    else {
      const result = JSON.parse(JSON.stringify(read()));
      assert.equal(result.context, 'webgl2'); assert.equal(result.is_context_lost, false);
      assert.deepEqual(result.drawing_buffer, [1280, 800]); assert.deepEqual(result.viewport, [640, 400]);
      assert.equal(result.unmasked_renderer, mode === 'extension-missing' ? null : 'ANGLE SwiftShader');
    }
    assert.deepEqual(contexts, ['webgl2'], 'no fallback context/API can substitute for actual evidence');
  }
});
