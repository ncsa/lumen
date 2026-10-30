// Runs the real strip() from common.js against a fake DOM: titles on degraded and down cells,
// the capped list of those periods in the aria-label, and no titles without times.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import vm from 'node:vm';

import { fakeElement, readStatic } from './fake_dom.mjs';

function loadStrip() {
  const context = { document: { createElement: () => fakeElement() }, Date, Object, String, isFinite, window: {} };
  vm.createContext(context);
  vm.runInContext(readStatic('common.js'), context);
  return context.window.MonitorCommon.strip;
}

const strip = loadStrip();
// Local times, so the expected titles hold in any time zone.
const at = (day, hour, minute) => new Date(2026, 9, day, hour, minute).toISOString();

test('degraded and down cells get a title with their local time and status', () => {
  const node = strip(['ok', 'degraded', 'down', 'unknown'], [at(26, 14, 0), at(26, 14, 5), at(26, 14, 10), at(26, 14, 15)]);
  const titles = node.children.map(c => c.attributes.title);
  assert.deepEqual(titles, [undefined, '26 Oct 14:05 – degraded', '26 Oct 14:10 – down', undefined]);
  assert.equal(node.attributes['aria-label'],
    'Status buckets: 1 ok, 1 degraded, 1 down, 1 no data. Not ok: 26 Oct 14:05 – degraded, 26 Oct 14:10 – down');
});

test('a status other than ok is drawn and titled as degraded', () => {
  const node = strip(['starting'], [at(26, 9, 30)]);
  assert.equal(node.children[0].attributes.title, '26 Oct 09:30 – degraded');
});

test('1y dates are shown as the day itself', () => {
  const node = strip(['ok', 'down'], ['2026-10-25', '2026-10-26']);
  assert.equal(node.children[1].attributes.title, '26 Oct 2026 – down');
});

test('the aria-label lists at most 5 periods plus how many more', () => {
  const statuses = Array(8).fill('down');
  const times = statuses.map((_, i) => at(26, 10, i));
  const label = strip(statuses, times).attributes['aria-label'];
  assert.equal(label, 'Status buckets: 8 down. Not ok: 26 Oct 10:00 – down, 26 Oct 10:01 – down, 26 Oct 10:02 – down, '
    + '26 Oct 10:03 – down, 26 Oct 10:04 – down, +3 more');
});

test('without times no cell gets a title and the label is the summary', () => {
  for (const times of [undefined, []]) {
    const node = strip(['ok', 'down', 'degraded'], times);
    assert.ok(node.children.every(c => c.attributes.title === undefined));
    assert.equal(node.attributes['aria-label'], 'Status buckets: 1 ok, 1 down, 1 degraded');
  }
});
