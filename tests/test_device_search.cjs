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
      if (reply && /\/(modem-channels|optical-power|edfa-levels|ping)$/.test(url)) return reply(url);
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
function installMetricRow(page) {
  vm.runInContext(`
    var metricRow = {dataset: {}, metricCells: new Map(metricColumns.map(column => [column.key, {
      cell: {dataset: {}}, value: {removeAttribute() {}}
    }]))};
  `, page.context);
}

test('amplifier sort applies to the whole API query and survives navigation', async () => {
  const storage = new Map();
  const page = loadPage(storage, 'TV_Amplifires');
  await flush();
  page.element('amplifier-sort').listeners.change({target: {value: 'level:desc'}});
  await flush();
  let params = new URL(page.requests.at(-1), 'https://example.test').searchParams;
  assert.equal(params.get('sort_by'), 'level');
  assert.equal(params.get('sort_dir'), 'desc');
  assert.equal(params.get('page'), '1');
  const restored = loadPage(storage, 'TV_Amplifires');
  params = new URL(restored.requests[0], 'https://example.test').searchParams;
  assert.equal(params.get('sort_by'), 'level');
  assert.equal(params.get('sort_dir'), 'desc');
});

test('amplifier level column rounds presentation and keeps missing values neutral', async () => {
  const page = loadPage(new Map(), 'TV_Amplifires');
  await flush();
  installMetricRow(page);
  for (const [value, text, state] of [[-2.90001, '-2.9 dBm', 'normal'],
    [-3.40001, '-3.4 dBm', 'critical'], [-0.00001, '0 dBm', 'normal'],
    [null, 'n/a', 'unknown'], ['', 'n/a', 'unknown'], ['Infinity', 'n/a', 'unknown']]) {
    page.context.testValue = value;
    vm.runInContext(`updateRowSummary(metricRow, {}, {optical_level: {value: testValue, units: 'dBm'}})`, page.context);
    assert.equal(vm.runInContext("metricRow.metricCells.get('optical').value.textContent", page.context), text);
    assert.equal(vm.runInContext('metricRow.dataset.levelState', page.context), state);
  }
});

test('tree sorting changes only the branch whose header was clicked', async () => {
  const storage = new Map();
  const page = loadPage(storage, 'TV_Amplifires');
  await flush();
  const requestCount = page.requests.length;
  vm.runInContext(`
    const created = [];
    function treeElement(tag) {
      const node = {
        tag, dataset: {}, children: [], attributes: {}, listeners: {},
        get rows() { return this.children; },
        classList: {add() {}},
        addEventListener(event, handler) { this.listeners[event] = handler; },
        appendChild(child) { child.parent = this; this.children.push(child); },
        append(...children) { children.forEach(child => this.appendChild(child)); },
        replaceChildren(...children) { this.children = []; this.append(...children); },
        setAttribute(key, value) { this.attributes[key] = value; },
        closest(tag) { return this.parent?.tag === tag ? this.parent : this.parent?.closest(tag); }
      };
      created.push(node);
      return node;
    }
    document.createElement = treeElement;
    document.createDocumentFragment = () => treeElement('fragment');
    document.querySelectorAll = selector => selector === '[data-amplifier-sort]'
      ? created.filter(node => node.dataset.amplifierSort) : [];
    deviceRow = device => ({dataset: {hostId: device.id}});
    for (const [id, value] of [['1', -1], ['2', -5], ['3', -2], ['4', -8]]) {
      summaryCache.set(id, {at: Date.now(), summary: {optical_level: {value, units: 'dBm'}}});
    }
    view = 'tree';
    openGroups.add('City/Street');
    renderTree([
      {id: '2', name: 'B', groups: ['City/Street']}, {id: '1', name: 'A', groups: ['City/Street']},
      {id: '4', name: 'D', groups: ['City/Other']}, {id: '3', name: 'C', groups: ['City/Other']}
    ]);
    var levelButton = created.find(node => node.dataset.amplifierSort === 'level' && node.sortBranch === 'City/Street');
    var otherButton = created.find(node => node.dataset.amplifierSort === 'level' && node.sortBranch === 'City/Other');
    var selectedBody = levelButton.closest('table').children.find(node => node.tag === 'tbody');
    var otherBody = otherButton.closest('table').children.find(node => node.tag === 'tbody');
  `, page.context);
  vm.runInContext('levelButton.listeners.click()', page.context);
  await flush();
  assert.equal(vm.runInContext("selectedBody.children.map(node => node.dataset.hostId).join(',')", page.context), '2,1');
  assert.equal(vm.runInContext("otherBody.children.map(node => node.dataset.hostId).join(',')", page.context), '3,4');
  assert.equal(vm.runInContext("levelButton.closest('th').attributes['aria-sort']", page.context), 'ascending');
  vm.runInContext('levelButton.listeners.click()', page.context);
  await flush();
  assert.equal(vm.runInContext("selectedBody.children.map(node => node.dataset.hostId).join(',')", page.context), '1,2');
  assert.equal(vm.runInContext("otherBody.children.map(node => node.dataset.hostId).join(',')", page.context), '3,4');
  assert.equal(vm.runInContext("branchSorts.has('City/Other')", page.context), false);
  assert.equal(vm.runInContext('amplifierSort', page.context), 'name:asc');
  assert.equal(vm.runInContext("openGroups.has('City/Street')", page.context), true);
  assert.equal(page.requests.length, requestCount);
  const saved = JSON.parse(storage.get('zabbix-device-search:1:TV_Amplifires'));
  assert.deepEqual(saved.branchSorts, [['City/Street', 'level:desc']]);
});

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

