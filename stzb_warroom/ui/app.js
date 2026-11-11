'use strict';
// The window's page: shows /api/state, does everything through /api/action.

const ICONS = {
  home: '<path d="M3 10.5 12 3l9 7.5"/><path d="M5 9.5V20h5v-6h4v6h5V9.5"/>',
  bell: '<path d="M6 16V11a6 6 0 1 1 12 0v5l1.5 2h-15z"/><path d="M10 20a2 2 0 0 0 4 0"/>',
  user: '<circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 3.6-7 8-7s8 3 8 7"/>',
  gear: '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M2 12h3M19 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1"/>',
  pulse: '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5v.5"/>',
  play: '<path d="M8 5v14l11-7z"/>',
  check: '<path d="M5 12.5 10 17 19 7"/>',
  cross: '<path d="M6 6l12 12M18 6 6 18"/>',
  alert: '<path d="M12 3 2 20h20z"/><path d="M12 10v4M12 17v.5"/>',
  pause: '<path d="M9 5v14M15 5v14"/>',
  wave: '<path d="M2 12c2-4 4-4 6 0s4 4 6 0 4-4 6 0"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  sliders: '<path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12"/><circle cx="16" cy="6" r="2"/><circle cx="10" cy="12" r="2"/><circle cx="18" cy="18" r="2"/>',
  question: '<circle cx="12" cy="12" r="9"/><path d="M9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.6.3-1 .8-1 1.5V14M12 17v.5"/>',
  shield: '<path d="M12 3 4 6v6c0 4.5 3.4 8 8 9 4.6-1 8-4.5 8-9V6z"/><path d="M9 12l2 2 4-4"/>',
  swords: '<path d="M14.5 17.5 3 6V3h3l11.5 11.5"/><path d="M13 19l6-6M16 16l4 4M19 21l2-2"/><path d="M9.5 6.5 6 3"/>',
  target: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/>',
  refresh: '<path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 4v7h-7"/>',
  phone: '<rect x="7" y="2.5" width="10" height="19" rx="2"/><path d="M11 18.5h2"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/>',
  chat: '<path d="M4 5h16v11H9l-5 4z"/><path d="M8 9.5h8M8 12.5h5"/>',
  copy: '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a1 1 0 0 1 1-1h10"/>',
  image: '<rect x="3" y="5" width="18" height="14" rx="2"/><circle cx="8.5" cy="10" r="1.5"/><path d="m21 15-5-5L5 19"/>',
};
const icon = (name) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${ICONS[name] || ''}</svg>`;
const $ = (id) => document.getElementById(id);
// The notices alone, in the small window kept above the game (electron/main.js): the main window
// still rings, counts what was seen and keeps the settings.
const MINI = /[?&]mini=1(&|$)/.test(location.search || '');
if (MINI) document.body?.classList.add('mini');
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
// The page's own settings (the sound, banners closed, the list cleared, …) are kept by the program:
// the page's storage starts empty at each start, the program listening on another port.
const saved = (() => {
  try {
    const request = new XMLHttpRequest();
    request.open('GET', '/api/ui', false);  // before anything reads them
    request.send();
    return request.status === 200 ? JSON.parse(request.responseText) || {} : {};
  } catch { return {}; }
})();
const store = {
  get(key, fallback) { return Object.prototype.hasOwnProperty.call(saved, key) ? saved[key] : fallback; },
  set(key, value) {
    if (JSON.stringify(saved[key]) === JSON.stringify(value)) return;
    saved[key] = value;
    fetch('/api/action', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'set_ui', key, value }) }).catch(() => {});
  },
};

// 设置 › 外观: the light colours (app.css) or the dark ones the window had before (classic.css); only the colours change.
function setTheme(theme) {
  const sheet = theme === 'classic' ? '/ui/classic.css' : '/ui/app.css';
  if ($('style').getAttribute('href') !== sheet) $('style').setAttribute('href', sheet);
  document.querySelectorAll('#set-theme input').forEach((input) => {
    input.checked = input.value === (theme === 'classic' ? 'classic' : 'light');
    input.closest('.option').classList.toggle('active', input.checked);
  });
}
setTheme(store.get('theme', 'light'));

const PAGES = {
  overview: ['概览', '上传数据，接收通知'],
  notices: ['通知', '服务器发来的通知'],
  chat: ['群聊', '所有开着本程序的人都在这里；消息只存在各自电脑上，没开程序时的消息收不到'],
  players: ['查询', '按名字查询'],
  prefs: ['提醒设置', '要哪些通知、推送哪些到手机'],
  phone: ['手机', '用手机玩游戏、收推送：一步一步照着做'],
  accounts: ['账号', '这台电脑上报的游戏账号'],
  settings: ['设置', '启动方式、网络组件与数据'],
  diagnostics: ['诊断', '检查运行环境，查看运行日志'],
  about: ['关于', ''],
};
const ACCOUNT_STATES = {
  ok: ['正常', 'ok'], pending: ['等待管理员审批', 'warn'], no_role: ['还没登录过游戏', 'warn'],
  problem: ['数据没有上传', 'err'],
  invalid: ['Token 无效', 'err'], offline: ['连不上服务器', ''], checking: ['检查中', ''],
};
// What the server says an invited account waits for comes first (a public domain's account is
// approved by its first login, nobody approves it).
const accountState = (a) => (a.state === 'pending' && a.pending
  ? [a.pending.label || '等待核验', 'warn'] : ACCOUNT_STATES[a.state] || ['', '']);
const LOGIN_STEPS = '先打开本程序，再进入游戏；如果已经在游戏里，退出后重新登录一次';

const S = {
  data: null, tapped: null, nextFlip: Infinity, page: 'notices', filter: 'all', notices: [], logs: [], n: 0, l: 0,
  hiddenBefore: store.get('hiddenBefore', 0), dismissed: new Set(store.get('dismissed', [])),
  closeAt: {},  // banners closing by themselves: when (banner)
  seenNotices: store.get('seenNotices', 0), loaded: false, accountsKey: '', prefsKey: '', busy: new Set(),
  // The server's pages (玩家信息), as it gave them: {query, data, error, loading, key} each.
  personal: { players: { query: {} } },
  personalRole: store.get('personalRole', null),
  // The server's phone page for the chosen account: {data, error, loading, role, key}.
  phone: {},
  phoneOs: store.get('phoneOs', null),  // the phone page's iPhone / Android
  phoneStep: store.get('phoneStep', {}),  // its step per phone
  phoneSeen: null,  // what the relays saw of the phone: {role, status}
  // 群聊: the messages this page has (the newest when it opened, older ones as asked), the program's
  // cursor, whether older ones are kept, and the newest one read (the badge counts those after it);
  // each account's messages are apart, so all of it starts again when the chat's account changes.
  chat: { account: '', messages: [], c: 0, more: false, loading: false, seen: 0, key: '', bottom: true, gone: new Set() },
  openRows: new Set(),  // the notice rows opened to their lineups (noticeTable)
};
const PERSONAL = new Set(['players']);

// ---------------------------------------------------------------- server

async function act(action, extra = {}, { quiet = false } = {}) {
  try {
    const response = await fetch('/api/action', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action, ...extra }),
    });
    const value = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(value.error || `HTTP ${response.status}`);
    return value.result;
  } catch (error) {
    if (!quiet) toast(error.message || String(error), true);
    throw error;
  } finally {
    schedule();
  }
}

let timer = null;
function schedule() {
  if (!timer) timer = setTimeout(() => { timer = null; refresh(); }, 60);
}

let refreshing = false;
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    const response = await fetch(`/api/state?n=${S.n}&l=${S.l}&c=${S.chat.c}`);
    if (!response.ok) throw new Error(response.status);
    const data = await response.json();
    for (const notice of data.notices) {
      if (!live(notice)) continue;
      S.notices.push(notice);
      if (notice.tapped) S.tapped = { role: notice.role, place: notice.tapped };
      if (S.loaded) announce(notice);
    }
    S.notices = S.notices.filter(live);
    if (S.notices.length > 500) S.notices.splice(0, S.notices.length - 500);
    if (data.notices.length) S.n = data.notices[data.notices.length - 1].seq;
    if (data.logs.length) { S.logs.push(...data.logs); S.l = data.logs[data.logs.length - 1].seq; appendLogs(data.logs); }
    if (S.logs.length > 1000) S.logs.splice(0, S.logs.length - 1000);
    takeChat(data.chat);
    S.data = data;
    S.loaded = true;
    render();
  } catch {
    /* the events stream reports a program that is gone */
  } finally {
    refreshing = false;
  }
}

// Which page this is, so the program knows when it closes (and ends unless kept running).
const PAGE = Math.random().toString(36).slice(2) + Date.now().toString(36);
window.addEventListener('pagehide', () => {
  navigator.sendBeacon('/api/action', new Blob([JSON.stringify({ action: 'closing', page: PAGE })], { type: 'application/json' }));
});

function connect() {
  const events = new EventSource('/api/events?page=' + PAGE);
  let failures = 0;
  events.addEventListener('change', () => { failures = 0; $('gone').classList.add('hidden'); schedule(); });
  events.onerror = () => {
    failures += 1;
    if (failures >= 3) $('gone').classList.remove('hidden');
  };
  setInterval(refresh, 1000);  // times shown ("3 分钟前") and stats move without events
}

// ---------------------------------------------------------------- helpers

function ago(seconds) {
  if (!seconds) return '-';
  const delta = Math.max(0, Date.now() / 1000 - seconds);
  if (delta < 10) return '刚刚';
  if (delta < 60) return `${Math.floor(delta)} 秒前`;
  if (delta < 3600) return `${Math.floor(delta / 60)} 分钟前`;
  if (delta < 86400) return `${Math.floor(delta / 3600)} 小时前`;
  return `${Math.floor(delta / 86400)} 天前`;
}
// A count as it fits the top bar: 128,456 up to 99,999, then 12.8万 (2026-11-07 user: room for big ones).
function count(n) {
  if (n == null) return '-';
  return n < 100000 ? n.toLocaleString() : `${(n / 10000).toFixed(n < 1000000 ? 1 : 0).replace(/\.0$/, '')}万`;
}
function clock(seconds, withDate = false) {
  const date = new Date(seconds * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  const time = `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  return withDate ? `${date.getMonth() + 1}月${date.getDate()}日 ${time}` : time;
}
function duration(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h ? `${h} 小时 ${m} 分` : m ? `${m} 分钟` : `${s} 秒`;
}
const roleName = (label) => String(label || '').split(' · ')[0];
function roleOf(notice) {
  const account = (S.data?.tokens || []).find((a) => a.role && a.role.id === notice.role);
  return account ? roleName(account.role.label) : (notice.role ? `角色 ${notice.role}` : '');
}
function toast(text, error = false) {
  const node = document.createElement('div');
  node.className = 'toast' + (error ? ' error' : '');
  node.textContent = text;
  $('toasts').append(node);
  setTimeout(() => node.remove(), error ? 6000 : 3000);
}
function setBusy(button, busy, label) {
  if (!button) return;
  if (busy) { button.dataset.label = button.innerHTML; button.innerHTML = `<span class="spin"></span>${esc(label || '')}`; }
  else if (button.dataset.label) button.innerHTML = button.dataset.label;
  button.disabled = busy;
}
async function withBusy(button, label, work) {
  setBusy(button, true, label);
  try { return await work(); } finally { setBusy(button, false); }
}

// ---------------------------------------------------------------- alarms

let audio = null;
function beep() {
  try {
    audio = audio || new AudioContext();
    const now = audio.currentTime;
    for (const [at, freq] of [[0, 880], [0.22, 660], [0.44, 880]]) {
      const osc = audio.createOscillator(), gain = audio.createGain();
      osc.frequency.value = freq; osc.type = 'sine';
      gain.gain.setValueAtTime(0.0001, now + at);
      gain.gain.exponentialRampToValueAtTime(0.25, now + at + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, now + at + 0.2);
      osc.connect(gain).connect(audio.destination);
      osc.start(now + at); osc.stop(now + at + 0.21);
    }
  } catch { /* no audio */ }
}
// A notice is shown until the time the server gave it (`expires`, epoch seconds), then cleared.
function live(notice) {
  return typeof notice.expires !== 'number' || notice.expires * 1000 > Date.now();
}
setInterval(() => {
  const before = S.notices.length;
  S.notices = S.notices.filter(live);
  if (S.notices.length !== before) render();
}, 15000);
function announce(notice) {
  if (MINI) return;
  if (notice.level !== 'alarm' || notice.hidden) return;
  if (store.get('sound', true)) beep();
}

// ---------------------------------------------------------------- rendering

