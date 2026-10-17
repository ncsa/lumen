# Browser OAuth example

A plain HTML + JavaScript page that gets a Lumen API key with the OAuth
**authorization-code + PKCE** flow, keeps it in `localStorage`, and shows:

- the key's usage (from `GET /v1/usage`), and
- a table of the models the key can use, with prices and recent request counts
  (from `GET /v1/models`).

It has no build step and no dependencies. The OAuth and API logic is in
`lumen-oauth.js`, an ES module you can copy into your own app; `index.html` and
`app.js` are only the demo page around it.

| File | What it is |
|------|------------|
| `lumen-oauth.js` | The reusable module: login, redirect handling, key storage, API calls |
| `index.html` | The demo page (markup and styles) |
| `app.js` | Wires the module to the page; uses only the module's public exports |

## Run it

1. Start Lumen locally (see [`scripts/README.md`](../README.md)): `uv run dummy`
   in one terminal and the app on `http://localhost:5001` with
   `dev.config.yaml` in another.
2. Serve this directory on its own origin:

   ```bash
   cd scripts/web-oauth
   python -m http.server 8000
   ```

3. Open <http://localhost:8000/> and click **Connect to Lumen**.

To point it at another Lumen, change `baseUrl` at the top of `app.js`. The
`redirect_uri` defaults to the page's own URL (`http://localhost:8000/` here).
Lumen accepts any `https` redirect URI, or `http` to `localhost` /
`127.0.0.1` for development, so serve the page from one of those.

The page must be served over HTTP: opening `index.html` as a `file://` URL
does not work, because browsers do not load ES modules from files and Lumen
cannot redirect back to one.

## The module

```js
import { createLumenClient, LumenError } from "./lumen-oauth.js";

const lumen = createLumenClient({
  baseUrl: "https://lumen.example.edu", // your Lumen server
  clientId: "my-web-app",               // label shown on the consent page
  keyName: "my-web-app-key",            // name of the key on the user's Profile page
  // redirectUri: defaults to the current page without query or fragment
  // storageKey:  localStorage key for the API key, default "lumen-api-key"
});

await lumen.handleRedirect();           // finish a login if Lumen just sent us back
if (!lumen.getKey()) {
  connectButton.onclick = () => lumen.login();
} else {
  const models = await lumen.getModels();
}
```

| Export | What it does |
|--------|--------------|
| `createLumenClient(options)` | Returns a client bound to one Lumen server and one key name |
| `client.login()` | Creates the PKCE verifier and `state`, keeps them in `sessionStorage`, and redirects to `/oauth/authorize` |
| `client.handleRedirect()` | On the page Lumen redirected back to: checks `state`, removes `code`/`state` from the URL, redeems the code at `/oauth/token`, and stores the key in `localStorage`. Returns the key, or `null` on a normal page load. Throws `LumenError` if the user denied or `state` does not match |
| `client.getKey()` | The stored key, or `null` |
| `client.logout()` | Forgets the key in this browser (it is **not** revoked on the server) |
| `client.api(path, init)` | `fetch(baseUrl + path, init)` with `Authorization: Bearer <key>`. Returns the `Response` when it is ok. Throws `LumenError` otherwise; a 401 also clears the stored key, and a 429 sets `error.retryAfter` from `Retry-After` |
| `client.getModels()` | `GET /v1/models`, returns the `data` array |
| `client.getUsage()` | `GET /v1/usage`, returns the usage object |
| `LumenError` | Error with `status` (HTTP status, 0 when there was no response), `code` and `retryAfter` |

## The flow, step by step

1. **Connect** calls `login()`. It makes a random 64-character `code_verifier`,
   hashes it with SHA-256 into the `code_challenge`, and makes a random `state`.
   The verifier and state go into `sessionStorage` (this tab only).
2. The browser goes to `/oauth/authorize` with `client_id`, `name` (the key
   name), `redirect_uri`, `state` and `code_challenge`.
3. The user signs in to Lumen if needed, sees the consent page (your
   `client_id` under an *Unverified application* banner, the destination, and
   the models), and clicks **Approve** or **Deny**.
4. Lumen redirects back to `redirect_uri` with `?code=…&state=…`, or
   `?error=access_denied&state=…` on Deny.
