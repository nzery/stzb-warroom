// Run with node tests/test_ui.js; no browser or third-party packages needed.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const ui = path.join(__dirname, '../stzb_warroom/ui');
const html = fs.readFileSync(path.join(ui, 'index.html'), 'utf8');
const nodes = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map((match) => [match[1], {
  addEventListener() {}, classList: { add() {}, remove() {}, toggle() {} },
}]));
assert.equal(nodes.has('quit'), false);
let resolveFetch;
let requests = 0;
const context = vm.createContext({
  document: { getElementById: (id) => nodes.get(id) || null, querySelectorAll: () => [], addEventListener() {} },
  window: { addEventListener() {} },
  localStorage: { getItem: () => null, setItem() {} },
  history: { replaceState() {} }, location: { hash: '' },
  setTimeout() {}, setInterval() {},
  EventSource: class { addEventListener() {} },
  fetch: () => { requests++; return new Promise((resolve) => { resolveFetch = resolve; }); },
});

async function main() {
  // Execute every startup binding against the real HTML IDs (removed buttons must not break startup).
  vm.runInContext(fs.readFileSync(path.join(ui, 'app.js'), 'utf8'), context);
  assert.equal(requests, 1);
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
    S.personal.battles = { query: {}, role: 7, data: { note: '说明', filters: [{ key: 'kind', label: '对手', value: 'pvp',
      options: [{ value: 'pvp', label: '打玩家' }, { value: 'npc', label: '打 NPC' }] }],
      cards: [{ title: '<曹操队>', subtitle: null, tags: [{ text: '可以出征', level: 'good' }], stats: [{ label: '交换比', value: '1.50', level: 'good' }],
        lines: [{ text: '打不过：孙权', level: 'bad' }], table: { columns: ['时间', '结果'], rows: [['10-13 09:00', { text: '胜', level: 'good' }]] } }],
      empty: null, page: { page: 2, pages: 3, total: 50 } } };
    renderPersonal('battles');`, context);
  const drawn = nodes.get('personal-battles').innerHTML;
  assert.ok(drawn.includes('&lt;曹操队&gt;') && !drawn.includes('<曹操队>'), 'texts are escaped');
  for (const part of ['data-prole="8"', 'data-pfilter="kind" data-value="npc"', 'data-ppage="3"', 'lv good', '打不过：孙权', '第 2 / 3 页']) {
    assert.ok(drawn.includes(part), part);
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
  // a tapped place's latest notice only
  const kept = vm.runInContext(`latestOfPlaces([{ seq: 1, role: 1, tapped: '9' }, { seq: 2, role: 1 },
    { seq: 3, role: 1, tapped: '9' }, { seq: 4, role: 2, tapped: '9' }]).map((n) => n.seq)`, context);
  assert.deepEqual([...kept], [2, 3, 4]);
  console.log('UI startup and refresh regression checks passed');
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
