// Runs the real detail.js against a fake DOM and checks the price charts' config:
// a stepped line per bucket below 7d, one point per local day from 7d up.
// Run with `node --test tests/js/*.test.mjs` (tests/test_overview_js.py does this under pytest).
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { answer, fakeElement, runPage } from './fake_dom.mjs';

const PRICE = { input_price: 0.2, output_price: 0.8, input_median: 0.25, input_p5: 0.1, input_p95: 0.4, output_median: 1, output_p5: 0.5, output_p95: 2 };
const TIMES = ['2026-10-17T04:55:00Z', '2026-10-17T05:00:00Z', '2026-10-17T05:05:00Z'];
const DAYS = ['2026-10-16', '2026-10-17'];

async function priceCharts(body) {
  const ids = {};
  ids.detail = fakeElement({ dataset: { seriesUrl: '/monitor/api/series', overviewUrl: '/monitor/', model: 'org/qwen', refreshMs: '300000', range: body.range } });
  const document = {
    getElementById: id => (ids[id] ??= fakeElement()),
    createElement: () => fakeElement(),
    querySelectorAll: () => [],
  };
  const { requests, charts } = runPage('detail.js', document);
  await answer(requests[0], body);
  assert.equal(ids['load-error'].textContent, '', 'the series must render');
  // render() draws the input then the output price chart first.
  // A JSON copy: objects built inside the page's vm context have that context's prototypes.
  return charts.slice(0, 2).map(c => JSON.parse(JSON.stringify(c.config)));
}

function seriesBody(range, daily, prices) {
  const t = TIMES[0];
  return {
    range, model: 'org/qwen', generated_at: '2026-10-17T05:05:00Z', daily,
    header: { status: 'ok', healthy: 1, configured: 1, uptime: 100, peak_rpm: 3, description: 'Qwen', strip: ['ok'] },
    strip_times: [t],
    prices,
    traffic: { unit: daily ? 'day' : 'minute', points: [{ t, requests: 2, tokens: null, estimated: false }], average: { requests: 2, tokens: null } },
    hardware: { points: [{ day: '2026-10-17', benefit: null, cost: 24, partial: true, estimated: false }], range: { benefit: null, cost: null, partial: false, days: 0 } },
    instances: { unit: 'bucket', points: [{ t, healthy: 1, configured: 1 }] },
  };
}

const timeLabel = t => new Date(t).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
const dayLabel = d => new Date(`${d}T12:00:00`).toLocaleDateString([], { month: 'short', day: 'numeric' });

test('below 7d the price charts are stepped lines per bucket', async () => {
  const configs = await priceCharts(seriesBody('24h', false, TIMES.map(t => ({ t, ...PRICE }))));
  assert.equal(configs.length, 2);
  for (const config of configs) {
    assert.deepEqual(config.data.labels, TIMES.map(timeLabel));
    assert.ok(!config.data.labels.includes('Invalid Date'));
    assert.equal(config.data.datasets.length, 4);
    for (const ds of config.data.datasets) {
      assert.equal(ds.stepped, true, ds.label);
      assert.equal(ds.pointRadius, 0, ds.label);
    }
    assert.deepEqual(config.options.scales.x, { ticks: { maxTicksLimit: 8, maxRotation: 0 } });
  }
  assert.deepEqual(configs[0].data.datasets[3].data, [0.2, 0.2, 0.2]);
  assert.deepEqual(configs[1].data.datasets[3].data, [0.8, 0.8, 0.8]);
});

test('from 7d up the price charts keep one point per local day', async () => {
  const configs = await priceCharts(seriesBody('7d', true, DAYS.map(day => ({ day, ...PRICE }))));
  for (const config of configs) {
    assert.deepEqual(config.data.labels, DAYS.map(dayLabel));
    const [p5, p95, median, ours] = config.data.datasets;
    for (const ds of [p5, p95, median, ours]) assert.equal('stepped' in ds, false, ds.label);
    assert.equal(p5.pointRadius, 0);
    assert.equal(p95.pointRadius, 0);
    assert.equal(median.pointRadius, 2);
    assert.equal(ours.pointRadius, 2);
    assert.deepEqual(config.options.scales, { y: { beginAtZero: true } });
  }
});