function render() {
  const d = S.data;
  if (!d) return;
  renderChrome(d);
  renderBanners(d);
  renderOverview(d);
  if (S.page === 'notices') renderNotices();
  renderAccounts(d);
  renderPrefs(d);
  if (PERSONAL.has(S.page)) {
    // Opened before the accounts were known, or its account changed: read it now.
    const view = S.personal[S.page], account = personalAccount();
    if (account && !view.loading && !view.error && view.role !== account.a.role.id) loadPersonal(S.page);
    else renderPersonal(S.page);
  }
  if (S.page === 'phone') renderPhone(d);
  if (S.page === 'chat') renderChat(d);
  renderSettings(d);
  renderChecks(d);
  $('about-version').textContent = d.app_version;
  $('site-link').classList.toggle('hidden', !d.site);
  if (d.site) $('site-link').href = d.site;
  $('update-state').textContent = d.update ? UPDATE_STATE[d.update.state](d.update.version) : '';
}

function captureView(d) {
  const c = d.capture;
  if (!d.tokens.length) return { cls: 'idle', icon: 'user', title: '还没有添加账号', desc: '添加管理员发给你的 Token 后就能开始上报。', dot: '', side: '未开始' };
  switch (c.state) {
    case 'running': if (d.phone?.blind) return { cls: 'wait', icon: 'alert', title: '手机的游戏数据没有抓到', desc: '手机正在经这台电脑玩游戏，但电脑没抓到数据，战报不会上传。点“重新抓包”试试；开着 VPN 或代理的话先关掉。', dot: 'wait', side: '没抓到数据' };
      if (c.lost) return { cls: 'wait', icon: 'alert', title: '换网络后没抓到游戏数据', desc: '电脑刚换了网络，之后没再抓到游戏数据，通知和战报都会中断。还在玩的话点“重新抓包”；没在玩可以不管，这条提示过几分钟会消失。', dot: 'wait', side: '没抓到数据' };
      return { cls: '', icon: 'wave', title: '正在上报', desc: `已运行 ${duration(Date.now() / 1000 - c.since)}。玩游戏时保持程序开着，可以最小化这个窗口。`, dot: 'run', side: '上报中' };
    case 'starting': return { cls: 'wait', icon: 'clock', title: '正在启动…', desc: '正在启动网络组件。', dot: 'wait', side: '启动中' };
    case 'stopping': return { cls: 'wait', icon: 'clock', title: '正在停止…', desc: '正在把没上传完的数据保存好。', dot: 'wait', side: '停止中' };
    case 'error': return { cls: 'err', icon: 'alert', title: '上报已停止', desc: c.hint || c.error || '', dot: 'err', side: '出错' };
    default: return { cls: 'idle', icon: 'pause', title: '上报没有运行', desc: '点“开始上报”启动。没有运行时游戏数据不会上传。', dot: '', side: '未运行' };
  }
}

function renderChrome(d) {
  const view = captureView(d);
  $('side-dot').className = 'dot ' + view.dot;
  $('side-state').textContent = view.side;
  $('side-detail').textContent = `版本 ${d.app_version}`;
  const s = d.stats;
  $('m-uploaded').textContent = count(s.uploaded);
  $('m-pending').textContent = count(s.pending);
  $('m-last').textContent = s.uploaded_at ? ago(s.uploaded_at) : '-';
  $('m-last-at').title = s.uploaded_at ? '最近一次上传 ' + clock(s.uploaded_at) : (d.capture.state === 'running' ? '登录游戏后开始上传' : '还没有上传');
  $('m-conn').textContent = s.connections;
  const unseen = S.notices.filter((n) => n.seq > S.seenNotices && !n.hidden).length;
  $('badge-notices').textContent = S.page === 'notices' || !unseen ? '' : unseen > 99 ? '99+' : unseen;
  const unread = S.chat.messages.filter((m) => !m.mine && m.seq > S.chat.seen).length;
  $('badge-chat').textContent = S.page === 'chat' || !unread ? '' : unread > 99 ? '99+' : unread;
  const problems = d.tokens.filter((a) => a.state === 'invalid' || a.state === 'problem').length;
  $('badge-accounts').textContent = problems || '';
  document.title = MINI ? '通知 · 置顶' : d.capture.state === 'error' || problems ? '工作台 · 需要处理' : '工作台';
}

// A banner with an id can be closed: a button that says so, the × alone was too small to be seen
// (2026-11-07 user). `closes`: it closes by itself at S.closeAt[closes] (ms), counting down on the button.
function banner(kind, iconName, text, actions = '', id = '', closes = '') {
  const left = closes ? `<span class="left" data-closes="${esc(closes)}"></span>` : '';
  return `<div class="banner ${kind}"${id ? ` data-id="${esc(id)}"` : ''}><i>${icon(iconName)}</i><div class="text">${text}</div>${actions}${id ? `<button class="btn close" data-dismiss="1">${icon('cross')}关闭${left}</button>` : ''}</div>`;
}
function tickBanners() {
  for (const el of document.querySelectorAll('#banners [data-closes]')) {
    const name = el.dataset.closes;
    // Not while the mouse is on it: someone reading the notes.
    if (el.closest('.banner').matches(':hover')) S.closeAt[name] = Math.max(S.closeAt[name], Date.now() + 5000);
    const left = Math.ceil((S.closeAt[name] - Date.now()) / 1000);
    if (left <= 0) { closeBanner(el.closest('.banner').dataset.id); return; }
    el.textContent = `（${left} 秒）`;
  }
}
function closeBanner(id) {
  S.dismissed.add(id);
  store.set('dismissed', [...S.dismissed].slice(-50));
  render();
}
setInterval(tickBanners, 1000);
// The notes of the version just updated to stay this long, then close by themselves (2026-11-07 user).
const UPDATED_SHOWN = 30;

function renderBanners(d) {
  const parts = [];
  const invalid = d.tokens.filter((a) => a.state === 'invalid');
  if (invalid.length) {
    parts.push(banner('error', 'alert', `<b>${invalid.length === d.tokens.length ? 'Token 无效' : `有 ${invalid.length} 个 Token 无效`}</b>：${esc(invalid.map((a) => a.token).join('、'))} 被服务器拒绝，这些账号的数据不会上传。请找管理员确认 Token，删掉后重新添加。`,
      '<button class="btn danger" data-goto="accounts">去处理</button>'));
  }
  const waiting = d.tokens.filter((a) => ['pending', 'no_role', 'problem'].includes(a.state));
  if (waiting.length) {
    parts.push(banner(waiting.some((a) => a.state === 'problem') ? 'error' : 'warn', 'user',
      `<b>账号还不能收通知</b>：${waiting.map((a) => `${esc(a.role ? roleName(a.role.label) : a.name || a.token)} ${esc(accountState(a)[0])}`).join('；')}`,
      '<button class="btn primary" data-goto="accounts">去看看</button>'));
  }
  if (!d.tokens.length) parts.push(banner('warn', 'user', '还没有添加 Token，数据不会上传。', '<button class="btn primary" data-goto="accounts">添加账号</button>'));
  // On the overview the status card says it already.
  if (d.capture.state === 'error' && S.page !== 'overview' && !(invalid.length && invalid.length === d.tokens.length)) {
    const needWireshark = (d.capture.error || '').includes('dumpcap not found');
    parts.push(banner('error', 'alert', `<b>上报已停止</b>：${esc(d.capture.hint || d.capture.error)}`,
      needWireshark ? '<button class="btn danger" data-goto="settings">去设置</button>' : '<button class="btn danger" data-action="start">重试</button>'));
  }
  for (const hint of d.hints) {
    if (!S.dismissed.has(hint.id)) parts.push(banner('warn', 'info', esc(hint.text), '', hint.id));
  }
  if (d.update) parts.push(updateBanner(d.update, d));
  else if (d.load_failed) parts.push(banner('warn', 'info', `下载的新版本 <b>${esc(d.load_failed)}</b> 在这台电脑上没能启动，现在用的还是 ${esc(d.app_version)}。请下载完整安装包，解压后替换原来的程序文件夹。`));
  // The first start of a new version: what it brought, until the player closes it.
  const updatedId = d.updated ? 'updated-' + d.updated.version : '';
  if (d.updated && !S.dismissed.has(updatedId)) {
    S.closeAt.notes = S.closeAt.notes || Date.now() + UPDATED_SHOWN * 1000;
    parts.push(banner('info', 'check', `<b>已更新到 ${esc(d.updated.version)}</b>${releaseNotes(d.updated.notes, true)}`, '', updatedId, 'notes'));
  }
  const html = parts.join('');
  if ($('banners').dataset.html !== html) { $('banners').innerHTML = html; $('banners').dataset.html = html; tickBanners(); }
}

const UPDATE_STATE = {
  available: (v) => `有新版本 ${v}`,
  downloading: (v) => `正在下载新版本 ${v}…`,
  ready: (v) => `新版本 ${v} 已下载，重启程序后生效`,
  failed: (v) => `新版本 ${v} 自动下载失败`,
};

// A release's notes, as it wrote them (the server's text), folded unless `open`.
function releaseNotes(notes, open = false) {
  if (!notes) return '';
  return `<details class="release-notes"${open ? ' open' : ''}><summary>这次更新了什么</summary><div class="notes-body">${esc(notes)}</div></details>`;
}

function updateBanner(u, d = {}) {
  const v = `<b>${esc(u.version)}</b>`;
  const notes = releaseNotes(u.notes);
  const download = `<a class="btn" href="${esc(u.url)}" target="_blank" rel="noopener">下载</a>`;
  switch (u.state) {
    case 'downloading': return banner('info', 'clock', `正在下载新版本 ${v}…${notes}`);
    case 'ready': return banner('info', 'info', `新版本 ${v} 已下载，重启程序后生效。${notes}`, '<button class="btn primary" data-action="restart_for_update">立即重启</button>');
    case 'failed': return banner('warn', 'info', `新版本 ${v} 自动下载失败（${esc(u.error || '')}），稍后会再试，也可以下载完整安装包。${notes}`, download);
    default:
      if (d.load_failed === u.version) return banner('warn', 'info', `新版本 ${v} 已下载，但在这台电脑上没能启动，现在用的还是 ${esc(d.app_version)}。请下载完整安装包，解压后替换原来的程序文件夹。${notes}`, download);
      return banner('info', 'info', `发现新版本 ${v}，请下载完整安装包，解压后替换原来的程序文件夹。${notes}`, download);
  }
}

function renderOverview(d) {
  const view = captureView(d);
  $('hero').className = 'hero ' + view.cls;
  $('hero-icon').innerHTML = icon(view.icon);
  $('hero-title').textContent = view.title;
  $('hero-desc').textContent = view.desc;
  const actions = view.cls === 'err' && (d.capture.error || '').includes('dumpcap not found')
    ? '<button class="btn primary" data-action="open_installer">安装 Wireshark</button><button class="btn" data-goto="settings">手动选择 dumpcap</button>'
    : !d.tokens.length ? '<button class="btn primary" data-goto="accounts">添加账号</button>'
    : view.cls === 'wait' && d.capture.state === 'running' ? '<button class="btn primary" data-action="restart_capture">重新抓包</button>'
    : view.cls === 'idle' ? '<button class="btn primary" data-action="start">开始上报</button>' : '';
  if ($('hero-actions').dataset.html !== actions) { $('hero-actions').innerHTML = actions; $('hero-actions').dataset.html = actions; }
  // What the top bar's figures are, said in words (2026-11-07 user: nobody finds the hover tips).
  const st = d.stats, running = d.capture.state === 'running';
  const tile = (label, value, note, cls = '') => `<div class="ov-stat${cls}"><span>${label}</span><b>${esc(value)}</b><small>${esc(note)}</small></div>`;
  const stats = [
    tile('已上传', st.uploaded.toLocaleString(), '这次打开程序后上传的数据条数'),
    tile('待上传', st.pending == null ? '-' : st.pending.toLocaleString(), st.pending ? '网络断了先存在本机，连上后补传' : '没有积压，都传上去了', st.pending ? ' warn' : ''),
    tile('最近上传', st.uploaded_at ? ago(st.uploaded_at) : '-', st.uploaded_at ? clock(st.uploaded_at) : running ? '登录游戏后开始上传' : '还没有上传'),
    tile('游戏连接', String(st.connections), st.connections ? '最近 5 分钟看到的游戏连接' : '最近 5 分钟没看到：没在玩时正常'),
  ].join('');
  if ($('ov-stats').dataset.html !== stats) { $('ov-stats').innerHTML = stats; $('ov-stats').dataset.html = stats; }
  // The accounts with their push binding, and the phone through this computer (2026-11-07 user:
  // the push's state is here, not on 账号).
  const phone = d.phone || { exits: [] };
  const pipes = (phone.exits || []).reduce((n, e) => n + (e.pipes || 0), 0);
  const exitProblem = (phone.exits || []).map((e) => EXIT_PROBLEMS[e.state]).find(Boolean);
  const rows = d.tokens.map((a) => {
    const [label, cls] = accountState(a);
    const b = a.state !== 'invalid' && a.bark;
    const push = !b ? '' : !b.bound ? '<span class="push warn">手机推送未绑定</span><button class="link" data-goto="phone">去绑定</button>'
      : b.error ? `<span class="push err" title="${esc(b.error)}">推送失败：${esc(b.error)}</span><button class="link" data-goto="phone">去看看</button>`
      : `<span class="push ok">推送到${b.kind === 'ntfy' ? '安卓' : ' iPhone'}</span>`;
    return `<div class="mini ov-account"><div class="main-text"><b>${esc(a.role ? roleName(a.role.label) : a.name || a.token)}</b>${push ? `<div class="sub-line">${push}</div>` : ''}</div><span class="state ${cls}">${label}</span></div>`;
  });
  if (d.tokens.length && phone.running && !phone.waiting) {
    const [text, cls] = exitProblem ? [exitProblem, 'warn'] : pipes ? [`手机正在经这台电脑玩：${pipes} 条游戏连接`, 'ok'] : ['没有手机经这台电脑玩', ''];
    rows.push(`<div class="mini ov-account"><div class="main-text"><b>手机</b><div class="sub-line"><span class="push ${cls}">${esc(text)}</span></div></div><button class="btn ghost small" data-goto="phone">设置</button></div>`);
  }
  const accounts = rows.join('') || '<div class="empty">还没有账号</div>';
  if ($('ov-accounts').dataset.html !== accounts) { $('ov-accounts').innerHTML = accounts; $('ov-accounts').dataset.html = accounts; }

  const gone = withdrawnArmies();
  const recent = latestOfPlaces(S.notices.filter((n) => n.seq > S.hiddenBefore && !n.hidden && !withdrawnAll(n, gone)))
    .filter((n) => !landedAll(n, gone)).slice(-6).reverse();
  $('ov-notices').innerHTML = recent.length ? recent.map((n) => `<div class="mini"><span class="tag ${esc(n.level)}">${esc(n.tag || '通知')}</span><span class="main-text">${esc(n.title)}</span><span class="time">${clock(n.time)}</span></div>`).join('')
    : '<div class="empty">还没有通知。登录游戏并上报后，敌袭等通知会显示在这里。</div>';
}

