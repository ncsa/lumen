// Overview page: loads /api/overview and /api/lumen, redraws in place every refresh.page.
// Every URL comes from the page (data-* on #overview), so the page works under any base_path.
(function () {
  'use strict';

  const root = document.getElementById('overview');
  const urls = { overview: root.dataset.overviewUrl, lumen: root.dataset.lumenUrl, model: root.dataset.modelUrl };
  const refreshMs = Number(root.dataset.refreshMs) || 300000;
  const state = { range: root.dataset.range, sort: null, dir: null };
  const STATUS_TEXT = { ok: 'ok', degraded: 'degraded', down: 'down', unknown: 'no data' };
  const STATUS_COLOR = { ok: '#1d7a3e', degraded: '#b5300c', down: '#6b0f0f', unknown: '#c8c8c8' };
  let rtChart = null;

  const dash = '—';
  const isNum = v => typeof v === 'number' && isFinite(v);
  const fmtInt = v => isNum(v) ? Math.round(v).toLocaleString() : dash;
  const fmtFix = (v, d) => isNum(v) ? v.toFixed(d) : dash;
  const fmtPct = v => isNum(v) ? (v >= 99.95 && v < 100 ? '99.9' : v.toFixed(1).replace(/\.0$/, '')) + '%' : dash;
  const fmtMs = v => !isNum(v) ? dash : v >= 1000 ? (v / 1000).toFixed(1) + 's' : Math.round(v) + 'ms';
  const fmtTime = iso => iso ? new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '';
  function fmtCount(v) {
    if (!isNum(v)) return dash;
    if (v >= 1e9) return (v / 1e9).toFixed(1) + ' B';
    if (v >= 1e6) return (v / 1e6).toFixed(1) + ' M';
    if (v >= 1e4) return (v / 1e3).toFixed(1) + ' K';
    return Math.round(v).toLocaleString();
  }

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => {
      if (k === 'text') node.textContent = v; else node.setAttribute(k, v);
    });
    (children || []).forEach(c => node.append(c));
    return node;
  }

  function marker(symbol, label) {
    return el('abbr', { class: 'marker', title: label, 'aria-label': label, text: symbol });
  }

  function strip(statuses) {
    const counts = {};
    statuses.forEach(s => { counts[s] = (counts[s] || 0) + 1; });
    const summary = Object.entries(counts).map(([s, n]) => `${n} ${STATUS_TEXT[s] || s}`).join(', ');
    const node = el('div', { class: 'strip', role: 'img', 'aria-label': `Status buckets: ${summary}`, title: summary });
    statuses.forEach(s => node.append(el('span', { class: `strip-cell s-${STATUS_COLOR[s] ? s : 'unknown'}` })));
    return node;
  }

  function priceRange(median, p5, p95) {
    if (!isNum(median)) return dash;
    return `${median.toFixed(2)} (${fmtFix(p5, 2)}–${fmtFix(p95, 2)})`;
  }

  function inOut(input, output) {
    return [el('div', { class: 'io', text: `in ${input}` }), el('div', { class: 'io', text: `out ${output}` })];
  }

  function cell(content) {
    const td = el('td');
    if (Array.isArray(content)) content.forEach(c => td.append(c)); else td.append(content);
    return td;
  }

  function hwPerToken(m) {
    const parts = [];
    if (isNum(m.hw_per_mtok)) parts.push(m.hw_per_mtok.toFixed(3));
    else if (isNum(m.hw_per_request)) parts.push(`${m.hw_per_request.toFixed(4)}/req`);
    else return m.hw_configured ? dash : [dash, marker('†', 'no GPU config')];
    if (m.hw_partial) parts.push(' ', marker('*', 'includes estimated or non-final days'));
    return parts;
  }

  function renderRows(models) {
    const body = document.getElementById('model-rows');
    body.replaceChildren();
    if (!models.length) {
      body.append(el('tr', {}, [el('td', { colspan: 11, class: 'text-muted', text: 'No models yet: waiting for the first successful probe.' })]));
      return;
    }
    models.forEach(m => {
      const href = urls.model + encodeURIComponent(m.id);
      const name = el('td', {}, [el('a', { href, text: m.id })]);
      if (m.description) name.append(el('div', { class: 'model-desc', text: m.description }));
      const tr = el('tr', {}, [
        name,
        cell(strip(m.strip)),
        cell(fmtPct(m.uptime)),
        cell(`${m.healthy ?? dash}/${m.configured ?? dash}`),
        cell(inOut(fmtFix(m.input_price, 2), fmtFix(m.output_price, 2))),
        cell(isNum(m.modelsdev_input_median)
          ? inOut(priceRange(m.modelsdev_input_median, m.modelsdev_input_p5, m.modelsdev_input_p95),
            priceRange(m.modelsdev_output_median, m.modelsdev_output_p5, m.modelsdev_output_p95))
          : dash),
        cell(isNum(m.hw_daily_cost) ? m.hw_daily_cost.toFixed(2) : (m.hw_configured ? dash : [dash, marker('†', 'no GPU config')])),
        cell(hwPerToken(m)),
        cell(isNum(m.peak_rpm) ? fmtInt(m.peak_rpm) : dash),
        cell(fmtInt(m.requests_24h)),
        cell(fmtCount(m.tokens_24h)),
      ]);
      // The model name is the keyboard target; a click anywhere else on the row follows it too.
      tr.addEventListener('click', ev => { if (!ev.target.closest('a')) window.location.href = href; });
      body.append(tr);
    });
  }

  function renderSort(sort, dir) {
    document.querySelectorAll('#model-table th[data-sort]').forEach(th => {
      if (th.dataset.sort === sort) th.setAttribute('aria-sort', dir === 'asc' ? 'ascending' : 'descending');
      else th.removeAttribute('aria-sort');
    });
  }

  function renderCards(c) {
    const today = (v, fmt) => c.today_estimated && isNum(v) ? [fmt(v), ' ', marker('*', 'includes estimated values')] : [fmt(v)];
    document.getElementById('card-active').textContent = fmtInt(c.active_models);
    document.getElementById('card-instances').textContent = `${c.healthy ?? dash} / ${c.configured ?? dash}`;
    document.getElementById('card-req-hour').textContent = fmtInt(c.requests_last_hour);
    document.getElementById('card-req-today').replaceChildren(...today(c.requests_today, fmtInt));
    document.getElementById('card-tok-hour').textContent = fmtCount(c.tokens_last_hour);
    document.getElementById('card-tok-today').replaceChildren(...today(c.tokens_today, fmtCount));
  }

  function renderLumen(l) {
    const badge = document.getElementById('lumen-state');
    badge.textContent = l.state;
    badge.className = `state-badge state-${l.state.toLowerCase()}`;
    document.getElementById('lumen-uptime').textContent = fmtPct(l.uptime);
    document.getElementById('lumen-last').textContent = fmtMs(l.last_ms);
    document.getElementById('lumen-last-at').textContent = l.last_at ? `(${fmtTime(l.last_at)})` : '';
    document.getElementById('lumen-min').textContent = fmtMs(l.min_ms);
    document.getElementById('lumen-avg').textContent = fmtMs(l.avg_ms);
    document.getElementById('lumen-max').textContent = fmtMs(l.max_ms);

    const labels = l.buckets.map(b => new Date(b.t).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }));
    const data = l.buckets.map(b => b.rt);
    const colors = l.buckets.map(b => STATUS_COLOR[b.status] || STATUS_COLOR.unknown);
    if (!rtChart) {
      rtChart = new Chart(document.getElementById('lumen-rt').getContext('2d'), {
        type: 'bar',
        data: { labels, datasets: [{ label: 'max response time (ms)', data, backgroundColor: colors, barPercentage: 1, categoryPercentage: 1 }] },
        options: {
          responsive: true, maintainAspectRatio: false, animation: false,
          plugins: { legend: { display: false }, tooltip: { callbacks: { afterLabel: ctx => STATUS_TEXT[l.buckets[ctx.dataIndex]?.status] || '' } } },
          // Log scale: one timeout (seconds) would otherwise flatten every normal bar (milliseconds).
          scales: { x: { display: false }, y: { display: false, type: 'logarithmic' } },
        },
      });
    }
    rtChart.data.labels = labels;
    rtChart.data.datasets[0].data = data;
    rtChart.data.datasets[0].backgroundColor = colors;
    rtChart.options.plugins.tooltip.callbacks.afterLabel = ctx => STATUS_TEXT[l.buckets[ctx.dataIndex]?.status] || '';
    rtChart.update();
  }

  async function getJson(url, params) {
    const r = await fetch(`${url}?${new URLSearchParams(params)}`, { headers: { Accept: 'application/json' } });
    if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
    return r.json();
  }

  // Every load gets a generation; only the newest one may touch state, render or report an
  // error, so a slow response for an earlier range or sort never overwrites a newer selection.
  let generation = 0;

  async function load() {
    const mine = ++generation;
    const params = { range: state.range };
    if (state.sort) params.sort = state.sort;
    if (state.dir) params.dir = state.dir;
    const error = document.getElementById('load-error');
    try {
      const [ov, lumen] = await Promise.all([getJson(urls.overview, params), getJson(urls.lumen, { range: params.range })]);
      if (mine !== generation) return;
      state.sort = ov.sort;
      state.dir = ov.direction;
      renderLumen(lumen);
      renderCards(ov.cards);
      renderRows(ov.models);
      renderSort(ov.sort, ov.direction);
      document.getElementById('updated').textContent = `updated ${fmtTime(ov.generated_at)}`;
      error.classList.add('d-none');
    } catch (e) {
      if (mine !== generation) return;
      error.textContent = `Could not load the dashboard data: ${e.message}`;
      error.classList.remove('d-none');
    }
  }

  document.querySelectorAll('.range-btn').forEach(btn => btn.addEventListener('click', () => {
    state.range = btn.dataset.range;
    document.querySelectorAll('.range-btn').forEach(b => b.setAttribute('aria-pressed', String(b === btn)));
    document.querySelectorAll('.range-label').forEach(s => { s.textContent = state.range; });
    const url = new URL(window.location.href);
    url.searchParams.set('range', state.range);
    window.history.replaceState(null, '', url);
    load();
  }));

  document.querySelectorAll('#model-table th[data-sort] .sort-btn').forEach(btn => btn.addEventListener('click', () => {
    const key = btn.closest('th').dataset.sort;
    // The same column flips; a new column lets the server pick (numeric descending, text ascending).
    if (key === state.sort) state.dir = state.dir === 'asc' ? 'desc' : 'asc';
    else { state.sort = key; state.dir = null; }
    load().then(() => btn.focus());
  }));

  load();
  setInterval(load, refreshMs);
})();
