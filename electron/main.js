// The window of 率土战局: shows the page the program serves on 127.0.0.1, nothing else.
//
// Started by the program (stzb_warroom/winsys.py) as `<this> <page url> --profile=<folder>`.
// Links to other sites open in the player's own browser; the page gets no Node and no
// permission but desktop notifications and copying. One window per profile: a second start hands its
// url to the first, which loads it (the program may have restarted) and comes to the front.

const { app, BrowserWindow, Menu, shell } = require('electron');
const path = require('path');

const args = process.argv.slice(1);
const pageUrl = args.find((arg) => /^http:\/\/127\.0\.0\.1:\d+\//.test(arg));
const profile = (args.find((arg) => arg.startsWith('--profile=')) || '').slice('--profile='.length);
if (!pageUrl) app.exit(2);
if (profile) app.setPath('userData', profile);
let origin = pageUrl && new URL(pageUrl).origin;  // the program's, which a restart changes
const icon = process.platform === 'win32' ? path.join(__dirname, 'icon.ico') : undefined;

let window = null;

function openOutside(url) {
  if (/^https?:\/\//.test(url)) shell.openExternal(url);
}

function create() {
  window = new BrowserWindow({
    width: 1200, height: 820, minWidth: 720, minHeight: 520, title: '率土战局', icon,
    show: false, autoHideMenuBar: true,
    webPreferences: { contextIsolation: true, sandbox: true, nodeIntegration: false, spellcheck: false },
  });
  window.once('ready-to-show', () => window.show());
  window.webContents.setWindowOpenHandler(({ url }) => { openOutside(url); return { action: 'deny' }; });
  window.webContents.on('will-navigate', (event, url) => {
    if (new URL(url).origin !== origin) { event.preventDefault(); openOutside(url); }
  });
  window.on('closed', () => { window = null; });
  window.loadURL(pageUrl);
}

if (pageUrl && !app.requestSingleInstanceLock()) {
  app.exit(0);
} else if (pageUrl) {
  app.on('second-instance', (event, argv) => {
    const url = argv.find((arg) => /^http:\/\/127\.0\.0\.1:\d+\//.test(arg));
    if (!window) return;
    if (url && new URL(url).origin !== origin) {
      origin = new URL(url).origin;
      window.loadURL(url);
    }
    if (window.isMinimized()) window.restore();
    window.show();
    window.focus();
  });
  app.on('window-all-closed', () => app.quit());
  app.whenReady().then(() => {
    Menu.setApplicationMenu(null);
    const session = require('electron').session.defaultSession;
    const allowed = new Set(['notifications', 'clipboard-sanitized-write']);  // desktop alerts, 复制诊断信息
    session.setPermissionRequestHandler((contents, permission, callback) => callback(allowed.has(permission)));
    session.setPermissionCheckHandler((contents, permission) => allowed.has(permission));
    create();
  });
}