// Enemy armies come as a small table (`view.blocks`, the server's: text and tables in order), a row an
// army. Each row stays on one line within the notice: columns share the width as the server says
// (`widths`, so the tables of all notices line up) or else by their longest text, and a cell too
// long for its column ends in … with the whole text on hover. The table's `key` column (the
// enemy lineup) stands out. Its `countdown` column (when each army lands, the server's epoch
// seconds in `at`, `draw` for one that attacks again then) counts down in the row, and a row
// whose army landed goes (2026-10-14 user: what is left is what is still to come, even when the
// player taps no other place); a table whose rows all went goes with the lines before it (its
// group's name, where its armies went). A row whose army a recall named (`armies`, the server's, a
// row's army id; `gone`, withdrawnArmies) goes too (2026-10-16 user). Any other notice is its text.
const nowSec = () => Date.now() / 1000;
function rowsLeft(t, gone = new Set()) {
  const at = t.countdown && Array.isArray(t.countdown.at) ? t.countdown.at : [];
  const armies = Array.isArray(t.armies) ? t.armies : [];
  return ((t && t.rows) || []).map((row, r) => [row, r])
    .filter(([, r]) => !(typeof at[r] === 'number' && at[r] <= nowSec()) && !gone.has(armies[r]));
}
const MINI_LEAVES_OUT = new Set(['来源', '最近兵力']);
const textWidth = (value) => [...String(value)].reduce((n, ch) => n + (ch.charCodeAt(0) > 0xff ? 2 : 1), 0);
// A row with `details` (the server's lines of the army's lineup, a hero a line) opens under itself
// when clicked, and again closes (2026-11-08 players: the whole lineup by hand, the table one line
// an army); which are open (S.openRows, `key` the notice's and table's) stays across redraws.
function noticeTable(t, gone, key = '') {
  const kept = rowsLeft(t, gone);
  const rows = kept.map(([row]) => row);
  if (!rows.length) return '';
  // +3: a cell's padding, about three half-width characters
  const widths = t.columns.map((h, i) => 3 + Math.min(40, Math.max(4, textWidth(h), ...rows.map((row) => textWidth(row[i] ?? '')))));
  const given = Array.isArray(t.widths) && t.widths.length === t.columns.length && t.widths.every((w) => w > 0);
  const share = given ? t.widths : widths;
  const total = share.reduce((a, b) => a + b, 0);
  // The page's CSP (style-src 'self') drops a style attribute: colWidths sets them from data-w.
  // The small window lets each column take what it holds (mini.css caps the long ones): the
  // server's shares are for the main window's width (2026-11-08 user: a small window, all of a row).
  const cols = MINI ? '' : `<colgroup>${share.map((w) => `<col data-w="${(w * 100 / total).toFixed(1)}">`).join('')}</colgroup>`;
  const cd = t.countdown && Array.isArray(t.countdown.at) ? t.countdown : null;
  const cellOf = (c, i, r) => {
    const at = cd && i === cd.col ? cd.at[r] : null;
    if (typeof at === 'number') {
      return `<td class="cd" data-at="${at}"${cd.draw && cd.draw[r] ? ' data-draw="1"' : ''} title="${esc(c)}">${esc(c)}</td>`;
    }
    return `<td title="${esc(c)}"${i === t.key ? ' class="key"' : ''}>${esc(c)}</td>`;
  };
  // The small window leaves out 来源 and 最近兵力, to be narrower (2026-11-08 user).
  const shown = (i) => !MINI || !MINI_LEAVES_OUT.has(t.columns[i]);
  return `<div class="vtable"><table class="${MINI ? 'auto' : 'fit'}">${cols}<thead><tr>${t.columns.map((h, i) => (shown(i) ? `<th>${esc(h)}</th>` : '')).join('')}</tr></thead>
    <tbody>${kept.map(([row, r]) => {
      const cells = row.map((c, i) => (shown(i) ? cellOf(c, i, r) : '')).join('');
      const detail = Array.isArray(t.details) && Array.isArray(t.details[r]) && t.details[r].length ? t.details[r] : null;
      if (!detail || !key) return `<tr>${cells}</tr>`;
      const id = `${key}:${r}`, open = S.openRows.has(id);
      return `<tr class="has-detail${open ? ' open' : ''}" data-detail="${esc(id)}" title="点一下${open ? '收起' : '看阵容和战法'}">${cells}</tr>`
        + (open ? `<tr class="detail"><td colspan="${t.columns.filter((h, i) => shown(i)).length}">${detail.map((l) => `<div>${esc(l)}</div>`).join('')}</td></tr>` : '');
    }).join('')}</tbody></table></div>`;
}
// The columns' widths noticeTable gives as data-w, set through the DOM, which the CSP allows.
function colWidths(root) {
  for (const col of root.querySelectorAll('col[data-w]')) col.style.width = `${col.dataset.w}%`;
}
// A notice of tables whose rows have all landed (or went back) has nothing left to tell.
function landedAll(n, gone) {
  const tables = (n.view?.blocks || []).filter((b) => b.table?.countdown || b.table?.armies);
  return tables.length > 0 && (n.view.blocks || []).every((b) => !b.table || !rowsLeft(b.table, gone).length);
}
// When the next row lands, for the list to drop it then.
function nextRowLanding(n) {
  return Math.min(Infinity, ...(n.view?.blocks || []).flatMap((b) => (b.table?.countdown?.at || [])
    .filter((at) => typeof at === 'number' && at > nowSec())));
}
const countsDown = (n) => (n.view?.blocks || []).some((b) => b.table?.countdown);
function noticeBody(n, gone) {
  const v = n.view;
  if (!v || !Array.isArray(v.blocks)) return n.body ? `<div class="body">${esc(n.body)}</div>` : '';
  const text = (t) => (t ? `<div class="line">${esc(t)}</div>` : '');
  const parts = [];
  let lines = [];
  for (const b of v.blocks) {
    if (!b.table) { lines.push(text(b.text)); continue; }
    const table = noticeTable(b.table, gone, `${n.seq}:${v.blocks.indexOf(b)}`);
    if (table || !(b.table.rows || []).length) parts.push(...lines, table);
    lines = [];
  }
  return `<div class="body view">${[...parts, ...lines].join('')}</div>`;
}

// A notice of raids still to come (`arrivals`, the server's, epoch seconds ascending) stays above
// the others, the soonest first, counting down to its next one; once they have all landed it goes
// back among the others by when it came. Armies a recall notice names (`withdrawn`) went back: they
// are not counted down to, and a notice of `withdrawn_drops` (one attack) all of whose armies did goes.
// The recall notice itself is `hidden`: only what it names is used (2026-10-16 user).
function withdrawnArmies() {
  return new Set(S.notices.flatMap((n) => (Array.isArray(n.withdrawn) ? n.withdrawn : [])));
}

function nextArrival(n, gone) {
  const now = Date.now() / 1000;
  if (!Array.isArray(n.arrivals)) return undefined;
  const armies = Array.isArray(n.armies) ? n.armies : [];
  return n.arrivals.find((t, i) => typeof t === 'number' && t > now && !gone.has(armies[i]));
}

function withdrawnAll(n, gone) {
  return n.withdrawn_drops && Array.isArray(n.armies) && n.armies.length > 0 && n.armies.every((a) => gone.has(a));
}

function left(at) {
  const s = Math.max(0, Math.ceil(at - Date.now() / 1000));
  return s >= 60 ? `还有 ${Math.floor(s / 60)}分${String(s % 60).padStart(2, '0')}秒` : `还有 ${s}秒`;
}

// '1分05秒' to an army's landing in its row, '打平 1分05秒' to one attacking again; '已到' after.
function short(at, draw) {
  const s = Math.ceil(at - Date.now() / 1000);
  if (s <= 0) return '已到';
  return (draw ? '打平 ' : '') + (s >= 60 ? `${Math.floor(s / 60)}分${String(s % 60).padStart(2, '0')}秒` : `${s}秒`);
}

function tickCountdowns() {
  for (const el of document.querySelectorAll('#notice-list .countdown')) el.textContent = left(Number(el.dataset.at));
  for (const el of document.querySelectorAll('#notice-list td.cd')) {
    el.textContent = short(Number(el.dataset.at), el.dataset.draw);
    el.classList.toggle('soon', Number(el.dataset.at) * 1000 - Date.now() < 60000);
  }
  if (Date.now() / 1000 >= S.nextFlip) renderNotices();  // a raid landed: the order changes
}
setInterval(tickCountdowns, 1000);

// A place the player tapped (`tapped`, the server's) is told anew at each tap that finds a change:
// only its latest notice is shown, and the latest tapped place's stays above all, until the
// player taps another (2026-10-14 user). One place for all the accounts: the one an account tapped
// stayed above all after the player went over to another account (2026-10-17 user).
function latestOfPlaces(notices) {
  const latest = new Map();
  for (const n of notices) if (n.tapped) latest.set(`${n.role}:${n.tapped}`, n.seq);
  return notices.filter((n) => !n.tapped || latest.get(`${n.role}:${n.tapped}`) === n.seq);
}

function renderNotices() {
  const gone = withdrawnArmies();
  const list = latestOfPlaces(S.notices.filter((n) => n.seq > S.hiddenBefore && !n.hidden && (S.filter === 'all' || n.level === S.filter)
    && !withdrawnAll(n, gone))).filter((n) => !landedAll(n, gone)).reverse();
  const pinned = list.filter((n) => n.tapped && S.tapped && S.tapped.role === n.role && S.tapped.place === n.tapped);
  const others = list.filter((n) => !pinned.includes(n));
  const coming = others.map((n) => [n, nextArrival(n, gone)]).filter(([, at]) => at).sort((a, b) => a[1] - b[1]);
  const rest = others.filter((n) => !nextArrival(n, gone));
  S.nextFlip = Math.min(Infinity, ...list.map((n) => Math.min(nextArrival(n, gone) || Infinity, nextRowLanding(n))));
  const card = (n, at) => {
    const who = roleOf(n);
    // A notice whose table counts down in its rows says when it came; else when its raid lands.
    const when = at && !countsDown(n) ? `<span class="when countdown" data-at="${at}" title="${clock(at)} 到"></span>`
      : `<span class="when" title="${clock(n.time, true)}">${clock(n.time)}</span>`;
    return `<article class="notice ${esc(n.level)}${at ? ' coming' : ''}"><span class="tag ${esc(n.level)}">${esc(n.tag || '通知')}</span>
      <div class="title">${who && S.data.tokens.length > 1 ? `<span class="who">${esc(who)}</span>` : ''}${esc(n.title)}</div>
      ${when}
      ${noticeBody(n, gone)}</article>`;
  };
  const group = (label, cards) => (cards.length ? `<div class="notice-group">${label}</div>${cards.join('')}` : '');
  const html = list.length
    ? group('刚点开的地点 · 点别处才换', pinned.map((n) => card(n, nextArrival(n, gone))))
      + group('还没打到 · 先到的在上', coming.map(([n, at]) => card(n, at)))
      + ((pinned.length || coming.length) && rest.length ? '<div class="notice-group">其他通知</div>' : '')
      + rest.map((n) => card(n)).join('')
    : `<div class="card empty">${S.filter === 'all' ? '还没有通知' : '没有这一类的通知'}</div>`;
  if ($('notice-list').dataset.html !== html) {
    $('notice-list').innerHTML = html; $('notice-list').dataset.html = html; colWidths($('notice-list'));
  }
  tickCountdowns();
  if (S.notices.length && !MINI) { S.seenNotices = S.notices[S.notices.length - 1].seq; store.set('seenNotices', S.seenNotices); }
}

