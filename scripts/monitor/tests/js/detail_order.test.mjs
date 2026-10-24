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
  const { requests, timers, charts, window } = runPage('detail.js', document);
  return { byId, requests, timers, charts, window, rangeButtons };
}

function seriesBody(range, uptime, hardwareRange = { benefit: null, cost: null, partial: false, days: 0 }, hardwarePoint = {}, traffic = undefined) {
  const t = '2026-10-17T16:59:00Z';
  return {
    range, model: 'org/qwen', generated_at: '2026-10-17T17:00:00Z', daily: false,
    header: { status: 'ok', healthy: 1, configured: 1, uptime, peak_rpm: 3, description: 'Qwen', strip: ['ok'] },
    strip: [{ t, status: 'ok' }],
    prices: [{ t, input_price: 0.2, output_price: 0.8, input_median: 0.25, input_p5: 0.1, input_p95: 0.4, output_median: 1, output_p5: 0.5, output_p95: 2 }],
    traffic: traffic ?? { unit: 'minute', points: [{ t, requests: 2, tokens: null, estimated: false }], average: { requests: 2, tokens: null } },
    hardware: { points: [{ day: '2026-10-17', benefit: hardwareRange.benefit, cost: 24, partial: true, estimated: false, ...hardwarePoint }], range: hardwareRange },
    instances: { unit: 'bucket', points: [{ t, healthy: 1, configured: 1 }] },
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
  assert.equal(p.byId('hardware-note').textContent, 'no input/output token counts yet');

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

test('the cost / benefit note shows both totals over the days with token counts', async () => {
  const p = page();
  await answer(p.requests[0], seriesBody('24h', 50, { benefit: 30.5, cost: 24, partial: false, days: 3 }));
  assert.equal(p.byId('hardware-note').textContent, 'benefit $30.50 / cost $24.00 over the 3 days with token counts');
});

test('the cost / benefit note flags estimated or partial days', async () => {
  const p = page();
  await answer(p.requests[0], seriesBody('24h', 50, { benefit: 3, cost: 12.25, partial: true, days: 1 }));
  assert.equal(p.byId('hardware-note').textContent, 'benefit $3.00 / cost $12.25 over the 1 day with token counts · lighter bars are estimated or not final');
});

// Two buckets with the split and one from before it, which only has the combined count.
const splitTraffic = (unit = 'minute', estimated = false) => ({
  unit,
  points: [
    { t: '2026-10-17T16:57:00Z', requests: 1, tokens: 7000, input_tokens: null, output_tokens: null, estimated: false },
    { t: '2026-10-17T16:58:00Z', requests: 1, tokens: 140000, input_tokens: 40000, output_tokens: 100000, estimated },
    { t: '2026-10-17T16:59:00Z', requests: 1, tokens: 6000, input_tokens: 1000, output_tokens: 5000, estimated: false },
  ],
  average: { requests: 1, tokens: 51000, input_tokens: 20500, output_tokens: 52500 },
});

const tokensChart = p => p.charts.find(c => c.data.datasets.some(d => d.label === 'tokens (no split)'));

test('the tokens note shows the output and input averages', async () => {
  const p = page();
  await answer(p.requests[0], seriesBody('24h', 50, undefined, undefined, splitTraffic()));
  assert.equal(p.byId('tokens-note').textContent, 'per minute, averaged per bucket · average out 52.5 K / in 20.5 K');
});

test('the tokens note per day flags estimated days', async () => {
  const p = page();
  await answer(p.requests[0], seriesBody('30d', 50, undefined, undefined, splitTraffic('day', true)));
  assert.equal(p.byId('tokens-note').textContent, 'per day · average out 52.5 K / in 20.5 K · lighter bars are estimated or not final');
});

test('the tokens note shows a dash for a side without counts', async () => {
  const p = page();
  const traffic = { unit: 'minute', points: [{ t: '2026-10-17T16:59:00Z', requests: 1, tokens: 70000, input_tokens: null, output_tokens: null, estimated: false }],
    average: { requests: 1, tokens: 70000, input_tokens: null, output_tokens: null } };
  await answer(p.requests[0], seriesBody('1h', 50, undefined, undefined, traffic));
  assert.equal(p.byId('tokens-note').textContent, 'per minute, averaged per bucket · average out — / in —');
});

test('the tokens chart puts output above zero, input and unsplit points below, labelled on the left', async () => {
  const p = page();
  await answer(p.requests[0], seriesBody('24h', 50, undefined, undefined, splitTraffic()));
  const chart = tokensChart(p);
  const [output, input, combined, avgOut, avgIn] = chart.data.datasets;
  assert.deepEqual([output.label, input.label], ['output (above zero)', 'input (below zero)']);
  assert.deepEqual([...output.data], [null, 100000, 5000]);
  assert.deepEqual([...input.data], [null, -40000, -1000]);
  assert.deepEqual([...combined.data], [-7000, null, null]);
  assert.deepEqual([output.yAxisID, input.yAxisID, combined.yAxisID, avgOut.yAxisID, avgIn.yAxisID], ['out', 'in', 'in', 'out', 'in']);
  assert.deepEqual([output.grouped, input.grouped, combined.grouped], [false, false, false]);
  assert.ok([output, input, combined].every(d => d.stack === undefined), 'no stacking');
  assert.equal(avgOut.data[0], 52500);
  assert.equal(avgIn.data[0], -20500);
  const { x, out, in: inn } = chart.options.scales;
  assert.ok(!x.stacked && !out.stacked && !inn.stacked);
  assert.deepEqual([out.position, out.min, out.max], ['left', -100000, 100000]);
  assert.deepEqual([inn.min, inn.max, inn.display], [-40000, 40000, false], 'the input axis only scales, its labels are on the left');
  // upper half: output counts; lower half: input counts at the same pixel (left value × 40 K / 100 K)
  assert.equal(out.ticks.callback(20000), '20.0 K');
  assert.equal(out.ticks.callback(-50000), '20.0 K');
  // the left axis keeps its own upper ticks and gets round input values below zero
  const axis = { ticks: [-100000, -50000, 0, 50000, 100000].map(value => ({ value })) };
  out.afterBuildTicks(axis);
  assert.deepEqual([...axis.ticks.map(t => t.value)], [0, 50000, 100000, -25000, -50000, -75000, -100000]);
  assert.deepEqual([...axis.ticks.filter(t => t.value < 0).map(t => out.ticks.callback(t.value))], ['10.0 K', '20.0 K', '30.0 K', '40.0 K']);
  const label = chart.options.plugins.tooltip.callbacks.label;
  assert.equal(label({ dataset: input, parsed: { y: -40000 } }), 'input (below zero): 40.0 K');
  assert.equal(label({ dataset: combined, parsed: { y: -70000 } }), 'tokens (no split): 70.0 K');
  assert.equal(chart.options.plugins.tooltip.filter({ parsed: { y: null } }), false);
});

test('mirroredTicks puts round input values below zero, whatever the two scales', () => {
  const { mirroredTicks } = page().window.MonitorDetail;
  // out 7 M, in 200 M: input labels every 50 M, placed at -v × 7 M / 200 M
  const ticks = mirroredTicks([{ value: 0 }, { value: 7e6 }, { value: -7e6 }], 7e6, 200e6);
  assert.deepEqual([...ticks.map(t => t.value)], [0, 7e6, -1.75e6, -3.5e6, -5.25e6, -7e6]);
});

test('mirroredBounds is symmetric on each side, whatever the magnitudes', () => {
  const { mirroredBounds } = page().window.MonitorDetail;
  const out = mirroredBounds([7e6, 3e6, null, 5e6]);
  const inn = mirroredBounds([-200e6, -150e6, null, -170e6]);
  assert.deepEqual({ ...out }, { min: -7e6, max: 7e6 });
  assert.deepEqual({ ...inn }, { min: -200e6, max: 200e6 });
});

test('mirroredBounds on a range with only unsplit points', async () => {
  const p = page();
  const t = day => ({ t: day, requests: 1, tokens: null, input_tokens: null, output_tokens: null, estimated: false });
  const traffic = { unit: 'day', points: [{ ...t('2026-10-16'), tokens: 3e7 }, { ...t('2026-10-17'), tokens: 9e7 }],
    average: { requests: 1, tokens: 6e7, input_tokens: null, output_tokens: null } };
  await answer(p.requests[0], seriesBody('30d', 50, undefined, undefined, traffic));
  const { out, in: inn } = tokensChart(p).options.scales;
  assert.deepEqual([inn.min, inn.max], [-9e7, 9e7], 'the input side scales to the largest combined total');
  assert.deepEqual([out.min, out.max], [-1, 1]);
});

test('mirroredBounds takes an unsplit total larger than every split input', async () => {
  const p = page();
  const traffic = splitTraffic();
  traffic.points[0].tokens = 250000; // the unsplit bucket
  await answer(p.requests[0], seriesBody('24h', 50, undefined, undefined, traffic));
  const { in: inn } = tokensChart(p).options.scales;
  assert.deepEqual([inn.min, inn.max], [-250000, 250000]);
});

test('mirroredBounds gives ±1 for a side with nothing drawn', () => {
  const { mirroredBounds } = page().window.MonitorDetail;
  assert.deepEqual({ ...mirroredBounds([]) }, { min: -1, max: 1 });
  assert.deepEqual({ ...mirroredBounds([null, null]) }, { min: -1, max: 1 });
  assert.deepEqual({ ...mirroredBounds([0, null, -0]) }, { min: -1, max: 1 });
});

// Arrays made inside the page's context have that context's prototype; compare them as plain data.
const plain = v => JSON.parse(JSON.stringify(v));

function hardwareChartOf(p) {
  return p.charts.find(c => c.data.datasets.some(d => d.label === 'hardware cost'));
}

async function tooltipNote(point) {
  const p = page();
  await answer(p.requests[0], seriesBody('24h', 50, { benefit: 3, cost: 12.25, partial: true, days: 1 }, point));
  return plain(hardwareChartOf(p).options.plugins.tooltip.callbacks.afterBody([{ dataIndex: 0 }]));
}

test('the cost / benefit tooltip says a partial day is projected only when it has a bar', async () => {
  assert.deepEqual(await tooltipNote({ benefit: 4 }), ['day not over: benefit projected to the whole day']);
  assert.deepEqual(await tooltipNote({ benefit: null }), []);
  assert.deepEqual(await tooltipNote({ benefit: 4, estimated: true }), ['day not over: benefit projected to the whole day', 'estimated or not final']);
  assert.deepEqual(await tooltipNote({ benefit: 4, partial: false }), [], 'an exact day adds no tooltip line');
});

test('the cost / benefit chart has one $/day axis with benefit bars and the cost line', async () => {
  const p = page();
  await answer(p.requests[0], seriesBody('24h', 50, { benefit: 3, cost: 12.25, partial: true, days: 1 }));
  const chart = hardwareChartOf(p);
  assert.deepEqual(plain(Object.keys(chart.options.scales)), ['y']);
  assert.deepEqual(plain(chart.data.datasets.map(d => [d.label, d.type ?? 'bar'])), [['benefit', 'bar'], ['hardware cost', 'line']]);
  assert.deepEqual(plain(chart.data.datasets[1].borderDash), [6, 4]);
});
