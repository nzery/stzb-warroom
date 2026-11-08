// What the main window's page may ask of the program's windows; the page itself gets no Node.
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('warroomWindow', {
  openMini: () => ipcRenderer.send('mini-open'),  // the notices in a small window above the game
});