function renderAccounts(d) {
  const key = JSON.stringify(d.tokens);
  if (key === S.accountsKey) return;
  S.accountsKey = key;
  $('account-list').innerHTML = d.tokens.map((a, index) => {
    const [label, cls] = accountState(a);
    const role = a.role ? a.role.label : '';
    const title = a.state === 'invalid' ? '无效的 Token' : a.role ? roleName(role) : (a.name || '新账号');
    let problem = '';
    if (a.state === 'problem') problem = `<div class="account-problem"><b>${esc(a.problem.title)}</b><br>${esc(a.problem.text)}</div>`;
    else if (a.state === 'invalid') problem = '<div class="account-problem">服务器不认这个 Token（可能复制错了，或已被管理员停用）。这个账号的数据不会上传。请找管理员要正确的 Token，删掉这个后重新添加。</div>';
    else if (a.state === 'pending' && a.pending) problem = `<div class="account-problem warn"><b>${esc(a.pending.title)}</b><br>${esc(a.pending.text)}</div>`;
    else if (a.state === 'pending') problem = '<div class="account-problem warn">这个 Token 需要管理员审批，审批后才能看到这个账号的通知和推送。可以先联系管理员。</div>';
    else if (a.state === 'no_role') problem = '<div class="account-problem warn">还没收到这个账号的游戏登录。' + LOGIN_STEPS + '。</div>';
    else if (a.state === 'offline') problem = `<div class="account-problem warn">暂时连不上服务器（${esc(a.error || '')}），稍后会自动重试。</div>`;
    // The name and state first, what it is next, then what can be done with it (2026-11-07 user:
    // the page was hard to read). Push is bound on 手机 and shown on 概览.
    const site = a.state !== 'invalid' && /^https:\/\//.test(a.site || '')
      ? `<a class="btn primary small" href="${esc(a.site)}" target="_blank" rel="noopener">打开网站</a>
        <button class="btn ghost small" data-copy-site="${index}" type="button">复制网站链接</button><span class="muted hint">链接只发给自己人</span>` : '';
    const facts = [role ? role.split(' · ').slice(1).join(' · ') : '还没有角色信息', a.name, `<span class="token-text">${esc(a.token)}</span>`]
      .filter(Boolean).map((f, i, all) => (i === all.length - 1 ? f : esc(f))).join(' · ');
    return `<div class="card account ${a.state === 'invalid' ? 'invalid' : ''}">
      <div class="account-head"><span class="avatar">${esc(title.slice(0, 1))}</span>
        <div class="who"><b>${esc(title)}<span class="state ${cls}">${label}</span></b><small>${facts}</small></div></div>
      ${problem}
      <div class="account-actions">${site}<button class="btn ghost small danger-text" data-action="remove_token" data-index="${index}" data-confirm="${a.state === 'ok' ? '这个 Token 已认领角色、正常上报。删除后这个账号的数据不再上传；再用邀请码重新加入，要等管理员确认或原 Token 停用一天后才换过来。确定删除？' : '删除这个 Token？删除后这个账号的数据不再上传。'}">删除账号</button></div></div>`;
  }).join('') || '<div class="card empty">还没有账号。在下面添加管理员发给你的 Token。</div>';
}

// The notice settings: the page as the server gives it (texts and all), the same for every account.
function renderPrefs(d) {
  const p = d.notice_prefs || {};
  const key = JSON.stringify([p, d.tokens.length]);
  if (key === S.prefsKey) return;
  S.prefsKey = key;
  if (!p.groups) {
    $('prefs').innerHTML = `<div class="card empty">${esc(!d.tokens.length ? '先到“账号”添加账号，再来设置通知。' : p.error || '正在读取通知设置…')}</div>`;
    return;
  }
  const values = p.values || {};
  // One line a setting: its name and one line of help, the switch or the choices beside it; a
  // choice's help is the chosen option's (2026-11-07 user: short, on one screen, no scrolling).
  const item = (it) => {
    if (it.type === 'switch') {
      return `<div class="pref"><div class="pref-text"><b>${esc(it.label)}</b><small>${esc(it.help)}</small></div>
        <label class="switch"><input type="checkbox" data-pref="${esc(it.key)}"${values[it.key] ? ' checked' : ''}><span></span></label></div>`;
    }
    const chosen = (it.options || []).find((o) => o.value === values[it.key]);
    return `<div class="pref choice"><div class="pref-text"><b>${esc(it.label)}</b><small>${esc(chosen ? chosen.help : it.help)}</small></div>
      <div class="segmented">${(it.options || []).map((o) => `<label class="${o === chosen ? 'active' : ''}" title="${esc(o.help)}">
        <input type="radio" name="pref-${esc(it.key)}" data-pref="${esc(it.key)}" value="${esc(o.value)}"${o === chosen ? ' checked' : ''}>${esc(o.label)}</label>`).join('')}</div></div>`;
  };
  $('prefs').innerHTML = (p.note ? `<p class="muted prefs-note">${esc(p.note)}</p>` : '')
    + (p.error ? banner('warn', 'info', esc(p.error)) : '')
    + `<div class="pref-groups">${p.groups.map((g) => `<div class="card pref-group"><h3>${esc(g.title)}</h3>${g.items.map(item).join('')}</div>`).join('')}</div>`;
}

// ---------------------------------------------------------------- the server's pages (玩家信息)
// Pages the server makes (texts, filters and all): cards of tags, figures, lines and a table.

// The accounts these pages can be about: approved, with a role the server has seen.
function personalAccounts() {
  return (S.data?.tokens || []).map((a, index) => ({ a, index })).filter(({ a }) => a.state === 'ok' && a.role);
}
function personalAccount() {
  const list = personalAccounts();
  return list.find(({ a }) => a.role.id === S.personalRole) || list[0] || null;
}

async function loadPersonal(page) {
  const view = S.personal[page];
  const account = personalAccount();
  if (!account) { view.data = null; view.error = null; renderPersonal(page); return; }
  view.loading = true;
  renderPersonal(page);
  const role = account.a.role.id;
  // Only the newest request of the page counts: an older one answering late (another player,
  // filter or account chosen meanwhile) must not put back what was chosen before.
  const seq = view.seq = (view.seq || 0) + 1;
  const newest = () => S.personal[page] === view && view.seq === seq;
  const current = () => newest() && personalAccount()?.a.role.id === role;  // not another account meanwhile
  try {
    const data = await act('personal', { index: account.index, page, query: view.query }, { quiet: true });
    if (current()) { view.data = data; view.error = null; view.role = role; }
  } catch (error) {
    if (current()) view.error = error.message || String(error);
  } finally {
    if (newest()) {
      view.loading = false;
      renderPersonal(page);
    }
  }
}

const level = (value) => (['good', 'bad', 'warn', 'muted'].includes(value) ? ` ${value}` : '');
function cell(value) {
  if (value && typeof value === 'object') return `<span class="lv${level(value.level)}">${esc(value.text)}</span>`;
  return esc(value);
}

