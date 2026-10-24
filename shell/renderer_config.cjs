'use strict';
// Explicit test backend selection. Never retries a failed backend or disables sandboxing.
function configureRenderer(commandLine, platform, env) {
  if (env.CASSQUERY_ANGLE && env.CASSQUERY_SOFTWARE_GL === '1') {
    throw new Error('Conflicting explicit hardware/software renderer requests');
  }
  const requested = env.CASSQUERY_ANGLE || (env.CASSQUERY_SOFTWARE_GL === '1' ? 'swiftshader' : '');
  if (!requested) return 'desktop-default';
  if (requested !== 'swiftshader' && !(platform === 'darwin' && requested === 'metal')) {
    throw new Error('Renderer requires explicit SwiftShader, or Metal on macOS');
  }
  commandLine.appendSwitch('use-angle', requested);
  if (requested === 'swiftshader') commandLine.appendSwitch('enable-unsafe-swiftshader');
  return `${requested}-requested`;
}
function recordRendererConsole(errors, event) {
  if (event && event.level === 'error') errors.push(String(event.message));
}
// Serialized into the existing production renderer; never creates a fallback context.
function readRendererContext() {
  const canvas = document.querySelector('#stage canvas');
  if (!canvas) throw new Error('Missing production renderer canvas');
  const gl = canvas.getContext('webgl2');
  if (!gl || gl.isContextLost()) throw new Error('Missing/lost production WebGL2 context');
  const extension = gl.getExtension('WEBGL_debug_renderer_info');
  return { context: 'webgl2', vendor: gl.getParameter(gl.VENDOR), renderer: gl.getParameter(gl.RENDERER),
    version: gl.getParameter(gl.VERSION), shading_language: gl.getParameter(gl.SHADING_LANGUAGE_VERSION),
    unmasked_vendor: extension ? gl.getParameter(extension.UNMASKED_VENDOR_WEBGL) : null,
    unmasked_renderer: extension ? gl.getParameter(extension.UNMASKED_RENDERER_WEBGL) : null,
    is_context_lost: gl.isContextLost(), drawing_buffer: [gl.drawingBufferWidth, gl.drawingBufferHeight],
    viewport: [innerWidth, innerHeight], dpr: devicePixelRatio };
}
module.exports = { configureRenderer, recordRendererConsole, readRendererContext };