5. `handleRedirect()` removes `code` and `state` from the address bar, then
   checks that `state` is the one saved in step 1. A mismatch means this tab
   did not start the login, so it stops with an error.
6. It POSTs the code and the original `code_verifier` to `/oauth/token`. The
   code works once and only for 60 seconds, and only with the verifier whose
   hash Lumen saw in step 2.
7. The response holds the new API key (`access_token`). Lumen shows it only
   this once, so the module stores it in `localStorage` right away.
8. From then on, `api()` sends the key as a Bearer token on every `/v1`
   request. Reloading the page reuses the stored key, so no new login is
   needed.

The `/oauth/token` endpoint and the whole `/v1` API send CORS headers
(`Access-Control-Allow-Origin: *`), which is why a page on another origin can
call them. See [CORS](../../docs/guides/api-reference.md#cors-browser-apps).

## Why the key is stored, and what that exposes

The key is kept in `localStorage` so the user connects once and the page keeps
working across reloads and browser restarts, which is what most apps want.

The trade-off: **any script that runs on this page's origin can read
`localStorage`**. A cross-site scripting (XSS) bug, or a compromised
third-party script, can steal the key and use it until it is revoked. So:

- keep the origin free of untrusted scripts, and never put server data into
  the page with `innerHTML` (the demo uses `textContent`);
- **Forget key** (`logout()`) only removes the key from this browser; the key
  still works;
- to really stop a key, revoke it on the Lumen **Profile** page (`/profile`,
  **API Keys**, **Revoke**). The next request with it gets a 401, and the page
  removes it and shows **Connect** again.

If your app only needs a key once (for example, to show it for copying), do
not store it at all; see the minimal example in
[OAuth for Client Developers](../../docs/guides/oauth-clients.md#minimal-browser-example).

## What the numbers mean

- **Model request counts are global.** The *last hour* and *last day* columns
  count requests to that model from **all users**, not just this key. Lumen
  caches them for 30 seconds.
- **Usage is lifetime totals for this key**: requests, tokens and cost since
  the key was created. `Coins available` is the balance of the account that
  owns the key; *unlimited* and *none configured* are shown when the account
  has no coin limit or no coin pool.
- **Prices** are per million tokens, as the Lumen admin set them. Usage is
  charged in the same unit, as coins.
- Lumen has no per-key or per-user hour/day/month windows in the API yet, so
  the page does not show them.

## Key names and the overwrite prompt

Each key has a name (`keyName`). If the user already has a key with that name
(for example, they connected before and then cleared their browser), the
consent page asks them to tick **Replace the secret of key …** before
approving. Approving then keeps that key, with its name and usage stats, and
gives it a new secret when your page collects it; the old secret stops working
at that point. If the key was revoked in the meantime, a new key with the same
name is issued instead. This is normal;
use a `keyName` that identifies your app so users recognize it.

## Calling chat with the same key

The demo has no chat UI, but the key works for every `/v1` endpoint,
including `POST /v1/chat/completions`. Paste this into the browser console on
the example page after connecting (replace `dummy` with a model from the
table):

```js
const { createLumenClient } = await import("./lumen-oauth.js");
const lumen = createLumenClient({
  baseUrl: "http://localhost:5001",
  clientId: "lumen-web-oauth-example",
  keyName: "web-oauth-example",
});
const resp = await lumen.api("/v1/chat/completions", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    model: "dummy",
    messages: [{ role: "user", content: "Hello from the browser" }],
  }),
});
console.log((await resp.json()).choices[0].message.content);
```

Or with the official OpenAI JS SDK, which needs `dangerouslyAllowBrowser`
because the key is visible to the page (see above):

```js
const { default: OpenAI } = await import("https://esm.sh/openai@7.32.0");
const openai = new OpenAI({
  baseURL: "http://localhost:5001/v1",
  apiKey: localStorage.getItem("lumen-api-key"),
  dangerouslyAllowBrowser: true,
});
const reply = await openai.chat.completions.create({
  model: "dummy",
  messages: [{ role: "user", content: "Hello from the browser" }],
});
console.log(reply.choices[0].message.content);
```