function viewCard(c) {
  const tags = (c.tags || []).map((t) => `<span class="vtag${level(t.level)}">${esc(t.text)}</span>`).join('');
  const stats = (c.stats || []).map((t) => `<div class="vstat"><span>${esc(t.label)}</span><b class="lv${level(t.level)}">${esc(t.value)}</b></div>`).join('');
  const lines = (c.lines || []).map((l) => `<li class="lv${level(l.level)}">${esc(l.text)}</li>`).join('');
  const table = c.table && c.table.rows?.length ? `<div class="vtable"><table>${c.table.columns?.length ? `<thead><tr>${c.table.columns.map((h) => `<th>${esc(h)}</th>`).join('')}</tr></thead>` : ''}
    <tbody>${c.table.rows.map((row) => `<tr>${row.map((v) => `<td>${cell(v)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>` : '';
  return `<div class="card vcard${c.wide ? ' wide' : ''}"><div class="vhead"><div class="vtitle"><h3>${esc(c.title)}</h3>${c.subtitle ? `<small>${esc(c.subtitle)}</small>` : ''}</div>
    ${tags ? `<div class="vtags">${tags}</div>` : ''}</div>
    ${stats ? `<div class="vstats">${stats}</div>` : ''}${lines ? `<ul class="vlines">${lines}</ul>` : ''}${table}</div>`;
}

function renderPersonal(page) {
  const view = S.personal[page];
  const accounts = personalAccounts();
  const account = personalAccount();
  const key = JSON.stringify([view.data, view.error, view.loading, accounts.map(({ a }) => [a.role.id, a.role.label]), account?.a.role.id]);
  const box = $('personal-' + page);
  if (!box || key === view.key) return;
  view.key = key;
  if (!account) {
    box.innerHTML = `<div class="card empty">${(S.data?.tokens || []).length ? '还没有可以查看的账号：Token 审批通过、登录过一次游戏后就能看。' : '先到“账号”添加账号。'}</div>`;
    return;
  }
  const d = view.data;
  const who = accounts.length > 1 ? `<div class="vfilter"><span>账号</span><div class="chips">${accounts.map(({ a }) =>
    `<button class="chip${a.role.id === account.a.role.id ? ' active' : ''}" data-prole="${esc(a.role.id)}">${esc(roleName(a.role.label))}</button>`).join('')}</div></div>` : '';
  const filters = (d?.filters || []).map((f) => `<div class="vfilter"><span>${esc(f.label)}</span><div class="chips">${f.options.map((o) =>
    `<button class="chip${o.value === f.value ? ' active' : ''}" data-pfilter="${esc(f.key)}" data-value="${esc(o.value)}">${esc(o.label)}</button>`).join('')}</div></div>`).join('');
  const refresh = `<button class="btn ghost small" data-prefresh="1"${view.loading ? ' disabled' : ''}>${view.loading ? '<span class="spin"></span>读取中' : `${icon('refresh').replace('<svg', '<svg width="14" height="14"')}刷新`}</button>`;
  // A page with a box to type in (玩家信息: the name); what is typed goes as the server's `search.key`.
  const search = d?.search || view.search;
  if (search) view.search = search;
  const box2 = search ? `<form class="inline-form vsearch" data-psearch="${esc(search.key)}"><input type="text" autocomplete="off" spellcheck="false"
    maxlength="40" value="${esc(view.query[search.key] ?? search.value ?? '')}" placeholder="${esc(search.placeholder || '')}">
    <button class="btn primary" type="submit"${view.loading ? ' disabled' : ''}>查询</button></form>` : '';
  const toolbar = `<div class="vtoolbar"><div class="vfilters">${box2}${who}${filters}</div>${refresh}</div>`;
  let body;
  if (view.error) body = banner('warn', 'info', esc(view.error));
  else if (!d) body = '<div class="card empty">正在读取…</div>';
  else body = (d.note ? `<p class="muted prefs-note">${esc(d.note)}</p>` : '')
    // layout 'grid': the cards side by side as the width allows (玩家信息: five armies at a glance);
    // a `wide` card takes a whole row (the player's own, above their armies).
    + (d.cards.length ? `<div class="vcards${d.layout === 'grid' ? ' grid' : ''}">${d.cards.map(viewCard).join('')}</div>`
      : `<div class="card empty">${esc(d.empty || '没有内容')}</div>`)
    + (d.page && d.page.pages > 1 ? `<div class="pager"><button class="btn ghost small" data-ppage="${d.page.page - 1}"${d.page.page <= 1 ? ' disabled' : ''}>上一页</button>
      <span>第 ${d.page.page} / ${d.page.pages} 页，共 ${d.page.total} 条</span>
      <button class="btn ghost small" data-ppage="${d.page.page + 1}"${d.page.page >= d.page.pages ? ' disabled' : ''}>下一页</button></div>` : '');
  box.innerHTML = toolbar + body;
}

function personalClick(event) {
  if (!PERSONAL.has(S.page)) return false;
  const view = S.personal[S.page];
  const role = event.target.closest('[data-prole]');
  const filter = event.target.closest('[data-pfilter]');
  const pager = event.target.closest('[data-ppage]');
  if (role) {
    S.personalRole = Number(role.dataset.prole);
    store.set('personalRole', S.personalRole);
    for (const name of PERSONAL) S.personal[name] = { query: {} };  // its teams are other teams
  } else if (filter) {
    view.query = { ...view.query, [filter.dataset.pfilter]: filter.dataset.value, page: 1 };
  } else if (pager) {
    view.query = { ...view.query, page: Number(pager.dataset.ppage) };
  } else if (!event.target.closest('[data-prefresh]')) {
    return false;
  }
  loadPersonal(S.page);
  return true;
}

// ---------------------------------------------------------------- the phone through this computer
// The server's steps (texts and checks) as it made them, one at a time; a step's QR code is shown
// as an image, so nothing the server sends runs here. The page asks for the steps when it opens
// and for a QR code when its step is shown (2026-11-07 user: the whole page at once was slow).

// Only the latest call's answer is shown: a read that set out before a reset and comes back after
// it would put back the QR code the reset just revoked.
let phoneCall = 0;

// What an answer has: the steps, or one QR code (a server from before 2026-11-07 asked for a part gives the
// whole page: all of it is taken).
function takePhone(view, data) {
  if (!data || typeof data !== 'object') return;
  if ('problem' in data) view.problem = data.problem;
  if (data.guide) view.guide = data.guide;
  view.parts = view.parts || {};
  if (Array.isArray(data.nodes) && data.nodes.length) view.parts.node = data.nodes[0];
  if (data.rules) view.parts.rules = data.rules;
  if (data.android) view.parts.sub = data.android;
}

// `part`: 'guide' (the steps) or a step's QR code ('node', 'rules', 'sub'); `reset` a new key first
// (the whole page comes back).
async function loadPhone(part = 'guide', reset = false) {
  const account = personalAccount();
  if (!account) { S.phone = {}; renderPhone(S.data); return; }
  const role = account.a.role.id;
  if (S.phone.role !== role) S.phone = { role };
  const view = S.phone;
  const call = ++phoneCall;
  view.loading = part;
  renderPhone(S.data);
  try {
    const data = await act('phone', reset ? { index: account.index, reset } : { index: account.index, part }, { quiet: true });
    if (call !== phoneCall || S.phone !== view) return;
    if (reset) view.parts = {};
    takePhone(view, data);
    view.error = null;
    if (reset) toast('已换新的二维码：手机要删掉旧的、重新扫码，旧的 30 秒内失效');
  } catch (error) {
    if (call === phoneCall && S.phone === view) view.error = error.message || String(error);
  } finally {
    if (call === phoneCall && S.phone === view) {
      view.loading = null;
      renderPhone(S.data);
    }
  }
}

const svgImage = (svg) => `data:image/svg+xml;base64,${btoa(unescape(encodeURIComponent(svg)))}`;
const copyLink = (text, label) => `<button class="link" data-copy-text="${esc(text)}">${esc(label)}</button>`;

// Problems of the exits, at the top of the page (nothing while all is well).
const EXIT_PROBLEMS = {
  refused: '中转不接受这台电脑的账号：账号可能还没审批通过，或已被停用。',
  busy: '另一台电脑正在用这个账号转发手机的游戏连接：手机会经那台电脑出去。要用这台，先关掉那台电脑上的本程序。',
  unreachable: '连不上中转，正在重试；手机暂时连不上游戏。',
};

function phoneProblems(p) {
  if (!p.running) return [banner('warn', 'info', '上报没有运行，手机现在连不上游戏；到“概览”看原因。')];
  if (p.waiting) return [banner('info', 'info', '正在向服务器要中转……账号审批通过后才有。')];
  return p.exits.filter((e) => EXIT_PROBLEMS[e.state]).map((e) => banner('warn', 'info', esc(EXIT_PROBLEMS[e.state])));
}

// The push binding of one account (Bark on an iPhone, ntfy on Android), under the step's text.
function pushBlock(a, index, os) {
  if (!a.bark) return '<p class="muted">这个账号暂时不能绑定推送。</p>';
  if (a.bark.bound) {
    const phone = a.bark.kind === 'ntfy' ? '安卓手机' : 'iPhone';
    const ntfy = a.bark.kind === 'ntfy' ? a.bark.android : null;
    const sub = ntfy ? `<p class="phone-sub">ntfy 里扫左边的码订阅；不能扫码时手动添加：服务器 <code>${esc(ntfy.server)}</code> ${copyLink(ntfy.server, '复制')}，
      主题 <code>${esc(ntfy.topic)}</code> ${copyLink(ntfy.topic, '复制')}</p>` : '';
    return `${sub}<div class="row-actions"><span class="token-text">已绑定 ${esc(a.bark.address)}</span></div>
      <div class="row-actions"><button class="btn ghost small" data-action="bark_test" data-index="${index}">发测试推送</button>
      <button class="btn ghost small" data-action="bark_unbind" data-index="${index}" data-confirm="解绑后这台${phone}不再收到推送，确定吗？">解绑</button></div>
      ${a.bark.ally_help ? '<p class="small-note muted">盟友没有电脑？上面“手机”选“盟友手机”，你的推送也发给他。</p>' : ''}`;
  }
  return os === 'android'
    ? `<div class="row-actions"><button class="btn primary" data-action="ntfy_bind" data-index="${index}">绑定安卓</button></div>`
    : `<form class="inline-form phone-bark" data-bark-form="${index}"><input type="text" data-bark-input="${index}" spellcheck="false" autocomplete="off" placeholder="粘贴 Bark 里的地址：https://api.day.app/…">
      <button class="btn primary" type="submit">绑定</button></form>`;
}
// An ally's phones (no computer of their own): the server's texts, one line each, a form to add one.
// The QR code of an Android one is what the last add / show gave (the status each minute has none).
function allyCard(a, index) {
  const bark = a.bark;
  if (!bark || !bark.ally_help) {
    return { media: '', html: '<p class="muted">服务器还不支持盟友手机，请稍后再试。</p>' };
  }
  const allies = bark.allies || [];
  const help = bark.ally_help;
  const max = bark.ally_max || 3;
  const shownId = S.allyShown && S.allyShown.role === a.role.id ? S.allyShown.id : null;
  const shown = allies.find((x) => x.id === shownId && x.kind === 'ntfy');
  const rows = allies.map((x) => `<div class="ally-row${x.id === shownId ? ' active' : ''}">
      <span class="ally-name">${esc(x.name || '未命名')}</span>
      <span class="token-text">${x.kind === 'ntfy' ? '安卓' : 'iPhone'} ${esc(x.address)}</span>
      ${x.error ? `<span class="ally-bad" title="${esc(x.error)}">上一条没送达</span>` : ''}
      <span class="ally-actions">${x.kind === 'ntfy' ? `<button class="link" data-ally-op="show" data-ally-id="${esc(x.id)}">订阅方法</button>` : ''}
        <button class="link" data-ally-op="test" data-ally-id="${esc(x.id)}">发测试推送</button>
        <button class="link" data-ally-op="remove" data-ally-id="${esc(x.id)}" data-confirm="删掉后这台手机不再收到你的推送，确定吗？">删除</button></span></div>`).join('');
  let media = '', how = '';
  if (shown) {
    const n = S.allyShown.android || shown.android || {};
    if (n.qr) media = `<img class="qr" alt="盟友的 ntfy 订阅二维码" src="${svgImage(n.qr)}">`;
    how = `<div class="ally-how"><b>${esc(shown.name || '这台安卓手机')}的订阅方法</b><ol>${(n.steps || []).map((t) => `<li>${esc(t)}</li>`).join('')}</ol>
      <p class="phone-sub">服务器 <code>${esc(n.server || '')}</code> ${copyLink(n.server || '', '复制')}，主题 <code>${esc(n.topic || '')}</code> ${copyLink(n.topic || '', '复制')}</p>
      ${n.note ? `<p class="small-note muted">${esc(n.note)}</p>` : ''}</div>`;
  }
  const form = allies.length >= max ? `<p class="muted">已经 ${allies.length} 台，最多 ${max} 台；要加新的先删掉一台。</p>`
    : `<form class="inline-form ally-form" data-ally-form="${index}">
        <input type="text" class="ally-name-input" data-ally-name maxlength="12" spellcheck="false" autocomplete="off" placeholder="盟友名字">
        <input type="text" data-ally-address spellcheck="false" autocomplete="off" placeholder="粘贴盟友 Bark 里的地址：https://api.day.app/…">
        <button class="btn primary" type="submit">添加 iPhone</button>
        <button class="btn" type="button" data-ally-ntfy="${index}">添加安卓手机</button></form>
      <p class="small-note muted">${esc(help.bark || '')}<br>${esc(help.ntfy || '')}</p>`;
  return {
    media,
    html: `<h3>盟友手机（${allies.length}/${max}）</h3><p>${esc(help.text || '')}</p>
      ${rows ? `<div class="ally-list">${rows}</div>` : ''}${how}${form}`,
  };
}

// Which step of the phone's guide the player is on, per phone (kept, so the page opens where they were).
function phoneStep(os) { return Number((S.phoneStep || {})[os]) || 0; }

// How a step is confirmed (the server's `check`): the push binding there; the relays seeing the
// phone (the connectivity test, a game connection) or refusing its other apps' connections.
function phoneCheck(step, account, pipes) {
  const line = (cls, text) => `<p class="phone-check ${cls}">${cls === 'ok' ? '✓ ' : cls === 'bad' ? '✗ ' : ''}${esc(text)}</p>`;
  if (step.check === 'push') {
    const bark = account.a.bark;
    if (!bark || !bark.bound) return line('wait', '还没绑定。绑定后手机会收到一条测试通知。');
    return bark.error ? line('bad', `已绑定，但上一条推送失败：${bark.error}`) : line('ok', '已绑定：敌袭等需要你下令的通知会推送到这台手机。');
  }
  if (step.check !== 'connect') return '';
  const seen = S.phoneSeen && S.phoneSeen.role === account.a.role.id ? S.phoneSeen.status : undefined;
  if (pipes) return line('ok', `手机已连上：正在经这台电脑转发 ${pipes} 条游戏连接。`);
  if (seen === undefined) return line('wait', '正在检查手机连上了没有……');
  if (seen === null) return '';  // the server cannot tell
  if (seen.misrouted) return line('bad', '小火箭把其他应用的连接也交给了这个节点，规则没生效：回到上一步，在小火箭“配置”页点这份配置选“使用配置”；首页“全局路由”选“配置”，不要选“代理”。');
  if (seen.game !== null && seen.game < 600) return line('ok', '手机已连上：游戏连接经这台电脑出去了。');
  if (seen.checked !== null && seen.checked < 600) {
    return seen.check_ok ? line('ok', '手机已连上：连通性测试通过，可以进游戏了。')
      : line('bad', '手机连到了中转，但这台电脑没连上中转：看这个页面顶部的提示。');
  }
  return line('wait', '还没看到手机连上来。照上面做完后，这里每 10 秒自动检查一次。');
}

// While the connect step is shown, ask the server every 10 s what the relays saw of the phone.
let phoneTimer = null;
function watchPhone(on) {
  if (!on) { clearTimeout(phoneTimer); phoneTimer = null; return; }
  if (phoneTimer) return;
  const tick = async () => {
    const account = personalAccount();
    if (S.page !== 'phone' || !account) { phoneTimer = null; return; }
    try {
      const reply = await act('phone_status', { index: account.index }, { quiet: true });
      S.phoneSeen = { role: account.a.role.id, status: reply ? reply.status : null };
    } catch { /* tried again next time */ }
    renderPhone(S.data);
    if (phoneTimer) phoneTimer = setTimeout(tick, 10000);
  };
  phoneTimer = setTimeout(tick, 0);
}

// The QR codes a step shows (the server's `show`), their part of the page and what copying gives.
const PHONE_QR = {
  node: { alt: '节点的二维码', link: (q) => q.link, copy: '复制节点链接', keyed: true },
  rules: { alt: '规则的二维码', link: (q) => q.link, copy: '复制规则地址' },  // the same for everyone: nothing to reset
  sub: { alt: '订阅的二维码', link: (q) => q.sub, copy: '复制订阅地址', keyed: true },
};

// The page: the account / phone / step choosers on one line, then the step: its QR code on the
// left (2026-11-07 user: easier on the eye), its text, check and what to do on the right, and
// 上一步 / 下一步 at the card's bottom, where they stay. It fits the window without scrolling.
function renderPhone(d) {
  if (!d) return;
  const p = d.phone || { exits: [] };
  const view = S.phone;
  const accounts = personalAccounts();
  const account = personalAccount();
  const os = S.phoneOs;
  const problems = phoneProblems(p).join('');
  if (S.phoneProblems !== problems) { S.phoneProblems = problems; $('phone-status').innerHTML = problems; }
  const pipes = p.exits.reduce((n, e) => n + (e.pipes || 0), 0);
  const steps = os && view.guide ? view.guide[os] || [] : [];
  const at = Math.min(phoneStep(os), Math.max(steps.length - 1, 0));
  const step = steps[at];
  watchPhone(S.page === 'phone' && Boolean(account) && step?.check === 'connect');
  const qrPart = step && PHONE_QR[step.show] ? step.show : null;
  const { key: _drawn, ...shown } = view;
  const key = JSON.stringify([shown, accounts.map(({ a }) => a.role.id), account?.a.role.id, account?.a.bark, os, pipes, S.phoneStep, S.phoneSeen, S.allyShown?.id]);
  if (key === view.key) return;
  const box = $('phone-box');
  if (!account) {
    view.key = key;
    box.innerHTML = `<div class="card empty">${(d.tokens || []).length ? '还没有能用手机玩的账号：Token 审批通过（先在这台电脑上登录一次游戏）后就有。' : '先到“账号”添加账号。'}</div>`;
    return;
  }
  if (view.role !== account.a.role.id || (!view.guide && view.problem === undefined && !view.error && !view.loading)) { loadPhone(); return; }
  if (qrPart && !(view.parts || {})[qrPart] && !view.loading && !view.error && !view.problem) { loadPhone(qrPart); return; }
  view.key = key;
  const typed = [...document.querySelectorAll('#phone-box [data-bark-input]')].map((input) => input.value)[0] || '';
  const allyTyped = ['data-ally-name', 'data-ally-address'].map((attr) => [...document.querySelectorAll(`#phone-box [${attr}]`)][0]?.value || '');
  const chips = (name, items) => `<div class="vfilter"><span>${name}</span><div class="chips">${items}</div></div>`;
  const who = accounts.length > 1 ? chips('账号', accounts.map(({ a }) =>
    `<button class="chip${a.role.id === account.a.role.id ? ' active' : ''}" data-phone-role="${esc(a.role.id)}">${esc(roleName(a.role.label))}</button>`).join('')) : '';
  const phones = chips('手机', [['iphone', 'iPhone'], ['android', '安卓'], ['ally', '盟友手机']].map(([id, label]) =>
    `<button class="chip${os === id ? ' active' : ''}" data-phone-os="${id}">${label}</button>`).join(''));
  const dots = steps.length ? `<div class="phone-dots">${steps.map((st, i) => `<button class="phone-dot${i === at ? ' active' : ''}${i < at ? ' done' : ''}" data-phone-step="${i}" title="${esc(st.title)}">${i + 1}</button>`).join('')}</div>` : '';
  const bar = `<div class="phone-bar">${who}${phones}${dots}</div>`;
  let body;
  if (os === 'ally') {
    const card = allyCard(account.a, account.index);
    body = `<div class="card phone-step${card.media ? '' : ' single'}"><div class="phone-step-body">
      ${card.media ? `<div class="phone-step-media">${card.media}</div>` : ''}<div class="phone-step-text">${card.html}</div></div></div>`;
  } else if (view.error) body = banner('warn', 'info', `${esc(view.error)}`, '<button class="btn" data-phone-retry="1">重试</button>');
  else if (view.problem) body = banner('warn', 'info', esc(view.problem));
  else if (!view.guide) body = '<div class="card empty">正在读取…</div>';
  else if (!os) body = '<div class="card empty">先在上面选你的手机是 iPhone 还是安卓，然后一步一步照着做。</div>';
  else if (!step) body = banner('warn', 'info', '服务器还没有这种手机的步骤，请稍后再试。');
  else {
    let media = '', extra = '';
    if (qrPart) {
      const part = view.parts[qrPart];
      media = part && part.qr ? `<img class="qr" alt="${PHONE_QR[qrPart].alt}" src="${svgImage(part.qr)}">`
        : part ? '<div class="qr-none">服务器没给二维码，复制下面的地址到手机上导入</div>' : '<div class="qr-none"><span class="spin dark"></span></div>';
      extra = part ? `<p class="phone-links">${copyLink(PHONE_QR[qrPart].link(part), PHONE_QR[qrPart].copy)}（手机上扫不了码时用）${PHONE_QR[qrPart].keyed
        ? '<span class="sep">·</span>二维码外泄了？<button class="link" data-phone-reset="1">换新的</button>' : ''}</p>` : '';
    } else if (step.show === 'push') {
      const ntfy = account.a.bark?.bound && account.a.bark.kind === 'ntfy' ? account.a.bark.android : null;
      if (ntfy?.qr) media = `<img class="qr" alt="ntfy 订阅二维码" src="${svgImage(ntfy.qr)}">`;
      extra = pushBlock(account.a, account.index, os);
    }
    body = `<div class="card phone-step${media ? '' : ' single'}">
      <div class="phone-step-body">${media ? `<div class="phone-step-media">${media}</div>` : ''}
        <div class="phone-step-text"><h3><span class="step-no">${at + 1}</span>${esc(step.title)}</h3><p>${esc(step.text)}</p>
          ${phoneCheck(step, account, pipes)}${extra}</div></div>
      <div class="phone-step-nav"><button class="btn ghost" data-phone-step="${at - 1}"${at ? '' : ' disabled'}>上一步</button>
        <span class="muted">第 ${at + 1} / ${steps.length} 步</span>
        <button class="btn primary${at < steps.length - 1 ? '' : ' invisible'}" data-phone-step="${at + 1}">下一步</button></div></div>`;
  }
  box.innerHTML = bar + body;
  document.querySelectorAll('#phone-box [data-bark-input]').forEach((input) => { input.value = typed; });
  ['data-ally-name', 'data-ally-address'].forEach((attr, i) => {
    const input = [...document.querySelectorAll(`#phone-box [${attr}]`)][0];
    if (input) input.value = allyTyped[i];
  });
}

// What the server answered an ally's add / show: which one to show, with its QR code.
function allyShow(account, reply, id) {
  const ally = (reply && reply.allies || []).find((x) => x.id === id);
  S.allyShown = ally && ally.kind === 'ntfy' ? { role: account.a.role.id, id, android: ally.android } : null;
}

async function allyAdd(button, payload) {
  const account = personalAccount();
  if (!account) return;
  const before = new Set(((account.a.bark || {}).allies || []).map((x) => x.id));
  const name = [...document.querySelectorAll('#phone-box [data-ally-name]')][0]?.value || '';
  try {
    const reply = await withBusy(button, '添加中', () => act('bark_ally', { index: account.index, op: 'add', name, ...payload }));
    const added = (reply && reply.allies || []).find((x) => !before.has(x.id));
    allyShow(account, reply, added && added.id);
    document.querySelectorAll('#phone-box [data-ally-name], #phone-box [data-ally-address]').forEach((input) => { input.value = ''; });
    toast(payload.kind === 'ntfy' ? '已添加，让盟友用 ntfy 扫码或手动订阅' : '已添加，盟友手机应该收到了一条测试通知');
    renderPhone(S.data);
  } catch { /* act said why */ }
}


async function phoneClick(event) {
  if (S.page !== 'phone') return false;
  const copy = event.target.closest('[data-copy-text]');
  if (copy) {
    try { await navigator.clipboard.writeText(copy.dataset.copyText); toast('已复制'); }
    catch { toast('复制失败', true); }
    return true;
  }
  const stepButton = event.target.closest('[data-phone-step]');
  if (stepButton && !stepButton.disabled) {
    S.phoneStep = { ...(S.phoneStep || {}), [S.phoneOs]: Math.max(0, Number(stepButton.dataset.phoneStep)) };
    store.set('phoneStep', S.phoneStep);
    renderPhone(S.data);
    return true;
  }
  const phone = event.target.closest('[data-phone-os]');
  if (phone) {
    S.phoneOs = phone.dataset.phoneOs;
    store.set('phoneOs', S.phoneOs);
    renderPhone(S.data);
    return true;
  }
  const role = event.target.closest('[data-phone-role]');
  if (role) {
    S.personalRole = Number(role.dataset.phoneRole);
    store.set('personalRole', S.personalRole);
    for (const name of PERSONAL) S.personal[name] = { query: {} };
    S.phone = {};
    loadPhone();
    return true;
  }
  if (event.target.closest('[data-phone-reset]')) {
    if (!S.phone.loading && confirm('换新的二维码？旧的 30 秒内失效，手机上要删掉旧节点、重新扫码。')) loadPhone('guide', true);
    return true;
  }
  const allyNtfy = event.target.closest('[data-ally-ntfy]');
  if (allyNtfy) { await allyAdd(allyNtfy, { kind: 'ntfy' }); return true; }
  const allyOp = event.target.closest('[data-ally-op]');
  if (allyOp) {
    const account = personalAccount();
    if (!account || (allyOp.dataset.confirm && !confirm(allyOp.dataset.confirm))) return true;
    const { allyOp: op, allyId: id } = allyOp.dataset;
    if (op === 'show' && S.allyShown?.id === id) { S.allyShown = null; renderPhone(S.data); return true; }
    try {
      const reply = await withBusy(allyOp, '', () => act('bark_ally', { index: account.index, op, id }));
      if (op === 'show') allyShow(account, reply, id);
      if (op === 'remove' && S.allyShown?.id === id) S.allyShown = null;
      if (op === 'test') toast('已发送测试推送，问问盟友有没有收到');
      if (op === 'remove') toast('已删除这台盟友手机');
      renderPhone(S.data);
    } catch { /* act said why */ }
    return true;
  }
  if (event.target.closest('[data-phone-retry]')) { S.phone.error = null; renderPhone(S.data); return true; }
  return false;
}

function renderSettings(d) {
  const s = d.settings;
  const autostart = $('set-autostart');
  autostart.checked = s.autostart;
  autostart.disabled = !s.autostart_available;
  autostart.closest('.setting').title = s.autostart_available ? '' : '只有打包好的 Windows 程序可以设置';
  $('set-keep').checked = s.keep_running;
  $('dumpcap-path').textContent = s.dumpcap ? `${s.dumpcap}${s.dumpcap_picked ? '（手动选择）' : '（自动找到）'}` : '没有找到，请安装 Wireshark 或手动选择';
  $('forget-dumpcap').classList.toggle('hidden', !s.dumpcap_picked);
  $('pick-dumpcap').disabled = !s.windows;
  $('state-dir').textContent = s.state_dir;
  $('server-origin').textContent = d.server;
}

function renderChecks(d) {
  const row = (ok, name, text) => `<div class="check-row ${ok === null ? 'unknown' : ok ? 'ok' : 'bad'}"><i>${icon(ok === null ? 'question' : ok ? 'check' : 'cross')}</i><b>${name}</b><span>${esc(text)}</span></div>`;
  const accounts = d.tokens;
  const offline = accounts.length && accounts.every((a) => a.state === 'offline');
  const rows = [
    row(!offline && accounts.some((a) => a.state !== 'checking') ? true : offline ? false : null, '服务器', offline ? `连不上 ${d.server}` : d.server),
    row(accounts.length ? accounts.every((a) => a.state === 'ok') : false, '账号', accounts.length ? accounts.map((a) => `${a.token} ${accountState(a)[0]}`).join('；') : '没有 Token'),
    row(Boolean(d.settings.dumpcap), '网络组件', d.settings.dumpcap || '没有找到 dumpcap.exe（Wireshark）'),
    row(d.capture.state === 'running' ? true : d.capture.state === 'error' ? false : null, '上报', d.capture.state === 'running' ? '运行中' : d.capture.error || '未运行'),
    row(d.stats.connections > 0 ? true : null, '连接', d.stats.connections > 0 ? `${d.stats.connections} 条` : '最近 5 分钟没有看到游戏连接（没在玩游戏时正常）'),
  ];
  const html = rows.join('');
  if ($('checks').dataset.html !== html) { $('checks').innerHTML = html; $('checks').dataset.html = html; }
}

function appendLogs(lines) {
  const box = $('log');
  const fragment = document.createDocumentFragment();
  for (const line of lines) {
    const div = document.createElement('div');
    div.className = line.level === 'info' ? '' : line.level;
    div.innerHTML = `<span class="t">${clock(line.time)}</span>${esc(line.text)}`;
    fragment.append(div);
  }
  box.append(fragment);
  while (box.childElementCount > 1000) box.firstElementChild.remove();
  if ($('log-follow').checked) box.scrollTop = box.scrollHeight;
}

// ---------------------------------------------------------------- 群聊

const CHAT_KEEP = 500;  // messages the page holds; older ones are asked for again
const chatSeenKey = () => `chatSeen:${S.chat.account}`;
function takeChat(chat) {
  if (!chat || !Array.isArray(chat.messages)) return;
  if ((chat.account || '') !== S.chat.account) {
    const stale = S.chat.c !== 0;
    Object.assign(S.chat, { account: chat.account || '', messages: [], c: 0, more: false, key: '', bottom: true });
    S.chat.seen = store.get(chatSeenKey(), store.get('chatSeen', 0));  // before v1.13.2: one for all
    if (stale) { schedule(); return; }  // asked after the other account's: ask again for the newest
  }
  const first = !S.chat.c;
  const fresh = chat.messages.filter((m) => m.seq > S.chat.c);
  if (fresh.length) {
    S.chat.messages.push(...fresh);
    S.chat.c = fresh[fresh.length - 1].seq;
  }
  // Read past the newest this account has: not its own (the "one for all" of before v1.13.2, on a
  // new account), and no message would count as unread until the numbers caught up with it.
  if (S.chat.seen > S.chat.c) { S.chat.seen = S.chat.c; store.set(chatSeenKey(), S.chat.c); }
  if (first) S.chat.more = chat.oldest != null && S.chat.messages.length > 0 && chat.oldest < S.chat.messages[0].seq;
  if (S.chat.messages.length > CHAT_KEEP) { S.chat.messages.splice(0, S.chat.messages.length - CHAT_KEEP); S.chat.more = true; }
}
const CHAT_STATES = {
  online: (c) => [c.online != null ? `在线 ${c.online} 人` : '已连接', ''],
  connecting: () => ['连接中…', ''],
  offline: (c) => [c.error || '连不上群聊，稍后自动重连', 'err'],
  unavailable: () => ['添加 Token 后，服务器确认了就能群聊', 'err'],
};
function chatClock(seconds) {
  const date = new Date(seconds * 1000), now = new Date();
  const pad = (n) => String(n).padStart(2, '0');
  const time = `${pad(date.getHours())}:${pad(date.getMinutes())}`;
  if (date.toDateString() === now.toDateString()) return time;
  return `${date.getFullYear() !== now.getFullYear() ? date.getFullYear() + '年' : ''}${date.getMonth() + 1}月${date.getDate()}日 ${time}`;
}
function avatar(name, official, level) {
  if (official) return '<span class="av official" title="率土战局官方">率</span>';
  const lv = Number.isInteger(level) && level >= 1 && level <= 3 ? ` lv${level}` : '';
  const text = String(name);
  const shown = text.includes('的') ? text.split('的').pop()[0] : text[0];
  let hash = 0;
  for (const ch of text) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
  return `<span class="av c${hash % 6}${lv}">${esc(shown || '?')}</span>`;
}
function renderChat(d) {
  const c = d.chat || { state: 'connecting' };
  const [stateText, stateClass] = (CHAT_STATES[c.state] || CHAT_STATES.connecting)(c);
  $('chat-state').textContent = stateText;
  $('chat-state').className = stateClass;
  // One's own name in the bar above the messages, always in sight (2026-11-08 players did not know
  // theirs: it was a line above the oldest message, scrolled away as the chat opens at the newest).
  const me = c.name ? `我在群里叫 <strong>${esc(c.name)}</strong>` : '';
  if ($('chat-me').innerHTML !== me) $('chat-me').innerHTML = me;
  // Which account speaks (2026-10-31 user: testing with another account without removing one).
  const choices = (c.choices || []).filter((index) => d.tokens[index]);
  const whoKey = JSON.stringify([choices, c.using, choices.map((index) => d.tokens[index].role?.label || d.tokens[index].name || '')]);
  if (whoKey !== S.chat.whoKey) {
    S.chat.whoKey = whoKey;
    $('chat-who').innerHTML = choices.length > 1 ? `<span>用哪个账号聊</span><div class="chips">${choices.map((index) => {
      const a = d.tokens[index];
      return `<button class="chip${index === c.using ? ' active' : ''}" data-chat-use="${index}">${esc(a.role ? roleName(a.role.label) : a.name || a.token)}</button>`;
    }).join('')}</div>` : '';
  }
  const canSend = c.state === 'online';
  $('chat-text').disabled = !canSend;
  if (!S.busy.has('chat')) $('chat-submit').disabled = !canSend;
  $('chat-image-button').disabled = !canSend;
  const list = $('chat-list'), messages = S.chat.messages;
  const last = messages.length ? messages[messages.length - 1].seq : 0;
  if (last > S.chat.seen) { S.chat.seen = last; store.set(chatSeenKey(), last); }
  const key = `${messages.length}:${messages[0]?.seq}:${last}:${S.chat.more}:${S.chat.loading}:${c.name}:${S.chat.gone.size}`;
  if (key === S.chat.key) return;
  S.chat.key = key;
  const atBottom = S.chat.bottom || list.scrollHeight - list.scrollTop - list.clientHeight < 80;
  const parts = [];
  if (S.chat.more) parts.push(`<button class="link chat-more" data-chat-older="1">${S.chat.loading ? '加载中…' : '查看更早的消息'}</button>`);
  let previous = 0;
  for (const m of messages) {
    if (m.time - previous > 300) parts.push(`<div class="chat-time">${esc(chatClock(m.time))}</div>`);
    previous = m.time;
    parts.push(`<div class="msg${m.mine ? ' mine' : ''}${m.official ? ' official' : ''}">${avatar(m.name, m.official, m.level)}<div class="col"><span class="who">${esc(m.name)}${m.official ? '<b class="tag-official">官方</b>' : ''}</span>${chatBody(m)}</div></div>`);
  }
  if (!messages.length) parts.push('<div class="empty">还没有消息。打个招呼吧</div>');
  list.innerHTML = parts.join('');
  if (atBottom) list.scrollTop = list.scrollHeight;
  S.chat.bottom = false;
}
// Images and emoji as in WeChat / WeCom (2026-11-11 user): images pasted, dropped or picked wait above the
// input as thumbnails and go with 发送 (each its own message, before the text); the emoji panel puts
// one at the cursor and closes, the latest picks first next time.
const CHAT_IMAGE_MAX = 1000 * 1024;  // the server takes 1 MB
const CHAT_IMAGE_SIDE = 1920;
const CHAT_IMAGES_AT_ONCE = 9;
const CHAT_SEND_GAP = 1100;  // ms between two messages: the server takes one a second
const CHAT_EMOJI = ('😀 😃 😄 😁 😆 😅 😂 🤣 😊 😇 🙂 😉 😌 😍 🥰 😘 😋 😛 😜 🤪 😎 🤩 🥳 😏 😒 😞 😔 😟 '
  + '😕 🙁 😣 😖 😫 😩 🥺 😢 😭 😤 😠 😡 🤬 🤯 😳 🥵 🥶 😱 😨 😰 😥 😓 🤗 🤔 🤭 🤫 😶 😐 😑 😬 🙄 '
  + '😯 😮 😲 🥱 😴 🤤 😪 😵 🤐 🥴 🤢 🤮 🤧 😷 🤒 🤕 🤑 🤠 😈 👿 💩 👻 💀 🤡 🙈 🙉 🙊 👍 👎 👌 ✌️ 🤞 '
  + '🤝 👏 🙌 🙏 💪 👊 ✊ 👋 🤙 👉 👆 ❤️ 💔 💯 🔥 🎉 ✨ ⭐ 🌹 🍺 ☕ 🎁 💰 ⚔️ 🛡️ 🏹 🐴 🏰 🚩 ✅ ❌ ❓ ❗').split(' ');
const chatPending = [];  // { file, url } waiting for 发送
function chatImageSize(w, h, side = 240) {
  const scale = Math.min(1, side / w, side / h);
  return [Math.max(40, Math.round(w * scale)), Math.max(40, Math.round(h * scale))];
}
function chatBody(m) {
  if (!m.image) return `<div class="bubble">${esc(m.text)}</div>`;
  if (S.chat.gone.has(m.image.name)) return '<div class="bubble gone">[图片已过期]</div>';
  const [width, height] = chatImageSize(m.image.w, m.image.h);
  const src = `/api/chat-image/${encodeURIComponent(m.image.name)}`;
  return `<div class="bubble pic"><img class="chat-img" src="${esc(src)}" width="${width}" height="${height}" alt="图片" title="点击查看大图" data-chat-view="${esc(src)}" data-image="${esc(m.image.name)}"></div>`;
}
// An image the page cannot show (gone after a week): said so, and not asked for again at every render.
$('chat-list').addEventListener('error', (event) => {
  const name = event.target?.dataset?.image;
  if (name && !S.chat.gone.has(name)) { S.chat.gone.add(name); render(); }
}, true);
function chatViewer(src) {
  const close = () => { overlay.remove(); document.removeEventListener('keydown', onKey, true); };
  const onKey = (event) => { if (event.key === 'Escape') { event.stopPropagation(); close(); } };
  const overlay = document.createElement('div');
  overlay.className = 'chat-viewer';
  overlay.title = '点击关闭';
  const image = document.createElement('img');
  image.src = src;
  image.alt = '图片';
  overlay.append(image);
  overlay.addEventListener('click', close);
  document.addEventListener('keydown', onKey, true);
  document.body.append(overlay);
}
// As it is sent: a GIF small enough kept (it moves), the rest drawn again as WebP, at most
// CHAT_IMAGE_SIDE a side and CHAT_IMAGE_MAX bytes -> { blob, w, h }.
async function chatShrink(file) {
  const url = URL.createObjectURL(file);
  try {
    const image = await new Promise((resolve, reject) => {
      const node = new Image();
      node.onload = () => resolve(node);
      node.onerror = () => reject(new Error('这个文件不是图片，或者打不开'));
      node.src = url;
    });
    const w = image.naturalWidth, h = image.naturalHeight;
    if (!w || !h) throw new Error('这个文件不是图片，或者打不开');
    if (file.type === 'image/gif' && file.size <= CHAT_IMAGE_MAX) return { blob: file, w, h };
    let side = Math.min(CHAT_IMAGE_SIDE, Math.max(w, h));
    for (let quality = 0.85; ; quality -= 0.15) {
      const scale = side / Math.max(w, h);
      const canvas = document.createElement('canvas');
      canvas.width = Math.max(1, Math.round(w * scale));
      canvas.height = Math.max(1, Math.round(h * scale));
      const context = canvas.getContext('2d');
      context.fillStyle = '#fff';
      context.fillRect(0, 0, canvas.width, canvas.height);
      context.drawImage(image, 0, 0, canvas.width, canvas.height);
      let blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/webp', quality));
      if (!blob || blob.type !== 'image/webp') blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/jpeg', quality));
      if (!blob) throw new Error('图片处理失败');
      if (blob.size <= CHAT_IMAGE_MAX) return { blob, w: canvas.width, h: canvas.height };
      if (quality < 0.5) { side = Math.round(side * 0.7); quality = 1; }
      if (side < 200) throw new Error('图片太大了');
    }
  } finally { URL.revokeObjectURL(url); }
}
function chatBase64(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(',', 2)[1] || '');
    reader.onerror = () => reject(new Error('图片读取失败'));
    reader.readAsDataURL(blob);
  });
}
function chatDrawPending() {
  const box = $('chat-pending');
  box.hidden = !chatPending.length;
  box.replaceChildren(...chatPending.map((item) => {
    const node = document.createElement('div');
    node.className = 'chat-pending-item';
    const image = document.createElement('img');
    image.src = item.url;
    image.alt = '待发送的图片';
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'chat-pending-remove';
    remove.title = remove.ariaLabel = '不发这张';
    remove.textContent = '×';
    remove.addEventListener('click', () => {
      URL.revokeObjectURL(item.url);
      chatPending.splice(chatPending.indexOf(item), 1);
      chatDrawPending();
    });
    node.append(image, remove);
    return node;
  }));
}
function chatAddImages(list) {
  const images = [...list].filter((f) => f.type.startsWith('image/'));
  if (list.length && !images.length) toast('只能发图片', true);
  for (const file of images) {
    if (chatPending.length >= CHAT_IMAGES_AT_ONCE) { toast(`一次最多 ${CHAT_IMAGES_AT_ONCE} 张`, true); break; }
    chatPending.push({ file, url: URL.createObjectURL(file) });
  }
  chatDrawPending();
  $('chat-text').focus();
}
function chatRecent() {
  const kept = store.get('chatEmojiRecent', []);
  return Array.isArray(kept) ? kept.filter((e) => CHAT_EMOJI.includes(e)).slice(0, 8) : [];
}
function chatEmojiPanel() {
  const grid = (list) => `<div class="chat-emoji-grid">${list.map((e) => `<button type="button" class="chat-emoji-item" data-emoji="${esc(e)}">${esc(e)}</button>`).join('')}</div>`;
  const recent = chatRecent();
  $('chat-emoji').innerHTML = (recent.length ? `<div class="chat-emoji-title">最近使用</div>${grid(recent)}` : '')
    + `<div class="chat-emoji-title">所有表情</div>${grid(CHAT_EMOJI)}`;
}
function chatInsert(emoji) {
  const input = $('chat-text');
  const start = input.selectionStart ?? input.value.length, end = input.selectionEnd ?? start;
  input.value = input.value.slice(0, start) + emoji + input.value.slice(end);
  input.selectionStart = input.selectionEnd = start + emoji.length;
  store.set('chatEmojiRecent', [emoji, ...chatRecent().filter((e) => e !== emoji)].slice(0, 8));
  $('chat-emoji').hidden = true;
  chatCount();
  input.focus();
}
$('chat-emoji-button').addEventListener('click', (event) => {
  event.stopPropagation();
  const panel = $('chat-emoji');
  const opening = panel.hidden;
  if (opening) chatEmojiPanel();
  panel.hidden = !opening;
  if (opening) panel.scrollTop = 0;  // the recent ones in sight (a hidden panel keeps no scroll)
});
$('chat-emoji').addEventListener('click', (event) => {
  event.stopPropagation();
  const item = event.target.closest('[data-emoji]');
  if (item) chatInsert(item.dataset.emoji);
});
document.addEventListener('click', () => { $('chat-emoji').hidden = true; });
document.addEventListener('keydown', (event) => { if (event.key === 'Escape') $('chat-emoji').hidden = true; });
$('chat-image-button').addEventListener('click', () => $('chat-file').click());
$('chat-file').addEventListener('change', () => { chatAddImages($('chat-file').files); $('chat-file').value = ''; });
// A screenshot or a copied image pasted: only when no text comes with it (a spreadsheet's cells also give a picture).
$('chat-text').addEventListener('paste', (event) => {
  const files = [...(event.clipboardData?.files || [])].filter((f) => f.type.startsWith('image/'));
  if (files.length && !event.clipboardData.getData('text/plain')) { event.preventDefault(); chatAddImages(files); }
});
// A file dropped anywhere would otherwise open in the window in place of the page.
document.addEventListener('dragover', (event) => { if (event.dataTransfer?.types.includes('Files')) event.preventDefault(); });
document.addEventListener('drop', (event) => {
  if (!event.dataTransfer?.files.length) return;
  event.preventDefault();
  if (S.page === 'chat') chatAddImages(event.dataTransfer.files);
});
async function chatOlder() {
  if (S.chat.loading || !S.chat.messages.length) return;
  S.chat.loading = true;
  render();
  const list = $('chat-list'), height = list.scrollHeight;
  try {
    const older = await act('chat_older', { before: S.chat.messages[0].seq });
    S.chat.messages.unshift(...older);
    S.chat.more = older.length >= 30;
  } catch { /* shown */ } finally {
    S.chat.loading = false;
    S.chat.key = '';
    render();
    list.scrollTop += list.scrollHeight - height;  // stays at what was read
  }
}
function chatCount() {
  const length = $('chat-text').value.length;
  $('chat-count').textContent = length > 400 ? `${length}/500` : '';
  $('chat-count').className = length > 500 ? 'over' : '';
}
async function chatSend() {
  const input = $('chat-text');
  const text = input.value.trim();
  if ((!text && !chatPending.length) || S.busy.has('chat')) return;
  if (text.length > 500) return toast('消息太长了，最多 500 个字', true);
  S.busy.add('chat');
  try {
    await withBusy($('chat-submit'), '', async () => {
      let first = true;
      while (chatPending.length) {  // what did not go stays, to be sent again
        const item = chatPending[0];
        let shrunk;
        try { shrunk = await chatShrink(item.file); } catch (error) { toast(error.message, true); throw error; }
        if (!first) await new Promise((resolve) => setTimeout(resolve, CHAT_SEND_GAP));
        first = false;
        await act('chat_image', { type: shrunk.blob.type, data: await chatBase64(shrunk.blob), w: shrunk.w, h: shrunk.h });
        URL.revokeObjectURL(item.url);
        chatPending.shift();
        chatDrawPending();
        S.chat.bottom = true;
      }
      if (text) {
        if (!first) await new Promise((resolve) => setTimeout(resolve, CHAT_SEND_GAP));
        await act('chat_send', { text });
        input.value = '';
        chatCount();
      }
    });
    S.chat.bottom = true;
  } catch { /* shown */ } finally {
    S.busy.delete('chat');
    render();
    input.focus();
  }
}
$('chat-text').addEventListener('keydown', (event) => {
  // Enter sends; not while an input method is still composing (Chinese is typed that way).
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
    event.preventDefault();
    chatSend();
  }
});
$('chat-text').addEventListener('input', chatCount);

