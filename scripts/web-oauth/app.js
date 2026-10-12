// app.js — the demo page. Everything OAuth- and API-related comes from the
// public exports of lumen-oauth.js; this file only wires it to the page.

import { createLumenClient } from "./lumen-oauth.js";

// ---- Configuration: change these for your own Lumen and app. -------------
const lumen = createLumenClient({
  baseUrl: "http://localhost:5001",     // your Lumen server
  clientId: "lumen-web-oauth-example",  // shown on the consent page
  keyName: "web-oauth-example",         // key name on the user's Profile page
  // redirectUri defaults to this page (e.g. http://localhost:8000/), and
  // storageKey to "lumen-api-key".
});

// ---- Page elements ---------------------------------------------------------
const $ = (id) => document.getElementById(id);
const connectBtn = $("connect");
const forgetBtn = $("forget");
const dataSection = $("data");

const setStatus = (text) => ($("status").textContent = text);
const setError = (text) => ($("error").textContent = text);

const money = new Intl.NumberFormat(undefined, { style: "currency", currency: "USD", maximumFractionDigits: 4 });
const count = new Intl.NumberFormat();

// Show Connect when there is no key, otherwise Forget key and the data.
function showConnected(connected) {
  connectBtn.hidden = connected;
  forgetBtn.hidden = !connected;
  dataSection.hidden = !connected;
}

// ---- Rendering -------------------------------------------------------------

function renderUsage(usage) {
  // coins_available: -2 means unlimited, null means no coin pool is configured.
  const coins =
    usage.coins_available === -2 ? "unlimited"
    : usage.coins_available === null ? "none configured"
    : count.format(usage.coins_available);
  const rows = [
    ["Requests", count.format(usage.requests)],
    ["Input tokens", count.format(usage.input_tokens)],
    ["Output tokens", count.format(usage.output_tokens)],
    ["Cost (coins)", count.format(usage.cost)],
    ["Coins available", coins],
    // last_used_at is UTC; toLocaleString shows it in the user's time zone.
    ["Last used", usage.last_used_at ? new Date(usage.last_used_at).toLocaleString() : "never"],
  ];
  const dl = $("usage");
  dl.replaceChildren();
  for (const [label, value] of rows) {
    const dt = document.createElement("dt");
    const dd = document.createElement("dd");
    dt.textContent = label;
    dd.textContent = value;
    dl.append(dt, dd);
  }
}

function renderModels(models) {
  const tbody = $("models");
  tbody.replaceChildren();
  for (const m of models) {
    // Aliases are listed as their own entries, with the real model in `parent`.
    const name = m.parent ? `${m.id} (alias of ${m.parent})` : m.id;
    const cells = [
      [name, ""],
      [m.status, ""], // "ok", "degraded" or "down", as text, not only colour
      [money.format(m.input_cost_per_million), "num"],
      [money.format(m.output_cost_per_million), "num"],
      [count.format(m.requests.last_hour), "num"],
      [count.format(m.requests.last_day), "num"],
    ];
    const tr = document.createElement("tr");
    cells.forEach(([text, cls], i) => {
      // The first cell is a row header, so screen readers announce the model name.
      const cell = document.createElement(i === 0 ? "th" : "td");
      if (i === 0) cell.scope = "row";
      if (cls) cell.className = cls;
      cell.textContent = text; // textContent, never innerHTML: server data is not HTML
      tr.append(cell);
    });
    tbody.append(tr);
  }
}

// ---- Loading data ----------------------------------------------------------

async function loadData() {
  setStatus("Loading usage and models…");
  try {
    const [usage, models] = await Promise.all([lumen.getUsage(), lumen.getModels()]);
    renderUsage(usage);
    renderModels(models);
    showConnected(true);
    setStatus(`Connected. ${models.length} models loaded.`);
    $("usage-heading").focus(); // move focus to the new content
  } catch (err) {
    setStatus("");
    if (err.status === 401) {
      // lumen.api() has already removed the stored key.
      setError("Your Lumen key is no longer valid (it may have been revoked). Connect again.");
      showConnected(false);
      connectBtn.focus();
    } else if (err.status === 429) {
      setError(`Lumen is rate limiting this key. Try again in ${err.retryAfter ?? "a few"} seconds.`);
    } else {
      setError(`Could not load data from Lumen: ${err.message}`);
    }
  }
}

// ---- Buttons ---------------------------------------------------------------

connectBtn.addEventListener("click", () => {
  setError("");
  setStatus("Redirecting to Lumen…");
  lumen.login();
});

forgetBtn.addEventListener("click", () => {
  lumen.logout();
  showConnected(false);
  setError("");
  setStatus("Key forgotten in this browser. It still works until you revoke it on your Lumen Profile page.");
  connectBtn.focus();
});

// ---- Start-up --------------------------------------------------------------

async function start() {
  try {
    // If Lumen just sent us back with ?code=… (or ?error=…), finish the login.
    if (await lumen.handleRedirect()) setStatus("Connected to Lumen.");
  } catch (err) {
    setError(err.message);
  }

  if (lumen.getKey()) {
    await loadData();
  } else {
    showConnected(false);
  }
}

start();