test('monitoring caps selection at five, polls ping each second and SNMP every five seconds', async () => {
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
  page.intervals.find(item => item.ms === 1000).handler();
  await flush();
  assert.equal(page.requests.filter(url => url.endsWith('/ping')).length, 5);
  assert.equal(page.requests.filter(url => url.endsWith('/modem-channels')).length, 5);
  assert.equal(page.requests.some(url => url.includes('/summaries?')), false);
  assert.equal(vm.runInContext('liveValues.get("1").summary.modem_levels.us.value', page.context), '40');
  assert.equal(vm.runInContext('liveValues.get("1").availability', page.context), 'available');
  const restored = loadPage(storage, 'Modems', '1', reply);
  await flush();
  assert.equal(vm.runInContext('monitoredIds.size', restored.context), 0);
  assert.equal(restored.requests.some(url => url.endsWith('/ping') || url.endsWith('/modem-channels')), false);
  page.element('list-monitor-clear').listeners.click();
  await flush();
  page.requests.length = 0;
  interval.handler();
  await flush();
  assert.equal(page.requests.length, 0);
});

test('new or cleared search preserves selected devices and pending polls', async () => {
  for (const query of ['', 'new modem']) {
    const storage = new Map();
    const page = loadPage(storage, 'Modems');
    await flush();
    vm.runInContext(`
      const pendingReplies = [];
      fetch = () => new Promise(resolve => pendingReplies.push(resolve));
      selectMonitor('1', true);
      var activeSignal = monitorRequests.ping.controller.signal;
      var metricSignal = monitorRequests.metrics.controller.signal;
    `, page.context);
    page.element('search').value = query;
    page.element('search').listeners.input();
    assert.equal(vm.runInContext('monitoredIds.size', page.context), 1);
    assert.equal(vm.runInContext('activeSignal.aborted', page.context), false);
    assert.equal(vm.runInContext('metricSignal.aborted', page.context), false);
    assert.equal(page.element('list-monitor-clear').hidden, false);
    vm.runInContext(`pendingReplies.forEach(resolve => resolve({ok: true, json: async () => ({items: [], sent: 1, current_ms: 3})}))`, page.context);
    await flush();
    assert.equal(vm.runInContext('liveValues.size', page.context), 1);
    assert.equal(vm.runInContext('Object.values(monitorRequests).some(state => state.inFlight)', page.context), false);
    const saved = JSON.parse(storage.get('zabbix-device-search:1:Modems'));
    assert.equal(saved.query, query);
    assert.equal('monitoredIds' in saved, false);
  }
});