// ---------------------------------------------------------------- navigation and actions

function show(page) {
  if (MINI) page = 'notices';
  S.page = page;
  // The steps are read again each time it is opened (renderPhone asks); the QR codes read are kept.
  if (page === 'phone') S.phone = S.phone.role ? { role: S.phone.role, parts: S.phone.parts } : {};
  document.querySelectorAll('.nav-item').forEach((b) => b.classList.toggle('active', b.dataset.page === page));
  document.querySelectorAll('.page').forEach((p) => p.classList.toggle('active', p.id === 'page-' + page));
  $('page-title').textContent = PAGES[page][0];
  $('page-sub').textContent = PAGES[page][1];
  if (!MINI) history.replaceState(null, '', '#' + page);
  render();
  if (page === 'diagnostics') $('log').scrollTop = $('log').scrollHeight;
  if (page === 'chat') { S.chat.bottom = true; S.chat.key = ''; render(); $('chat-text').focus(); }
  if (PERSONAL.has(page)) loadPersonal(page);  // read again each time it is opened
}

const MESSAGES = {
  bark_test: '已发送测试推送，看看手机有没有收到', ntfy_bind: '已生成安卓订阅，用手机扫二维码', bark_unbind: '已解绑手机推送', remove_token: '已删除 Token',
  open_installer: '正在打开 Wireshark 安装程序，或在浏览器里下载',
};

