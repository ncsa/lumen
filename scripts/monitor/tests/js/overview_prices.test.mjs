// Runs the real overview.js against a fake DOM to check the Our $/M column:
// models with a cached-input price get a "cached in" line, models without one do not.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { answer, fakeElement, flush, runPage } from './fake_dom.mjs';

function page() {
  const ids = {};
  const byId = id => (ids[id] ??= fakeElement());
  ids.overview = fakeElement({ dataset: { overviewUrl: '/api/overview', lumenUrl: '/api/lumen', modelUrl: '/model/', refreshMs: '300000', range: '24h' } });
  ids['load-error'] = fakeElement();
  ids['load-error'].classList.add('d-none');
  const document = {
    getElementById: byId,
    createElement: () => fakeElement(),
    querySelectorAll: () => [],
  };
  const { requests } = runPage('overview.js', document);
  return { byId, requests };
}

function model(extra = {}) {
  return {
    id: 'org/qwen', strip: ['ok'], uptime: 99, healthy: 1, configured: 1,
    input_price: 1.1, output_price: 2.2, requests_24h: 100, tokens_24h: 50000, ...extra,
  };
}

function overviewBody(models) {
  return { range: '24h', generated_at: '2026-10-17T17:00:00Z', sort: 'model', direction: 'asc',
           models, strip_times: ['2026-10-17T16:50:00Z'],
           cards: { active_models: 1, healthy: 1, configured: 1, today_estimated: false } };
}

function lumenBody() {
  return { range: '24h', state: 'UP', uptime: 99, buckets: [] };
}

async function renderCell(models) {
  const p = page();
  const [ov, lumen] = p.requests;
  await answer(ov, overviewBody(models));
  await answer(lumen, lumenBody());
  return p.byId('model-rows').children[0].children[4].children;
}

async function render(models) {
  return (await renderCell(models)).map(d => d.textContent);
}

test('a cached-input price adds a "cached in" line to the Our $/M cell', async () => {
  const lines = await render([model({ cached_input_price: 0.1 })]);
  assert.deepEqual(lines, ['in 1.10', 'out 2.20', 'cached in 0.10']);
});

test('without a cached-input price the Our $/M cell stays two lines', async () => {
  const lines = await render([model({ cached_input_price: null })]);
  assert.deepEqual(lines, ['in 1.10', 'out 2.20']);
});

test('a zero cached-input price still gets its line', async () => {
  const lines = await render([model({ cached_input_price: 0 })]);
  assert.deepEqual(lines, ['in 1.10', 'out 2.20', 'cached in 0.00']);
});

test('a cache-write price adds a "cache write" line under the cached line', async () => {
  const lines = await render([model({ cached_input_price: 0.1, cache_write_price: 0.05 })]);
  assert.deepEqual(lines, ['in 1.10', 'out 2.20', 'cached in 0.10', 'cache write 0.05']);
});

test('a cache-write price without a cached-input price still gets its line', async () => {
  const lines = await render([model({ cached_input_price: null, cache_write_price: 0.05 })]);
  assert.deepEqual(lines, ['in 1.10', 'out 2.20', 'cache write 0.05']);
});

test('a cache price the backend does not report is marked', async () => {
  const [cached, written] = (await renderCell([model({
    cached_input_price: 0.1, cache_write_price: 0.05, cache_warning: { reads: false, writes: true },
  })])).slice(2);
  // Reads were reported, so only the write line carries the marker.
  assert.equal(cached.textContent, 'cached in 0.10');
  assert.equal(cached.children.length, 0);
  assert.equal(written.textContent, 'cache write 0.05');
  const [mark] = written.children;
  assert.equal(mark.textContent, '!');
  assert.equal(mark.attributes.class, 'marker');
  assert.match(mark.attributes.title, /no cache writes in the last day/);
});

test('an unreported read price is marked on the cached line', async () => {
  const [cached, written] = (await renderCell([model({
    cached_input_price: 0.1, cache_write_price: 0.05, cache_warning: { reads: true, writes: false },
  })])).slice(2);
  assert.equal(cached.children.length, 1);
  assert.match(cached.children[0].attributes.title, /no cache reads in the last day/);
  assert.equal(written.children.length, 0);
});

test('a reported cache price carries no marker', async () => {
  const lines = await render([model({ cached_input_price: 0.1, cache_write_price: 0.05, cache_warning: null })]);
  assert.deepEqual(lines, ['in 1.10', 'out 2.20', 'cached in 0.10', 'cache write 0.05']);
});

test('both unreported cache prices get one marker whose tip names both sides', async () => {
  const [cached, written] = (await renderCell([model({
    cached_input_price: 0.1, cache_write_price: 0.05, cache_warning: { reads: true, writes: true },
  })])).slice(2);
  const [mark] = cached.children;
  assert.equal(mark.textContent, '!');
  assert.equal(mark.attributes.class, 'marker');
  assert.match(mark.attributes.title, /no cache reads or writes in the last day/);
  // The same label appears once: the write line carries no second marker.
  assert.equal(written.textContent, 'cache write 0.05');
  assert.equal(written.children.length, 0);
});

test('a missing side without its price gets no marker at all', async () => {
  // Defensive: Lumen only flags a side whose price is set, but a stray flag must not add a label
  // to a line that is not shown.
  const lines = await render([model({
    cached_input_price: null, cache_write_price: 0.05, cache_warning: { reads: true, writes: false },
  })]);
  assert.deepEqual(lines, ['in 1.10', 'out 2.20', 'cache write 0.05']);
});