test('monitored-only filter requests selected IDs across searches and restores search when unchecked', async () => {
  const page = loadPage(new Map(), 'Modems', '1', async () => ({ok: true,
    json: async () => ({items: [], sent: 1, current_ms: 3})}));
  await flush();
  vm.runInContext('selectMonitor("1", true); selectMonitor("2", true)', page.context);
  await flush();
  page.element('search').value = 'unrelated device';
  page.element('search').listeners.input();
  assert.equal(vm.runInContext('monitoredIds.size', page.context), 2);
  page.element('monitored-only').checked = true;
  page.element('monitored-only').listeners.change();
  await flush();
  let params = new URL(page.requests.at(-1), 'https://example.test').searchParams;
  assert.equal(params.get('ids'), '1,2');
  assert.equal(params.get('q'), '');
  assert.equal(params.get('page'), '1');
  assert.equal(page.element('monitored-only-label').hidden, false);
  page.element('monitored-only').checked = false;
  page.element('monitored-only').listeners.change();
  await flush();
  params = new URL(page.requests.at(-1), 'https://example.test').searchParams;
  assert.equal(params.has('ids'), false);
  assert.equal(params.get('q'), 'unrelated device');
  page.element('monitored-only').checked = true;
  page.element('list-monitor-clear').listeners.click();
  await flush();
  assert.equal(page.element('monitored-only').checked, false);
  assert.equal(page.element('monitored-only-label').hidden, true);
});

test('slow SNMP does not delay ping updates or trigger extra SNMP requests', async () => {
  let finishSnmp;
  let pingMs = 3;
  const page = loadPage(new Map(), 'Modems', '1', url => {
    if (url.endsWith('/modem-channels')) return new Promise(resolve => { finishSnmp = resolve; });
    return Promise.resolve({ok: true, json: async () => ({sent: 1, current_ms: pingMs})});
  });
  await flush();
  vm.runInContext('selectMonitor("1", true)', page.context);
  await flush();
  assert.equal(vm.runInContext('liveValues.get("1").availability', page.context), 'available');
  assert.equal(vm.runInContext('"summary" in liveValues.get("1")', page.context), false);
  pingMs = null;
  await page.intervals.find(item => item.ms === 1000).handler();
  assert.equal(vm.runInContext('liveValues.get("1").availability', page.context), 'unavailable');
  assert.equal(page.requests.filter(url => url.endsWith('/modem-channels')).length, 1);
  assert.equal(page.requests.filter(url => url.endsWith('/ping')).length, 2);
  finishSnmp({ok: true, json: async () => ({items: [{label: 'US1 Level', value: '40'}]})});
  await flush();
  assert.equal(vm.runInContext('liveValues.get("1").summary.modem_levels.us.value', page.context), '40');
  assert.equal(vm.runInContext('liveValues.get("1").availability', page.context), 'unavailable');
});