document.addEventListener('click', async (event) => {
  const nav = event.target.closest('[data-page]');
  if (nav) return show(nav.dataset.page);
  const goto = event.target.closest('[data-goto]');
  if (goto) return show(goto.dataset.goto);
  if (event.target.closest('[data-chat-older]')) return chatOlder();
  const view = event.target.closest('[data-chat-view]');
  if (view) return chatViewer(view.dataset.chatView);
  const army = event.target.closest('tr[data-detail]');
  if (army) {
    const id = army.dataset.detail;
    if (!S.openRows.delete(id)) S.openRows.add(id);
    if (S.openRows.size > 200) S.openRows.delete(S.openRows.values().next().value);
    return renderNotices();
  }
  const chatUse = event.target.closest('[data-chat-use]');
  if (chatUse) {
    if (chatUse.classList.contains('active')) return;
    try { await act('chat_use', { index: Number(chatUse.dataset.chatUse) }); } catch { /* act said why */ }
    return;
  }
  if (personalClick(event)) return;
  if (await phoneClick(event)) return;
  const dismiss = event.target.closest('[data-dismiss]');
  if (dismiss) return closeBanner(dismiss.closest('.banner').dataset.id);
  const copy = event.target.closest('[data-copy-site]');
  if (copy) {
    try { await navigator.clipboard.writeText(S.data.tokens[Number(copy.dataset.copySite)].site); toast('链接已复制'); }
    catch { toast('复制失败，请点“打开”后在浏览器里复制地址', true); }
    return;
  }
  const button = event.target.closest('[data-action]');
  if (!button) return;
  if (button.dataset.confirm && !confirm(button.dataset.confirm)) return;
  const action = button.dataset.action;
  const extra = button.dataset.index !== undefined ? { index: Number(button.dataset.index) } : {};
  try {
    const result = await withBusy(button, '', () => act(action, extra));
    if (MESSAGES[action]) toast(MESSAGES[action]);
    if (action === 'join_public') toast((result && result.joined) || '已生成账号');
    if (action === 'remove_token' || action === 'join_public') S.accountsKey = '';
  } catch { /* shown */ }
});

