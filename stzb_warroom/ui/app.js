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
};
const icon = (name) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${ICONS[name] || ''}</svg>`;
const $ = (id) => document.getElementById(id);
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

const PAGES = {
  overview: ['概览', '上报游戏数据，接收战局通知'],
  notices: ['通知', '服务器根据你的战局生成的通知'],
  teams: ['我的队伍', '你每套配置的战绩，打得过谁、打不过谁'],
  battles: ['对局记录', '你的部队上场的每一场战报'],
  rivals: ['对手分析', '和你交过手的敌方阵容，你每支队伍打它们的结果'],
  prefs: ['通知设置', '要哪些通知、过滤哪些敌军、推送哪些到手机'],
  accounts: ['账号与推送', 'Token、角色与 iPhone 推送（Bark）'],
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
const LOGIN_STEPS = '请在本程序点「开始上报」，再进入游戏；如果已经在游戏里，退出后重新登录一次';

const S = {
  data: null, tapped: {}, nextFlip: Infinity, page: 'notices', filter: 'all', notices: [], logs: [], n: 0, l: 0,
  hiddenBefore: store.get('hiddenBefore', 0), dismissed: new Set(store.get('dismissed', [])),
  seenNotices: store.get('seenNotices', 0), loaded: false, accountsKey: '', prefsKey: '', busy: new Set(),
  // The pages about the account's own armies, as the server gave them: {query, data, error, loading, key} each.
  personal: { teams: { query: {} }, battles: { query: {} }, rivals: { query: {} } },
  personalRole: store.get('personalRole', null),
};
const PERSONAL = new Set(['teams', 'battles', 'rivals']);

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
    const response = await fetch(`/api/state?n=${S.n}&l=${S.l}`);
    if (!response.ok) throw new Error(response.status);
    const data = await response.json();
    for (const notice of data.notices) {
      if (!live(notice)) continue;
      S.notices.push(notice);
      if (notice.tapped) S.tapped[notice.role] = notice.tapped;
      if (S.loaded) announce(notice);
    }
    S.notices = S.notices.filter(live);
    if (S.notices.length > 500) S.notices.splice(0, S.notices.length - 500);
    if (data.notices.length) S.n = data.notices[data.notices.length - 1].seq;
    if (data.logs.length) { S.logs.push(...data.logs); S.l = data.logs[data.logs.length - 1].seq; appendLogs(data.logs); }
    if (S.logs.length > 1000) S.logs.splice(0, S.logs.length - 1000);
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
  if (notice.level !== 'alarm') return;
  if (store.get('sound', true)) beep();
  if (store.get('desktop', false) && 'Notification' in window && Notification.permission === 'granted' && document.hidden) {
    const who = roleOf(notice);
    const shown = new Notification(`【${notice.tag || '通知'}】${who ? who + ' · ' : ''}${notice.title || ''}`, { body: notice.body || '', tag: `n${notice.seq}` });
    if (typeof notice.expires === 'number') setTimeout(() => shown.close(), Math.max(0, notice.expires * 1000 - Date.now()));
  }
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
    case 'running': return { cls: '', icon: 'wave', title: '正在上报', desc: `已运行 ${duration(Date.now() / 1000 - c.since)}。玩游戏时保持程序开着，可以最小化这个窗口。`, dot: 'run', side: '上报中' };
    case 'starting': return { cls: 'wait', icon: 'clock', title: '正在启动…', desc: '正在启动网络组件。', dot: 'wait', side: '启动中' };
    case 'stopping': return { cls: 'wait', icon: 'clock', title: '正在停止…', desc: '正在把没上传完的数据保存好。', dot: 'wait', side: '停止中' };
    case 'error': return { cls: 'err', icon: 'alert', title: '上报已停止', desc: c.hint || c.error || '', dot: 'err', side: '出错' };
    default: return { cls: 'idle', icon: 'pause', title: '上报已暂停', desc: '点“开始上报”继续。暂停期间游戏数据不会上传。', dot: '', side: '已暂停' };
  }
}

function renderChrome(d) {
  const view = captureView(d);
  $('side-dot').className = 'dot ' + view.dot;
  $('side-state').textContent = view.side;
  $('side-detail').textContent = `版本 ${d.app_version}`;
  const toggle = $('toggle');
  const state = d.capture.state;
  const running = state === 'running' || state === 'starting';
  if (!S.busy.has('toggle')) {
    toggle.disabled = !d.tokens.length || state === 'stopping' || state === 'starting';
    toggle.className = 'btn ' + (running ? '' : 'primary');
    toggle.innerHTML = state === 'starting' || state === 'stopping' ? `<span class="spin"></span>${state === 'starting' ? '启动中' : '停止中'}`
      : running ? `${icon('pause').replace('<svg', '<svg width="14" height="14"')}暂停上报` : `${icon('play').replace('<svg', '<svg width="14" height="14"')}开始上报`;
  }
  const unseen = S.notices.filter((n) => n.seq > S.seenNotices).length;
  $('badge-notices').textContent = S.page === 'notices' || !unseen ? '' : unseen > 99 ? '99+' : unseen;
  const problems = d.tokens.filter((a) => a.state === 'invalid' || a.state === 'problem').length;
  $('badge-accounts').textContent = problems || '';
  document.title = d.capture.state === 'error' || problems ? '率土战局 · 需要处理' : '率土战局';
}

function banner(kind, iconName, text, actions = '', id = '') {
  return `<div class="banner ${kind}"${id ? ` data-id="${esc(id)}"` : ''}><i>${icon(iconName)}</i><div class="text">${text}</div>${actions}${id ? '<button class="close" data-dismiss="1" title="关闭">×</button>' : ''}</div>`;
}

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
    const needWireshark = (d.capture.error || '').includes('dumpcap');
    parts.push(banner('error', 'alert', `<b>上报已停止</b>：${esc(d.capture.hint || d.capture.error)}`,
      needWireshark ? '<button class="btn danger" data-goto="settings">去设置</button>' : '<button class="btn danger" data-action="start">重试</button>'));
  }
  for (const hint of d.hints) {
    if (!S.dismissed.has(hint.id)) parts.push(banner('warn', 'info', esc(hint.text), '', hint.id));
  }
  if (d.update) parts.push(updateBanner(d.update));
  // The first start of a new version: what it brought, until the player closes it.
  if (d.updated && !S.dismissed.has('updated-' + d.updated.version)) {
    parts.push(banner('info', 'check', `<b>已更新到 ${esc(d.updated.version)}</b>${releaseNotes(d.updated.notes, true)}`, '', 'updated-' + d.updated.version));
  }
  const html = parts.join('');
  if ($('banners').dataset.html !== html) { $('banners').innerHTML = html; $('banners').dataset.html = html; }
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

function updateBanner(u) {
  const v = `<b>${esc(u.version)}</b>`;
  const notes = releaseNotes(u.notes);
  const download = `<a class="btn" href="${esc(u.url)}" target="_blank" rel="noopener">下载</a>`;
  switch (u.state) {
    case 'downloading': return banner('info', 'clock', `正在下载新版本 ${v}…${notes}`);
    case 'ready': return banner('info', 'info', `新版本 ${v} 已下载，重启程序后生效。${notes}`, '<button class="btn primary" data-action="restart_for_update">立即重启</button>');
    case 'failed': return banner('warn', 'info', `新版本 ${v} 自动下载失败（${esc(u.error || '')}），稍后会再试，也可以下载完整安装包。${notes}`, download);
    default: return banner('info', 'info', `发现新版本 ${v}，请下载完整安装包，解压后替换原来的程序文件夹。${notes}`, download);
  }
}

function renderOverview(d) {
  const view = captureView(d);
  $('hero').className = 'hero ' + view.cls;
  $('hero-icon').innerHTML = icon(view.icon);
  $('hero-title').textContent = view.title;
  $('hero-desc').textContent = view.desc;
  const actions = view.cls === 'err' && (d.capture.error || '').includes('dumpcap')
    ? '<button class="btn primary" data-action="open_installer">安装 Wireshark</button><button class="btn" data-goto="settings">手动选择 dumpcap</button>'
    : !d.tokens.length ? '<button class="btn primary" data-goto="accounts">添加账号</button>' : '';
  if ($('hero-actions').dataset.html !== actions) { $('hero-actions').innerHTML = actions; $('hero-actions').dataset.html = actions; }
  const s = d.stats;
  $('m-uploaded').textContent = s.uploaded.toLocaleString();
  $('m-pending').textContent = s.pending == null ? '-' : s.pending.toLocaleString();
  $('m-last').textContent = s.uploaded_at ? ago(s.uploaded_at) : '-';
  $('m-last-at').textContent = s.uploaded_at ? clock(s.uploaded_at) : (d.capture.state === 'running' ? '登录游戏后开始上传' : '还没有上传');
  $('m-conn').textContent = s.connections;

  $('ov-accounts').innerHTML = d.tokens.length ? d.tokens.map((a) => {
    const [label, cls] = accountState(a);
    const bark = a.bark ? (a.bark.bound ? '<span class="tag">Bark 已绑定</span>' : '<span class="tag warn">Bark 未绑定</span>') : '';
    return `<div class="mini"><span class="main-text">${esc(a.role ? roleName(a.role.label) : a.name || a.token)}</span>${bark}<span class="state ${cls}">${label}</span></div>`;
  }).join('') : '<div class="empty">还没有账号</div>';

  const gone = withdrawnArmies();
  const recent = latestOfPlaces(S.notices.filter((n) => n.seq > S.hiddenBefore && !withdrawnAll(n, gone)))
    .filter((n) => !landedAll(n)).slice(-6).reverse();
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
// group's name, where its armies went). Any other notice is its text.
const nowSec = () => Date.now() / 1000;
function rowsLeft(t) {
  const at = t.countdown && Array.isArray(t.countdown.at) ? t.countdown.at : [];
  return ((t && t.rows) || []).map((row, r) => [row, r]).filter(([, r]) => !(typeof at[r] === 'number' && at[r] <= nowSec()));
}
const textWidth = (value) => [...String(value)].reduce((n, ch) => n + (ch.charCodeAt(0) > 0xff ? 2 : 1), 0);
function noticeTable(t) {
  const kept = rowsLeft(t);
  const rows = kept.map(([row]) => row);
  if (!rows.length) return '';
  // +3: a cell's padding, about three half-width characters
  const widths = t.columns.map((h, i) => 3 + Math.min(40, Math.max(4, textWidth(h), ...rows.map((row) => textWidth(row[i] ?? '')))));
  const given = Array.isArray(t.widths) && t.widths.length === t.columns.length && t.widths.every((w) => w > 0);
  const share = given ? t.widths : widths;
  const total = share.reduce((a, b) => a + b, 0);
  // The page's CSP (style-src 'self') drops a style attribute: colWidths sets them from data-w.
  const cols = share.map((w) => `<col data-w="${(w * 100 / total).toFixed(1)}">`).join('');
  const cd = t.countdown && Array.isArray(t.countdown.at) ? t.countdown : null;
  const cellOf = (c, i, r) => {
    const at = cd && i === cd.col ? cd.at[r] : null;
    if (typeof at === 'number') {
      return `<td class="cd" data-at="${at}"${cd.draw && cd.draw[r] ? ' data-draw="1"' : ''} title="${esc(c)}">${esc(c)}</td>`;
    }
    return `<td title="${esc(c)}"${i === t.key ? ' class="key"' : ''}>${esc(c)}</td>`;
  };
  return `<div class="vtable"><table class="fit"><colgroup>${cols}</colgroup><thead><tr>${t.columns.map((h) => `<th>${esc(h)}</th>`).join('')}</tr></thead>
    <tbody>${kept.map(([row, r]) => `<tr>${row.map((c, i) => cellOf(c, i, r)).join('')}</tr>`).join('')}</tbody></table></div>`;
}
// The columns' widths noticeTable gives as data-w, set through the DOM, which the CSP allows.
function colWidths(root) {
  for (const col of root.querySelectorAll('col[data-w]')) col.style.width = `${col.dataset.w}%`;
}
// A notice of tables whose rows have all landed has nothing left to tell.
function landedAll(n) {
  const tables = (n.view?.blocks || []).filter((b) => b.table?.countdown);
  return tables.length > 0 && (n.view.blocks || []).every((b) => !b.table || !rowsLeft(b.table).length);
}
// When the next row lands, for the list to drop it then.
function nextRowLanding(n) {
  return Math.min(Infinity, ...(n.view?.blocks || []).flatMap((b) => (b.table?.countdown?.at || [])
    .filter((at) => typeof at === 'number' && at > nowSec())));
}
const countsDown = (n) => (n.view?.blocks || []).some((b) => b.table?.countdown);
function noticeBody(n) {
  const v = n.view;
  if (!v || !Array.isArray(v.blocks)) return n.body ? `<div class="body">${esc(n.body)}</div>` : '';
  const text = (t) => (t ? `<div class="line">${esc(t)}</div>` : '');
  const parts = [];
  let lines = [];
  for (const b of v.blocks) {
    if (!b.table) { lines.push(text(b.text)); continue; }
    const table = noticeTable(b.table);
    if (table || !(b.table.rows || []).length) parts.push(...lines, table);
    lines = [];
  }
  return `<div class="body view">${[...parts, ...lines].join('')}</div>`;
}

// A notice of raids still to come (`arrivals`, the server's, epoch seconds ascending) stays above
// the others, the soonest first, counting down to its next one; once they have all landed it goes
// back among the others by when it came. Armies a recall notice names (`withdrawn`) went back: they
// are not counted down to, and a notice of `withdrawn_drops` (one attack) all of whose armies did goes.
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
// player taps another (2026-10-14 user).
function latestOfPlaces(notices) {
  const latest = new Map();
  for (const n of notices) if (n.tapped) latest.set(`${n.role}:${n.tapped}`, n.seq);
  return notices.filter((n) => !n.tapped || latest.get(`${n.role}:${n.tapped}`) === n.seq);
}

function renderNotices() {
  const gone = withdrawnArmies();
  const list = latestOfPlaces(S.notices.filter((n) => n.seq > S.hiddenBefore && (S.filter === 'all' || n.level === S.filter)
    && !withdrawnAll(n, gone))).filter((n) => !landedAll(n)).reverse();
  const pinned = list.filter((n) => n.tapped && S.tapped[n.role] === n.tapped);
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
      ${noticeBody(n)}</article>`;
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
  if (S.notices.length) { S.seenNotices = S.notices[S.notices.length - 1].seq; store.set('seenNotices', S.seenNotices); }
}

