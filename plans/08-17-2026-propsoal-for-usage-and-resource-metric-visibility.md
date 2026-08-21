# Proposal — Usage and Resource-Metric Visibility

**Date:** 2026-08-17
**Status:** Pre-plan / meta-plan. This is a proposal about *how* and *why*, not an implementation.
**Scope:** Operator and user visibility into resource usage and contention — queueing, rejection,
latency and wait — for a bursty, classroom-shaped workload (~300 students hitting one model when
a class starts).

> **Explicitly out of scope for the work currently in flight.** The disconnect/abort/timeout work
> tracked in `08-17-2026-ServiceEdgeCases-plan.md` ships independently. Nothing here should be
> implemented as part of it. This document is forward-looking and is written to be *consistent
> with* that work's end state (client-disconnect detection, `request_logs.aborted`,
> `lumen_stream_aborts_total{source,reason}`, bounded upstream calls) rather than to duplicate or
> revise it.

**Reading conventions used throughout:**

- **[FACT]** — verified behaviour of the code as it exists today, with `file:line`.
- **[REC]** — a recommendation. Debatable.
- **[OPEN]** — a question this document deliberately leaves to the team.

---

## Table of Contents

1. [Executive summary](#1-executive-summary)
2. [What exists today — inventory](#2-what-exists-today--inventory)
3. [Where does queueing actually happen?](#3-where-does-queueing-actually-happen)
4. [Defining "wait time" concretely](#4-defining-wait-time-concretely)
5. [Answering the four asks](#5-answering-the-four-asks)
6. [Unique users waiting — the cardinality problem](#6-unique-users-waiting--the-cardinality-problem)
7. [Bursty load — what breaks first, and how to tell during the incident](#7-bursty-load--what-breaks-first-and-how-to-tell-during-the-incident)
8. [Redis or not](#8-redis-or-not)
9. [Retention, cardinality and the storage plan](#9-retention-cardinality-and-the-storage-plan)
10. [The cost of measurement](#10-the-cost-of-measurement)
11. [Presentation — where new views live](#11-presentation--where-new-views-live)
12. [Additional instrumentation worth having](#12-additional-instrumentation-worth-having)
13. [SLIs, SLOs and alerts](#13-slis-slos-and-alerts)
14. [Phased plan](#14-phased-plan)
15. [Risks, gaps, and what could not be determined](#15-risks-gaps-and-what-could-not-be-determined)

---

## 1. Executive summary

Lumen already has more observability infrastructure than a project of its size usually does: a
Prometheus endpoint with a DB-backed collector, an HTTP middleware with counters and a latency
histogram, a per-request append-only TimescaleDB hypertable with a continuous aggregate, three
cumulative rollup tables, a forensic pool-checkout tracker, an app-context leak probe, a
self-contained `/metrics/debug` capture, a Chart.js analytics page, and a Locust load-test harness.
**Almost nothing about resource *contention* is visible in any of it.**

The five conclusions that matter:

1. **The queue that will actually bite is invisible and unbounded.** With chart defaults the entire
   deployment serves **10 concurrent requests** (`chart/values.yaml:5,12` — `replicaCount: 1`,
   `wsgiWorkers: 10`; one uvicorn process, `entrypoint.sh:9`). Request 11 onward waits in
   `ThreadPoolExecutor`'s internal work queue
   (`a2wsgi/wsgi.py:158-159`, driven from `lumen/services/wsgi_disconnect.py:285-287`), which is
   unbounded, untimed, unlogged and has no metric. 300 students arriving at once means ~290
   requests parked there. **This is the single most important gap and also the cheapest to close** —
   the enqueue timestamp and the queue depth are both trivially capturable in code Lumen already
   owns and has already subclassed.

2. **Real queue depth for a model is not inferable from Lumen's timing.** vLLM/SGLang run their own
   schedulers with their own waiting queues (`docker-compose.yml.example:82` — `--max-num-seqs 32`).
   From Lumen's side, "queued behind 40 other sequences" and "the model is just slow" are the same
   number. **Recommendation: scrape the backends' own `/metrics`.** Lumen already talks to backend
   management endpoints outside the `/v1` prefix (`lumen/services/model_sync.py:60-65,82`), so the
   pattern and the URL-derivation helper exist. Everything else about upstream queueing is
   *inference*, and should be labelled as such in the UI.

3. **A database is sufficient. Redis is not needed, and should stay optional.** Redis is *not* new
   infrastructure — the package is already installed (`pyproject.toml:17`, `flask-limiter[redis]`;
   `uv.lock` pins redis 7.4.1) and the chart ships four hand-written Redis templates
   (`chart/templates/redis/`, `chart/values.yaml:232-263`, default `enabled: false`). But the
   chart's Redis is a single replica with `strategy: Recreate` and persistence off — it is not
   highly available. Making observability *depend* on it would trade a visibility gap for an
   availability risk. Timescale answers every historical question; process-local memory answers
   every live question correctly at today's topology (1 pod × 1 process). Redis becomes worth it at
   `replicaCount ≥ 2` — at which point it is already required for rate limiting
   (`chart/values.yaml:4`), so it is the same decision, not a new one. **Design every live gauge to
   degrade to per-pod numbers if Redis is absent or down, never to fail.**

4. **The latency histogram Lumen already has is blind to exactly the requests operators care
   about.** `lumen_http_request_duration_seconds` stops its clock when `wsgi_app()` *returns*
   (`lumen/blueprints/metrics/middleware.py:148,168-177`), which for a streaming SSE response is
   before a single token has been fetched. Its top bucket is 10 s. Stream duration exists only in
   `request_logs.duration`. There is a clean fix: the middleware already wraps the response body in
   `_ContextCheckingBody` (`middleware.py:56-96`), whose `close()` is guaranteed to run
   (`a2wsgi/wsgi.py:263-264`, `finally: getattr(iterable, "close", ...)()`). Observing there costs
   nothing and makes the histogram truthful.

5. **Time-to-first-token is already computed and then thrown away for the API.** `llm.py:818-819`
   sets `t_first`; `llm.py:882` returns it; `chat/routes.py:276-278` persists it — but only onto
   `messages` (`lumen/models/message.py:39-43`), the chat-conversation table, which a user can
   delete. `request_logs` has exactly one timing column, `duration` (`request_log.py:64`), and the
   `/v1` API path captures no TTFT at all. **Adding a handful of nullable columns to `request_logs`
   costs zero extra statements on the hot path** (it is the same INSERT) and unlocks nearly every
   historical question asked here, including "how many unique users were waiting for model X at
   09:05".

Everything else in this document follows from those five.

---

## 2. What exists today — inventory

### 2.1 Prometheus surface

**[FACT] Three metric families defined in-process**, all in
`lumen/blueprints/metrics/middleware.py`:

| Metric | Type | Labels | Line |
|---|---|---|---|
| `lumen_http_requests_total` | Counter | `method`, `path_template`, `status` | `:22-26` |
| `lumen_http_request_duration_seconds` | Histogram | `method`, `path_template` | `:27-32` |
| `lumen_stream_aborts_total` | Counter | `source`, `reason` | `:33-37` |

Buckets are `0.005 … 10.0` (`:31`). `path_template` is produced by `_normalize_path`
(`:182-184`), which only collapses `/\d+` → `/{id}`.

**[FACT] Five more families come from a DB-backed collector**, `LumenDBCollector.collect()`
(`lumen/blueprints/metrics/routes.py:47-179`), registered once per app instance
(`lumen/__init__.py:81-85`):

- `lumen_model_requests_total`, `lumen_model_input_tokens_total`, `lumen_model_output_tokens_total`,
  `lumen_model_cost_coins_total`, all `{model, source}`, from a `GROUP BY` over `ModelStat`
  (`routes.py:67-78`).
- `lumen_model_endpoint_healthy{model, endpoint_url}` (`routes.py:116-126`).
- `lumen_users{status}` — two `COUNT(*)` over `entities` (`routes.py:135-140`).
- `lumen_db_pool_connections{state}` — `size`, `checked_in`, `checked_out`, `overflow`,
  `stranded`, `limit` (`routes.py:150-179`), with a near-capacity warning at 80 %
  (`routes.py:170-174`) and a stack-dumping watchdog (`routes.py:175`).

**[FACT] `collect()` queries the database on every scrape**, and explicitly releases its own
session in a `finally` because of a production connection leak fixed in 1.22.0
(`routes.py:58-66,144-145`; `CHANGELOG.md`, 1.22.0 entry).

**[FACT] `/metrics` is gated behind `api.prometheus.enabled` plus a mandatory bearer token**
(`routes.py:28-44`, `lumen/__init__.py:90-98`). If enabled without a token the app disables
Prometheus at startup and logs an error. **When Prometheus is disabled, the HTTP middleware is not
installed at all** (`lumen/__init__.py:99-104`) — so `lumen_http_requests_total` and the abort
counter do not exist either.

**[FACT] `/metrics/debug`** (`routes.py:268-306`) serves a plain-text, self-contained operator
capture: deployment topology, live pool status, session-registry count, scope-key teardown
cross-reference, app-context anomalies, stranded checkouts with retainer chains, and a full thread
dump. Same bearer auth. **This is the closest thing to an operator dashboard that exists, and it is
not rendered anywhere.**

**[FACT] Multi-process aggregation is wired but currently moot.** `/metrics` uses
`MultiProcessCollector` when `PROMETHEUS_MULTIPROC_DIR` is set (`routes.py:191-198`), and
`create_app` sets that env var from `api.prometheus.multiproc_dir` before importing the middleware
(`lumen/__init__.py:99-104`, with the ordering constraint documented at `:88-89`). But
`entrypoint.sh:9` starts uvicorn with **no `--workers`**, so there is one process per pod, and the
chart mounts **no volume** at `multiprocDir` (`chart/templates/deployment.yaml` volumes are config
and themes only). Multi-*replica* aggregation is Prometheus's job, not the app's, and works
normally — provided something scrapes each pod.

**[FACT] Nothing in the chart scrapes anything.** `grep` over `chart/` finds no `ServiceMonitor`,
no `PodMonitor`, no `prometheus.io/*` annotations. Scrape configuration is currently external and
undocumented in-repo.

### 2.2 Per-request storage

**[FACT] `request_logs`** (`lumen/models/request_log.py`) — the append-only per-request table.
Columns: `id` (surrogate BigInt PK, `:30-35`), `time` (`DateTime(timezone=True)`, the Timescale
partition key and the **only** tz-aware timestamp in the codebase, `:37`), `entity_id`,
`model_config_id`, `model_endpoint_id` (all `SET NULL`, `:39-55`), `source` (`chat`|`api`, `:57`),
`input_tokens`, `output_tokens`, `cost`, `audio_seconds`, `duration` (`:58-64`), `aborted`
(`:70-76`).

Three indexes: `time`, `entity_id`, `model_config_id` (`:24-26`). **No index on
`model_endpoint_id`, `source`, or `aborted`.**

**[FACT] `time` is the *completion* timestamp, not the start** — the row is constructed at
`llm.py:554-567` inside `update_stats`, which is only ever called after the upstream call finishes.
There is no "request started" row anywhere, so **an in-flight request is invisible in the
database.**

**[FACT] `duration` is the only timing column**, and it does not mean what an operator would
assume. It is measured from `t0` (`llm.py:752`), which is set *after* the model lookup, endpoint
selection and timeout resolution have already run (`llm.py:725-750`) — so all Lumen preflight time
is excluded. It ends at `llm.py:832`, before the billing commit. And because the generator blocks
inside `yield` when the client reads slowly, **client download time is charged to `duration` and
deflates `output_speed`** as though the backend were slow.

**[FACT] `aborted` has no read site.** It is written (`llm.py:565`, `api/routes.py:457,513,551`) and
never queried, charted or exported. The information is being collected and discarded.

### 2.3 Hypertable, aggregate, retention

**[FACT] Hypertable setup** — `migrations/versions/i9j0k1l2m3n4_timescaledb_tracking.py`:

- PostgreSQL only, gated on dialect (`:31-32`). SQLite gets a plain table (`:74-84`), and the
  migration comment says so outright: "no hypertable (analytics page requires PostgreSQL)".
- `create_hypertable('request_logs', 'time', chunk_time_interval => INTERVAL '7 days')` (`:52`).
- **One continuous aggregate: `request_counts_hourly`** (`:53-67`) —
  `time_bucket('1 hour', time)`, grouped by `bucket, model_config_id, source`, aggregating
  `COUNT(*)`, `SUM(input_tokens)`, `SUM(output_tokens)`, `SUM(cost)`. **No `entity_id` dimension.
  No `duration`. No `audio_seconds`. No `aborted`.**
- Refresh policy (`:68-73`): `start_offset => 3 hours`, `end_offset => 1 hour`,
  `schedule_interval => 1 hour`. **The aggregate therefore lags real time by at least an hour**, and
  rows backfilled more than three hours late are never picked up automatically — which is why
  `seed_analytics.py:131-140` has to call `refresh_continuous_aggregate` manually on an AUTOCOMMIT
  connection.

**[FACT] There is no retention policy and no compression policy anywhere.** `grep` for
`add_retention_policy`, `add_compression_policy`, `compress_chunks`, `drop_chunks` over
`migrations/` and `lumen/` returns nothing. `request_logs` grows without bound and its chunks are
never compressed. The docs *aspire* to both (`docs/architecture.md:463,591`,
`docs/dbschema.md:565`, `lumen/models/request_log.py:15`) but nothing is configured.

**[FACT] Nothing deletes old rows.** `lumen/commands.py` defines exactly two CLI commands
(`init-db`, `reassign-model`); there is no cron, Celery or APScheduler. Background work is three
daemon threads started in `create_app` (`lumen/__init__.py:472-480`): health checker, coin refiller,
config watcher.

### 2.4 Rollups

**[FACT] Three cumulative counter stores, all all-time with no time dimension:**

- `ModelStat` — one row per `(entity_id, model_config_id, source)`
  (`lumen/models/model_stat.py:34`); `requests`, `input_tokens`, `output_tokens`, `audio_seconds`,
  `cost`, `last_used_at`.
- `EntityStat` — one row per entity, PK `entity_id` (`lumen/models/entity_stat.py:20-25`); same
  counters. Exists purely to avoid a `GROUP BY` over `model_stats` (docstring `:13-15`).
- `APIKey` counters (`lumen/models/api_key.py:32-38`), incremented separately by
  `_record_api_key_usage` (`api/routes.py:95-107`) — **not** by `update_stats`, so aborted requests
  are counted in `model_stats`/`entity_stats`/`request_logs` but not per key.

**[FACT] `update_stats` (`llm.py:483-568`) is the single write funnel** and costs **five statements
per request**: a `SELECT` + conditional savepoint `INSERT` for `ModelStat`, an atomic `UPDATE`, the
same pair for `EntityStat`, and the `RequestLog` `INSERT` + `flush()`. Plus `subtract_coins` and
(on the API path) the `APIKey` update in the same transaction.

### 2.5 Per-message performance metrics — the near-miss

**[FACT] TTFT and tokens/sec already exist, for the chat UI only.**

```
llm.py:818-819    if t_first is None: t_first = time.time() - t0     # first *content* delta
llm.py:842        output_speed = output_tokens / duration
llm.py:882-883    "time_to_first_token": t_first or duration, "output_speed": output_speed
chat/routes.py:276-278   persisted onto the Message row
message.py:39-43         time_to_first_token, duration, output_speed (nullable Float)
chat.html:507            rendered as "Speed: N tok/s"
```

Two consequences. First, **the `/v1` API path records no TTFT at all** — API traffic is a latency
blind spot. Second, `t_first` is set inside `if delta.content:` (`llm.py:816`), so
**reasoning/thinking deltas (`llm.py:812-815`) do not stop the clock**: on a reasoning model,
"time to first token" is really time-to-first-*visible*-token and can be tens of seconds late.

### 2.6 Rate limiting and rejection

**[FACT] flask-limiter, keyed per identity, not per IP** — despite the constructor default
(`lumen/extensions.py:10`, `key_func=get_remote_address`), every decorated route overrides
`key_func`: API routes use the API-key id (`api/routes.py:86-88`), chat routes use the session
entity id (`chat/routes.py:60-62`); `request.remote_addr` is only the unauthenticated fallback.
`ProxyFix` is installed (`lumen/__init__.py:49`) so that fallback sees the real client IP.

**[FACT] Default limit `"30 per minute"`**, hardcoded as the fallback in two places
(`api/routes.py:90-92`, `chat/routes.py:65-67`), read from `YAML_DATA` per request so it is
hot-reloaded. Twelve routes are decorated; six of them are chat routes
(`chat/routes.py:72,114,164,306,367,403`) — **page loads, conversation listing and the stream all
draw from the same 30/min budget per user.**

**[FACT] Storage is Redis-or-in-memory** (`lumen/__init__.py:107-114`), with a startup warning when
unset. It is documented as requiring a restart (`lumen/__init__.py:258-260`) but is **absent from
`RESTART_REQUIRED`** in the config watcher (`config_watcher.py:189-197`) — an existing
inconsistency.

**[FACT] Two unrelated conditions both return 429 and are indistinguishable.** The limiter's
handler (`lumen/__init__.py:331-337`) returns `type: rate_limit_error, code: rate_limit_exceeded`;
`check_coin_budget` returns `HTTPStatus.TOO_MANY_REQUESTS, "Coin budget exhausted"`
(`llm.py:477-479`) as an explicit `jsonify(...), code` that bypasses the error handler entirely, so
even the body shape differs. Neither writes a `request_logs` row (the limiter aborts before the
view; `update_stats` never runs). Neither logs. The only trace is
`lumen_http_requests_total{status="429"}` — and only when Prometheus is enabled.

**[FACT] No `Retry-After` header is set on either path.**

### 2.7 Resource-health instrumentation

**[FACT] `lumen/services/db_pool.py`** auto-sizes the connection pool from Postgres
`max_connections`, split across `workers × replicas` (60 % pool / 20 % overflow / 20 % reserved,
`:25-28`). `resolve_wsgi_workers` (`:87-115`) reads `LUMEN_WSGI_WORKERS`: an integer, or `auto` to
derive `pool_size + max_overflow` clamped to 10–64, defaulting to 10.

**[FACT] `lumen/services/pool_tracker.py`** records the endpoint, thread, 25-frame stack and
app-context scope key of **every** pool checkout (`:38`), drops it on check-in, flags checkouts
older than 300 s as stranded (`:41`), and walks the reference graph to name what retains a leaked
connection. Always on, by design.

**[FACT] `lumen/services/ctx_probe.py`** instruments `AppContext.push`/`pop` into a ring buffer of
anomalies (double push, skipped teardown, cross-thread pop).

**[FACT] `lumen/services/health.py`** probes every endpoint every 60 s (`:99`) with
`client.models.list()`, bounded by a 5 s client timeout (`:31`) and a hard 10 s executor deadline
(`:26,60`), on an 8-thread pool. It records **only a boolean** `healthy` and `last_checked_at`
(`:79`) — the probe's own latency is discarded, and a healthy-but-saturated endpoint is
indistinguishable from an idle one.

**[FACT] Endpoint selection is round-robin with no load or latency awareness and no failover**
(`llm.py:295-305`). `_rr_counters` is a module global, so with multiple processes or replicas the
rotation is several independent counters. A failed endpoint returns an error to the client; there
is no retry against a sibling endpoint.

### 2.8 Existing UI

**[FACT] `/usage`** (`profile/routes.py:363-366` → `lumen/templates/usage.html`) is the analytics
page. Chart.js 4 from CDN (`usage.html:4`) — the only charting library in the repo. Four line
charts, one horizontal bar chart, a hand-rolled 7×24 heatmap table, five stat cards, a period
selector (week/month/year/all) and an admin "Show all users" toggle. Data arrives from seven
parallel `fetch()` calls to `/api/usage/*`.

**[FACT] Those endpoints switch data source by scope** (`profile/routes.py:369-683`): org-wide
queries read `request_counts_hourly`; single-entity queries fall back to raw `request_logs`
**because the aggregate has no `entity_id` column**. Every one of them short-circuits to empty on
non-PostgreSQL (`:372,440,471,516,557,601,642`).

**[FACT] `/admin/analytics` is a redirect to `/usage`** (`admin/routes.py:206-209`), leaving
`lumen/templates/admin/analytics.html` (346 lines) orphaned — a near-identical older copy of
`usage.html`. **Prime real estate to reclaim.**

**[FACT] `/models`** (`models.html`) shows a health table: healthy count, last-checked, status badge
(ok/degraded/down/disabled). **`/models/<name>`** (`model_detail.html:79-120`) has an Availability
card with an **admin-only** per-endpoint up/down block (`:101-108`) and live Requests/hr and
Requests/24h — served by **two uncached `COUNT(*)` scans of `request_logs` on every page view**
(`models_page/routes.py:49-60`). By contrast the API's equivalent is cached 30 s per worker
(`api/routes.py:108-146`).

**[FACT] There is no operator/status/system page.** No `admin/status.html`, no pool or thread
information rendered in HTML anywhere.

**[FACT] There is no admin nav partial.** Nav is duplicated across four theme headers
(`themes/{default,illinois,uic,uis}/templates/theme/header.html`); adding a page means editing all
four.

**[FACT] Accessibility patterns are consistent and should be matched**: `<caption
class="visually-hidden">` on every data table, `role="img"` + `aria-label` + fallback text on every
`<canvas>` (`usage.html:82,90,102,110,122`), `aria-sort` + `tabindex="0"` + Enter/Space on sortable
headers (`profile.html:327-350`), `role="progressbar"` with full aria-value attributes, status
conveyed by text as well as colour, `local-datetime` spans converted by `app.js:69-77`.

### 2.9 Deployment topology

**[FACT]** `entrypoint.sh:9` — `uvicorn asgi:app`, single process, no `--workers`.
`asgi.py:10-13` wraps Flask in `DisconnectAwareWSGIMiddleware` with
`workers=resolve_wsgi_workers(...)`. Chart defaults: `replicaCount: 1` (`values.yaml:5`),
`wsgiWorkers: 10` (`values.yaml:12`), `wsgiSendTimeout: 300` (`values.yaml:20`),
`gateway.timeout: "600s"` (`values.yaml:294`). Pod resources default to
`requests cpu 250m / limits cpu 1` (`deployment.yaml:151-161`).

**[FACT] No HPA.** `values.yaml:76-80` documents why: `LUMEN_REPLICAS` is frozen at
`replicaCount`, so autoscaling would break pool auto-sizing unless `poolSize`/`maxOverflow` are
pinned.

**[FACT] Backends are vLLM.** `docker-compose.yml.example:73-150` runs two vLLM containers;
`--max-num-seqs 32` is set explicitly on the first (`:82`). `chart/templates/models-deployment.yaml`
can deploy models in-cluster with a Service. `model_sync.py:60-65,82` already probes SGLang's
`/get_server_info` at the server root (stripping `/v1`) and detects the backend type — but
**the detected `backend` value is never persisted** (`grep` shows it consumed only transiently at
`model_sync.py:332`).

**[FACT] There is a Locust load-test harness** (`loadtesting/`) with a dummy backend, an account
provisioner and a one-command stack (`run_loadtest.sh`, defaults to 500 users). It measures
end-to-end elapsed time only — **no TTFT split** (`loadtesting/locustfile.py:90,96`).

### 2.10 Inventory summary

| Capability | Status |
|---|---|
| Per-request durable log | **Exists** (`request_logs`, hypertable) |
| Per-request start timestamp | **Missing** (`time` is completion) |
| TTFT | **Exists for chat only**, on `messages`, not `request_logs` |
| Queue wait (Lumen's own) | **Missing entirely** |
| Queue depth (Lumen's own) | **Missing entirely** |
| Queue depth (upstream) | **Missing entirely**; not inferable |
| Unique users waiting | **Missing**; derivable once start/TTFT are stored |
| 429 counting | Partial — one undifferentiated `status="429"` label |
| 429 attribution (who/which model/why) | **Missing** |
| Upstream retry visibility | **Missing** (SDK-internal) |
| Stream response time | Partial — `request_logs.duration`, contaminated by client backpressure |
| HTTP latency histogram | **Exists but blind to streams** |
| Abort/disconnect counting | **Exists** (`lumen_stream_aborts_total`, `request_logs.aborted`) |
| Endpoint health | **Exists** (boolean only, no latency) |
| DB pool state | **Exists**, with forensic tracker |
| WSGI thread pool state | **Missing** |
| Retention / compression | **Missing** |
| Entity-dimensioned aggregate | **Missing** |
| Operator UI | **Missing** (only plain-text `/metrics/debug`) |
| Scrape config (ServiceMonitor) | **Missing** |
| Redis | **Available, optional, off by default, not HA** |

---

## 3. Where does queueing actually happen?

Six places. They are not the same thing and they fail differently.

```
  client
    │
    │  ── Q0 ── gateway / ingress accept + 600 s deadline          [external, not measured]
    ▼
  uvicorn event loop  (single process per pod)
    │
    │  ── Q1 ── a2wsgi ThreadPoolExecutor work queue               [THE hidden queue]
    │           max_workers = LUMEN_WSGI_WORKERS (default 10)
    │           overflow queue: unbounded, untimed, unlogged
    ▼
  WSGI worker thread
    │
    │  ── Q2 ── flask-limiter check → 429 (a rejection, not a queue)
    │  ── Q3 ── DB pool checkout for preflight (pool_timeout, default 10 s)
    │
    │           preflight: model lookup, access, consent, coin budget,
    │                      endpoint selection  → connection released
    ▼
  new httpx client per request  (TCP + TLS, connect_timeout 5 s)
    │
    │  ── Q4 ── UPSTREAM SCHEDULER QUEUE                            [invisible to Lumen]
    │           vLLM: running ≤ --max-num-seqs, rest wait
    ▼
  token stream back
    │
    │  ── Q5 ── a2wsgi send_queue (10 slots) → client backpressure
    │           bounded by LUMEN_WSGI_SEND_TIMEOUT (300 s)
    ▼
  client
```

### Q1 — the a2wsgi thread pool. This is the one that matters.

**[FACT]** `a2wsgi/wsgi.py:154-159` constructs `ThreadPoolExecutor(max_workers=workers)`;
`WSGIResponder.__call__` submits each request with `loop.run_in_executor(self.executor, ...)`
(`a2wsgi/wsgi.py:199-202`, reproduced in Lumen's subclass at
`lumen/services/wsgi_disconnect.py:285-287`). `ThreadPoolExecutor`'s internal `_work_queue` is an
**unbounded** `queue.SimpleQueue`. Requests beyond `workers` sit there with no timeout, no depth
limit, no metric and no log line. One streaming request pins one thread for its entire life.

**[FACT]** The disconnect pump is created *before* `run_in_executor`
(`wsgi_disconnect.py:284-285`) and runs on the event loop, so a client that gives up **while
queued** does set the disconnect Event. But nothing checks it before the work item eventually runs:
the queued request still executes its full preflight and upstream call, and the disconnect is only
noticed at the first inter-chunk poll (`llm.py:807`).

**[REC]** Both the depth and the wait are cheap to capture, in a file Lumen already owns and has
already subclassed:

- Stamp `time.monotonic()` into the environ in `_DisconnectAwareWSGIResponder.__call__` just before
  `run_in_executor`; read it in a Flask `before_request` (or in `wsgi()` itself). The difference is
  **exact queue wait**, and it is the number nobody has today.
- Maintain an explicit in-flight/queued counter around the submit rather than reading
  `executor._work_queue.qsize()` — same information, no private API.
- Optionally: if the disconnect Event is already set when the work item starts, short-circuit with a
  499-style response and count it. That is measurable proof of "students gave up while queued", and
  it frees a thread instantly during exactly the incident this document is about.

### Q3 — the DB connection pool

**[FACT]** Preflight touches the DB, then every path deliberately releases the connection before the
upstream call (`chat/routes.py:202`, `api/routes.py:365,440,638`, `llm.py:725-750`). So threads do
**not** hold a connection across the LLM call. Pool wait is bounded by `pool_timeout` (default 10 s,
`config.yaml.example:20`); exhaustion raises and surfaces as a 500.

**[FACT] This creates a contradiction in the auto-sizing heuristic.** `resolve_wsgi_workers`'s
`auto` mode derives the thread count from `pool_size + max_overflow` on the reasoning that "a thread
inside a database call holds a pooled connection" (`db_pool.py:87-96`). For a *streaming proxy* that
is exactly wrong — the threads that matter are the ones parked in an upstream read, holding no
connection at all. `auto` therefore systematically under-provisions threads for the workload Lumen
actually serves. **[OPEN]** Should `auto` be redefined for this app (e.g. threads sized to expected
concurrent streams, pool sized to expected concurrent *DB phases*, which is a much smaller number)?
This is a sizing question, not a metrics question, but the metrics proposed here are what would let
the team answer it with data.

### Q4 — the upstream scheduler. Measurable only by asking the backend.

**[FACT]** vLLM admits up to `--max-num-seqs` sequences into its running batch and queues the rest.
Lumen sees one number — the gap until the first chunk — which conflates: TCP connect, TLS handshake,
HTTP round trip, **time waiting in the backend queue**, prefill/prompt-processing time, and (for a
reasoning model) all the thinking tokens before the first visible content.

**Measurable vs inferable:**

| Quantity | From Lumen alone | From backend `/metrics` |
|---|---|---|
| TTFT (per request, per user) | **Measurable** (already computed) | No — histogram only, no identity |
| Queue wait inside the backend | **Not measurable.** Inferable only as "TTFT is high" | **Measurable** |
| Number of sequences running / waiting | **Not measurable** | **Measurable** |
| KV-cache utilisation | **Not measurable** | **Measurable** |
| Prefix-cache hit rate | **Not measurable** | **Measurable** |
| Which *users* are queued | **Measurable** | No — backend has no user identity |

The two halves are complementary and neither substitutes for the other. Lumen knows *who*; the
backend knows *how deep*.

**[REC] Scrape the backends.** Precedent exists: `model_sync.py:60-65` already strips `/v1` to reach
a backend's management root and `:82` already issues a plain `requests.get` there with a timeout.
Two ways to consume it:

- **Option A — let Prometheus scrape the backends directly.** A ServiceMonitor on the model
  Deployments the chart already renders (`chart/templates/models-deployment.yaml`,
  `models-service.yaml`). Zero Lumen code, full resolution, works with existing Grafana. *Downsides:*
  nothing is visible inside Lumen's own UI; endpoints configured in `config.yaml` that live outside
  the cluster are not covered; joining backend labels to Lumen model names is a Grafana-side chore.
- **Option B — Lumen polls each `model_endpoint`'s `/metrics` itself**, in a background thread
  modelled on `health.py` (bounded executor, hard deadline, best-effort), parsing a short allowlist
  of gauges into memory and re-exporting them through `LumenDBCollector`.
  *Downsides:* another network dependency in the app; parse coupling to backend metric names, which
  churn between versions; polling interval is a compromise between freshness and load.
- **Option C — do nothing and infer from TTFT.** Cheapest. *Downside:* it cannot distinguish "queued
  behind 40 sequences" from "the model is slow today", which is precisely the distinction the
  operators asked for.

**Recommendation: B as primary, A as a free complement, never C alone.** B is what makes queue depth
visible *in Lumen*, durable, and joinable to per-user data; the requirement "was model X queued at
09:05, and how many distinct users were waiting" cannot be answered by Grafana alone because Grafana
does not know who the users are. A costs almost nothing on top and gives high-resolution data for
incident response.

**[OPEN]** Backend metric names must be verified against the deployed versions. The compose example
pins `vllm/vllm-openai:v0.18.0` and `nvcr.io/nvidia/vllm:26.01-py3`; vLLM's metric set has changed
across engine generations, and SGLang's names differ entirely. Treat the specific names as
configuration, not as constants baked into code — an allowlist with a per-backend mapping, resolved
from the `backend` type that `model_sync` already detects but currently discards.

**[OPEN]** Are backend `/metrics` endpoints reachable and unauthenticated from the Lumen pod?
vLLM's `--api-key` protects `/v1` but historically not `/metrics`; network policy may still block
it. Needs checking before committing to Option B.

### Q5 — client backpressure

**[FACT]** `a2wsgi` bounds the send queue at 10 chunks, and Lumen's subclass bounds the wait on it
at `LUMEN_WSGI_SEND_TIMEOUT` (default 300 s, `wsgi_disconnect.py:118`). A slow reader therefore
inflates `request_logs.duration` and deflates `output_speed` without any indication that the cause
was downstream. **[REC]** Accumulate the time spent blocked in `send` into a per-request total and
record it; a `duration` with a large `send_blocked` component is a client problem, not a capacity
problem, and today those look identical.

---

## 4. Defining "wait time" concretely

"Wait" needs to be defined in terms of timestamps the app can actually take. Here they are, with the
exact place each would be captured.

| Mark | Meaning | Where captured | Exists today? |
|---|---|---|---|
| **T0** | ASGI request accepted; responder entered | `wsgi_disconnect.py:_DisconnectAwareWSGIResponder.__call__`, before `run_in_executor` (`:284-287`) | No |
| **T1** | A WSGI worker thread picked the request up | top of `wsgi()` in the worker thread, or a Flask `before_request` | No |
| **T2** | Preflight done; about to call upstream | `llm.py:752` (`t0`) / `api/routes.py:368,462,641` | **Yes** |
| **T3** | Upstream connection established and request sent | httpx event hook, or approximated as T2 | No |
| **T4** | First chunk of any kind received from upstream | inside the `for chunk in stream` loop, before the content check | No |
| **T4c** | First *visible content* delta received | `llm.py:818-819` (`t_first`) | **Yes** (chat only) |
| **T5** | Upstream stream ended | `llm.py:832` (`duration` end) | **Yes** |
| **T6** | Response fully flushed, billing committed | after `update_stats` commit | No |

Derived quantities:

```
local_queue_wait   = T1 - T0     # Lumen's own admission queue.  ← THE missing number
preflight          = T2 - T1     # DB, auth, access, coin budget, endpoint choice
connect            = T3 - T2     # TCP + TLS.  Paid EVERY request (see below)
upstream_wait      = T4 - T3     # backend queue + prefill, CONFLATED
thinking_lag       = T4c - T4    # reasoning tokens before visible output
generation         = T5 - T4     # includes client backpressure (Q5)
response_time      = T5 - T0     # what an operator means by "response time"
user_perceived_wait= T4c - T0    # what a STUDENT means by "wait" — nothing has appeared yet
```

**[REC] Adopt `T4c - T0` as the user-experience SLI** and `T1 - T0` as the
Lumen-controls-this-directly SLI. They answer different questions: the first is what the class
complains about; the second is the part the operator can fix by changing `wsgiWorkers`.

**[FACT] `connect` is paid on every single request.** Every call site constructs a fresh
`openai.OpenAI(...)` inside a `with` (`llm.py:789-790`, `api/routes.py:374,498,645`,
`health.py:31`), so a new httpx pool is created and destroyed per request — no keep-alive, no
connection reuse. **[REC]** Measure it before optimising it; if `connect` is a material share of
TTFT, a long-lived per-endpoint client is a straightforward win, but that is a change with its own
concurrency and credential-rotation implications and should be justified by data.

**[REC] Fix the reasoning-model blind spot** by stamping T4 on the *first chunk of any kind* while
keeping T4c as today's value. `thinking_lag` then becomes a first-class number, and "the model
took 40 s to say anything" stops being indistinguishable from "the model was queued for 40 s".

**[REC] Store, not export, the per-request values.** Prometheus gets histograms
(`{model, source}` labels only); `request_logs` gets the per-request numbers, because that is where
per-user attribution has to live.

---

## 5. Answering the four asks

### 5.1 Queue depth for a particular model — currently and historically

This decomposes into three distinct quantities that are often conflated:

| Quantity | Now | Historically | How |
|---|---|---|---|
| **Waiting for a Lumen thread** | in-memory counter | derivable from `queue_wait` on completed rows | new |
| **In flight to the backend** (admitted, awaiting/receiving tokens) | in-memory gauge per model | derivable from `[T2, T5]` intervals on completed rows | new |
| **Waiting inside the backend scheduler** | scraped gauge | persisted samples | new, from backend `/metrics` |

**[FACT] A hard limitation, stated plainly:** requests still queued at Q1 have not been parsed yet.
Lumen does not know which model they are for — the model name is in the JSON body, which is only
read after a thread picks the request up. **Pre-admission queue depth is therefore inherently
model-agnostic.** Per-model depth is only available for admitted requests.

**[REC]** Expose it honestly rather than faking it:
- `lumen_wsgi_queue_depth` (no model label) — "requests waiting for a worker".
- `lumen_model_inflight{model, phase}` where `phase ∈ {awaiting_first_token, streaming}` — admitted
  requests only.
- `lumen_upstream_queue_depth{model, endpoint}` — scraped from the backend.

Do **not** try to route or peek at the body pre-admission to attribute the Q1 queue to a model. It
would mean parsing an untrusted body outside a request context, on the event loop, before auth —
a bad trade for a number that is a proxy for a number you can get properly.

**Historical reconstruction is a pure SQL query once the timestamps are stored.** With
`queue_wait`, `ttft` and the existing `duration` on each row, the request's interval is
`[time - duration - queue_wait, time]` and its "waiting" sub-interval is
`[start, start + queue_wait + ttft]`. "How many requests for model X were waiting at 09:05" is then
an interval-overlap count over one hypertable — no extra writes, no extra table, no live state.
That is the single strongest argument for putting the timestamps on `request_logs` rather than
inventing a separate queue-events table.

### 5.2 429s / rate-limit rejections and retries

**[FACT]** Today: one undifferentiated `lumen_http_requests_total{status="429"}`, present only if
Prometheus is enabled, with no model, no user, no reason, no log line and no DB row. Limiter
rejections and coin exhaustion are the same number.

**[REC]**

1. **`lumen_rejections_total{reason, source, model}`**, with
   `reason ∈ {rate_limit, coin_budget, no_access, needs_consent, no_healthy_endpoint, queue_shed}`.
   Cardinality is bounded and small. Note `model` is empty for `rate_limit` (the body has not been
   parsed) — that is honest, not a defect.
2. **Separate the two 429s in the response itself.** Coin exhaustion should carry a distinct code
   (OpenAI's taxonomy uses `insufficient_quota`), so a client can distinguish "slow down" from "you
   are out of budget" — they need opposite reactions.
3. **Add `Retry-After`.** This matters more than it sounds for the burst scenario: 300 OpenAI-SDK
   clients receiving a bare 429 with no `Retry-After` will retry on their own schedules and can
   synchronise into a retry storm. The SDK honours `Retry-After`.
4. **Upstream retries are currently invisible.** Streaming sets `max_retries=0` deliberately
   (`llm.py:104`, and the rationale at `:778-782` — never restart a generation); non-streaming uses
   `LLM_MAX_RETRIES` (default 1, `config_watcher.py:146`), and those attempts happen inside the SDK
   where Lumen sees only the total elapsed time. Options: (a) an httpx event hook counting attempts
   without changing behaviour — **recommended**; (b) set `max_retries=0` and retry in Lumen where it
   is countable and could try a *sibling endpoint* — more valuable but a behaviour change that
   should not be smuggled in under a metrics banner.
5. **Historically:** Prometheus retention (typically 15–30 d) is likely enough for rejection *rates*.
   Per-user historical attribution ("which students got rate-limited during Tuesday's lab") needs a
   row. **[REC] Do not write rejections into `request_logs`** — it would add a DB write to the
   cheapest possible request, precisely during the burst that caused it. If per-user attribution
   turns out to be needed, use a separate small hypertable written from a batched in-memory buffer
   flushed every few seconds. Decide with data, not upfront.

### 5.3 Response time

**[FACT] The existing histogram is blind to streams.** `middleware.py:148` starts the clock;
`:168-177` observes it in the `finally` immediately after `wsgi_app(...)` returns at `:150`. For an
SSE `Response(generate())` that is before the first token is fetched. Top bucket 10 s.

**[REC] The fix is three lines and needs no new machinery.** The middleware already wraps the body
in `_ContextCheckingBody` (`middleware.py:56-96`) whose `close()` is guaranteed to run
(`a2wsgi/wsgi.py:263-264`). Move the observation there, keep the `finally` path as the fallback for
the raise case, and guard against double-observation with a flag.

**[REC] Widen the buckets** to cover streaming (`… 10, 20, 60, 120, 300, 600`) — the gateway budget
is 600 s (`chart/values.yaml:294`), so anything above that is not a real bucket.

**[REC] Add a dedicated LLM histogram** rather than overloading the HTTP one:
`lumen_llm_duration_seconds{model, source, stream}` plus `lumen_llm_ttft_seconds{model, source}`.
Model cardinality is bounded by `config.yaml` and is fine.

**[REC] Watch out for the cardinality leak in the existing counter.** `_normalize_path`
(`middleware.py:182-184`) only collapses numeric segments. An internet-facing deployment being
scanned (`/.env`, `/wp-admin/…`) mints a new `path_template` label value per probed path, in
process memory and in the Prometheus TSDB, without bound. **This is a pre-existing risk, not caused
by anything proposed here**, and it gets worse the moment `/metrics` is scraped seriously. The fix
is to label with the matched Flask **url_rule** (`request.url_rule.rule`) and fall back to a single
`"<unmatched>"` bucket for 404s.

### 5.4 Wait time

Covered in §4. The headline: `local_queue_wait = T1 - T0` does not exist today, is the number that
explains the class-start incident, and is capturable in code Lumen already subclasses.

---

## 6. Unique users waiting — the cardinality problem

**The constraint.** Prometheus labels must never carry user identity. 300 students × N models × a
few states is 4–5 figures of series churn per class, permanently retained by the TSDB. This is not
a matter of degree; it is a hard rule.

So the question "how many *unique users* were waiting for model X at 09:05" has to be answered by a
store that can hold identity. Three candidates:

### Option 1 — TimescaleDB (historical), from the existing row

Once `queue_wait` and `ttft` are columns on `request_logs`, every completed request carries the
interval during which its user was waiting. The query is an interval-overlap `COUNT(DISTINCT
entity_id)` over one hypertable, at any past instant, at any resolution.

- **Pros:** no new writes at all — same INSERT, more columns. Durable, joinable to everything else,
  survives restarts, works with the existing `/usage` query patterns. Answers arbitrary retrospective
  questions the team has not thought of yet.
- **Cons:** **cannot answer "right now"** — a request that has not finished has no row. Query cost
  grows with the window; needs an index on `(model_config_id, time)`.

### Option 2 — Redis (live), a per-model set of in-flight entity ids

`SADD` on admit, `SREM` on first token or completion, `SCARD` to read. Shared across replicas.

- **Pros:** correct "right now" across any number of pods. ~2 ops per request, sub-millisecond,
  pipelined. Natural TTL-based self-healing for leaked entries.
- **Cons:** a new runtime dependency for observability; the chart's Redis is single-replica with
  persistence off (`chart/values.yaml:237,254`, `redis/deployment.yaml:10-12`) so it is not HA;
  needs careful cleanup on every abort path or the sets drift upward and lie.

### Option 3 — process-local memory (live), a dict per worker process

- **Pros:** zero dependencies, zero latency, no failure mode. **And it is exactly correct at today's
  topology** — one pod, one uvicorn process (`chart/values.yaml:5`, `entrypoint.sh:9`).
- **Cons:** silently becomes per-pod the day someone sets `replicaCount: 2`, and the UI would
  under-report without saying so.

### Recommendation

**Option 1 for history, Option 3 for live, with Option 2 as the documented upgrade path.**

Concretely: store the timestamps on `request_logs` and answer every retrospective question in SQL.
For the live number, keep a process-local structure, and **label it in the UI with the replica count
it was computed from** — "12 users waiting (this pod; 1 of 1 replicas)" — so it cannot silently
become a lie. Add the Redis-backed implementation behind the same interface when
`replicaCount > 1`, at which point Redis is already mandatory for rate limiting anyway
(`chart/values.yaml:4`).

**[OPEN]** Is "unique users waiting" the right metric, or is "unique users who waited more than N
seconds" more actionable? The second is a better alarm (it ignores the healthy case where everyone
is briefly waiting) and is equally easy from the same data. Worth deciding with the operators before
building a UI around the first.

---

## 7. Bursty load — what breaks first, and how to tell during the incident

### The failure order at chart defaults

Defaults: `replicaCount: 1`, `wsgiWorkers: 10`, `rate_limiting.limit: "30 per minute"`,
`pool_timeout: 10`, gateway 600 s, backend `--max-num-seqs 32`.

**1. The rate limiter — but not where you would expect.** [FACT] Six chat routes share one 30/min
per-user bucket (`chat/routes.py:72,114,164,306,367,403`): the chat page itself, uploads, the
stream, the conversation list, message fetch and delete. A student who reloads the page a few times
while nothing appears is spending the same budget the stream needs. **Under a burst, the visible
failure is likely to be a 429 on the chat *page*, which reads as a total outage.** Normal message
sending would not trip 30/min on its own.

**2. The a2wsgi thread pool — the first hard wall.** [FACT] 300 concurrent streams need 300 threads;
there are 10. 290 requests park in an unbounded queue. Each in-flight stream occupies its thread for
its full duration, so throughput is `10 / mean_stream_duration`. At a 20-second mean that is 0.5
requests/second — the 300th student waits about ten minutes, then the gateway cuts the request at
600 s (`chart/values.yaml:294`) and they see a failure with **nothing at all in Lumen's logs or
metrics explaining it.**

**3. The DB pool.** [FACT] Preflight is short and the connection is released before the upstream
call, so demand is bursty but brief. Visible today via `lumen_db_pool_connections{state}` and the
80 % warning (`metrics/routes.py:170-174`). Exhaustion raises rather than queueing past
`pool_timeout`.

**4. PostgreSQL write spike.** [FACT] `update_stats` is five statements per request
(`llm.py:507-568`), plus `subtract_coins`, plus the `APIKey` update on the API path. A synchronized
class means a synchronized burst of completions and therefore of writes. Concurrently:
`models_page/routes.py:49-60` runs **two uncached `COUNT(*)` scans of `request_logs` per model-page
view** — 300 students opening a model page is 600 sequential scans of a growing hypertable, at the
worst possible moment. The API's equivalent is cached 30 s (`api/routes.py:108-146`); the web page's
is not.

**5. The upstream, last.** [FACT] With only 10 threads, at most 10 requests can reach a backend
configured for 32 concurrent sequences. **Under defaults, the GPU is starved, not saturated** — and
the operators' instinct that "the model is overloaded" would be wrong. Once the thread pool is
raised, vLLM's own queue becomes the real constraint and its waiting count becomes the real answer.

### Telling them apart *during* the incident

| Hypothesis | Distinguishing signal |
|---|---|
| Waiting for a Lumen thread | `lumen_wsgi_queue_depth > 0`, `queue_wait` p95 climbing, TTFT p95 **flat** |
| DB pool contention | `lumen_db_pool_connections{state="checked_out"}` at limit, pool-wait histogram rising; `/metrics/debug` names the holders |
| Upstream saturated | backend `num_requests_waiting > 0` and `running == max_num_seqs`, TTFT p95 climbing, `lumen_wsgi_queue_depth ≈ 0` |
| Rate limited | `lumen_rejections_total{reason="rate_limit"}` rate |
| Out of coins | `lumen_rejections_total{reason="coin_budget"}` rate |
| Students giving up | `lumen_stream_aborts_total{reason="disconnect"}` rate, and `request_logs.aborted` share |
| Slow clients, not slow model | large `send_blocked` share of `duration` |
| Backend degraded, not queued | TTFT flat but tokens/sec down |

The pairing of `lumen_wsgi_queue_depth` with TTFT is the crux: **queue depth high + TTFT normal
means Lumen is the bottleneck; queue depth zero + TTFT high means the backend is.** Neither number
exists today, which is why the incident is currently undiagnosable from the outside.

### Does the metrics collector survive a burst?

**[FACT] It queries the database on every scrape** (`metrics/routes.py:67-143`): a `GROUP BY` over
`model_stats` — one row per `(entity, model, source)`, so 300 students × 5 models × 2 sources is
~3 000 rows and grows linearly with the user base — plus two `COUNT(*)` over `entities`. It takes a
pooled connection to do so. **[FACT]** This exact collector has already caused a production
connection leak (fixed in 1.22.0) and carries a long comment explaining why it releases its own
session (`routes.py:58-66`).

At a 15 s scrape interval in steady state this is fine. During a burst it is not: the scrape
competes for the pool it is trying to report on, and can block for up to `pool_timeout` — so
**`/metrics` degrades exactly when it is needed**, and raising the scrape rate to investigate makes
the problem worse.

**[REC] Decouple the scrape from the database.** Refresh the DB-derived gauges from a background
thread (the same daemon-thread pattern as `health.py:89-102` and the coin refiller) every 30–60 s
into an in-memory snapshot; have `collect()` serve that snapshot. `/metrics` becomes a pure
in-memory read: scrapeable at 5 s during an incident at zero database cost, and immune to the pool
state it reports on. Export the snapshot's age as a gauge so staleness is visible rather than
silent. **This is a small change with an outsized payoff and belongs in the first phase.**

### Validating any of this

**[FACT]** `loadtesting/` already provides a dummy backend, an account provisioner and a
500-user Locust harness. **[REC]** Extend the locustfile to record TTFT separately from total
elapsed (`locustfile.py:90,96` currently measures only the total), and add a "class start" load
shape — a step from 0 to N users in a few seconds — because a ramp does not reproduce this failure.
The dummy backend responds instantly, which is perfect for isolating *Lumen's* queueing from the
backend's; add a configurable artificial delay and concurrency cap to it to model vLLM's scheduler.

---

## 8. Redis or not

### The facts first

- **[FACT] Redis is already an installed dependency.** `pyproject.toml:17` declares
  `flask-limiter[redis]>=3.5` unconditionally; `uv.lock` pins `redis` 7.4.1. There is no optional
  extra — the package is present in every install.
- **[FACT] The chart already deploys it.** Four hand-written templates under
  `chart/templates/redis/` (deployment, service, pvc, secret), a `lumen.redisUrl` helper
  (`chart/templates/_helpers.tpl:113-124`), values (`chart/values.yaml:232-263`) and schema
  (`chart/values.schema.json:168-195`). There is **no** Bitnami subchart — `chart/Chart.yaml` has no
  `dependencies:` block.
- **[FACT] It is off by default and not highly available.** `redis.enabled: false`
  (`values.yaml:237`), `persistence.enabled: false` (`values.yaml:254`), `replicas: 1` with
  `strategy: Recreate` (`redis/deployment.yaml:10-12`).
- **[FACT] It is currently used for exactly one thing** — flask-limiter storage, injected as
  `rate_limiting.storage_url` (`chart/templates/config-secret.yaml:59-64`). Nothing else in the app
  imports `redis`.
- **[FACT] Multi-replica already requires it.** `chart/values.yaml:4`: "≥2 requires redis for shared
  rate-limit state."
- **[FACT] Three pre-existing chart bugs around it**, found while inventorying and worth fixing
  regardless of this proposal: `redis.existingSecret`/`existingSecretKey` are read by **no template**
  (dead values); `redis.auth.existingSecret` produces a password-less URL for the app against a
  `requirepass` server (`_helpers.tpl:116-117`); and `redis.auth.password` is additionally written
  in plaintext into the rendered `config.yaml` inside the config Secret.

### Recommendation

**Redis is not needed, and observability must not depend on it.**

Reasoning:

1. **Today's topology makes shared state unnecessary.** One pod, one uvicorn process
   (`entrypoint.sh:9`), one thread pool. Every "live" question — queue depth, in-flight per model,
   unique users waiting now — has a correct answer in process-local memory. Introducing a network
   round trip to learn something the process already knows is pure cost.
2. **A Redis outage must never degrade the proxy.** The bundled Redis is a single replica with
   `Recreate` and no persistence: a rollout or a node drain takes it away. If a `/v1/chat/completions`
   request depended on Redis to record that it started, a Redis blip would become an LLM outage. That
   is trading a visibility gap for an availability risk, which is the wrong direction for a *metrics*
   feature.
3. **Timescale already answers every historical question**, and answers them better — joinable to
   entities, models, endpoints and costs, with continuous aggregates and (once configured) retention.
   Redis is not a time-series store and should not be used as one.
4. **The one thing Redis is genuinely good for here is cross-replica live gauges** — and that need
   only arises at `replicaCount ≥ 2`, where Redis is already mandatory for rate limiting. So it is
   not a new dependency decision; it is the same one, already made.

### The rule to build to

Anything that touches Redis must be:

- **Optional at import time.** Absent config ⇒ fall back to process-local, log once at INFO.
- **Fail-open at call time.** Every Redis operation on a request path is wrapped, with a short
  timeout, and a failure falls back to local state and increments a counter
  (`lumen_redis_errors_total`). It must never propagate into a user-facing error.
- **Off the token path entirely.** At most two operations per *request* (admit / release), never per
  chunk.
- **Self-healing.** Any per-request key carries a TTL comfortably above the gateway budget, so a
  process killed mid-request cannot inflate a gauge forever.
- **Honest in the UI.** When the live number is process-local, say so.

**[OPEN]** If the deployment does move to `replicaCount ≥ 2`, several *existing* behaviours become
subtly wrong and should be revisited in the same change: `_rr_counters` round-robin becomes N
independent rotations (`llm.py:291-305`); the `_get_request_rates` 30 s cache becomes N caches
(`api/routes.py:110-146`); the health checker, coin refiller and config watcher run once per pod
(`lumen/__init__.py:463-480`, mitigated by `BACKGROUND_WORKER=false`); and `flask db upgrade` runs
on every pod start (`entrypoint.sh:8`). None are caused by this proposal; all become visible because
of it.

---

## 9. Retention, cardinality and the storage plan

### The problem

**[FACT]** `request_logs` grows one row per proxied request forever, with no compression and no
retention. At **1 M requests/month**, with three btree indexes and the tuple overhead, a reasonable
planning figure is **250–400 bytes/row all-in**, i.e. roughly **0.3–0.4 GB/month, 4–5 GB/year** —
and the columns proposed here add perhaps 40 bytes of `float8`, taking it to ~0.5 GB/month.

That is not alarming in absolute terms. What is alarming is that **nothing is bounded**, so the
figure is whatever the deployment's lifetime happens to be, and the analytics queries that scan raw
rows (`profile/routes.py` per-entity branch, `models_page/routes.py:49-60`) get slower forever.

### What retention would break — read this before enabling it

**[FACT] The `/usage` page's per-user charts read raw `request_logs`, not the aggregate**, because
`request_counts_hourly` has no `entity_id` dimension
(`migrations/versions/i9j0k1l2m3n4_timescaledb_tracking.py:53-67`;
`profile/routes.py:389,533,575,615,659`). **Turning on a retention policy today would silently
truncate every individual user's "All Time" history while the org-wide charts, fed by the aggregate,
kept going.** That asymmetry would be reported as a data-loss bug.

**[FACT] Lifetime totals are safe.** `entity_stats` and `model_stats` are cumulative all-time
counters written synchronously with every request (`llm.py:507-552`), so dropping raw chunks does
not lose anyone's lifetime usage or cost. This is worth knowing, and worth stating in the UI.

### Recommended storage plan

**[REC] Order matters. Do these in this sequence:**

1. **Add an entity-dimensioned continuous aggregate first**, e.g. `request_counts_hourly_by_entity`
   bucketed hourly on `(entity_id, model_config_id, source)`. Rewrite the per-entity `/usage`
   queries to read it beyond a recency threshold. Cardinality is bounded by *active* user-hours, not
   by users × models × hours — a student uses one or two models in an hour, so a 300-student class
   generates on the order of a few hundred rows per hour, not tens of thousands.
2. **Then add compression.** `add_compression_policy('request_logs', INTERVAL '7 days')` with
   `segmentby = model_config_id, source` and `orderby = time DESC`. Timescale columnar compression on
   data shaped like this typically achieves 10–20×, taking the archive to tens of MB per month.
   Chunks are already 7 days (`i9j0k1l2m3n4:52`), so the policy aligns naturally.
3. **Then add retention**, and make the window a deliberate, documented decision rather than a
   default. Two defensible choices: **90 days** (small, fast, requires step 1) or **13 months**
   (lets any query be "same week last year", still only ~5 GB uncompressed / well under 1 GB
   compressed). **[REC] 13 months**, on the grounds that this is an academic deployment where
   term-over-term comparison is the natural analysis and the storage cost of being generous is
   trivial.
4. **Add a fine-grained aggregate for the operator view.** The hourly aggregate lags by at least an
   hour (`i9j0k1l2m3n4:68-73`) and is useless for "what happened during the 9 a.m. lab". Add
   `request_metrics_1m` — 1-minute buckets on `(model_config_id, source)` with
   `end_offset => 1 minute, schedule_interval => 1 minute` — carrying counts, token sums, abort
   counts, and duration/TTFT sums-and-maxima. Keep raw-table queries for the trailing few minutes.
   Consider building the hourly aggregate *on top of* the 1-minute one (Timescale supports
   hierarchical continuous aggregates) to avoid rescanning raw chunks twice. **[OPEN]** Verify the
   minimum Timescale version in use supports hierarchical CAggs.
5. **Index for the new access pattern.** The operator queries filter by model and time;
   `(model_config_id, time DESC)` is the missing composite. Also consider `(entity_id, time DESC)`
   if per-user latency views become common.

**[REC] Percentiles need a decision.** Continuous aggregates can hold `COUNT`/`SUM`/`MAX`, which
gives means and worst cases but **not p95**. Real percentiles over aggregates need
`percentile_agg`/`tdigest` from `timescaledb_toolkit`. **[FACT/OPEN]** The compose example uses
`timescale/timescaledb:latest-pg17` (`docker-compose.yml.example:38`), which to the best of my
knowledge does **not** bundle the toolkit — that is the `timescaledb-ha` image. This needs
verifying against whatever the production cluster actually runs. Three ways out, in order of
preference: (a) confirm/enable the toolkit and use `percentile_agg`; (b) store fixed-bucket counts
in the aggregate (a hand-rolled histogram — ugly but exact and dependency-free); (c) compute
percentiles from raw rows for short windows only and show mean/max for long ones. **Recommend (a),
falling back to (b).**

**[REC] Where do the policies live?** These are DDL. Hot-reloading DDL from `config.yaml` is a bad
idea. Put the defaults in an Alembic migration (guarded on dialect, like every other Timescale
migration in the tree) and expose changes through a `flask` CLI command rather than config. If a
config key is wanted anyway, mirror it into `chart/values.yaml` and `values.schema.json` per
CLAUDE.md and have startup apply it idempotently with `if_not_exists => true`.

**[REC] Migration safety.** Follow the precedent already established: new columns must be
**nullable with a server default** or Timescale rejects propagating them to populated chunks — this
is documented explicitly in `e6f7a8b9c0d1_add_request_logs_aborted.py:48-53` and
`y9z0a1b2c3d4_request_logs_surrogate_pk.py:31-32`. And **do not backfill**; follow the reasoning in
`e6f7a8b9c0d1:7-31` and say so in the migration docstring.

**[REC] Do not change the meaning of `request_logs.time`.** It is the completion timestamp and the
partition key, and every existing chart depends on it. Add `queue_wait` and `ttft` as new columns
and derive the start time as `time - duration - queue_wait`. Changing `time` to the start would
silently shift every historical chart.

**[RISK]** The test suite runs on SQLite (`tests/conftest.py`, `db.create_all()` rather than
`flask db upgrade`), so **no test exercises the hypertable, the continuous aggregate, the refresh
policy, or any of the `/api/usage/*` SQL** — those paths short-circuit on the dialect check before
touching the database. Anything aggregate-shaped built here would ship untested. **[REC]** Add a
PostgreSQL + TimescaleDB service container to CI and a small suite that runs the migrations and the
aggregate queries against it. This is arguably a prerequisite for phases 1–2, not a nice-to-have.

---

## 10. The cost of measurement

The workload is streaming LLM responses measured in seconds. Instrumentation measured in
microseconds is free — *provided it stays off the per-token path*.

### Hard rules

1. **Never a database write per token.** Not a checkpoint, not a heartbeat, not a progress row. The
   ServiceEdgeCases plan already reached this conclusion for a different reason (task 6.3, "may not
   be worth it — decide explicitly"); it should be a standing rule for metrics too.
2. **Never a Prometheus `observe()` per token.** `Histogram.observe` takes a lock and walks buckets;
   at 50 tokens/sec × 300 concurrent streams that is 15 000 lock acquisitions/second on shared
   objects, for a number nobody reads per-token. **Accumulate locally in the generator's frame —
   plain ints and floats — and observe once at the end.** The existing code already does exactly
   this for `parts`/`content_deltas`.
3. **Never a Redis call per token.** At most two per request.
4. **New `request_logs` columns cost nothing.** They ride the INSERT that already happens
   (`llm.py:554-568`). Widening a row is free; adding a statement is not.
5. **Capture timestamps with `time.monotonic()` for durations** and reserve wall-clock for stored
   timestamps. The existing LLM path uses `time.time()` throughout (`llm.py:752,819,832`), which is
   subject to NTP steps — a small existing wart worth not replicating in new code.

### Existing costs worth knowing about

- **[FACT] `pool_tracker` captures a 25-frame stack on every pool checkout** (`pool_tracker.py:38`),
  several times per request. The module argues this is microseconds against LLM calls measured in
  seconds (`:10-12`), which is true per-request. Under a 300-request burst it is still small, but it
  is the one always-on instrumentation whose cost scales with concurrency rather than with duration.
  **[OPEN]** Worth a sampling or kill switch? Deliberately *not* recommending removal — it exists
  because it caught a production leak nothing else could, and that value has been proven.
- **[FACT] `/metrics` costs a DB query per scrape** — see §7. Fixing this is a cost *reduction*.
- **[FACT] `models_page/routes.py:49-60` costs two uncached `COUNT(*)` scans per model-page view.**
  Fixing this is also a cost reduction, and the caching pattern to copy is 40 lines away in
  `api/routes.py:110-146`.

### Estimated added cost per request, if everything here is implemented

| Item | Cost |
|---|---|
| Two `time.monotonic()` calls (T0, T1) | ~100 ns |
| Two atomic counter updates (queue depth in/out) | ~200 ns |
| Local dict insert/remove for in-flight-by-model | ~200 ns |
| 3–5 `Histogram.observe()` at end of request | ~5 µs |
| 3–5 extra columns on the existing INSERT | ~0 (same statement) |
| Optional Redis `SADD`/`SREM`, pipelined | ~0.2 ms |
| **Total, without Redis** | **well under 10 µs** |

Against a request whose median duration is measured in seconds, that is under one part in 100 000.
The measurement is not the problem; the *storage* and the *scrape* are, and both are addressed
above.

---

## 11. Presentation — where new views live

Three audiences, three surfaces. Keep them separate: an operator dashboard shown to students is
noise, and a student-facing "the model is busy" hint shown to operators is not enough.

### 11.1 Operator: a new `/admin/status` page

**[REC] Reclaim the dead `/admin/analytics` slot.** `admin/routes.py:206-209` is a bare redirect and
`lumen/templates/admin/analytics.html` (346 lines) is orphaned — an older near-copy of
`usage.html`. Either delete it and add `admin/status.html`, or repurpose the route. Either way this
is the cleanest place for a page that has no home today.

Structure:

- **Live tiles, refreshed on a timer** (5–10 s) from a new `/admin/api/status` JSON endpoint that
  reads the **in-memory snapshot** — not the database. Per model: in-flight, unique users waiting,
  upstream running/waiting (with capacity, if known), TTFT p50/p95 over the last 5 minutes, 429 rate,
  abort rate, endpoint health. Plus a system row: WSGI threads busy/total, queue depth, DB pool
  checked-out/limit, stranded count, replica count.
- **Historical charts** below, Chart.js 4 as elsewhere (`usage.html:4`), with a time-range selector,
  reading `/admin/api/status/history` backed by the 1-minute aggregate.
- **A diagnostics link** to the existing `/metrics/debug` text capture. **[REC]** Leave it as plain
  text — it is a capture artifact meant to be pasted into an incident channel, and HTML-ifying it
  would make it worse.

**Authorization:** `@admin_required` (`lumen/decorators.py:28-42`), which already content-negotiates
JSON vs HTML.

**Navigation:** a third `{% if is_admin %}` item after Users and Config, edited in **all four** theme
headers (`themes/{default,illinois,uic,uis}/templates/theme/header.html`). **[REC]** While doing so,
consider factoring the admin nav items into a shared partial — four copies is how the next page gets
added to three of them.

### 11.2 Operator: existing surfaces to extend rather than replace

- **`/metrics`** — extend, do not replace. Add the new families; make `collect()` serve a cached
  snapshot (§7).
- **`/metrics/debug`** — extend with a WSGI thread-pool section (busy/total, queue depth, per-thread
  current request), which is the natural companion to the existing DB-pool and thread-dump sections.
- **`/models`** (`models.html`) — the status badge already has ok/degraded/down/disabled. **[REC]**
  add a **busy** state driven by upstream queue depth. Must not be colour-only (CLAUDE.md §6): badge
  text plus an icon.

### 11.3 The individual user's experience

This is the third point of view the operators asked for and the easiest to forget.

- **`model_detail.html`** already has an Availability card with an admin-only per-endpoint block
  (`:79-120`) and live request rates (`:109-116`). **[REC]** Add a user-visible "typical wait right
  now" — median TTFT over the last 5 minutes for that model — replacing the two uncached `COUNT(*)`
  scans with a read from the cached snapshot. Same information, less database, more useful.
- **`chat.html`** — **[REC]** when nothing has arrived yet, show an elapsed-time indicator and, if
  the model reports a queue, "waiting for the model (N ahead of you)". This is the single change
  that most improves the class-start experience: a student who knows they are queued does not
  reload, and every reload is another 30/min bucket entry and another thread.
- **`/usage` single-user mode** — **[REC]** add latency to the existing stat cards: median and p95
  TTFT, and share of requests aborted. The data comes free from the new `request_logs` columns, and
  it answers "is it slow for *me*" — which is the question a student actually has.
- **`profile.html`** already shows per-model usage; **[REC]** add "your median wait" per model.

### 11.4 Accessibility notes specific to this work (CLAUDE.md §6)

- **Auto-refreshing live tiles must not be an `aria-live` region.** A polite live region updating
  every 5 seconds is unusable with a screen reader. **[REC]** `aria-live="off"` on the numeric tiles,
  an explicit "Refresh" button, and a *separate* small `role="status"` region that announces only
  **state transitions** ("model X is now queued", "model X recovered") rather than every value.
- Every `<canvas>` needs `role="img"` + `aria-label` + text fallback, matching
  `usage.html:82,90,102,110,122`.
- Every data table needs `<caption class="visually-hidden">`, matching `models.html:12` and the rest.
- Queue/health state must never be colour-only — pair every badge with text and an icon, as the
  existing status badges already do.
- Timestamps as `<span class="local-datetime" data-utc="…Z">` for `app.js:69-77` to convert; never
  hardcode "UTC" in displayed text. Note that the *relative* "in N minutes" helper is per-template
  (`profile.html:310-313`), not global, so a new page must bring its own.
- Any auto-dismissing banner must last ≥20 s and pause on hover/focus — the pattern already exists
  in `app.js:31-50`.

---

## 12. Additional instrumentation worth having

Not padding. Each entry states why it earns its place.

### Strongly recommended

1. **TTFT on `request_logs`, for every source.** *Why:* it is the user-perceived latency SLI, it is
   already computed (`llm.py:818-819`), and it is currently discarded for the entire `/v1` API and
   deletable-with-the-conversation for chat. The single highest value-per-line change in this
   document.

2. **Queue wait (`T1 - T0`) on `request_logs` and as a histogram.** *Why:* §3. It is the number that
   explains the class-start incident and it does not exist.

3. **First-token-of-any-kind vs first-visible-content.** *Why:* on reasoning models these differ by
   tens of seconds (`llm.py:812-819`), and conflating them makes a thinking model look like a queued
   one.

4. **DB pool checkout-wait histogram.** *Why:* `pool_tracker` already hooks SQLAlchemy's
   `checkout`/`checkin` events; the wait is a subtraction away. Today the pool is observable only as
   a depth, so "the pool is full" and "the pool is full *and requests are waiting on it*" look the
   same.

5. **Per-model, per-endpoint error taxonomy.** `lumen_upstream_errors_total{model, endpoint, kind}`
   with `kind ∈ {connect_timeout, read_timeout, http_429, http_4xx, http_5xx, context_length,
   decode_error, other}`. *Why:* `lumen_stream_aborts_total{source, reason}` has three reasons and
   covers streams only (`middleware.py:33-37`, `llm.py:870`); non-streaming and audio failures are
   invisible, and "the model is down" vs "the prompt was too long" are currently the same 500 to an
   operator.

6. **Health-probe latency.** *Why:* `health.py` records only a boolean (`:60,79`). A backend whose
   probe latency has quadrupled is degrading and would be caught before it flips to unhealthy — one
   extra field on a call that already happens every 60 s.

7. **Saturation/headroom as explicit ratios**, not raw numbers: `threads_busy / threads_total`,
   `pool_checked_out / pool_limit` (a "limit" gauge already exists, `metrics/routes.py:168`),
   `upstream_running / max_num_seqs`, KV-cache utilisation. *Why:* these are the only *leading*
   indicators here. Latency and error rate are lagging — by the time they move, the class has
   noticed.

8. **Abort rate as a first-class UX signal.** *Why:* `request_logs.aborted` is written and never
   read (§2.2). A rising abort share is students closing the tab, which is the most direct available
   measurement of "the service is unusable right now", and it is already in the database.

9. **`send_blocked` time per request.** *Why:* §3 Q5 — client backpressure is currently
   indistinguishable from backend slowness inside `duration`, and mistaking one for the other sends
   an operator to the wrong system.

### Recommended, second tier

10. **Fairness during contention.** Per model per 5 minutes: share of requests from the top user, and
    the count of users who attempted but got zero successful responses. *Why:* the 300-student
    scenario is exactly when one runaway script starves a class, and a per-user rate limit does not
    prevent it (30/min × 300 users is 9 000/min against a 32-sequence backend). Cheap from data
    already collected. Low cardinality if aggregated server-side and exported as a summary, **not**
    as per-user labels.

11. **Prefix-cache hit rate per endpoint** (from the backend's metrics). *Why:* it is a first-order
    driver of TTFT, **and Lumen deliberately reduces it**: the 1.24.0 security fix salts the prefix
    cache per entity (`cache_salt`, CHANGELOG 1.24.0, issue #36). Measuring the hit rate quantifies
    the latency price of that decision, which is currently an unknown.

12. **Tokens/sec per endpoint, aggregated.** *Why:* `output_speed` exists per chat message
    (`llm.py:842`) but is not aggregated anywhere. A drop with flat TTFT means backend degradation;
    a drop with rising TTFT means queueing. The pair discriminates; neither alone does.

13. **Cost/coin burn-rate and projected exhaustion**, per entity and per project. *Why:* all the
    inputs exist (`entity_stats`, `EntityBalance`, `token_refill.py`), and "when will this class run
    out of coins" is the budget question that always arrives mid-term. Purely derived; no new
    collection.

14. **Config/deployment drift gauges.** `RESTART_REQUIRED` is already computed
    (`config_watcher.py:189-197`); export it, plus app version, replica count and resolved thread
    pool. *Why:* half of all "it broke after a config change" incidents are answered by "the config
    changed but the pods did not restart."

15. **Bounded admission control per model, with fast rejection.** *Why:* this is a *control*, not a
    metric, but it is what makes every metric above actionable. Cap concurrent in-flight requests per
    model near the backend's `max_num_seqs`, keep a bounded queue with a deadline, and return
    429 + `Retry-After` beyond it. A student told "the model is busy, try again in 30 s" is far
    better served than one who watches a spinner for ten minutes and is then cut off by the gateway.
    It also converts the invisible unbounded Q1 queue into a bounded, measured, explainable one.
    **[OPEN]** This is the biggest behaviour change suggested anywhere in this document and deserves
    its own decision, separate from the observability work.

### Considered and deliberately not recommended

- **Per-token spans / full OpenTelemetry tracing.** Cost on the hot path, an entirely new
  infrastructure dependency, and it answers questions this deployment is not asking. Revisit if
  Lumen ever fans out across services.
- **Shipping `request_logs` to an external log aggregator.** They already have a queryable,
  time-partitioned store with the right joins. Duplicating it buys nothing.
- **User identity in Prometheus labels.** §6. Not a matter of degree.
- **A DB write per token, per chunk, or per N seconds of a stream.** §10.
- **Replacing the existing rollups (`model_stats`, `entity_stats`) with queries over
  `request_logs`.** Tempting on purity grounds, but they are what make lifetime totals survive
  retention (§9), and they are O(1) reads on the profile and admin pages. **Keep them.**

---

## 13. SLIs, SLOs and alerts

**[REC] Four SLIs, chosen because each is directly attributable to something the team controls:**

| SLI | Definition | Suggested SLO |
|---|---|---|
| **Availability** | non-5xx, non-`upstream_error` proxied requests / total attempts | ≥ 99.0 % over 30 d |
| **User-perceived latency** | `T4c - T0` (first visible token from request arrival) | p95 ≤ 5 s over 30 d, per model class |
| **Proxy overhead** | `T1 - T0` (queue wait — the part Lumen owns) | p99 ≤ 1 s |
| **Completion** | 1 − (aborted-by-disconnect / total streams) | ≥ 95 % |

The split between the second and third is the point: a bad TTFT with a good queue wait is a GPU
capacity conversation; a bad queue wait is a `wsgiWorkers` conversation. Separating them stops the
two being argued about together.

**[REC] Alerts, roughly in order of usefulness:**

| Alert | Condition | Why |
|---|---|---|
| Lumen admission queue | `lumen_wsgi_queue_depth > 0` for 2 min | Should be zero in steady state. Any sustained value means the deployment is under-provisioned *in a way an operator can fix immediately.* |
| Upstream saturated | backend `waiting > 0` and `running == max_num_seqs` for 5 min | GPU capacity, not app capacity |
| Pool pressure | `checked_out ≥ 0.8 × limit` for 5 min | Already logged at scrape time (`metrics/routes.py:170-174`); make it page |
| **Stranded connections** | `lumen_db_pool_connections{state="stranded"} > 0` | A regression detector for the leak class that has already bitten twice (1.21.0, 1.22.0). Should always be zero. |
| Endpoint down | `lumen_model_endpoint_healthy == 0` for 3 min | Existing gauge, no alert on it today |
| Abort surge | disconnect-abort share > 10 % over 10 min | Users giving up |
| Rejection surge | `rate(lumen_rejections_total{reason="rate_limit"})` above baseline | Distinguishes a retry storm from real demand |
| Error-budget burn | multi-window (1 h and 6 h) fast/slow burn on availability | Standard; keeps the page count sane |
| Metrics staleness | snapshot age gauge > 3 × refresh interval | The failure mode of the cached-snapshot design in §7 — silently stale numbers |

**[OPEN]** Alert routing and a Grafana dashboard are out of scope here because the chart ships no
ServiceMonitor and no scrape config (§2.1), so it is not clear what the team's Prometheus setup
actually is. That needs establishing before any alert can be said to exist.

---

## 14. Phased plan

Each phase is independently useful and independently shippable. Phase 0 alone would have made the
class-start incident diagnosable.

### Phase 0 — make the invisible visible (no schema change)

Small, self-contained, high value. Nothing here changes behaviour.

- Stamp T0 in `_DisconnectAwareWSGIResponder.__call__`, read at T1; export
  `lumen_wsgi_queue_wait_seconds` (histogram) and `lumen_wsgi_queue_depth` /
  `lumen_wsgi_threads_busy` (gauges).
  → *verify:* under a Locust step load of 300 users against the dummy backend, depth rises to ~290
  and wait p95 tracks it; both return to zero afterwards.
- Fix the streaming-blind HTTP latency histogram by observing in `_ContextCheckingBody.close()`;
  widen buckets to 600 s.
  → *verify:* a streamed request's observed duration matches its `request_logs.duration` within a
  small tolerance.
- Add `lumen_rejections_total{reason, source, model}`; give coin exhaustion its own error code; add
  `Retry-After` to both 429 paths.
  → *verify:* a synthetic over-limit client sees `Retry-After` and the counter splits by reason.
- Make `LumenDBCollector` serve a background-refreshed in-memory snapshot; export snapshot age.
  → *verify:* `/metrics` issues zero queries during a scrape; scraping at 1 s does not touch the DB.
- Cache the `models_page` per-model counts the way `_get_request_rates` already does.
  → *verify:* repeated model-page loads issue no new `COUNT(*)`.
- Label HTTP metrics by matched `url_rule` with an `<unmatched>` bucket for 404s.
  → *verify:* a request to `/wp-admin` does not mint a new label value.
- Extend the Locust harness with a TTFT split and a step load shape.

### Phase 1 — record the timestamps (schema)

- New nullable columns on `request_logs`: `queue_wait`, `ttft`, `send_blocked`, `outcome`
  (small string enum). Timescale-safe (nullable + server default); no backfill, with the reason in
  the migration docstring following `e6f7a8b9c0d1:7-31`. Column comments; `docs/dbschema.md`
  updated (CLAUDE.md).
- Populate on all four paths (chat stream, API stream, API non-stream, audio) — TTFT is currently
  chat-only.
- New composite index `(model_config_id, time DESC)`.
- Add `lumen_llm_ttft_seconds` and `lumen_llm_duration_seconds` histograms.
- **Prerequisite:** a PostgreSQL + TimescaleDB CI service, because none of this is testable on
  SQLite (§9).
  → *verify:* migration up/down clean on Postgres; a chat request and an API request both produce
  rows with non-null `ttft`; the "unique users waiting at time T" query returns sane results
  against seeded data.

### Phase 2 — real queue depth

- Persist the backend type that `model_sync` already detects but discards (`model_sync.py:87,332`),
  and the backend's concurrency capacity (`max_num_seqs` / SGLang equivalent) so depth has a
  denominator.
- Background scraper for each `model_endpoint`'s `/metrics`, modelled on `health.py`'s bounded
  executor, with a per-backend allowlist mapping. Latest values into the snapshot; re-exported as
  `lumen_upstream_*` gauges.
- A small hypertable (or a sampled aggregate) for the historical view of upstream depth.
- A ServiceMonitor in the chart for both Lumen and, optionally, the model Deployments; and an
  `emptyDir` at `multiprocDir` if `--workers` is ever used.
  → *verify:* saturating a real backend with more than `max_num_seqs` concurrent requests shows a
  non-zero waiting gauge in Lumen that tracks the backend's own.

### Phase 3 — the views

- `/admin/status` (live tiles + historical charts), reclaiming the dead `/admin/analytics` slot.
- The `request_metrics_1m` continuous aggregate that backs the historical charts.
- User-facing: queue/wait indicator in chat, "typical wait now" on the model detail page, latency on
  `/usage` single-user mode.
- Nav in all four theme headers; accessibility per §11.4 and CLAUDE.md §6, with tests in
  `tests/ui/test_accessibility.py`.

### Phase 4 — control, scale and lifecycle

Each of these is a separate decision, listed together only because they share prerequisites.

- Entity-dimensioned continuous aggregate, **then** compression, **then** retention (order matters,
  §9).
- Redis-backed live gauges, activated only when `replicaCount > 1`, fail-open by construction (§8).
- Bounded per-model admission control with fast 429 + `Retry-After` (§12 item 15) — the biggest
  behaviour change, and the one that most improves the burst experience.
- Revisit `resolve_wsgi_workers`'s `auto` heuristic in light of the data (§3 Q3).
- Fix the three Redis chart bugs and the `RESTART_REQUIRED` omission found while inventorying (§2.6,
  §8) — unrelated to this proposal, worth doing anyway.

---

## 15. Risks, gaps, and what could not be determined

### Risks

- **Retention will truncate per-user history if enabled before the entity aggregate exists.**
  §9. This is the most likely way this work causes a user-visible regression.
- **None of the Timescale surface is covered by tests.** The suite is SQLite-only and the analytics
  endpoints short-circuit on dialect before touching SQL. Anything aggregate-shaped ships untested
  until CI grows a Postgres service.
- **Backend metric names are a moving target.** They must be configuration with a per-backend
  mapping, not constants; a version bump of vLLM should degrade to "unknown" rather than to a crash
  or, worse, a silently wrong gauge.
- **Raising `LUMEN_WSGI_WORKERS` interacts with pool auto-sizing.** `auto` derives threads from the
  pool (`db_pool.py:100-105`); raising threads to hundreds while leaving `auto` set would also
  attempt to raise the pool. The two need decoupling before the thread count is changed.
- **`_normalize_path` label cardinality** (§5.3) becomes a real memory and TSDB problem the moment
  `/metrics` is scraped seriously on an internet-facing deployment. Pre-existing, but this work is
  what surfaces it.
- **A cached metrics snapshot can be silently stale.** Mitigated by exporting its age and alerting on
  it — but the failure mode is "confidently wrong numbers during an incident", which is worse than
  no numbers. Worth the extra alert.
- **Adding an admission-control queue changes behaviour under load** from "everyone waits" to "some
  are rejected quickly". That is a better experience, but it is a *policy* change and needs the
  operators' agreement, not just an engineer's.

### Open questions for the team

1. **Is `timescaledb_toolkit` available in production?** Determines whether percentiles are exact or
   approximated with hand-rolled buckets (§9).
2. **What Timescale version is in production?** Determines whether hierarchical continuous
   aggregates are available (§9).
3. **Are backend `/metrics` endpoints reachable and unauthenticated from the Lumen pod?** Determines
   whether Option B in §3 is viable at all.
4. **What is the actual production request volume and growth?** Every storage figure here is
   parameterised on the stated 1 M/month; nothing in the repo reveals real traffic.
5. **Does a Prometheus/Grafana stack already exist for this cluster?** The chart ships no
   ServiceMonitor and no scrape annotations, which suggests scraping may be ad hoc. This changes
   whether Grafana or the in-app page is the primary surface.
6. **Retention window: 90 days or 13 months?** §9 recommends 13; it is a policy call.
7. **Is "unique users waiting" or "unique users waiting more than N seconds" the target metric?**
   §6. The second is more actionable.
8. **Should the chat rate limit be split?** Page loads, conversation listing and streaming currently
   share one 30/min bucket per user (§7), so the visible burst failure may be a 429 on the page
   rather than on the stream. Splitting is easy; whether it is wanted is a product call.
9. **Is per-model admission control acceptable?** §12 item 15. Biggest behaviour change proposed.
10. **`replicaCount > 1` — is it planned?** It changes the Redis answer (§8) and several existing
    behaviours that are currently correct only because there is one process.

### What could not be determined from the repository

- Real production traffic volume, growth rate, or the current size of `request_logs`.
- The actual `--max-num-seqs` and model mix in production. `docker-compose.yml.example:82` shows
  `32`, but the compose file is an example and the chart's `models:` list is empty by default
  (`chart/values.yaml:321`).
- Whether operators currently scrape `/metrics` at all, and at what interval.
- Whether the Gateway API's 600 s timeout (`chart/values.yaml:294`) is the effective deadline in
  production or whether something in front of it is stricter.
- Exact per-row storage of `request_logs` on the production database; the figures in §9 are
  estimates from column types and standard PostgreSQL overhead, not measurements. A single
  `pg_total_relation_size('request_logs')` would replace all of them with a fact.
