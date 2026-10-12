// lumen-oauth.js — a small, dependency-free ES module that gets a Lumen API key
// in the browser with the OAuth authorization-code + PKCE flow, keeps it in
// localStorage, and calls the Lumen /v1 API with it.
//
// Usage:
//
//   import { createLumenClient } from "./lumen-oauth.js";
//   const lumen = createLumenClient({
//     baseUrl: "https://lumen.example.edu",
//     clientId: "my-web-app",
//     keyName: "my-web-app-key",
//   });
//   await lumen.handleRedirect();          // finish a login if we just came back
//   if (!lumen.getKey()) lumen.login();    // otherwise start one
//   const models = await lumen.getModels();
//
// See README.md next to this file for the full walk-through.

// Thrown for every failure the caller may want to handle: a denied or
// tampered login, a failed token exchange, or an error response from /v1.
//   status      HTTP status (0 when no response was involved, e.g. a state mismatch)
//   code        machine-readable error code when the server sent one
//   retryAfter  seconds to wait, from the Retry-After header of a 429
export class LumenError extends Error {
  constructor(message, { status = 0, code = null, retryAfter = null } = {}) {
    super(message);
    this.name = "LumenError";
    this.status = status;
    this.code = code;
    this.retryAfter = retryAfter;
  }
}

// Base64url without padding, the encoding PKCE (RFC 7636) uses for both the
// verifier and the challenge.
function base64url(bytes) {
  let s = "";
  new Uint8Array(bytes).forEach((b) => (s += String.fromCharCode(b)));
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function randomString(byteLength) {
  return base64url(crypto.getRandomValues(new Uint8Array(byteLength)));
}

async function sha256(text) {
  return crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
}

/**
 * Create a client bound to one Lumen server and one key.
 *
 * @param {object} options
 * @param {string} options.baseUrl     Lumen URL, e.g. "https://lumen.example.edu"
 * @param {string} options.clientId    label for your app, shown on the consent page ([A-Za-z0-9._-])
 * @param {string} options.keyName     name the new API key gets on the user's Profile page
 * @param {string} [options.redirectUri]  where Lumen sends the browser back; defaults to
 *                                     this page without query or fragment. Must be https,
 *                                     or http to localhost.
 * @param {string} [options.storageKey]   localStorage key that holds the API key
 */
export function createLumenClient({
  baseUrl,
  clientId,
  keyName,
  redirectUri = location.origin + location.pathname,
  storageKey = "lumen-api-key",
}) {
  const base = baseUrl.replace(/\/+$/, "");
  // The PKCE verifier and state only need to survive one round trip to Lumen
  // and back in this tab, so they live in sessionStorage, not localStorage.
  const pendingKey = storageKey + ":pending";

  function getKey() {
    return localStorage.getItem(storageKey);
  }

  // Forget the key in this browser. The key itself stays valid on the server
  // until the user revokes it on their Lumen Profile page.
  function logout() {
    localStorage.removeItem(storageKey);
  }

  // Step 1: send the browser to Lumen's consent page.
  async function login() {
    const verifier = randomString(48); // 64 characters, within PKCE's 43-128
    const challenge = base64url(await sha256(verifier));
    const state = randomString(16); // ties the answer to this request (CSRF guard)
    sessionStorage.setItem(pendingKey, JSON.stringify({ verifier, state }));

    const query = new URLSearchParams({
      response_type: "code",
      client_id: clientId,
      name: keyName,
      redirect_uri: redirectUri,
      state,
      code_challenge: challenge,
      code_challenge_method: "S256",
    });
    location.assign(`${base}/oauth/authorize?${query}`);
  }

  // Step 2: on the page Lumen redirected back to, turn ?code=… into a key.
  // Returns the new key, or null when the URL is not an OAuth redirect
  // (a normal page load). Throws LumenError when the user denied, the state
  // does not match, or the code could not be redeemed.
  async function handleRedirect() {
    const params = new URLSearchParams(location.search);
    if (!params.has("code") && !params.has("error")) return null;
    const answer = new URLSearchParams(params); // keep a copy before we strip the URL

    // Read and clear the pending login first, so it can never be used twice.
    const pending = JSON.parse(sessionStorage.getItem(pendingKey) || "null");
    sessionStorage.removeItem(pendingKey);

    // Strip code/state from the address bar and history: the code is a
    // one-time secret and must not end up in bookmarks or Referer headers.
    params.delete("code");
    params.delete("state");
    params.delete("error");
    params.delete("error_description");
    const rest = params.toString();
    history.replaceState(null, "", location.pathname + (rest ? `?${rest}` : "") + location.hash);

    // The state must be the one we generated in login(). Anything else means
    // the redirect was not started by this tab (or was tampered with).
    if (!pending || answer.get("state") !== pending.state) {
      throw new LumenError("Login failed: state mismatch (the redirect was not started here)", {
        code: "state_mismatch",
      });
    }
    // Lumen redirects with ?error=access_denied when the user clicks Deny.
    if (answer.has("error")) {
      const code = answer.get("error");
      const message = code === "access_denied" ? "Access was denied on the Lumen consent page" : code;
      throw new LumenError(message, { code });
    }

    // Step 3: redeem the one-time code (valid for 60 s) for the API key.
    // /oauth/token sends CORS headers, so this cross-origin POST is allowed.
    const resp = await fetch(`${base}/oauth/token`, {
      method: "POST",
      body: new URLSearchParams({
        grant_type: "authorization_code",
        code: answer.get("code"),
        client_id: clientId,
        redirect_uri: redirectUri,
        code_verifier: pending.verifier, // proves we are the tab that started login()
      }),
    });
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok || !body.access_token) {
      throw new LumenError(`Token exchange failed: ${body.error_description || body.error || resp.status}`, {
        status: resp.status,
        code: body.error || null,
      });
    }

    // The key is shown exactly once, so store it now. localStorage keeps it
    // across reloads and browser restarts; see README.md for the trade-off.
    localStorage.setItem(storageKey, body.access_token);
    return body.access_token;
  }

  // Fetch a /v1 path with the stored key as a Bearer token. Returns the
  // Response when it is ok (so you can read JSON or a stream yourself).
  // A 401 means the key was revoked or deleted: it is removed from storage.
  // A 429 carries Retry-After, exposed as error.retryAfter (seconds).
  async function api(path, init = {}) {
    const key = getKey();
    if (!key) throw new LumenError("Not connected to Lumen", { status: 401 });

    const headers = new Headers(init.headers);
    headers.set("Authorization", `Bearer ${key}`);
    const resp = await fetch(base + path, { ...init, headers });
    if (resp.ok) return resp;

    // /v1 errors follow the OpenAI shape: {"error": {"message", "type", "code"}}
    const body = await resp.json().catch(() => ({}));
    const message = body.error?.message || `${resp.status} ${resp.statusText}`;
    if (resp.status === 401) logout();
    const retry = resp.headers.get("Retry-After");
    throw new LumenError(message, {
      status: resp.status,
      code: body.error?.code || body.error?.type || null,
      retryAfter: retry === null ? null : Number(retry),
    });
  }

  // GET /v1/models → array of models the key may use, with prices, health
  // and request counts (global across all users) for the last minute/hour/day.
  async function getModels() {
    return (await (await api("/v1/models")).json()).data;
  }

  // GET /v1/usage → lifetime totals for this key, plus the account's coins left.
  async function getUsage() {
    return (await api("/v1/usage")).json();
  }

  return { login, handleRedirect, getKey, logout, api, getModels, getUsage };
}
