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
  const COLOR = {
    ours: '#13294B', median: '#b5300c', band: 'rgba(181, 48, 12, 0.15)', bar: '#13294B', estimated: 'rgba(19, 41, 75, 0.35)', line: '#1d7a3e', cost: '#c8102e',
    input: '#4b8bbe', inputEstimated: 'rgba(75, 139, 190, 0.35)', combined: '#9aa0a6', combinedEstimated: 'rgba(154, 160, 166, 0.4)', inputAverage: '#1d7a3e',
  };
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

  // From 7d up one point per local day; below that one point per bucket, drawn as a stepped line.
  function priceChart(id, side, prices, daily) {
    const labels = prices.map(p => daily ? dayLabel(p.day) : timeLabel(p.t));
    const pick = key => prices.map(p => p[`${side}_${key}`]);
    const line = daily ? { pointRadius: 2 } : { stepped: true, pointRadius: 0 };
    const band = daily ? {} : { stepped: true };
    const scales = { y: { beginAtZero: true } };
    if (!daily) scales.x = { ticks: { maxTicksLimit: 8, maxRotation: 0 } };
    draw(id, {
      type: 'line',
      data: {
        labels,
        datasets: [
          { label: 'models.dev p5', data: pick('p5'), borderWidth: 0, pointRadius: 0, fill: false, ...band },
          { label: 'models.dev p5–p95', data: pick('p95'), borderWidth: 0, pointRadius: 0, backgroundColor: COLOR.band, fill: '-1', ...band },
          { label: 'models.dev median', data: pick('median'), borderColor: COLOR.median, borderDash: [6, 4], ...line },
          { label: 'ours', data: prices.map(p => p[`${side}_price`]), borderColor: COLOR.ours, borderWidth: 3, ...line },
        ],
      },
      options: { plugins: { legend: { labels: { filter: item => item.text !== 'models.dev p5' } } }, scales },
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

  // ±the largest magnitude drawn on one side of the tokens chart, so zero sits in the vertical
  // centre; ±1 when nothing on that side is non-zero.
  function mirroredBounds(values) {
    const max = Math.max(0, ...values.filter(isNum).map(Math.abs));
    return max > 0 ? { min: -max, max } : { min: -1, max: 1 };
  }

  // A round step of about `span / 4`: 1, 2 or 5 × a power of ten.
  function niceStep(span) {
    const power = 10 ** Math.floor(Math.log10(span / 4));
    return [1, 2, 5, 10].map(m => m * power).find(step => step >= span / 4);
  }

  // The left axis's own ticks on the upper half, plus round input values on the lower half,
  // placed where the input axis draws them (`-v × outMax / inMax` in left-axis units).
  function mirroredTicks(ticks, outMax, inMax) {
    const upper = ticks.filter(t => t.value >= 0);
    const step = Math.max(1, niceStep(inMax));
    const lower = [];
    for (let v = step; v <= inMax * (1 + 1e-9); v += step) lower.push({ value: -v * outMax / inMax });
    return [...upper, ...lower];
  }

  // Output above the centre line, input below it (as negative values), each on its own axis
  // symmetric around zero, so each side fills its own half on its own scale; both are labelled
  // on the left. A point without the split (older data or an older Lumen) shows its combined count
  // in grey on the input side. Axes and the tooltip show absolute counts.
  function tokensChart(traffic) {
    const daily = traffic.unit === 'day';
    const points = traffic.points;
    const split = p => isNum(p.input_tokens) || isNum(p.output_tokens);
    const neg = v => (isNum(v) ? -v : null);
    const { input_tokens: avgIn, output_tokens: avgOut } = traffic.average;
    const output = points.map(p => p.output_tokens ?? null);
    const input = points.map(p => neg(p.input_tokens));
    const combined = points.map(p => (split(p) ? null : neg(p.tokens)));
    const out = mirroredBounds([...output, avgOut]);
    const inn = mirroredBounds([...input, avgIn, ...combined]);
    const shade = (base, light) => points.map(p => (p.estimated ? light : base));
    const bar = { grouped: false, barPercentage: 1, categoryPercentage: daily ? 0.8 : 1, order: 1 };
    const average = { type: 'line', borderDash: [4, 4], pointRadius: 0, order: 0 };
    draw('tokens', {
      type: 'bar',
      data: {
        labels: points.map(p => daily ? dayLabel(p.t) : timeLabel(p.t)),
        datasets: [
          { label: 'output (above zero)', data: output, yAxisID: 'out', backgroundColor: shade(COLOR.bar, COLOR.estimated), ...bar },
          { label: 'input (below zero)', data: input, yAxisID: 'in', backgroundColor: shade(COLOR.input, COLOR.inputEstimated), ...bar },
          { label: 'tokens (no split)', data: combined, yAxisID: 'in', backgroundColor: shade(COLOR.combined, COLOR.combinedEstimated), ...bar },
          { label: 'output average', data: points.map(() => avgOut ?? null), yAxisID: 'out', borderColor: COLOR.median, ...average },
          { label: 'input average', data: points.map(() => neg(avgIn)), yAxisID: 'in', borderColor: COLOR.inputAverage, ...average },
        ],
      },
      options: {
        plugins: {
          legend: { labels: {
            filter: item => item.text !== 'tokens (no split)' || combined.some(isNum),
            sort: (a, b) => a.datasetIndex - b.datasetIndex,
          } },
          tooltip: {
            filter: item => isNum(item.parsed.y),
            itemSort: (a, b) => a.datasetIndex - b.datasetIndex,
            callbacks: {
              label: ctx => `${ctx.dataset.label}: ${fmtCount(Math.abs(ctx.parsed.y))}`,
              afterBody: items => (points[items[0]?.dataIndex]?.estimated ? 'estimated or not final' : ''),
            },
          },
        },
        scales: {
          x: { ticks: { maxTicksLimit: 12 } },
          // All labels sit on the left: output counts on the upper half, input counts on the
          // lower half at round input values, mapped onto the left axis. The right axis only
          // scales the input datasets. One grid with a stronger centre line.
          out: { position: 'left', ...out,
            afterBuildTicks: axis => { axis.ticks = mirroredTicks(axis.ticks, out.max, inn.max); },
            ticks: { precision: 0, callback: v => fmtCount(v >= 0 ? v : -v * inn.max / out.max) },
            grid: { color: ctx => (ctx.tick?.value === 0 ? 'rgba(0, 0, 0, 0.5)' : 'rgba(0, 0, 0, 0.1)') } },
          in: { position: 'left', ...inn, display: false },
        },
      },
    });
    document.getElementById('tokens-note').textContent = tokensNote(traffic);
  }

  function tokensNote(traffic) {
    const { input_tokens: avgIn, output_tokens: avgOut } = traffic.average;
    const per = traffic.unit === 'day' ? 'per day' : 'per minute, averaged per bucket';
    const estimated = traffic.points.some(p => p.estimated) ? ' · lighter bars are estimated or not final' : '';
    return `${per} · average out ${fmtCount(avgOut)} / in ${fmtCount(avgIn)}${estimated}`;
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
            const lines = [];
            if (p?.partial && isNum(p.benefit)) lines.push('day not over: benefit projected to the whole day');
            if (p?.estimated) lines.push('estimated or not final');
            return lines;
          },
        } } },
        scales: { y: { beginAtZero: true, title: { display: true, text: '$/day' } } },
      },
    });
    const { benefit, cost, partial, days } = hardware.range;
    const note = isNum(benefit)
      ? `benefit ${fmtUsd(benefit)} / cost ${fmtUsd(cost)} over the ${days} day${days === 1 ? '' : 's'} with token counts${partial ? ' · lighter bars are estimated or not final' : ''}`
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
    priceChart('input-price', 'input', s.prices, s.daily);
    priceChart('output-price', 'output', s.prices, s.daily);
    trafficChart('requests', 'requests', s.traffic, fmtInt);
    tokensChart(s.traffic);
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

  // Exposed for the node tests.
  window.MonitorDetail = { mirroredBounds, mirroredTicks };

  load();
  setInterval(load, refreshMs);
})();