test('initial ping snapshot without completed probes preserves the existing state', async () => {
  let snapshot = {sent: 0, current_ms: null};
  const page = loadPage(new Map(), 'VOIP', '1', async () => ({ok: true, json: async () => snapshot}));
  await flush();
  vm.runInContext(`
    let displayedState = 'available';
    availabilityIndicator = state => ({state, classList: {add() {}}});
    const testRow = {dataset: {hostId: '1'}, querySelector() {
      return {replaceWith(indicator) { displayedState = indicator.state; }};
    }};
    document.querySelectorAll = selector => selector === '#device-list tr[data-host-id]' ? [testRow] : [];
    selectMonitor('1', true);
  `, page.context);
  await flush();
  assert.equal(vm.runInContext('displayedState', page.context), 'available');
  assert.equal(vm.runInContext('liveValues.has("1")', page.context), false);
  snapshot = {sent: 1, current_ms: null};
  await vm.runInContext('pollMonitorKind("ping")', page.context);
  assert.equal(vm.runInContext('displayedState', page.context), 'unavailable');
  snapshot = {sent: 2, current_ms: 3};
  await vm.runInContext('pollMonitorKind("ping")', page.context);
  assert.equal(vm.runInContext('displayedState', page.context), 'available');
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

test('EDFA polling reads both Zabbix levels and clears them after an error', async () => {
  let fail = false;
  const page = loadPage(new Map(), 'EDFA', '1', async url => {
    if (fail) throw new Error('Unavailable');
    return {ok: true, json: async () => url.endsWith('/edfa-levels') ? {items: [
      {id: '2', label: 'Optical input level', value: '-5.123456', units: 'dBm'},
      {id: '1', label: 'Optical output level', value: '19.123456', units: 'dBm'}
    ]} : {sent: 1, current_ms: 1}};
  });
  await flush();
  vm.runInContext('selectMonitor("1", true)', page.context);
  await flush();
  assert.ok(page.requests.some(url => url.endsWith('/1/edfa-levels')));
  assert.equal(vm.runInContext('liveValues.get("1").summary.edfa_levels.output.value', page.context), '19.123456');
  assert.equal(vm.runInContext('liveValues.get("1").summary.edfa_levels.input.value', page.context), '-5.123456');
  fail = true;
  await vm.runInContext('pollMonitorKind("metrics")', page.context);
  assert.equal(vm.runInContext('liveValues.get("1").summary', page.context), null);
});

test('selecting a device keeps existing status and parameters until the first poll finishes', async () => {
  for (const category of ['Modems', 'TV_Amplifires', 'VOIP']) {
    const page = loadPage(new Map(), category);
    await flush();
    vm.runInContext(`
      const unchangedRow = {dataset: {hostId: '1'}, querySelector() {
        throw new Error('Row must stay unchanged before the first poll response');
      }};
      document.querySelectorAll = selector => selector === '#device-list tr[data-host-id]' ? [unchangedRow] : [];
      fetch = () => new Promise(() => {});
      selectMonitor('1', true);
      paintLiveRow(unchangedRow);
    `, page.context);
    assert.equal(vm.runInContext('monitoredIds.has("1")', page.context), true);
    assert.equal(vm.runInContext('liveValues.has("1")', page.context), false);
  }
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

test('parameters blink on each successful SNMP reply, independently of ping and alert styling', async () => {
  const page = loadPage(new Map(), 'Modems');
  await flush();
  vm.runInContext(`
    monitoredIds.add('1');
    let flashes = 0;
    let renders = 0;
    updateRowSummary = () => { renders++; };
    const value = {animate() { flashes++; return {cancel() {}}; }};
    const testRow = {dataset: {hostId: '1'}, metricCells: new Map([['us', {value}], ['ds', {value}]])};
    liveValues.set('1', {summary: {modem_levels: {us: {value: '40'}}}, metricsUpdated: true});
    paintLiveRow(testRow, false, true);
    paintLiveRow(testRow);
  `, page.context);
  assert.equal(vm.runInContext('flashes', page.context), 2);
  assert.equal(vm.runInContext('renders', page.context), 1);
  vm.runInContext(`
    liveValues.set('1', {summary: {modem_levels: {us: {value: '40'}}}, metricsUpdated: true});
    paintLiveRow(testRow, false, true);
  `, page.context);
  assert.equal(vm.runInContext('flashes', page.context), 4);
  vm.runInContext(`
    liveValues.set('1', {summary: null, metricsUpdated: false});
    paintLiveRow(testRow, false, true);
  `, page.context);
  assert.equal(vm.runInContext('flashes', page.context), 4);
});

test('missing, empty and invalid SNMP values render as n/a without units', async () => {
  for (const category of ['Modems', 'TV_Amplifires']) {
    const page = loadPage(new Map(), category);
    await flush();
    installMetricRow(page);
    for (const value of [null, '', 'NaN']) {
      page.context.emptyValue = value;
      vm.runInContext(`
        var metric = {value: emptyValue, units: 'dBmV'};
        updateRowSummary(metricRow, {}, {modem_levels: {us: metric, ds: metric}, optical_level: metric});
      `, page.context);
      for (const text of vm.runInContext('[...metricRow.metricCells.values()].map(entry => entry.value.textContent)', page.context)) {
        assert.equal(text, 'n/a');
      }
      assert.equal(vm.runInContext('metricRow.dataset.levelState', page.context), 'unknown');
    }
  }
});

test('only all abnormal numeric columns make the entire row abnormal', async () => {
  const page = loadPage(new Map(), 'Modems');
  await flush();
  installMetricRow(page);
  for (const [us, ds, rowState, usState, dsState] of [
    [40, 20, 'mixed', 'normal', 'critical'], [60, 20, 'critical', 'critical', 'critical'],
    [null, 20, 'mixed', 'unknown', 'critical'], [40, 0, 'normal', 'normal', 'normal'],
    [null, null, 'unknown', 'unknown', 'unknown']]) {
    page.context.testSummary = {modem_levels: {us: {value: us}, ds: {value: ds}}};
    vm.runInContext('updateRowSummary(metricRow, {}, testSummary)', page.context);
    assert.equal(vm.runInContext('metricRow.dataset.levelState', page.context), rowState);
    assert.equal(vm.runInContext("metricRow.metricCells.get('us').cell.dataset.metricState", page.context), usState);
    assert.equal(vm.runInContext("metricRow.metricCells.get('ds').cell.dataset.metricState", page.context), dsState);
  }
  page.context.testSummary = {modem_levels: {us: {value: 20, alert: true}, ds: {value: 0}}};
  vm.runInContext('updateRowSummary(metricRow, {}, testSummary)', page.context);
  assert.equal(vm.runInContext("metricRow.metricCells.get('us').cell.dataset.metricState", page.context), 'critical');
});
test('EDFA cells use server thresholds and whole rows require both abnormal readings', async () => {
  const page = loadPage(new Map(), 'EDFA');
  await flush();
  installMetricRow(page);
  for (const [output, input, expected] of [
    ['warning', 'normal', 'mixed'], ['critical', 'normal', 'mixed'],
    ['warning', 'critical', 'warning'], ['critical', 'critical', 'critical'],
    ['normal', 'normal', 'normal'], ['warning', 'unknown', 'mixed']
  ]) {
    page.context.summary = {edfa_levels: {
      output: {value: '20', state: output},
      input: {value: input === 'unknown' ? null : '-1', state: input}
    }};
    vm.runInContext('updateRowSummary(metricRow, {}, summary)', page.context);
    assert.equal(vm.runInContext('metricRow.dataset.levelState', page.context), expected);
    assert.equal(vm.runInContext("metricRow.metricCells.get('output').cell.dataset.metricState", page.context), output);
    assert.equal(vm.runInContext("metricRow.metricCells.get('input').cell.dataset.metricState", page.context), input);
  }
});

test('every list has separate value cells and the favorite action remains last', async () => {
  for (const category of ['Modems', 'TV_Amplifires', 'Switches', 'EDFA', 'VOIP', 'Routers']) {
    const page = loadPage(new Map(), category);
    await flush();
    vm.runInContext(`
      document.createElement = tag => ({
        tag, dataset: {}, children: [], classList: {add() {}},
        setAttribute() {}, getAttribute() { return ''; }, removeAttribute() {}, addEventListener() {},
        appendChild(child) { this.children.push(child); },
        append(...children) { this.children.push(...children); }
      });
      var renderedRow = deviceRow({id: '42', name: 'Device', display_name: 'Device - 192.0.2.42',
        address: '192.0.2.42', device_description: 'Description',
        modem_levels: {us: {value: '40', units: 'dBmV'}, ds: {value: '0', units: 'dBmV'}},
        optical_level: {value: '-2', units: 'dBm'},
        edfa_levels: {output: {value: '19.12345', units: 'dBm'}, input: {value: '-5', units: 'dBm'}}});
    `, page.context);
    assert.equal(vm.runInContext('renderedRow.children.length', page.context), ['Modems', 'EDFA'].includes(category) ? 4 : 3);
    assert.equal(vm.runInContext('renderedRow.children.at(-1).className', page.context), 'device-favorite-cell');
    assert.equal(vm.runInContext('renderedRow.children[0].children.length', page.context), 1);
    assert.equal(vm.runInContext('renderedRow.children[0].children[0].children.at(-1).textContent', page.context),
      category === 'TV_Amplifires' ? 'Device - 192.0.2.42 (Description)' : 'Device - 192.0.2.42');
    if (category === 'Modems') {
      assert.equal(vm.runInContext('renderedRow.children[1].dataset.label', page.context), 'US Level');
      assert.equal(vm.runInContext('renderedRow.children[2].dataset.label', page.context), 'DS Level');
      assert.equal(vm.runInContext('renderedRow.children[1].children[0].textContent', page.context), '40 dBmV');
    }
    if (category === 'EDFA') {
      assert.equal(vm.runInContext('renderedRow.children[1].dataset.label', page.context), 'Optical output level');
      assert.equal(vm.runInContext('renderedRow.children[2].dataset.mobileLabel', page.context), 'IN');
      assert.equal(vm.runInContext('renderedRow.children[1].children[0].textContent', page.context), '19.12 dBm');
      assert.equal(vm.runInContext('renderedRow.children[2].dataset.metricState', page.context), 'unrated');
      assert.equal(vm.runInContext('renderedRow.dataset.levelState', page.context), 'unknown');
    }
  }
});

test('amplifier model names come from Device description and stay after live SNMP updates', async () => {
  const page = loadPage(new Map(), 'TV_Amplifires');
  await flush();
  installMetricRow(page);
  vm.runInContext("metricRow.nameLink = {}; var namedDevice = {name: 'Amp', display_name: 'Amp - 192.0.2.42'}", page.context);
  for (const [description, model] of [
    ['Lambda PRO 71 firmware 1.0', 'Vector L PRO 71'], ['SNMP ECM01 unit', 'Vector B PRO 51'],
    ['Firmware V4.7', 'OPR-2108G'], ['FW V6.2.3', 'ASCENT AON1110'],
    ['FW V6.2.4', 'ASCENT AON1110'], ['fw v6.2.4', 'ASCENT AON1110'],
    ['FW V5.2.4', 'ASCENT AON1110'], ['fw v5.2.4', 'ASCENT AON1110'],
    ['lambda  pro 71', 'Vector L PRO 71'], ['Other model', 'Other model'], ['', ''], [null, '']]) {
    page.context.sourceDescription = description;
    vm.runInContext('updateRowSummary(metricRow, namedDevice, {device_description: sourceDescription})', page.context);
    assert.equal(vm.runInContext('metricRow.nameLink.textContent', page.context),
      'Amp - 192.0.2.42' + (model ? ' (' + model + ')' : ''));
  }
  vm.runInContext(`
    updateRowSummary(metricRow, namedDevice, {device_description: 'ECM01'});
    updateRowSummary(metricRow, {id: '42'}, {optical_level: {value: '-2', units: 'dBm'}});
  `, page.context);
  assert.equal(vm.runInContext('metricRow.nameLink.textContent', page.context), 'Amp - 192.0.2.42 (Vector B PRO 51)');
});
