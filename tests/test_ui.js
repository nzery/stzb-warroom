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
  console.log('UI startup and refresh regression checks passed');
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
