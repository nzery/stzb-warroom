// The windows of 率土战局: they show the pages the program serves on 127.0.0.1, nothing else.
//
// Started by the program (stzb_warroom/winsys.py) as `<this> <page url> --profile=<folder>`, with
// `--banner-only` for the top-of-screen banners alone. Links to other sites open in the player's own
// browser; the pages get no Node and no permission but copying. One process per profile: a second
// start hands its url to the first, which loads it (the program may have restarted) and comes to the
// front, or starts the banners. The banners outlive the main window: the program keeps running when
// it is closed, and a notice is wanted most while playing.

const { app, BrowserWindow, Menu, ipcMain, screen, shell } = require('electron');
const path = require('path');

const PAGE = /^http:\/\/127\.0\.0\.1:\d+\//;
const BANNER_WIDTH = 640;  // with the room for the shadow (2026-11-02 user: 440 was too narrow for the text)
const args = process.argv.slice(1);
let pageUrl = args.find((arg) => PAGE.test(arg));
const profile = (args.find((arg) => arg.startsWith('--profile=')) || '').slice('--profile='.length);
if (!pageUrl) app.exit(2);
if (profile) app.setPath('userData', profile);
let origin = pageUrl && new URL(pageUrl).origin;  // the program's, which a restart changes
const icon = process.platform === 'win32' ? path.join(__dirname, 'icon.ico') : undefined;

let window = null;
let banner = null;

function openOutside(url) {
  if (/^https?:\/\//.test(url)) shell.openExternal(url);
}

function create() {
  window = new BrowserWindow({
    width: 1052, height: 662, minWidth: 720, minHeight: 520, title: '工作台', icon,  // an office chat app's size (2026-10-29 user)
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

function bannerUrl() {
  const url = new URL(pageUrl);
  url.pathname = '/banner';
  return url.href;
}

// Above other windows, a borderless game's too, and never taking the focus from it.
function createBanner() {
  if (banner) return;
  banner = new BrowserWindow({
    width: BANNER_WIDTH, height: 120, show: false, frame: false, transparent: true, hasShadow: false, thickFrame: false,
    resizable: false, movable: false, minimizable: false, maximizable: false, fullscreenable: false,
    skipTaskbar: true, focusable: false, alwaysOnTop: true, title: '工作台通知', icon,
    webPreferences: {
      contextIsolation: true, sandbox: true, nodeIntegration: false, spellcheck: false,
      backgroundThrottling: false,  // it waits for notices while hidden
      preload: path.join(__dirname, 'banner-preload.js'),
    },
  });
  banner.setAlwaysOnTop(true, 'screen-saver');
  banner.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  banner.webContents.on('will-navigate', (event, url) => {
    if (new URL(url).origin !== origin) event.preventDefault();
  });
  banner.on('closed', () => { banner = null; });
  banner.loadURL(bannerUrl());
}

function fromBanner(event) {
  return banner && event.sender === banner.webContents;
}

// A banner asks for this: unlike a program the player just started, it may not be allowed the front.
function showWindow() {
  if (!window) return create();
  window.webContents.executeJavaScript("location.hash = 'notices'").catch(() => {});
  if (window.isMinimized()) window.restore();
  window.setAlwaysOnTop(true);
  window.show();
  window.focus();
  window.setAlwaysOnTop(false);
}

if (pageUrl && !app.requestSingleInstanceLock()) {
  app.exit(0);
} else if (pageUrl) {
  app.on('second-instance', (event, argv) => {
    const url = argv.find((arg) => PAGE.test(arg));
    const moved = url && new URL(url).origin !== origin;
    if (moved) {
      origin = new URL(url).origin;
      pageUrl = url;
      if (banner) banner.loadURL(bannerUrl());
    }
    if (argv.includes('--banner-only')) return createBanner();
    if (!window) return create();
    if (moved) window.loadURL(url);
    if (window.isMinimized()) window.restore();
    window.show();
    window.focus();
  });
  app.on('window-all-closed', () => app.quit());
  ipcMain.handle('banner-fit', (event, height) => {
    if (!fromBanner(event)) return;
    // On the screen the player is at (the game's, when there are several).
    const { workArea } = screen.getDisplayNearestPoint(screen.getCursorScreenPoint());
    banner.setBounds({
      x: workArea.x + Math.round((workArea.width - BANNER_WIDTH) / 2), y: workArea.y,
      width: BANNER_WIDTH, height: Math.min(Math.max(1, Math.round(Number(height)) || 1), workArea.height),
    });
    banner.setAlwaysOnTop(true, 'screen-saver');  // a game that went fullscreen since may have taken the top
    banner.showInactive();
  });
  ipcMain.on('banner-hide', (event) => { if (fromBanner(event)) banner.hide(); });
  ipcMain.on('banner-open', (event) => { if (fromBanner(event)) showWindow(); });
  ipcMain.on('banner-quit', (event) => {
    if (!fromBanner(event)) return;
    banner.destroy();
    if (!window) app.quit();
  });
  app.whenReady().then(() => {
    Menu.setApplicationMenu(null);
    const session = require('electron').session.defaultSession;
    const allowed = new Set(['clipboard-sanitized-write']);  // 复制诊断信息
    session.setPermissionRequestHandler((contents, permission, callback) => callback(allowed.has(permission)));
    session.setPermissionCheckHandler((contents, permission) => allowed.has(permission));
    if (args.includes('--banner-only')) createBanner();
    else create();
  });
}