document.addEventListener('submit', async (event) => {
  const form = event.target;
  event.preventDefault();
  if (form.id === 'chat-form') {
    chatSend();
  } else if (form.id === 'add-form') {
    const input = $('add-token');
    const button = form.querySelector('button');
    try {
      const result = await withBusy(button, '添加中', () => act('add_token', { token: input.value }));
      input.value = '';
      toast(result && result.joined ? result.joined : '已添加，正在检查这个 Token');
      S.accountsKey = '';
    } catch { /* shown */ }
  } else if (form.dataset.psearch !== undefined) {
    // A new name: the page from its start, the player chosen before no longer meant.
    const view = S.personal[S.page];
    if (!view) return;
    const { show } = view.query;
    view.query = { [form.dataset.psearch]: form.querySelector('input').value.trim(), page: 1, ...(show ? { show } : {}) };
    loadPersonal(S.page);
  } else if (form.dataset.allyForm !== undefined) {
    await allyAdd(form.querySelector('button[type="submit"]'), { address: form.querySelector('[data-ally-address]').value });
  } else if (form.dataset.barkForm !== undefined) {
    const index = Number(form.dataset.barkForm);
    const input = form.querySelector('input');
    const button = form.querySelector('button');
    try {
      await withBusy(button, '绑定中', () => act('bark_bind', { index, address: input.value }));
      toast('绑定成功，手机应该收到了一条测试通知');
      S.accountsKey = '';
    } catch { /* shown */ }
  }
});


$('filters').addEventListener('click', (event) => {
  const chip = event.target.closest('[data-filter]');
  if (!chip) return;
  S.filter = chip.dataset.filter;
  document.querySelectorAll('.chip').forEach((c) => c.classList.toggle('active', c === chip));
  renderNotices();
});
$('clear-notices').addEventListener('click', () => {
  S.hiddenBefore = S.n;
  store.set('hiddenBefore', S.hiddenBefore);
  render();
  renderNotices();
});
$('opt-sound').checked = store.get('sound', true);
$('opt-sound').addEventListener('change', (e) => { store.set('sound', e.target.checked); if (e.target.checked) beep(); });
const showDesktop = (on) => { $('opt-desktop').checked = on; $('test-desktop').classList.toggle('hidden', !on); };
showDesktop(store.get('desktop', false));
$('opt-desktop').addEventListener('change', (e) => {
  showDesktop(e.target.checked);
  store.set('desktop', e.target.checked);
});
$('test-desktop').addEventListener('click', () => act('banner_test').catch(() => {}));
$('open-mini').classList.toggle('hidden', MINI || !window.warroomWindow);  // only in the program's own window
$('open-mini').addEventListener('click', () => window.warroomWindow?.openMini());

document.addEventListener('change', async (event) => {
  const input = event.target.closest('[data-pref]');
  if (!input) return;
  const value = input.type === 'checkbox' ? input.checked : input.value;
  document.querySelectorAll('[data-pref]').forEach((node) => { node.disabled = true; });
  try {
    await act('set_notice_pref', { key: input.dataset.pref, value });
    toast('已保存，这台电脑上的所有账号都按这个设置');
  } catch { /* shown */ } finally {
    S.prefsKey = '';  // drawn again from what was saved (or what was there before)
    render();
  }
});

$('set-theme').addEventListener('change', (e) => { store.set('theme', e.target.value); setTheme(e.target.value); });
$('set-autostart').addEventListener('change', (e) => act('set_setting', { name: 'autostart', value: e.target.checked }).catch(() => {}));
$('set-keep').addEventListener('change', (e) => act('set_setting', { name: 'keep_running', value: e.target.checked }).catch(() => {}));
$('pick-dumpcap').addEventListener('click', (e) => withBusy(e.currentTarget, '等待选择', async () => {
  const path = await act('pick_dumpcap');
  if (path) { toast('已选择 ' + path); if (S.data && S.data.capture.state !== 'running') act('start', {}, { quiet: true }).catch(() => {}); }
}).catch(() => {}));
$('forget-dumpcap').addEventListener('click', () => act('forget_dumpcap').catch(() => {}));
$('install-wireshark').addEventListener('click', () => act('open_installer').then(() => toast(MESSAGES.open_installer)).catch(() => {}));
$('open-folder').addEventListener('click', () => act('open_folder').catch(() => {}));
$('check-update').addEventListener('click', (e) => withBusy(e.currentTarget, '检查中', async () => {
  const result = await act('check_update');
  toast(result.newer ? `有新版本 ${result.latest}` : `已经是最新版本（${S.data.app_version}）`);
}).catch(() => {}));
$('send-diag').addEventListener('click', (e) => withBusy(e.currentTarget, '发送中', async () => {
  const sent = await act('send_diagnostics');
  toast(`已发送给管理员（编号 ${sent.id}）`);
}).catch(() => {}));
$('copy-diag').addEventListener('click', (e) => withBusy(e.currentTarget, '收集中', async () => {
  const text = await act('diagnostics');
  try { await navigator.clipboard.writeText(text); toast('诊断信息已复制，粘贴发给管理员即可'); }
  catch { toast('复制失败，请在日志里手动选择复制', true); }
}).catch(() => {}));

document.querySelectorAll('[data-icon]').forEach((node) => { node.innerHTML = icon(node.dataset.icon); });
// Notices are what the window is for (2026-10-14 user): it opens on them; what needs doing
// elsewhere (an account to fix, the capture stopped) is a banner above them with a way there.
window.addEventListener('hashchange', () => {  // a top-of-screen banner was clicked
  const page = location.hash.slice(1);
  if (PAGES[page] && page !== S.page) show(page);
});
const first = location.hash.slice(1) || 'notices';
show(PAGES[first] ? first : 'notices');
refresh();
connect();
