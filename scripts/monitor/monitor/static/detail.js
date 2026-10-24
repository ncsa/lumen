// Model detail page: loads /api/series for one model, redraws in place every refresh.page.
// Every URL comes from the page (data-* on #detail), so the page works under any base_path.
(function () {
  'use strict';

  const root = document.getElementById('detail');
  const urls = { series: root.dataset.seriesUrl, overview: root.dataset.overviewUrl };
  const model = root.dataset.model;
  const refreshMs = Number(root.dataset.refreshMs) || 300000;
  const state = { range: root.dataset.range };
  const { STATUS_COLOR, dash, isNum, fmtInt, fmtFix, fmtPct, fmtTime, fmtCount, strip, getJson } = window.MonitorCommon;
  const COLOR = { ours: '#13294B', median: '#b5300c', band: 'rgba(181, 48, 12, 0.15)', bar: '#13294B', estimated: 'rgba(19, 41, 75, 0.35)', line: '#1d7a3e', cost: '#c8102e' };
  const charts = {};

  // A day is 'YYYY-MM-DD' (a local date already); a bucket is a UTC time shown in local time.
  const dayLabel = d => new Date(`${d}T12:00:00`).toLocaleDateString([], { month: 'short', day: 'numeric' });
  const timeLabel = t => new Date(t).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });

  function draw(id, config) {
    if (charts[id]) charts[id].destroy();
    config.options = {
      responsive: true, maintainAspectRatio: false, animation: false, spanGaps: false,
      interaction: { mode: 'index', intersect: false },
      ...config.options,
    };
    charts[id] = new Chart(document.getElementById(`chart-${id}`).getContext('2d'), config);
  }

  function priceChart(id, side, prices) {
    const labels = prices.map(p => dayLabel(p.day));
    const pick = key => prices.map(p => p[`${side}_${key}`]);
    draw(id, {
      type: 'line',
      data: {
        labels,
        datasets: [
          { label: 'models.dev p5', data: pick('p5'), borderWidth: 0, pointRadius: 0, fill: false },
          { label: 'models.dev p5–p95', data: pick('p95'), borderWidth: 0, pointRadius: 0, backgroundColor: COLOR.band, fill: '-1' },
          { label: 'models.dev median', data: pick('median'), borderColor: COLOR.median, borderDash: [6, 4], pointRadius: 2 },
          { label: 'ours', data: prices.map(p => p[`${side}_price`]), borderColor: COLOR.ours, borderWidth: 3, pointRadius: 2 },
        ],
      },
      options: { plugins: { legend: { labels: { filter: item => item.text !== 'models.dev p5' } } }, scales: { y: { beginAtZero: true } } },
    });
  }

  function trafficChart(id, key, traffic, fmt) {
    const daily = traffic.unit === 'day';
    const points = traffic.points;
    const avg = traffic.average[key];
    draw(id, {
      type: 'bar',
      data: {
        labels: points.map(p => daily ? dayLabel(p.t) : timeLabel(p.t)),
        datasets: [
          { label: daily ? `${key} per day` : `${key}/min`, data: points.map(p => p[key]),
            backgroundColor: points.map(p => p.estimated ? COLOR.estimated : COLOR.bar), barPercentage: 1, categoryPercentage: daily ? 0.8 : 1, order: 1 },
          { type: 'line', label: 'range average', data: points.map(() => avg), borderColor: COLOR.median, borderDash: [4, 4], pointRadius: 0, order: 0 },
        ],
      },
      options: {
        plugins: { tooltip: { callbacks: { afterLabel: ctx => ctx.datasetIndex === 0 && points[ctx.dataIndex]?.estimated ? 'estimated or not final' : '' } } },
        scales: { x: { ticks: { maxTicksLimit: 12 } }, y: { beginAtZero: true, ticks: { callback: v => fmt(v) } } },
      },
    });
    const estimated = points.some(p => p.estimated) ? ' · lighter bars are estimated or not final' : '';
    document.getElementById(`${id}-note`).textContent = `${daily ? 'per day' : 'per minute, averaged per bucket'} · average ${fmt(avg)}${estimated}`;
  }

  function hardwareChart(hardware) {
    const points = hardware.points;
    const fmtUsd = v => `$${fmtFix(v, 2)}`;
    draw('hardware', {
      type: 'bar',
      data: {
        labels: points.map(p => dayLabel(p.day)),
        datasets: [
          { label: 'benefit', data: points.map(p => p.benefit),
            backgroundColor: points.map(p => p.partial || p.estimated ? COLOR.estimated : COLOR.bar), order: 1 },
          { type: 'line', label: 'hardware cost', data: points.map(p => p.cost), borderColor: COLOR.cost, borderDash: [6, 4], borderWidth: 2, pointRadius: 2, order: 0 },
        ],
      },
      options: {
        plugins: { tooltip: { callbacks: {
          label: ctx => `${ctx.dataset.label}: ${isNum(ctx.parsed.y) ? fmtUsd(ctx.parsed.y) : dash}`,
          afterBody: items => {
            const p = points[items[0]?.dataIndex];
            if (p?.partial) return 'day not over: benefit projected to the whole day';
            return p?.estimated ? 'estimated or not final' : '';
          },
        } } },
        scales: { y: { beginAtZero: true, title: { display: true, text: '$/day' } } },
      },
    });
    const { benefit, cost, partial } = hardware.range;
    const note = isNum(benefit)
      ? `benefit ${fmtUsd(benefit)} / cost ${fmtUsd(cost)} over the range${partial ? ' · lighter bars are estimated or not final' : ''}`
      : 'no input/output token counts yet';
    document.getElementById('hardware-note').textContent = note;
  }

  function instancesChart(instances) {
    const daily = instances.unit === 'day';
    const points = instances.points;
    draw('instances', {
      type: 'line',
      data: {
        labels: points.map(p => daily ? dayLabel(p.t) : timeLabel(p.t)),
        datasets: [
          { label: 'healthy', data: points.map(p => p.healthy), borderColor: COLOR.line, stepped: true, pointRadius: 0 },
          { label: 'configured', data: points.map(p => p.configured), borderColor: COLOR.ours, borderDash: [6, 4], stepped: true, pointRadius: 0 },
        ],
      },
      options: { scales: { x: { ticks: { maxTicksLimit: 8, maxRotation: 0 } }, y: { beginAtZero: true, ticks: { precision: 0 } } } },
    });
  }

  function renderHeader(h) {
    const status = h.status || 'unknown';
    const badge = document.getElementById('model-status');
    badge.textContent = status;
    badge.className = `state-badge state-${STATUS_COLOR[status] ? status : 'degraded'}`;
    document.getElementById('model-instances').textContent = `${h.healthy ?? dash}/${h.configured ?? dash}`;
    document.getElementById('model-uptime').textContent = fmtPct(h.uptime);
    document.getElementById('model-peak').textContent = isNum(h.peak_rpm) ? fmtInt(h.peak_rpm) : dash;
    document.getElementById('model-desc').textContent = h.description || '';
    document.getElementById('model-strip').replaceChildren(strip(h.strip));
  }

  function render(s) {
    renderHeader(s.header);
    priceChart('input-price', 'input', s.prices);
    priceChart('output-price', 'output', s.prices);
    trafficChart('requests', 'requests', s.traffic, fmtInt);
    trafficChart('tokens', 'tokens', s.traffic, fmtCount);
    hardwareChart(s.hardware);
    instancesChart(s.instances);
  }

  // Every load gets a generation; only the newest one may render or report an error,
  // so a slow response for an earlier range never overwrites a newer selection.
  let generation = 0;

  async function load() {
    const mine = ++generation;
    const error = document.getElementById('load-error');
    try {
      const series = await getJson(urls.series, { model, range: state.range });
      if (mine !== generation) return;
      render(series);
      document.getElementById('updated').textContent = `updated ${fmtTime(series.generated_at)}`;
      error.classList.add('d-none');
    } catch (e) {
      if (mine !== generation) return;
      error.textContent = `Could not load the model data: ${e.message}`;
      error.classList.remove('d-none');
    }
  }

  document.querySelectorAll('.range-btn').forEach(btn => btn.addEventListener('click', () => {
    state.range = btn.dataset.range;
    document.querySelectorAll('.range-btn').forEach(b => b.setAttribute('aria-pressed', String(b === btn)));
    const url = new URL(window.location.href);
    url.searchParams.set('range', state.range);
    window.history.replaceState(null, '', url);
    document.getElementById('back-link').setAttribute('href', `${urls.overview}?range=${encodeURIComponent(state.range)}`);
    load();
  }));

  load();
  setInterval(load, refreshMs);
})();
