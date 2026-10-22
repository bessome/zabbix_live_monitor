const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/modem-reboot.js', 'utf8');

async function scenario({confirm = true, failure = false, method = 'snmp'} = {}) {
  let click;
  const calls = [], timers = [];
  const button = {dataset: {rebootMethod: method}, disabled: false, addEventListener: (_, fn) => { click = fn; }};
  const alternative = {dataset: {rebootMethod: method === 'snmp' ? 'cmts' : 'snmp'}, addEventListener() {}};
  const message = {textContent: ''};
  const root = {
    dataset: {deviceLabel: 'Modem', csrfToken: 'csrf-test', modemRebootUrl: '/api/modem-reboot'},
    querySelector: () => message, querySelectorAll: () => [button, alternative]
  };
  vm.runInNewContext(source, {
    document: {querySelectorAll: () => [root]},
    confirm: () => confirm, FormData, AbortController, location: {},
    setTimeout: (fn, ms) => { timers.push({fn, ms}); return timers.length; },
    clearTimeout: () => {},
    fetch: async (url, options) => {
      calls.push({url, options});
      if (failure) throw new Error('network');
      return {ok: true, status: 200, json: async () => ({outcome: 'unknown', message: 'unknown-result'})};
    }
  });
  await click();
  return {click, calls, timers, button, alternative, message};
}

(async () => {
  const cancelled = await scenario({confirm: false});
  assert.equal(cancelled.calls.length, 0);
  const sent = await scenario();
  assert.equal(sent.calls.length, 1);
  assert.equal(sent.calls[0].options.method, 'POST');
  assert.equal(sent.calls[0].options.body.get('csrf_token'), 'csrf-test');
  assert.equal(sent.calls[0].options.body.get('method'), 'snmp');
  assert.equal(sent.alternative.disabled, true);
  assert.equal(sent.message.textContent, 'unknown-result');
  assert.equal(sent.button.disabled, true);
  await sent.click();
  assert.equal(sent.calls.length, 1);
  sent.timers.find(timer => timer.ms === 30000).fn();
  assert.equal(sent.button.disabled, false);
  const failed = await scenario({failure: true});
  assert.match(failed.message.textContent, /неизвестен/);
  assert.equal(failed.calls.length, 1);
  const cmts = await scenario({method: 'cmts'});
  assert.equal(cmts.calls[0].options.body.get('method'), 'cmts');
  assert.equal(cmts.calls.length, 1);
  console.log('Modem reboot UI checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
