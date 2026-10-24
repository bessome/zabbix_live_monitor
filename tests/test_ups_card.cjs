const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const flush = () => new Promise(resolve => setImmediate(resolve));

function card() {
  function node(tag = '') {
    return {
      tag, dataset: {}, children: [], listeners: {}, textContent: '', open: false,
      classList: {values: new Set(), toggle(key, on) { on ? this.values.add(key) : this.values.delete(key); },
        remove(key) { this.values.delete(key); }},
      addEventListener(key, fn) { this.listeners[key] = fn; }, setAttribute() {},
      append(...children) { this.children.push(...children); },
      appendChild(child) { this.children.push(child); }, replaceChildren(...children) { this.children = children; },
      querySelectorAll(selector) {
        return this.children.flatMap(child => [
          ...(selector.startsWith('.') ? child.className === selector.slice(1) : child.tag === selector) ? [child] : [],
          ...child.querySelectorAll(selector)
        ]);
      },
      querySelector(selector) { return this.querySelectorAll(selector)[0]; },
      contains(child) { return this.querySelectorAll('.optical-row').includes(child); },
      closest() { return this; }, showModal() { this.open = true; }, close() { this.open = false; }
    };
  }
  const ids = new Map();
  for (const id of ['ups-load-panel', 'optical-monitor', 'optical-channels', 'optical-state',
    ...['ups', 'modem'].flatMap(prefix => ['dialog', 'canvas', 'status', 'stats', 'title', 'close']
      .map(part => prefix + '-history-' + part))]) ids.set(id, node());
  for (const id of ['ups-load-panel', 'optical-monitor']) ids.get(id).dataset = {category: 'UPSs', hostId: '42'};
  const plottedLabels = [];
  for (const prefix of ['ups', 'modem']) {
    const canvas = ids.get(prefix + '-history-canvas');
    canvas.clientWidth = 600; canvas.clientHeight = 220;
    canvas.getContext = () => new Proxy({}, {get: (_, key) => key === 'fillText'
      ? text => plottedLabels.push(text) : () => {}});
  }
  const listeners = {};
  const timers = [];
  const requests = [];
  let items = [];
  let unavailable = false;
  const periods = Object.fromEntries(['ups', 'modem'].map(prefix => [prefix,
    ['1h', '12h'].map(period => ({...node(), dataset: {period}}))]));
  const context = vm.createContext({
    document: {
      getElementById: id => ids.get(id) || null, createElement: node,
      documentElement: {dataset: {timezone: 'Europe/Tallinn', theme: 'light'}},
      querySelectorAll: selector => periods[selector.startsWith('#ups') ? 'ups' : 'modem'],
      addEventListener(key, fn) { listeners[key] = fn; },
      dispatchEvent(event) { listeners[event.type]?.(event); }
    },
    window: {addEventListener() {}, devicePixelRatio: 1},
    CustomEvent: class { constructor(type, options) { this.type = type; this.detail = options.detail; } },
    AbortController, Intl, Map, Set,
    setInterval(fn, ms) { const timer = {fn, ms}; timers.push(timer); return timer; }, clearInterval() {},
    fetch: async url => {
      requests.push(url);
      if (url.endsWith('/ups-levels')) return {ok: !unavailable, status: unavailable ? 503 : 200,
        json: async () => unavailable ? {error: 'unavailable', items: []} : {items}};
      return {ok: true, status: 200, json: async () => ({label: url.includes('/battery/') ? 'Battery Capacity' : 'Output Load L1',
        units: '%', period: '1h', from: 100, to: 3700,
        points: [{time: 100, value: 12.123456789}, {time: 3700, value: 13}]})};
    }
  });
  for (const file of ['metric-history.js', 'optical.js']) {
    vm.runInContext(readFileSync(path.join(__dirname, '../static', file), 'utf8'), context);
  }
  return {ids, requests, timers, periods, plottedLabels,
    setItems(value) { items = value; }, fail() { unavailable = true; }};
}

test('UPS card keeps load history inline and opens battery history separately', async () => {
  const page = card();
  page.setItems([
    {id: 'load', label: 'Output Load L1', value: '12.123456789', units: '%', state: 'unrated'},
    {id: 'battery', label: 'Battery Capacity', value: '49.999999', units: '%', state: 'critical'},
    {id: 'autonomy', label: 'Autonomy', value: '600', units: 's', state: 'unrated'}
  ]);
  await flush();
  await page.timers.find(timer => timer.ms === 5000).fn();
  await flush();
  assert.ok(page.requests.includes('/api/devices/UPSs/42/ups-levels/load/history?period=1h'));
  assert.equal(page.ids.get('ups-history-title').textContent, 'Output Load L1');
  assert.equal(page.ids.get('modem-history-dialog').open, false);
  assert.ok(page.plottedLabels.includes('%'));
  const rows = page.ids.get('optical-channels').children;
  assert.equal(rows[0].querySelector('.modem-number').textContent, '12.12');
  assert.equal(rows[1].querySelector('.modem-number').textContent, '50');
  assert.ok(rows[1].querySelector('strong').classList.values.has('optical-value-alert'));
  assert.equal(rows[2].querySelector('.modem-units').textContent, ' s');
  page.ids.get('optical-channels').listeners.click({target: rows[1]});
  await flush();
  assert.equal(page.ids.get('modem-history-dialog').open, true);
  assert.equal(page.ids.get('ups-history-title').textContent, 'Output Load L1');
  assert.ok(page.requests.includes('/api/devices/UPSs/42/ups-levels/battery/history?period=1h'));
  page.periods.ups[1].listeners.click();
  await flush();
  assert.ok(page.requests.includes('/api/devices/UPSs/42/ups-levels/load/history?period=12h'));
  const count = page.requests.filter(url => url.includes('/load/history')).length;
  await page.timers.find(timer => timer.ms === 5000).fn();
  assert.equal(page.requests.filter(url => url.includes('/load/history')).length, count);
  page.fail();
  await page.timers.find(timer => timer.ms === 5000).fn();
  assert.ok(page.ids.get('optical-channels').children.every(row => row.querySelector('.modem-number').textContent === 'n/a'));
  assert.equal(page.ids.get('ups-history-stats').children.length, 0);
});
