const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/modem-reboot.js', 'utf8');

async function scenario({confirm = true, failure = false} = {}) {
  let click;
  const calls = [], timers = [];
  const button = {disabled: false, addEventListener: (_, fn) => { click = fn; }};
  const message = {textContent: ''};
  const root = {
    dataset: {deviceLabel: 'Modem', csrfToken: 'csrf-test', modemRebootUrl: '/api/modem-reboot'},
    querySelector: selector => selector === 'button' ? button : message
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
  return {click, calls, timers, button, message};
}

(async () => {
  const cancelled = await scenario({confirm: false});
  assert.equal(cancelled.calls.length, 0);
  const sent = await scenario();
  assert.equal(sent.calls.length, 1);
  assert.equal(sent.calls[0].options.method, 'POST');
  assert.equal(sent.calls[0].options.body.get('csrf_token'), 'csrf-test');
  assert.equal(sent.message.textContent, 'unknown-result');
  assert.equal(sent.button.disabled, true);
  await sent.click();
  assert.equal(sent.calls.length, 1);
  sent.timers.find(timer => timer.ms === 30000).fn();
  assert.equal(sent.button.disabled, false);
  const failed = await scenario({failure: true});
  assert.match(failed.message.textContent, /неизвестен/);
  assert.equal(failed.calls.length, 1);
  console.log('Modem reboot UI checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
