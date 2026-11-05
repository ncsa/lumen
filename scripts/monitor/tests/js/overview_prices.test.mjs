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

async function render(models) {
  const p = page();
  const [ov, lumen] = p.requests;
  await answer(ov, overviewBody(models));
  await answer(lumen, lumenBody());
  return p.byId('model-rows').children[0].children[4].children.map(d => d.textContent);
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
