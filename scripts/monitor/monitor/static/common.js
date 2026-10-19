// Helpers shared by the overview and model pages: formatting, DOM building, status strips and fetch.
(function () {
  'use strict';

  const STATUS_TEXT = { ok: 'ok', degraded: 'degraded', down: 'down', unknown: 'no data' };
  const STATUS_COLOR = { ok: '#1d7a3e', degraded: '#b5300c', down: '#6b0f0f', unknown: '#c8c8c8' };
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

  // The server sends ok/degraded/down/unknown; anything else would be a status that reported
  // but was not ok, so it is drawn as degraded, the way uptime counts it.
  const drawn = s => STATUS_COLOR[s] ? s : 'degraded';

  function strip(statuses) {
    const counts = {};
    statuses.forEach(s => { counts[drawn(s)] = (counts[drawn(s)] || 0) + 1; });
    const summary = Object.entries(counts).map(([s, n]) => `${n} ${STATUS_TEXT[s]}`).join(', ');
    const node = el('div', { class: 'strip', role: 'img', 'aria-label': `Status buckets: ${summary}`, title: summary });
    statuses.forEach(s => node.append(el('span', { class: `strip-cell s-${drawn(s)}` })));
    return node;
  }

  async function getJson(url, params) {
    const r = await fetch(`${url}?${new URLSearchParams(params)}`, { headers: { Accept: 'application/json' } });
    if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
    return r.json();
  }

  window.MonitorCommon = {
    STATUS_TEXT, STATUS_COLOR, dash, isNum, fmtInt, fmtFix, fmtPct, fmtMs, fmtTime, fmtCount, el, marker, strip, getJson,
  };
})();
