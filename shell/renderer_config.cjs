'use strict';
// Explicit test backend selection. Never retries a failed backend or disables sandboxing.
function configureRenderer(commandLine, platform, env) {
  if (env.CASSQUERY_ANGLE && env.CASSQUERY_SOFTWARE_GL === '1') {
    throw new Error('Conflicting explicit hardware/software renderer requests');
  }
  const requested = env.CASSQUERY_ANGLE || (env.CASSQUERY_SOFTWARE_GL === '1' ? 'swiftshader' : '');
  if (!requested) return 'desktop-default';
  if (platform === 'darwin') {
    if (requested !== 'metal') throw new Error('macOS native CI requires explicit ANGLE Metal; forced Vulkan SwiftShader failed on the hosted runners');
  } else if (requested !== 'swiftshader') {
    throw new Error('Non-macOS headless tests require the explicit SwiftShader backend');
  }
  commandLine.appendSwitch('use-angle', requested);
  if (requested === 'swiftshader') commandLine.appendSwitch('enable-unsafe-swiftshader');
  return `${requested}-requested`;
}
function recordRendererConsole(errors, event) {
  if (event && event.level === 'error') errors.push(String(event.message));
}
module.exports = { configureRenderer, recordRendererConsole };
