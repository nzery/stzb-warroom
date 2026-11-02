// What the banner page may ask of its window; the page itself gets no Node.
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('bannerWindow', {
  fit: (height) => ipcRenderer.invoke('banner-fit', height),  // shown at the top of the screen, this tall
  hide: () => ipcRenderer.send('banner-hide'),
  open: () => ipcRenderer.send('banner-open'),  // a banner was clicked: the main window, on the notices
  quit: () => ipcRenderer.send('banner-quit'),  // turned off, or the program is gone
});
