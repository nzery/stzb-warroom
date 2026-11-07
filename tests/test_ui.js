// Run with node tests/test_ui.js; no browser or third-party packages needed.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const ui = path.join(__dirname, '../stzb_warroom/ui');
const html = fs.readFileSync(path.join(ui, 'index.html'), 'utf8');
const nodes = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map((match) => [match[1], {
  addEventListener() {}, classList: { add() {}, remove() {}, toggle() {} },
  attrs: {}, getAttribute(name) { return this.attrs[name] ?? null; }, setAttribute(name, value) { this.attrs[name] = value; },
}]));
assert.equal(nodes.has('quit'), false);
let resolveFetch;
let requests = 0;
const context = vm.createContext({
  document: { getElementById: (id) => nodes.get(id) || null, querySelectorAll: () => [], addEventListener() {} },
  window: { addEventListener() {} },
  localStorage: { getItem: () => null, setItem() {} },
  history: { replaceState() {} }, location: { hash: '' },
  setTimeout() { return 1; }, clearTimeout() {}, setInterval() {}, btoa,
  EventSource: class { addEventListener() {} },
  fetch: () => { requests++; return new Promise((resolve) => { resolveFetch = resolve; }); },
});

async function main() {
  // Execute every startup binding against the real HTML IDs (removed buttons must not break startup).
  vm.runInContext(fs.readFileSync(path.join(ui, 'app.js'), 'utf8'), context);
  assert.equal(requests, 1);
  // 设置 › 外观: light unless the dark colours were chosen; only the stylesheet changes.
  assert.equal(nodes.get('style').getAttribute('href'), '/ui/app.css');
  vm.runInContext("setTheme('classic')", context);
  assert.equal(nodes.get('style').getAttribute('href'), '/ui/classic.css');
  vm.runInContext("setTheme('light')", context);
  assert.equal(nodes.get('style').getAttribute('href'), '/ui/app.css');
  vm.runInContext('render = () => {}; appendLogs = () => {}; announce = () => {};', context);
  await vm.runInContext('refresh()', context);
  await vm.runInContext('refresh()', context);
  assert.equal(requests, 1, 'polling and SSE must share the in-flight refresh');
  resolveFetch({ ok: true, json: async () => ({ notices: [{ seq: 1 }], logs: [] }) });
  await new Promise(setImmediate);
  assert.equal(vm.runInContext('S.notices.length', context), 1);
  assert.equal(vm.runInContext('S.n', context), 1);
  const failure = vm.runInContext('refresh()', context);
  resolveFetch({ ok: false, status: 503 });
  await failure;
  const recovery = vm.runInContext('refresh()', context);
  assert.equal(requests, 3, 'a failed refresh must not block later refreshes');
  resolveFetch({ ok: true, json: async () => ({ notices: [{ seq: 2 }], logs: [] }) });
  await recovery;
  assert.equal(vm.runInContext('S.notices.length', context), 2);
  // a notice past the time the server gave it is dropped, an old one already held as well
  vm.runInContext('S.notices[0].expires = Date.now() / 1000 - 1', context);
  const expiring = vm.runInContext('refresh()', context);
  resolveFetch({ ok: true, json: async () => ({ notices: [{ seq: 3, expires: Date.now() / 1000 + 600 },
    { seq: 4, expires: Date.now() / 1000 - 5 }], logs: [] }) });
  await expiring;
  assert.deepEqual(Array.from(vm.runInContext('S.notices.map((n) => n.seq)', context)), [2, 3]);
  // every update state has its banner; a downloaded one restarts, the others link the whole package
  for (const state of ['available', 'downloading', 'ready', 'failed']) {
    const html = vm.runInContext(`updateBanner({ version: '9.0.0', url: 'https://site.test/x.zip', state: '${state}', error: 'e' })`, context);
    assert.equal(html.includes('restart_for_update'), state === 'ready', state);
    assert.equal(html.includes('https://site.test/x.zip'), state === 'available' || state === 'failed', state);
    assert.ok(vm.runInContext(`UPDATE_STATE['${state}']('9.0.0')`, context).includes('9.0.0'));
  }
  // a page about the account's own armies is drawn as the server gave it, its texts escaped
  vm.runInContext(`S.data = { tokens: [{ state: 'ok', role: { id: 7, label: '甲 · 某盟' } }, { state: 'ok', role: { id: 8, label: '乙' } }] };
    S.personal.players = { query: { name: '<张' }, role: 7, data: { note: '说明', search: { key: 'name', value: '<张', placeholder: '输入玩家名字' },
      filters: [{ key: 'player', label: '玩家', value: '张三',
      options: [{ value: '张三', label: '张三（敌盟）' }, { value: 'npc', label: '张三丰（我方）' }] }],
      cards: [{ title: '<曹操队>', subtitle: null, tags: [{ text: '可以出征', level: 'good' }], stats: [{ label: '交换比', value: '1.50', level: 'good' }],
        lines: [{ text: '打不过：孙权', level: 'bad' }], table: { columns: ['时间', '结果'], rows: [['10-13 09:00', { text: '胜', level: 'good' }]] } }],
      empty: null, page: { page: 2, pages: 3, total: 50 } } };
    renderPersonal('players');`, context);
  const drawn = nodes.get('personal-players').innerHTML;
  assert.ok(drawn.includes('&lt;曹操队&gt;') && !drawn.includes('<曹操队>'), 'texts are escaped');
  for (const part of ['data-prole="8"', 'data-pfilter="player" data-value="npc"', 'data-ppage="3"', 'lv good', '打不过：孙权', '第 2 / 3 页',
    'data-psearch="name"', 'value="&lt;张"', 'placeholder="输入玩家名字"']) {
    assert.ok(drawn.includes(part), part);
  }
  // 甲 then 乙 asked quickly: 甲 answering last (or failing) does not put 甲 back, nor end 乙's loading
  vm.runInContext(`S.personalRole = 7; S.personal.players = { query: {} }; pending = [];
    act = (name, args) => new Promise((resolve, reject) => pending.push({ args, resolve, reject }));`, context);
  for (const name of ['甲', '乙']) vm.runInContext(`S.personal.players.query = { name: '${name}' }; loadPersonal('players');`, context);
  const answer = (i, how, value) => vm.runInContext('pending', context)[i][how](value);
  answer(1, 'resolve', { cards: [], empty: '乙' });
  await new Promise(setImmediate);
  answer(0, 'resolve', { cards: [], empty: '甲' });
  await new Promise(setImmediate);
  assert.equal(vm.runInContext('S.personal.players.data.empty', context), '乙');
  vm.runInContext(`loadPersonal('players'); loadPersonal('players');`, context);
  answer(2, 'reject', new Error('旧的失败'));
  await new Promise(setImmediate);
  assert.equal(vm.runInContext('S.personal.players.error ?? null', context), null);
  assert.equal(vm.runInContext('S.personal.players.loading', context), true, 'the newest is still loading');
  answer(3, 'resolve', { cards: [], empty: '乙' });
  await new Promise(setImmediate);
  assert.equal(vm.runInContext('S.personal.players.loading', context), false);
  // the phone page: pick the phone, then the server's steps one at a time; QR codes only as images, texts escaped
  vm.runInContext(`S.page = 'phone'; S.phoneOs = null; S.phoneStep = {}; S.phoneSeen = null; S.data.tokens.forEach((t) => { t.bark = { bound: false }; });
    act = async () => ({ status: null });
    S.data.phone = { enabled: true, running: true, waiting: false, exits: [{ relay: 'relay.example', state: 'online', since: 1, pipes: 0 }] };
    S.phone = { role: 7, data: { problem: null, note: '', steps: ['扫码'], rule: 'DST-PORT,8001,率土战局',
      nodes: [{ name: '率土战局', host: 'relay.example', link: 'vless://k@relay.example:443?type=ws#x',
        qr: '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>' }],
      rules: { link: 'https://api.example/v1/phone/r.conf', qr: '<svg xmlns="http://www.w3.org/2000/svg"></svg>', steps: [] },
      android: { sub: 'https://api.example/v1/phone/sub?k=<x>', qr: '<svg xmlns="http://www.w3.org/2000/svg"></svg>', steps: [] },
      guide: { iphone: [{ title: '<通知>', text: 'Bark', show: 'push', check: 'push' }, { title: '装', text: '装好', show: null, check: null },
                        { title: '扫节点', text: '首页', show: 'node', check: null }, { title: '扫规则', text: '配置页', show: 'rules', check: null },
                        { title: '打开连接', text: '测速', show: 'exit', check: 'connect' }],
               android: [{ title: '通知', text: 'ntfy', show: 'push', check: 'push' }, { title: '扫订阅', text: 'v2rayNG', show: 'sub', check: null },
                         { title: '启动', text: '进游戏', show: 'exit', check: 'connect' }] } } };
    renderPhone(S.data);`, context);
  let phone = nodes.get('phone-box').innerHTML;
  assert.ok(phone.includes('data-phone-os="iphone"') && phone.includes('data-phone-os="android"') && !phone.includes('<img'), phone);
  assert.strictEqual(nodes.get('phone-status').innerHTML, '', 'nothing at the top while all is well');
  const at = (step) => { vm.runInContext(`S.phoneStep = { [S.phoneOs]: ${step} }; renderPhone(S.data);`, context); return nodes.get('phone-box').innerHTML; };
  vm.runInContext(`S.phoneOs = 'iphone';`, context);
  phone = at(0);
  assert.ok(phone.includes('第 1 / 5 步') && phone.includes('&lt;通知&gt;') && phone.includes('data-bark-form') && phone.includes('还没绑定'), phone);
  assert.ok(phone.includes('data-phone-step="1"') && phone.includes('disabled>上一步'), 'the first step: next only');
  phone = at(1);
  assert.ok(phone.includes('phone-step-body single') && !phone.includes('<img'), 'a step without anything to show');
  phone = at(2);
  assert.ok(phone.includes('① 节点') && !phone.includes('② 规则') && (phone.match(/<img class="qr"/g) || []).length === 1, phone);
  assert.ok(phone.includes('src="data:image/svg+xml;base64,') && !phone.includes('<svg xmlns') && !phone.includes('<script>'), 'the server\'s SVG is never inlined');
  assert.ok(phone.includes('data-copy-text="vless://k@relay.example:443?type=ws#x"') && phone.includes('data-phone-reset'));
  phone = at(3);
  assert.ok(phone.includes('② 规则') && !phone.includes('① 节点'), phone);
  // the connect step's check: what the relays saw of the phone
  const check = (seen, pipes = 0) => {
    vm.runInContext(`S.phoneSeen = ${seen === undefined ? 'null' : JSON.stringify({ role: 7, status: seen })}; S.data.phone.exits[0].pipes = ${pipes};`, context);
    return at(4);
  };
  phone = at(4);
  assert.ok(phone.includes('第 5 / 5 步') && !phone.includes('下一步'), 'the last step: no next');
  assert.ok(check({ checked: null, check_ok: false, game: null, misrouted: 0 }).includes('还没看到手机连上来'));
  assert.ok(check({ checked: 30, check_ok: true, game: null, misrouted: 0 }).includes('✓ 手机已连上：连通性测试通过'));
  assert.ok(check({ checked: 30, check_ok: false, game: null, misrouted: 0 }).includes('这台电脑没连上中转'));
  assert.ok(check({ checked: 30, check_ok: true, game: null, misrouted: 9 }).includes('规则没生效'), 'misrouted wins over a passed test');
  assert.ok(check({ checked: null, check_ok: false, game: 40, misrouted: 0 }).includes('游戏连接经这台电脑出去了'));
  assert.ok(check(null, 2).includes('正在经这台电脑转发 2 条'));
  vm.runInContext(`S.phoneOs = 'android';`, context);
  phone = at(1);
  assert.ok(phone.includes('data-copy-text="https://api.example/v1/phone/sub?k=&lt;x&gt;"') && !phone.includes('① 节点'), phone);
  phone = at(9);
  assert.ok(phone.includes('第 3 / 3 步'), 'a step past the end is the last');
  // a problem of the exits shows at the top
  vm.runInContext(`S.data.phone.exits[0].state = 'busy'; renderPhone(S.data);`, context);
  assert.ok(nodes.get('phone-status').innerHTML.includes('另一台电脑正在用'));
  vm.runInContext(`S.data.phone = { running: false, waiting: false, exits: [] }; renderPhone(S.data);`, context);
  assert.ok(nodes.get('phone-status').innerHTML.includes('上报没有运行'));
  vm.runInContext(`S.data.phone = { running: true, waiting: false, exits: [] }; S.phone.key = null; S.page = 'overview'; watchPhone(false);`, context);
  vm.runInContext(`S.phone = { role: 7, data: { problem: '还没审核通过', nodes: [], relays: [] } }; renderPhone(S.data);`, context);
  assert.ok(nodes.get('phone-box').innerHTML.includes('还没审核通过'));
  // a read that set out before a reset and comes back after it does not put back the revoked QR code
  {
    const replies = [];
    vm.runInContext(`personalAccount = () => ({ index: 0, a: { role: { id: 7 } } });
      toast = () => {};`, context);
    context.act = (name, extra) => new Promise((resolve) => replies.push({ resolve, reset: extra.reset }));
    vm.runInContext(`S.phone = {};`, context);
    const read = vm.runInContext('loadPhone()', context);
    assert.ok(nodes.get('phone-box').innerHTML.includes('data-phone-refresh="1" disabled'));
    const reset = vm.runInContext('loadPhone(true)', context);
    const fresh = { problem: null, note: '', steps: [], rule: 'r', nodes: [{ link: 'new', qr: '' }] };
    replies[1].resolve(fresh);
    await reset;
    replies[0].resolve({ problem: null, note: '', steps: [], rule: 'r', nodes: [{ link: 'old', qr: '' }] });
    await read;
    assert.strictEqual(vm.runInContext('S.phone.data.nodes[0].link', context), 'new');
    assert.strictEqual(vm.runInContext('S.phone.loading', context), false);
  }
  // enemy armies are a table, a row an army, each row on one line; texts escaped; another notice is its body
  assert.ok(vm.runInContext(`noticeBody({ body: '<b>' })`, context).includes('&lt;b&gt;'));
  assert.ok(vm.runInContext(`noticeBody({ body: '旧格式', view: { head: 'x' } })`, context).includes('旧格式'));
  const place = vm.runInContext(`noticeBody({ body: '全文', view: { blocks: [{ text: '1. 甲 驻守' },
    { table: { columns: ['阵容', '玩家', '战法'], rows: [['曹操', '1', '<x>']], key: 0 } }, { text: '2. 乙' }] } })`, context);
  assert.ok(place.includes('class="key">曹操</td>') && !place.includes('class="key">1<'), 'the key column stands out');
  for (const part of ['1. 甲 驻守', '>曹操</td>', '&lt;x&gt;', '2. 乙']) assert.ok(place.includes(part), part);
  assert.ok(!place.includes('全文'));
  assert.ok(place.includes('<td title="&lt;x&gt;">') && place.includes('<colgroup>'), 'one line, the whole text on hover');
  const widths = vm.runInContext(`noticeTable({ columns: ['玩家', '阵容'], rows: [['甲', '庞统 红4、程昱 红1、刘备 红5']] })`, context)
    .match(/data-w="([\d.]+)"/g).map((w) => parseFloat(w.slice(8)));
  assert.ok(widths[1] > widths[0] && Math.abs(widths[0] + widths[1] - 100) < 0.5, String(widths));
  // the server's widths, the same for every notice so that their tables line up
  const fixed = vm.runInContext(`noticeTable({ columns: ['玩家', '阵容'], widths: [3, 1], rows: [['甲', '庞统/程昱/刘备']] })`, context)
    .match(/data-w="([\d.]+)"/g).map((w) => parseFloat(w.slice(8)));
  assert.deepEqual(fixed, [75, 25]);
  // when each army lands counts down in its row, not a line of its own
  const later = Math.floor(Date.now() / 1000) + 600;
  const rows = vm.runInContext(`noticeTable({ columns: ['到达', '阵容'], key: 1,
    countdown: { col: 0, at: [${later}, null], draw: [true, false] }, rows: [['21:33:02', '甲'], ['已驻守', '乙']] })`, context);
  assert.ok(rows.includes(`<td class="cd" data-at="${later}" data-draw="1" title="21:33:02">`), rows);
  assert.ok(rows.includes('<td title="已驻守">已驻守</td>'), 'an army staying does not count down');
  assert.equal(vm.runInContext(`short(Date.now() / 1000 + 65, '1')`, context), '打平 1分05秒');
  assert.equal(vm.runInContext(`short(Date.now() / 1000 - 1)`, context), '已到');
  // a row whose army landed goes; a table left with none goes with its group's lines; then the notice
  const soon = Math.floor(Date.now() / 1000) + 600;
  const landed = vm.runInContext(`({ view: { blocks: [{ text: '来袭 2 支' },
    { table: { columns: ['到达', '阵容'], countdown: { col: 0, at: [1, 2] }, rows: [['a', '甲'], ['b', '乙']] } },
    { text: '驻守 1 支' }, { table: { columns: ['到达', '阵容'], countdown: { col: 0, at: [null] }, rows: [['已驻守', '丙']] } }] } })`, context);
  context.landed = landed;
  const drawn2 = vm.runInContext('noticeBody(landed)', context);
  assert.ok(!drawn2.includes('来袭 2 支') && !drawn2.includes('甲') && drawn2.includes('驻守 1 支') && drawn2.includes('丙'), drawn2);
  assert.equal(vm.runInContext('landedAll(landed)', context), false);  // one stays
  const partly = vm.runInContext(`noticeTable({ columns: ['到达', '阵容'], countdown: { col: 0, at: [1, ${soon}] }, rows: [['a', '甲'], ['b', '乙']] })`, context);
  assert.ok(!partly.includes('甲') && partly.includes(`data-at="${soon}"`), partly);
  assert.equal(vm.runInContext(`landedAll({ view: { blocks: [{ table: { columns: ['到达'], countdown: { col: 0, at: [1] }, rows: [['a']] } }] } })`, context), true);
  // a row whose army a recall named goes, and with all of them the notice (2026-10-16 user)
  const recalled = vm.runInContext(`noticeTable({ columns: ['到达', '阵容'], countdown: { col: 0, at: [${soon}, ${soon}] },
    armies: [7, 8], rows: [['a', '甲'], ['b', '乙']] }, new Set([7]))`, context);
  assert.ok(!recalled.includes('甲') && recalled.includes('乙'), recalled);
  assert.equal(vm.runInContext(`landedAll({ view: { blocks: [{ table: { columns: ['到达'], countdown: { col: 0, at: [${soon}] },
    armies: [7], rows: [['a']] } }] } }, new Set([7]))`, context), true);
  // the recall notice itself is not shown; what it names is still used
  vm.runInContext(`S.notices = [{ seq: 1, title: '撤回', hidden: true, withdrawn: [7] }]`, context);
  assert.deepEqual([...vm.runInContext('withdrawnArmies()', context)], [7]);
  // a tapped place's latest notice only
  const kept = vm.runInContext(`latestOfPlaces([{ seq: 1, role: 1, tapped: '9' }, { seq: 2, role: 1 },
    { seq: 3, role: 1, tapped: '9' }, { seq: 4, role: 2, tapped: '9' }]).map((n) => n.seq)`, context);
  assert.deepEqual([...kept], [2, 3, 4]);
  // the place tapped last stays above all, whichever account tapped it (2026-10-17 user)
  nodes.get('notice-list').dataset = {};
  vm.runInContext(`colWidths = () => {}; S.hiddenBefore = 0; S.filter = 'all'`, context);
  vm.runInContext(`S.tapped = { role: 8, place: '5' }; S.notices = [
    { seq: 1, role: 7, tapped: '9', level: 'info', title: '甲点开', view: { blocks: [] } },
    { seq: 2, role: 8, tapped: '5', level: 'info', title: '乙点开', view: { blocks: [] } }]; renderNotices();`, context);
  const html = vm.runInContext(`document.getElementById('notice-list').innerHTML`, context);
  assert.ok(html.indexOf('乙点开') < html.indexOf('其他通知') && html.indexOf('其他通知') < html.indexOf('甲点开'), html);
  console.log('UI startup and refresh regression checks passed');
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