function renderAccounts(d) {
  const key = JSON.stringify(d.tokens);
  if (key === S.accountsKey) return;
  S.accountsKey = key;
  const typed = {};
  document.querySelectorAll('[data-bark-input]').forEach((input) => { typed[input.dataset.barkInput] = input.value; });
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
    // The server gives the link of the account's website once the account is set up.
    const site = a.state !== 'invalid' && /^https:\/\//.test(a.site || '') ? `<div class="bark"><div class="label"><b>战局网站</b>网页</div>
      <div class="value"><small>这个账号所在的战局网站。链接请只发给自己人，拿到链接的人都能看。</small></div>
      <div class="row-actions"><a class="btn primary small" href="${esc(a.site)}" target="_blank" rel="noopener">打开</a>
      <button class="btn ghost small" data-copy-site="${index}" type="button">复制链接</button></div></div>` : '';
    let bark = '';
    if (a.state !== 'invalid') {
      if (a.bark && a.bark.bound) {
        bark = `<div class="bark"><div class="label"><b>iPhone 推送</b>Bark</div>
          <div class="value"><span class="state ok">已绑定</span> <span class="token-text">${esc(a.bark.address)}</span>
          ${a.bark.error ? `<small class="err">上一条推送失败：${esc(a.bark.error)}</small>` : '<small>敌袭等需要你下令的通知会推送到这台 iPhone。</small>'}</div>
          <div class="row-actions"><button class="btn ghost small" data-action="bark_test" data-index="${index}">发测试推送</button>
          <button class="btn ghost small" data-action="bark_unbind" data-index="${index}" data-confirm="解绑后这台 iPhone 不再收到推送，确定吗？">解绑</button></div></div>`;
      } else if (a.bark) {
        bark = `<div class="bark"><div class="label"><b>iPhone 推送</b><button class="link" data-guide="1" type="button">怎么获取？</button></div>
          <form class="inline-form" data-bark-form="${index}"><input type="text" data-bark-input="${index}" spellcheck="false" autocomplete="off" placeholder="粘贴 Bark 里的地址：https://api.day.app/…">
          <button class="btn primary small" type="submit">绑定</button></form></div>`;
      }
    }
    return `<div class="card account ${a.state === 'invalid' ? 'invalid' : ''}">
      <div class="account-head"><span class="avatar">${esc(title.slice(0, 1))}</span>
        <div class="who"><b>${esc(title)}</b><small>${esc(role || '还没有角色信息')} · ${a.name ? esc(a.name) + ' · ' : ''}<span class="token-text">${esc(a.token)}</span></small></div>
        <span class="state ${cls}">${label}</span>
        <button class="btn ghost small" data-action="remove_token" data-index="${index}" data-confirm="删除这个 Token？删除后这个账号的数据不再上传。">删除</button></div>
      ${problem}${site}${bark}</div>`;
  }).join('') || '<div class="card empty">还没有账号。在下面添加管理员发给你的 Token。</div>';
  document.querySelectorAll('[data-bark-input]').forEach((input) => { input.value = typed[input.dataset.barkInput] || ''; });
}

// The notice settings: the page as the server gives it (texts and all), the same for every account.
function renderPrefs(d) {
  const p = d.notice_prefs || {};
  const key = JSON.stringify([p, d.tokens.length]);
  if (key === S.prefsKey) return;
  S.prefsKey = key;
  if (!p.groups) {
    $('prefs').innerHTML = `<div class="card empty">${esc(!d.tokens.length ? '先到“账号与推送”添加账号，再来设置通知。' : p.error || '正在读取通知设置…')}</div>`;
    return;
  }
  const values = p.values || {};
  const item = (it) => {
    const head = `<div><b>${esc(it.label)}</b><p>${esc(it.help)}</p></div>`;
    if (it.type === 'switch') {
      return `<div class="setting">${head}<label class="switch"><input type="checkbox" data-pref="${esc(it.key)}"${values[it.key] ? ' checked' : ''}><span></span></label></div>`;
    }
    return `<div class="setting column">${head}<div class="options">${(it.options || []).map((o) => {
      const on = o.value === values[it.key];
      return `<label class="option${on ? ' active' : ''}"><input type="radio" name="pref-${esc(it.key)}" data-pref="${esc(it.key)}" value="${esc(o.value)}"${on ? ' checked' : ''}>
        <span><b>${esc(o.label)}</b><small>${esc(o.help)}</small></span></label>`;
    }).join('')}</div></div>`;
  };
  $('prefs').innerHTML = (p.note ? `<p class="muted prefs-note">${esc(p.note)}</p>` : '')
    + (p.error ? banner('warn', 'info', esc(p.error)) : '')
    + p.groups.map((g) => `<div class="card settings"><h3>${esc(g.title)}</h3>${g.items.map(item).join('')}</div>`).join('');
}

// ---------------------------------------------------------------- the account's own armies
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
  try {
    const data = await act('personal', { index: account.index, page, query: view.query }, { quiet: true });
    if (personalAccount()?.a.role.id !== role) return;  // another account was chosen meanwhile
    view.data = data; view.error = null; view.role = role;
  } catch (error) {
    view.error = error.message || String(error);
  } finally {
    view.loading = false;
    renderPersonal(page);
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
  const table = c.table && c.table.rows?.length ? `<div class="vtable"><table><thead><tr>${c.table.columns.map((h) => `<th>${esc(h)}</th>`).join('')}</tr></thead>
    <tbody>${c.table.rows.map((row) => `<tr>${row.map((v) => `<td>${cell(v)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>` : '';
  return `<div class="card vcard"><div class="vhead"><div class="vtitle"><h3>${esc(c.title)}</h3>${c.subtitle ? `<small>${esc(c.subtitle)}</small>` : ''}</div>
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
    box.innerHTML = `<div class="card empty">${(S.data?.tokens || []).length ? '还没有可以查看的账号：Token 审批通过、登录过一次游戏后就能看。' : '先到“账号与推送”添加账号。'}</div>`;
    return;
  }
  const d = view.data;
  const who = accounts.length > 1 ? `<div class="vfilter"><span>账号</span><div class="chips">${accounts.map(({ a }) =>
    `<button class="chip${a.role.id === account.a.role.id ? ' active' : ''}" data-prole="${esc(a.role.id)}">${esc(roleName(a.role.label))}</button>`).join('')}</div></div>` : '';
  const filters = (d?.filters || []).map((f) => `<div class="vfilter"><span>${esc(f.label)}</span><div class="chips">${f.options.map((o) =>
    `<button class="chip${o.value === f.value ? ' active' : ''}" data-pfilter="${esc(f.key)}" data-value="${esc(o.value)}">${esc(o.label)}</button>`).join('')}</div></div>`).join('');
  const refresh = `<button class="btn ghost small" data-prefresh="1"${view.loading ? ' disabled' : ''}>${view.loading ? '<span class="spin"></span>读取中' : `${icon('refresh').replace('<svg', '<svg width="14" height="14"')}刷新`}</button>`;
  const toolbar = `<div class="vtoolbar"><div class="vfilters">${who}${filters}</div>${refresh}</div>`;
  let body;
  if (view.error) body = banner('warn', 'info', esc(view.error));
  else if (!d) body = '<div class="card empty">正在读取…</div>';
  else body = (d.note ? `<p class="muted prefs-note">${esc(d.note)}</p>` : '')
    + (d.cards.length ? d.cards.map(viewCard).join('') : `<div class="card empty">${esc(d.empty || '没有内容')}</div>`)
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
    row(d.stats.connections > 0 ? true : null, '游戏连接', d.stats.connections > 0 ? `${d.stats.connections} 条` : '最近 5 分钟没有看到游戏连接（没在玩游戏时正常）'),
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

// ---------------------------------------------------------------- navigation and actions

function show(page) {
  S.page = page;
  document.querySelectorAll('.nav-item').forEach((b) => b.classList.toggle('active', b.dataset.page === page));
  document.querySelectorAll('.page').forEach((p) => p.classList.toggle('active', p.id === 'page-' + page));
  $('page-title').textContent = PAGES[page][0];
  $('page-sub').textContent = PAGES[page][1];
  history.replaceState(null, '', '#' + page);
  render();
  if (page === 'diagnostics') $('log').scrollTop = $('log').scrollHeight;
  if (PERSONAL.has(page)) loadPersonal(page);  // read again each time it is opened
}

const MESSAGES = {
  bark_test: '已发送测试推送，看看手机有没有收到', bark_unbind: '已解绑 Bark', remove_token: '已删除 Token',
  open_installer: '正在打开 Wireshark 安装程序或下载页',
};

document.addEventListener('click', async (event) => {
  const nav = event.target.closest('[data-page]');
  if (nav) return show(nav.dataset.page);
  const goto = event.target.closest('[data-goto]');
  if (goto) return show(goto.dataset.goto);
  if (personalClick(event)) return;
  if (event.target.closest('[data-guide]')) {
    const guide = document.querySelector('.guide');
    guide.open = true;
    return guide.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
  const dismiss = event.target.closest('[data-dismiss]');
  if (dismiss) {
    S.dismissed.add(dismiss.closest('.banner').dataset.id);
    store.set('dismissed', [...S.dismissed].slice(-50));
    return render();
  }
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
    await withBusy(button, '', () => act(action, extra));
    if (MESSAGES[action]) toast(MESSAGES[action]);
    if (action === 'remove_token') S.accountsKey = '';
  } catch { /* shown */ }
});

document.addEventListener('submit', async (event) => {
  const form = event.target;
  event.preventDefault();
  if (form.id === 'add-form') {
    const input = $('add-token');
    const button = form.querySelector('button');
    try {
      const result = await withBusy(button, '添加中', () => act('add_token', { token: input.value }));
      input.value = '';
      toast(result && result.joined ? result.joined : '已添加，正在检查这个 Token');
      S.accountsKey = '';
    } catch { /* shown */ }
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

$('toggle').addEventListener('click', async () => {
  const running = ['running', 'starting'].includes(S.data?.capture.state);
  S.busy.add('toggle');
  try { await withBusy($('toggle'), running ? '停止中' : '启动中', () => act(running ? 'stop' : 'start')); }
  catch { /* shown */ } finally { S.busy.delete('toggle'); render(); }
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
$('opt-desktop').checked = store.get('desktop', false) && 'Notification' in window && Notification.permission === 'granted';
$('opt-desktop').addEventListener('change', async (e) => {
  if (e.target.checked && 'Notification' in window && Notification.permission !== 'granted') {
    e.target.checked = (await Notification.requestPermission()) === 'granted';
    if (!e.target.checked) toast('没有得到弹窗权限', true);
  }
  store.set('desktop', e.target.checked);
});

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
$('copy-diag').addEventListener('click', (e) => withBusy(e.currentTarget, '收集中', async () => {
  const text = await act('diagnostics');
  try { await navigator.clipboard.writeText(text); toast('诊断信息已复制，粘贴发给管理员即可'); }
  catch { toast('复制失败，请在日志里手动选择复制', true); }
}).catch(() => {}));

document.querySelectorAll('[data-icon]').forEach((node) => { node.innerHTML = icon(node.dataset.icon); });
// Notices are what the window is for (2026-10-14 user): it opens on them; what needs doing
// elsewhere (an account to fix, the capture stopped) is a banner above them with a way there.
const first = location.hash.slice(1) || 'notices';
show(PAGES[first] ? first : 'notices');
refresh();
connect();
