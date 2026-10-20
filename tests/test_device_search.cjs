// Run with: node --test tests/test_device_search.cjs
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');

const template = readFileSync(require('node:path').join(__dirname, '../templates/devices.html'), 'utf8');
const script = template.match(/<script>([\s\S]*?)<\/script>/)[1];

function loadPage(storage, category = 'VOIP', user = '1', reply = null) {
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
  const intervals = [];
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
    URLSearchParams, Intl, AbortController, setTimeout: () => 1, clearTimeout() {},
    setInterval(handler, ms) { intervals.push({handler, ms}); },
    fetch: async url => {
      requests.push(url);
      if (reply && /\/(modem-channels|optical-power|ping)$/.test(url)) return reply(url);
      const params = new URL(url, 'https://example.test').searchParams;
      return {ok: true, json: async () => ({devices: [], total: 500,
        page: Number(params.get('page')), pages: 20,
        per_page: Number(params.get('per_page')), updated_at: 1700000000})};
    }
  });
  vm.runInContext(script.replaceAll('{{ current_user.id }}', user), context);
  return {context, element, requests, intervals};
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

test('monitoring caps selection at five, polls levels and ping every five seconds and restores selection', async () => {
  const storage = new Map();
  const reply = async url => ({ok: true, json: async () => url.endsWith('/modem-channels')
    ? {items: [{id: '10', label: 'US1 Level', value: '40', units: 'dBmV'}]}
    : {sent: 2, current_ms: 3}});
  const page = loadPage(storage, 'Modems', '1', reply);
  await flush();
  for (let id = 1; id <= 6; id++) {
    vm.runInContext(`selectMonitor('${id}', true)`, page.context);
    await flush();
  }
  assert.equal(vm.runInContext('monitoredIds.size', page.context), 5);
  assert.equal(vm.runInContext('monitoredIds.has("6")', page.context), false);
  assert.equal(page.element('list-monitor-clear').hidden, false);
  const interval = page.intervals.find(item => item.ms === 5000);
  assert.ok(interval);
  page.requests.length = 0;
  interval.handler();
  await flush();
  assert.equal(page.requests.filter(url => url.endsWith('/ping')).length, 5);
  assert.equal(page.requests.filter(url => url.endsWith('/modem-channels')).length, 5);
  assert.equal(page.requests.some(url => url.includes('/summaries?')), false);
  assert.equal(vm.runInContext('liveValues.get("1").summary.modem_levels.us.value', page.context), '40');
  assert.equal(vm.runInContext('liveValues.get("1").availability', page.context), 'available');
  const restored = loadPage(storage, 'Modems', '1', reply);
  await flush();
  assert.equal(vm.runInContext('monitoredIds.size', restored.context), 5);
  page.element('list-monitor-clear').listeners.click();
  await flush();
  page.requests.length = 0;
  interval.handler();
  await flush();
  assert.equal(page.requests.length, 0);
});

test('lost ping turns red and poll errors discard stale values', async () => {
  let fail = false;
  const page = loadPage(new Map(), 'Modems', '1', async url => {
    if (fail) throw new Error('Network failed');
    return {ok: true, json: async () => url.endsWith('/modem-channels')
      ? {items: [{id: '10', label: 'US1 Level', value: '40'}]}
      : {sent: 1, current_ms: null}};
  });
  await flush();
  vm.runInContext('selectMonitor("1", true)', page.context);
  await flush();
  assert.equal(vm.runInContext('liveValues.get("1").availability', page.context), 'unavailable');
  fail = true;
  await vm.runInContext('pollMonitored()', page.context);
  assert.equal(vm.runInContext('liveValues.get("1").availability', page.context), 'unknown');
  assert.equal(vm.runInContext('liveValues.get("1").summary', page.context), null);
  assert.equal(vm.runInContext('liveValues.get("1").pingUpdated', page.context), false);
});

test('SNMP summary selects lowest channel and keeps missing values unavailable', async () => {
  const page = loadPage(new Map(), 'Modems');
  await flush();
  const summary = vm.runInContext(`summaryFromSnmp({items: [
    {label: 'US2 Level', value: '42'}, {label: 'US1 Level', value: null},
    {label: 'DS10 602 MHz Level', value: '2'}, {label: 'DS2 400 MHz Level', value: '-3'},
    {label: 'DS1 396 MHz SNR', value: '35'}
  ]})`, page.context);
  assert.equal(summary.modem_levels.us.value, null);
  assert.equal(summary.modem_levels.ds.value, '-3');
  const amplifier = loadPage(new Map(), 'TV_Amplifires', '1', async url => ({
    ok: true, json: async () => url.endsWith('/optical-power')
      ? {items: [{id: '1', label: 'Optical input power', value: '-4.2', units: 'dBm'}]}
      : {sent: 1, current_ms: 1}
  }));
  await flush();
  vm.runInContext('selectMonitor("1", true)', amplifier.context);
  await flush();
  assert.equal(vm.runInContext('liveValues.get("1").summary.optical_level.value', amplifier.context), '-4.2');
});

test('state indicator pulses only after a successful poll', async () => {
  const page = loadPage(new Map());
  await flush();
  vm.runInContext(`
    monitoredIds.add('1');
    let painted;
    let pulsed = false;
    availabilityIndicator = state => ({state, classList: {add() { pulsed = true; }}});
    const testRow = {dataset: {hostId: '1'}, querySelector() {
      return {replaceWith(indicator) { painted = indicator; }};
    }};
    liveValues.set('1', {availability: 'available', at: Date.now(), pingUpdated: true});
    paintLiveRow(testRow, true);
  `, page.context);
  assert.equal(vm.runInContext('painted.state', page.context), 'available');
  assert.equal(vm.runInContext('pulsed', page.context), true);
  vm.runInContext(`pulsed = false; liveValues.set('1', {availability: 'unknown', at: Date.now(), pingUpdated: false}); paintLiveRow(testRow, true)`, page.context);
  assert.equal(vm.runInContext('painted.state', page.context), 'unknown');
  assert.equal(vm.runInContext('pulsed', page.context), false);
});

test('missing, empty and invalid SNMP values render as n/a without units', async () => {
  for (const category of ['Modems', 'TV_Amplifires']) {
    const page = loadPage(new Map(), category);
    await flush();
    vm.runInContext(`
      document.createElement = () => ({classList: {add() {}}});
      const description = {
        childNodes: [], classList: {add() {}}, removeAttribute() {},
        replaceChildren() { this.childNodes = []; }, append(text) { this.childNodes.push(text); },
        appendChild(child) { this.childNodes.push(child); }
      };
    `, page.context);
    for (const value of [null, '', 'NaN']) {
      page.context.emptyValue = value;
      vm.runInContext(`
        var metric = {value: emptyValue, units: 'dBmV'};
        updateDeviceSummary(description, {}, {modem_levels: {us: metric, ds: metric}, optical_level: metric});
      `, page.context);
      const rendered = vm.runInContext(category === 'Modems'
        ? 'description.childNodes.map(child => typeof child === "string" ? child : child.textContent).join("")'
        : 'description.textContent', page.context);
      assert.equal(rendered, category === 'Modems' ? 'US: n/a · DS: n/a' : 'Optical input power: n/a');
    }
  }
});
