'use strict';
const { contextBridge, ipcRenderer } = require('electron');
const invoke = (name, ...args) => ipcRenderer.invoke(`cassquery:${name}`, ...args);
contextBridge.exposeInMainWorld('cassquery', Object.freeze({
  workbench: (action, payload = {}) => invoke('workbench', action, payload),
  settings: (action, patch) => invoke('settings', action, patch),
  recovery: (action, record) => invoke('recovery', action, record),
  setDirty: (dirty) => invoke('dirty', dirty),
  update: (action) => invoke('update', action),
  onCommand: (callback) => {
    if (typeof callback !== 'function') throw new TypeError('Command callback required');
    // Never expose Electron's event/sender object to the isolated page.
    const listener = (_event, command) => callback(command);
    ipcRenderer.on('cassquery:command', listener);
    return () => ipcRenderer.removeListener('cassquery:command', listener);
  },
}));
