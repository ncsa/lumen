// Runs the real detail.js against a fake DOM and a fetch whose responses the test resolves
// by hand, to check that only the newest load renders or reports an error.
// Run with `node --test tests/js/*.test.mjs` (tests/test_overview_js.py does this under pytest).
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { answer, fakeElement, flush, runPage } from './fake_dom.mjs';

function page() {
  const ids = {};
  const byId = id => (ids[id] ??= fakeElement());
  ids.detail = fakeElement({ dataset: { seriesUrl: '/monitor/api/series', overviewUrl: '/monitor/', model: 'org/qwen', refreshMs: '300000', range: '24h' } });
  ids['load-error'] = fakeElement();
  ids['load-error'].classList.add('d-none');
  const rangeButtons = ['24h', '1h'].map(range => fakeElement({ dataset: { range } }));
  const document = {
    getElementById: byId,
    createElement: () => fakeElement(),
    querySelectorAll: sel => (sel === '.range-btn' ? rangeButtons : []),
  };
  const { requests, timers } = runPage('detail.js', document);
  return { byId, requests, timers, rangeButtons };
}

function seriesBody(range, uptime) {
  const t = '2026-10-17T16:59:00Z';
  return {
    range, model: 'org/qwen', generated_at: '2026-10-17T17:00:00Z', daily: false,
    header: { status: 'ok', healthy: 1, configured: 1, uptime, peak_rpm: 3, description: 'Qwen', strip: ['ok'] },
    strip: [{ t, status: 'ok' }],
    prices: [{ day: '2026-10-17', input_price: 0.2, output_price: 0.8, input_median: 0.25, input_p5: 0.1, input_p95: 0.4, output_median: 1, output_p5: 0.5, output_p95: 2 }],
    traffic: { unit: 'minute', points: [{ t, requests: 2, tokens: null, estimated: false }], average: { requests: 2, tokens: null } },
    hardware: { points: [{ day: '2026-10-17', daily_cost: 24, hw_per_mtok: null, hw_per_request: 0.01, hw_partial: true }], range: { hw_per_mtok: null, hw_per_request: 0.01, hw_partial: true } },
    instances: [{ t, healthy: 1, configured: 1 }],
  };
}

test('requests the model series under the page prefix', () => {
  const p = page();
  assert.equal(p.requests.length, 1);
  assert.equal(p.requests[0].url, '/monitor/api/series?model=org%2Fqwen&range=24h');
});

test('a late response for an earlier range does not replace the selected range', async () => {
  const p = page();
  p.rangeButtons[1].click(); // select 1h while 24h is still loading
  const [old, latest] = p.requests;
  assert.match(latest.url, /range=1h/);
  assert.equal(p.byId('back-link').attributes.href, '/monitor/?range=1h');

  await answer(latest, seriesBody('1h', 11));
  assert.equal(p.byId('model-uptime').textContent, '11%');
  assert.match(p.byId('hardware-note').textContent, /\$\/request/);

  await answer(old, seriesBody('24h', 99));
  assert.equal(p.byId('model-uptime').textContent, '11%', 'the stale 24h series must not render');

  p.timers[0]();
  assert.match(p.requests.at(-1).url, /range=1h/);
});

test('a late failure of an obsolete load does not replace a newer success', async () => {
  const p = page();
  p.rangeButtons[1].click();
  const [old, latest] = p.requests;
  await answer(latest, seriesBody('1h', 50));
  old.reject(new Error('timeout'));
  await flush();
  assert.equal(p.byId('load-error').classList.contains('d-none'), true, 'an obsolete failure must not show the error');
  assert.equal(p.byId('updated').textContent.startsWith('updated'), true);
});

test('a failure of the newest load is still reported', async () => {
  const p = page();
  p.requests[0].reject(new Error('boom'));
  await flush();
  assert.equal(p.byId('load-error').classList.contains('d-none'), false);
  assert.match(p.byId('load-error').textContent, /boom/);
});
