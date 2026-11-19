const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function chart(width = 1300, timezone = 'Europe/Tallinn') {
  const labels = [];
  const nodes = new Map();
  const context2d = new Proxy({}, {get: (target, key) => {
    if (key === 'measureText') return text => ({width: text.length * 6.6});
    if (key === 'fillText') return (text, x, y) => labels.push({text, x, y});
    return () => {};
  }});
  const node = () => ({dataset: {}, style: {}, children: [], textContent: '',
    addEventListener() {}, setAttribute() {}, replaceChildren() {},
    append(...items) {this.children.push(...items);},
    appendChild(item) {this.children.push(item);}});
  for (const name of ['modem-monitor','victor-open','victor-dialog','victor-close',
    'victor-canvas','victor-status','victor-legend']) nodes.set(name, node());
  const canvas = nodes.get('victor-canvas');
  canvas.clientWidth = width;
  canvas.clientHeight = 400;
  canvas.getContext = () => context2d;
  const sandbox = vm.createContext({
    document: {getElementById: key => nodes.get(key), querySelectorAll: () => [],
      documentElement: {dataset: {timezone}}, createElement: node},
    window: {devicePixelRatio: 1, addEventListener() {}}, Intl,
  });
  const code = readFileSync(path.join(__dirname, '../static/victor.js'), 'utf8')
    .replace(/\}\)\(\);\s*$/, 'globalThis.chart = {timeTicks, format, draw, show, setPeriod(p) {period = p;}};})();');
  vm.runInContext(code, sandbox);
  return {api: sandbox.chart, labels, nodes};
}

function data(period, span) {
  const to = Date.parse('2026-11-19T06:55:00Z') / 1000;
  return {period, from: to - span, to, series: [{key: 'us', units: 'dBmV',
    label: 'US', points: [{time: to - span, value: 45.123456}, {time: to, value: 46}]}]};
}

test('24h scale labels use full even hours and are denser than five labels', () => {
  const c = chart();
  const d = data('24h', 86400);
  c.api.setPeriod('24h');
  c.api.draw(d);
  const labels = c.labels.filter(l => /^\d\d:\d\d$/.test(l.text));
  assert.equal(labels.length, 12);
  assert.ok(labels.every(l => l.text.endsWith(':00')));
  assert.ok(labels.every(l => Number(l.text.slice(0,2)) % 2 === 0));
  assert.ok(c.labels.some(l => l.text === '19.11'));
  assert.equal(d.series[0].points[0].value, 45.123456);
});

test('mobile time labels fit the canvas without overlapping', () => {
  const c = chart(320);
  c.api.setPeriod('24h');
  c.api.draw(data('24h', 86400));
  const labels = c.labels.filter(l => /^\d\d:\d\d$/.test(l.text));
  assert.equal(labels.length, 4);
  for (let i = 0; i < labels.length; i++) {
    assert.ok(labels[i].x - 16.5 >= 43);
    assert.ok(labels[i].x + 16.5 <= 308);
    if (i) assert.ok(labels[i].x - labels[i-1].x > 40);
  }
});

test('one hour labels are aligned to five minutes, not the rolling range start', () => {
  const c = chart();
  c.api.draw(data('1h', 3600));
  const labels = c.labels.filter(l => /^\d\d:\d\d$/.test(l.text));
  assert.ok(labels.length >= 12);
  assert.ok(labels.every(l => Number(l.text.slice(3)) % 5 === 0));
});

test('local daily boundaries follow Tallinn DST instead of a fixed UTC offset', () => {
  const c = chart();
  const ticks = [...c.api.timeTicks(Date.parse('2026-10-24T00:00Z') / 1000,
    Date.parse('2026-10-27T00:00Z') / 1000, 86400)];
  const local = new Intl.DateTimeFormat('en-GB', {timeZone: 'Europe/Tallinn',
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23'});
  assert.equal(ticks.length, 3);
  assert.ok(ticks.every(t => local.format(new Date(t * 1000)) === '00:00'));
  assert.equal(ticks[1] - ticks[0], 25 * 3600);
});

test('long range labels are calendar dates; statistics retain display formatting', () => {
  const c = chart();
  c.api.setPeriod('14d');
  c.api.show(data('14d', 14 * 86400));
  assert.equal(c.labels.filter(l => /^\d\d\.\d\d$/.test(l.text)).length, 14);
  assert.ok(c.nodes.get('victor-status').textContent.includes('—'));
  assert.equal(c.api.format(12), '12');
  assert.equal(c.api.format(-19.400000000000002), '-19.4');
  assert.equal(c.api.format(-0.000001), '0');
  assert.equal(c.api.format(NaN), '—');
});
