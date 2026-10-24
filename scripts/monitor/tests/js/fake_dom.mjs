// A fake DOM and a hand-resolved fetch for running the dashboard's page scripts under Node.
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const STATIC = new URL('../../monitor/static/', import.meta.url);
export const readStatic = name => readFileSync(new URL(name, STATIC), 'utf8');

export function fakeElement(props = {}) {
  const classes = new Set();
  const handlers = {};
  return {
    dataset: {}, textContent: '', className: '', children: [], attributes: {},
    classList: { add: c => classes.add(c), remove: c => classes.delete(c), contains: c => classes.has(c) },
    setAttribute(k, v) { this.attributes[k] = String(v); },
    removeAttribute(k) { delete this.attributes[k]; },
    append(...c) { this.children.push(...c); },
    replaceChildren(...c) { this.children = c; this.textContent = c.map(x => (typeof x === 'string' ? x : x.textContent)).join(''); },
    addEventListener(name, fn) { handlers[name] = fn; },
    click() { return handlers.click(); },
    closest() { return null; },
    focus() {},
    getContext() { return {}; },
    ...props,
  };
}

export class FakeChart {
  constructor(_ctx, config) { this.data = config.data; this.options = config.options; }
  update() {}
  destroy() {}
}

// Runs common.js and then `script` in one context. Each fetch stays pending until the test settles it.
// `charts` collects every chart the page draws, in order.
export function runPage(script, document) {
  const requests = [];
  const timers = [];
  const charts = [];
  const Chart = class extends FakeChart {
    constructor(ctx, config) { super(ctx, config); charts.push(this); }
  };
  const context = {
    document, Chart, URL, URLSearchParams, Promise, Error, Number, String, Date, Math, Object, Array, isFinite,
    window: { location: { href: 'http://monitor.test/' }, history: { replaceState() {} } },
    setInterval: fn => timers.push(fn),
    fetch: url => new Promise((resolve, reject) => requests.push({ url, resolve, reject })),
  };
  vm.createContext(context);
  vm.runInContext(readStatic('common.js'), context);
  vm.runInContext(readStatic(script), context);
  return { requests, timers, charts };
}

export const flush = () => new Promise(r => setImmediate(r));

export async function answer(request, body) {
  request.resolve({ ok: true, json: async () => body });
  await flush();
}
