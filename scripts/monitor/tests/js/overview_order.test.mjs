// Runs the real overview.js against a fake DOM and a fetch whose responses the test resolves
// by hand, to check that only the newest load renders, updates state or reports an error.
// Run with `node --test tests/js/*.test.mjs` (tests/test_overview_js.py does this under pytest).
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { answer, fakeElement, flush, runPage } from './fake_dom.mjs';

function page() {
  const ids = {};
  const byId = id => (ids[id] ??= fakeElement());
  ids.overview = fakeElement({ dataset: { overviewUrl: '/api/overview', lumenUrl: '/api/lumen', modelUrl: '/model/', refreshMs: '300000', range: '24h' } });
  ids['load-error'] = fakeElement();
  ids['load-error'].classList.add('d-none');
  const rangeButtons = ['24h', '1h'].map(range => fakeElement({ dataset: { range } }));
  const sortButtons = [fakeElement({ closest: () => ({ dataset: { sort: 'uptime' } }) })];

  const document = {
    getElementById: byId,
    createElement: () => fakeElement(),
    querySelectorAll: sel => (sel === '.range-btn' ? rangeButtons : sel.includes('.sort-btn') ? sortButtons : []),
  };
  const { requests, timers } = runPage('overview.js', document);
  return { byId, requests, timers, rangeButtons, sortButtons };
}

function overviewBody(range, extra = {}) {
  return {
    range, generated_at: '2026-10-17T17:00:00Z', sort: 'model', direction: 'asc', models: [], strip_times: [],
    cards: { active_models: range === '1h' ? 1 : 24, healthy: 1, configured: 1, today_estimated: false },
    ...extra,
  };
}

function lumenBody(range) {
  return { range, state: 'UP', uptime: range === '1h' ? 11 : 99, buckets: [], last_ms: 1, min_ms: 1, avg_ms: 1, max_ms: 1 };
}

function requestsFor(requests, range) {
  return requests.filter(r => r.url.includes(`range=${range}`));
}

test('a late response for an earlier range does not replace the selected range', async () => {
  const p = page();
  assert.equal(p.requests.length, 2); // initial 24h load: overview + lumen
  p.rangeButtons[1].click(); // select 1h while 24h is still loading
  const [ov1h, lumen1h] = requestsFor(p.requests, '1h');
  const [ov24h, lumen24h] = requestsFor(p.requests, '24h');

  await answer(ov1h, overviewBody('1h', { sort: 'uptime', direction: 'desc' }));
  await answer(lumen1h, lumenBody('1h'));
  assert.equal(p.byId('card-active').textContent, '1');
  assert.equal(p.byId('lumen-uptime').textContent, '11%');

  await answer(ov24h, overviewBody('24h'));
  await answer(lumen24h, lumenBody('24h'));
  assert.equal(p.byId('card-active').textContent, '1', 'the stale 24h overview must not render');
  assert.equal(p.byId('lumen-uptime').textContent, '11%', 'the stale 24h Lumen card must not render');

  // The stale response did not reset the sort either: the next refresh keeps the 1h sort.
  p.timers[0]();
  const refresh = p.requests.at(-2);
  assert.match(refresh.url, /range=1h/);
  assert.match(refresh.url, /sort=uptime/);
  assert.match(refresh.url, /dir=desc/);
});

test('a late failure of an obsolete load does not replace a newer success', async () => {
  const p = page();
  p.sortButtons[0].click(); // a newer load starts while the first is pending
  const [ovOld, lumenOld, ovNew, lumenNew] = p.requests;

  await answer(ovNew, overviewBody('24h', { sort: 'uptime', direction: 'desc' }));
  await answer(lumenNew, lumenBody('24h'));
  assert.equal(p.byId('updated').textContent.startsWith('updated'), true);

  ovOld.reject(new Error('timeout'));
  lumenOld.reject(new Error('timeout'));
  await flush();
  assert.equal(p.byId('load-error').classList.contains('d-none'), true, 'an obsolete failure must not show the error');
  assert.equal(p.byId('load-error').textContent, '');
});

test('a failure of the newest load is still reported', async () => {
  const p = page();
  const [ov] = p.requests;
  ov.reject(new Error('boom'));
  await flush();
  assert.equal(p.byId('load-error').classList.contains('d-none'), false);
  assert.match(p.byId('load-error').textContent, /boom/);
});

test('the model link keeps the selected range', async () => {
  const p = page();
  p.rangeButtons[1].click(); // select 1h
  const [ov, lumen] = requestsFor(p.requests, '1h');
  await answer(ov, overviewBody('1h', { models: [{ id: 'org/model a', strip: [] }] }));
  await answer(lumen, lumenBody('1h'));
  const [row] = p.byId('model-rows').children;
  const link = row.children[0].children[0];
  assert.equal(link.attributes.href, '/model/org/model%20a?range=1h');
});

test('model strips get the time of their cells', async () => {
  const p = page();
  const [ov, lumen] = requestsFor(p.requests, '24h');
  await answer(ov, overviewBody('24h', { models: [{ id: 'm', strip: ['ok', 'degraded'] }], strip_times: ['2026-10-17T16:50:00Z', '2026-10-17T16:55:00Z'] }));
  await answer(lumen, lumenBody('24h'));
  const [row] = p.byId('model-rows').children;
  const [strip] = row.children[1].children;
  assert.equal(strip.children[0].attributes.title, undefined);
  assert.match(strip.children[1].attributes.title, / – degraded$/);
});
