// Run with: node --test tests/test_device_search.cjs
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');

const template = readFileSync(require('node:path').join(__dirname, '../templates/devices.html'), 'utf8');
const script = template.match(/<script>([\s\S]*?)<\/script>/)[1];

function loadPage(storage, category = 'VOIP', user = '1') {
  const elements = new Map();
  function element(id) {
    if (!elements.has(id)) elements.set(id, {
      value: id === 'page-size' ? '50' : '', dataset: {category},
      listeners: {}, addEventListener(name, handler) { this.listeners[name] = handler; },
      replaceChildren() {}, querySelectorAll() { return []; }
    });
    return elements.get(id);
  }
  const requests = [];
  const context = vm.createContext({
    document: {
      getElementById: element, querySelector: element, querySelectorAll: () => [],
      documentElement: {dataset: {timezone: 'Europe/Tallinn'}}
    },
    window: {addEventListener() {}, matchMedia: () => ({matches: false})},
    localStorage: {getItem: () => null},
    sessionStorage: {
      getItem: key => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, value)
    },
    URLSearchParams, Intl, setTimeout: () => 1, clearTimeout() {}, setInterval() {},
    fetch: async url => {
      requests.push(url);
      const params = new URL(url, 'https://example.test').searchParams;
      return {ok: true, json: async () => ({devices: [], total: 500,
        page: Number(params.get('page')), pages: 20,
        per_page: Number(params.get('per_page')), updated_at: 1700000000})};
    }
  });
  vm.runInContext(script.replaceAll('{{ current_user.id }}', user), context);
  return {context, element, requests};
}
const flush = () => new Promise(resolve => setImmediate(resolve));

test('returning from monitoring restores the query, page and page size', async () => {
  const storage = new Map();
  const original = loadPage(storage);
  await flush();
  original.element('search').value = 'Pärnu phone 42';
  original.element('search').listeners.input();
  original.element('page-size').value = '25';
  original.element('page-size').listeners.change();
  await flush();
  vm.runInContext('goTo(3)', original.context);
  await flush();

  const returned = loadPage(storage);
  await flush();
  const params = new URL(returned.requests[0], 'https://example.test').searchParams;
  assert.equal(returned.element('search').value, 'Pärnu phone 42');
  assert.equal(params.get('q'), 'Pärnu phone 42');
  assert.equal(params.get('page'), '3');
  assert.equal(params.get('per_page'), '25');
  assert.equal(returned.element('page-info').textContent, 'Страница 3 из 20');
  assert.equal(loadPage(storage, 'Modems').element('search').value, '');
  assert.equal(loadPage(storage, 'VOIP', '2').element('search').value, '');

  returned.element('search').value = '';
  returned.element('search').listeners.input();
  const cleared = loadPage(storage);
  assert.equal(cleared.element('search').value, '');
  assert.equal(new URL(cleared.requests[0], 'https://example.test').searchParams.get('page'), '1');
});

test('tree view and expanded groups survive navigation', async () => {
  const storage = new Map();
  const original = loadPage(storage);
  await flush();
  vm.runInContext('view = "tree"; openGroups.add("Pärnu/VOIP"); saveSearch()', original.context);
  const returned = loadPage(storage);
  await flush();
  assert.equal(new URL(returned.requests[0], 'https://example.test').searchParams.get('view'), 'tree');
  assert.equal(vm.runInContext('openGroups.has("Pärnu/VOIP")', returned.context), true);
  assert.equal(returned.element('device-tree').hidden, false);
});

test('invalid or unavailable session storage does not prevent loading devices', async () => {
  for (const value of ['{broken', '{"page":-2,"pageSize":"7","query":42,"view":"other"}']) {
    const page = loadPage(new Map([['zabbix-device-search:1:VOIP', value]]));
    await flush();
    assert.equal(page.element('search').value, '');
    const params = new URL(page.requests[0], 'https://example.test').searchParams;
    assert.equal(params.get('page'), '1');
    assert.equal(params.get('per_page'), '50');
    assert.equal(params.get('view'), 'list');
  }
  const page = loadPage({get() { throw new Error('Storage blocked'); }, set() { throw new Error('Storage blocked'); }});
  await flush();
  assert.equal(page.requests.length, 1);
});
