# Implementation Plan — Usage and Resource-Metric Visibility

**Date:** 2026-08-17
**Status:** Plan. Companion to `08-17-2026-propsoal-for-usage-and-resource-metric-visibility.md`.
**Premise change from the proposal:** the proposal reasoned from *chart defaults* (`replicaCount: 1`,
one uvicorn process). This plan does not. It assumes production may run **N processes × M replicas**,
and every design decision below is chosen so that the answer does not change when N or M changes.

**Conventions:** **[FACT]** verified against the tree at `196f642`. **[GATE]** a blocking exit
criterion. **[DECISION]** a call this plan makes, with its reason.

---

## Table of Contents

1. [Three findings that revise the proposal](#1-three-findings-that-revise-the-proposal)
2. [The decision that makes topology irrelevant](#2-the-decision-that-makes-topology-irrelevant)
3. [Does this push us towards Redis?](#3-does-this-push-us-towards-redis)
4. [Gate 0 — ground truth before code](#4-gate-0--ground-truth-before-code)
5. [Phase 1 — multi-process integrity](#5-phase-1--multi-process-integrity)
6. [Phase 2 — the missing queue numbers](#6-phase-2--the-missing-queue-numbers)
7. [Phase 3 — schema and a database CI](#7-phase-3--schema-and-a-database-ci)
8. [Phase 4 — decouple `/metrics`, introduce the LiveState seam](#8-phase-4--decouple-metrics-introduce-the-livestate-seam)
9. [Phase 5 — the Redis backend](#9-phase-5--the-redis-backend)
10. [Phase 6 — upstream queue depth](#10-phase-6--upstream-queue-depth)
11. [Phase 7 — the views](#11-phase-7--the-views)
12. [Phase 8 — lifecycle: aggregates, compression, retention](#12-phase-8--lifecycle-aggregates-compression-retention)
13. [Phase 9 — admission control (separate decision)](#13-phase-9--admission-control-separate-decision)
13a. [Burst pathologies this plan did not originally address](#13a-burst-pathologies-this-plan-did-not-originally-address)
14. [Standing invariants, enforced by test](#14-standing-invariants-enforced-by-test)
15. [Corrections to the proposal](#15-corrections-to-the-proposal)
16. [Adversarial review — what changed](#16-adversarial-review--what-changed)

---

## 1. Three findings that revise the proposal

### 1.1 Yes — TimescaleDB is in use, and it is mandatory, not opportunistic

**[FACT]** `migrations/versions/i9j0k1l2m3n4_timescaledb_tracking.py:37` executes

```sql
CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE
```

unconditionally on the PostgreSQL branch, then `create_hypertable('request_logs', 'time', ...)` and a
continuous aggregate `request_counts_hourly`. This is **not** a soft feature-detect. If the extension
is unavailable the migration raises, and because `entrypoint.sh:8` runs `flask db upgrade` before
`exec uvicorn`, **the container fails to start**. The compose example backs this with
`timescale/timescaledb:latest-pg17` (`docker-compose.yml.example:38`).

So: any PostgreSQL deployment of Lumen that is running at all **has Timescale**. Only the SQLite path
gets a plain table, and every `/api/usage/*` endpoint short-circuits on the dialect check there.

Your question was framed as *"if not TimescaleDB, but already Redis, then Redis is a good place."*
The tree says the opposite of both halves:

| | Proposal's assumption | Verified state |
|---|---|---|
| TimescaleDB | present | **present and load-bearing** — the app cannot boot on Postgres without it |
| Redis | "already a dependency" | **installed as a Python package, not deployed** — `chart/values.yaml` `redis.enabled: false`, and the only consumer is flask-limiter storage |

That inverts the storage argument rather than weakening it. The purpose-built time-series store with
retention, compression, continuous aggregates and foreign keys to `entities`/`model_configs` is
already provisioned, already migrated, already backed up with the primary database, and already the
thing `/usage` reads. Redis is a cache that the chart ships **single-replica, `strategy: Recreate`,
`persistence.enabled: false`** — i.e. a rollout or node drain deletes its contents.

**[DECISION]** No historical or per-user instrumentation goes to Redis. Ever. That is not a
close call once Timescale is known to be mandatory.

### 1.2 Your critique of the Redis argument is correct — but it does not lead to Redis

The proposal's §8 conclusion rests on one premise: *one pod, one process, therefore process-local
memory is exactly correct.* That premise is a reading of `chart/values.yaml:5` and `entrypoint.sh:9`
— **chart defaults, not verified production config.** You are right to distrust it. Two things
follow, and only the second is about Redis:

1. **Most of the proposal is topology-invariant and survives the critique untouched.** Columns on
   `request_logs`, Prometheus counters and histograms, the backend scraper, the retention plan — none
   of these care how many processes exist, because every process writes to the same database and
   Prometheus sums counters across scrape targets by construction.

2. **A specific, small set of numbers is topology-sensitive**, and for those the proposal's
   recommendation (process-local dict) is wrong under N>1. That set is enumerated in §2.

The mistake to avoid is generalising from (2) to "so use Redis for the instrumentation." The
topology-sensitive set is three numbers, and only one of them genuinely needs Redis.

### 1.3 Scaling beyond one process is not currently safe — for reasons unrelated to metrics

**[FACT]** Nothing in the tree runs more than one process today: `entrypoint.sh:9` is
`exec uvicorn asgi:app --host 0.0.0.0 --port 5001 $@` with no `--workers`. The code *anticipates*
multi-process — `db_pool.detect_workers()` reads `WEB_CONCURRENCY` and parses `--workers` off the
parent cmdline, `detect_replicas()` reads `LUMEN_REPLICAS` — but several things break quietly if you
turn it on:

| Concern | State at N processes | Severity |
|---|---|---|
| Prometheus `multiproc_dir` | Config key exists (`config.yaml.example:58`); **chart mounts no volume at it**, and nothing wipes it at startup, so a restarted pod's metrics are summed with the previous run's dead PIDs | **Blocks Phase 1** |
| New in-process gauges | `prometheus_client` gauges default to `multiprocess_mode='all'` → one series **per PID**, not a fleet number | **Blocks Phase 2** |
| Rate limiting | Already documented as requiring Redis (`chart/values.yaml:4`) — but the requirement is stated for **replicas**, and it applies equally to **processes** | Medium |
| Health checker | `lumen/__init__.py:470` gates on `BACKGROUND_WORKER`, which is a **process-wide env var**; uvicorn's children all inherit it, so it cannot mean "extra workers only". N processes ⇒ N× probe load on every backend every 60 s | Medium |
| `_rr_counters` round-robin (`lumen/services/llm.py`) | N independent rotations ⇒ uneven endpoint distribution | Low |
| `_get_request_rates` 30 s cache (`lumen/blueprints/api/routes.py`) | N caches ⇒ N× the `COUNT(*)` load | Low |
| Coin refiller | **Safe.** `lumen/services/token_refill.py:101-113` is a compare-and-set (`WHERE last_refill_at <= one_hour_ago`), so a second process's pass is a no-op | None |
| Config watcher | **Correct as-is.** Per-process reload is what you want | None |

**[DECISION]** "Scale beyond one process" is a prerequisite of this work, not a consequence of it.
Phase 1 exists to make the metrics that *already* exist true at N>1, before adding any new ones.

---

## 2. The decision that makes topology irrelevant

Three stores, one rule each. Every number produced by this work is assigned to exactly one, and the
assignment does not change with N or M.

### Store 1 — TimescaleDB (`request_logs`): everything historical, everything per-user

**Rule:** if the question contains a past tense or a user identity, it is answered by SQL over
`request_logs`.

**Why topology-invariant:** every process holds a connection to the same database. A row written by
process 7 of pod 3 is indistinguishable from any other. Adding processes adds write throughput
demand, not correctness risk.

**Covers:** TTFT history, queue-wait history, "how many distinct users were waiting for model X at
09:05" (interval-overlap `COUNT(DISTINCT entity_id)`), abort share, cost, per-user latency on
`/usage`. **This is the majority of the four asks.**

### Store 2 — Prometheus: everything aggregate, live, for machines

**Rule:** counters and histograms, labelled only by bounded-cardinality dimensions
(`model`, `source`, `reason`, `endpoint`). Never user identity.

**Why topology-invariant:** counters and histograms are additive. Within a pod, `prometheus_client`'s
multiprocess mode sums across processes (`lumen/blueprints/metrics/routes.py:182-200` already wires
`MultiProcessCollector`). Across pods, Prometheus sums across targets. **Provided** the plumbing in
Phase 1 is finished, N and M are free variables.

**Covers:** queue-wait distribution, queue depth, rejection rates, abort rates, latency histograms,
pool state, upstream gauges.

### Store 3 — `LiveState`: the three numbers that are live, fleet-wide, and needed *inside the app*

This is the entire topology-sensitive surface. It is small on purpose:

| Number | Why Prometheus cannot serve it | Why the DB cannot serve it |
|---|---|---|
| In-flight requests per model, right now | Could, in principle — but the in-app admin page has no Prometheus client and the chart ships no scrape config, so there is nothing to query | An in-flight request has no row; `request_logs.time` is the **completion** timestamp |
| **Unique users waiting per model, right now** | **Cannot.** Distinct-count over identities is exactly what Prometheus labels must never carry (§6 of the proposal) | Same — no row yet |
| Admission-control token per model (Phase 9 only) | Not a metric; a semaphore | Not a semaphore |

**[DECISION]** Put this behind `lumen/services/live_state.py`, with **at most two** implementations —
`LocalLiveState` (a dict + lock) and `RedisLiveState` — selected at startup by whether a Redis URL is
configured. Not three backends, not a plugin registry.

**Settled: the interface is justified, because both implementations will exist.** Round 2 objected
that an interface with one implementation is the speculative generality CLAUDE.md §2 forbids, and
that objection was correct *given* the possibility that Phase 5 would be deferred. The team has since
decided to implement every phase, so `RedisLiveState` is not conditional — two implementations exist,
and the abstraction earns its place on the ordinary grounds. Build the interface with
`LocalLiveState` in Phase 4 and add `RedisLiveState` in Phase 5.

This does **not** relax the honesty requirement: `LocalLiveState` remains the fallback whenever Redis
is absent or failing, so the topology label is what keeps a per-process number from being read as a
fleet number.

**The honesty requirement.** Every live number rendered in the UI or served by
`/admin/api/status` carries the topology it was computed from, using the functions that already
exist:

```python
from lumen.services.db_pool import detect_workers, detect_replicas
# → {"scope": "local", "processes": 1, "replicas": 1}  or  {"scope": "fleet"}
```

so the tile reads *"12 users waiting (this process — 1 of 4 processes × 2 replicas)"* rather than
silently under-reporting by 8×. This is the single most important guard against the failure mode you
identified: a number that was correct under the defaults and became a lie under production config,
without anybody noticing.

---

## 3. Does this push us towards Redis?

**Partly — for one metric, one control, and one convenience. Not for "the instrumentation."**

### It does not push us to Redis for

- **History or per-user attribution** — Timescale, §1.1. Redis is not a time-series store, has no
  persistence in this chart, and putting the answer to "which students were rate-limited on Tuesday"
  in a cache that a node drain empties is not a design.
- **Rates, distributions, depths, saturation ratios** — Prometheus, which is additive across
  processes and replicas by construction. A queue-depth *gauge* summed over N processes with
  `multiprocess_mode='livesum'` is the fleet number, with no Redis involved.
- **Anything on the token path.** Two Redis round-trips per request is acceptable; two per token is
  not (§14).

### It does push us to Redis for

1. **"Unique users waiting for model X, right now", fleet-wide.** This is the one number that is
   simultaneously live, identity-bearing, and cross-process. Prometheus structurally cannot hold it;
   the database does not have the rows yet. A Redis `SADD`/`SREM`/`SCARD` per model — two operations
   per request, off the token path, TTL'd above the gateway budget so a killed process cannot inflate
   it forever — is the correct tool, and it is the *only* new thing in this plan that Redis is
   uniquely good at.
2. **Cross-fleet admission control (Phase 9).** A per-model concurrency cap that means anything at
   M replicas is a distributed semaphore. If admission control is adopted, Redis is required. If it
   is not adopted, this reason evaporates.
3. **Convenience: the in-app `/admin/status` live tiles being fleet-wide rather than per-process.**
   The alternative — have Lumen scrape its own Prometheus — trades a Redis dependency for a Prometheus
   dependency that the chart does not currently provision at all (**[FACT]** no `ServiceMonitor`, no
   `PodMonitor`, no `prometheus.io/*` annotations anywhere under `chart/`).

### And the cost is bounded by construction

**[DECISION]** Redis is never on the critical path of a proxied request. Encoded as five rules,
each with a test in §14:

- **Optional at import.** No Redis URL ⇒ `LocalLiveState`, logged once at INFO. No `redis` import at
  module scope in any request-path module.
- **Fail-open at call.** Every operation wrapped, `socket_timeout=0.25`, failure falls back to the
  local value and increments `lumen_live_state_errors_total{op}`. A Redis outage degrades the *admin
  page*, never a `/v1/chat/completions`.
- **Bounded call count.** At most two operations per request. Never per chunk.
- **Self-healing.** Per-request members carry a TTL above `gateway.timeout` (600 s), so a SIGKILL
  mid-request cannot leave a permanently inflated set.
- **Honest in the UI.** When `LocalLiveState` is active, the topology label says so.

**The scheduling answer:** Redis is *already required* at replicas ≥ 2 for rate limiting
(`chart/values.yaml:4`) and, per §1.3, at processes ≥ 2 as well. So if Gate 0 finds production is
already multi-process or multi-replica, **Redis is already deployed and this is not a new
dependency** — Phase 5 is then a small addition to something running. If Gate 0 finds production is
genuinely 1×1, Phase 5 is deferred and `LocalLiveState` is exactly correct. **Either way the plan
does not change shape**, which is the point of the seam in §2.

---

## 4. Gate 0 — ground truth before code

No code. Four of these cannot be derived from the repository.

**[DECISION] Every phase will be implemented, so Gate 0 no longer decides *whether* or *in what
order* — it decides *how*.** That removes the conditional-scheduling language from this document
(Phase 5 no longer defers; Phase 7 no longer jumps the queue). Two things it does **not** remove:

- **Dependency constraints are not scheduling preferences.** Phase 8's internal order (entity
  aggregate → **rewrite the per-entity `/usage` queries onto it** → compression → retention) is a
  correctness constraint: following it out of order silently truncates every user's history. So is
  the rule that **compression lands after all `request_logs` column additions** — though §4.1
  now downgrades this one from a hard constraint to a preference, having measured 2.27.2 accepting
  `ADD COLUMN` on compressed chunks directly. And Phase 4's refresher must not
  copy `lumen/services/health.py` *because* Phase 1 turns that file into a single-runner election.
  These hold no matter what order the phases are worked in.
- **G0.6 is a safety gate, not a scheduling one**, and still blocks Phase 6 (see below).

| # | Question | How to get it | What it decides |
|---|---|---|---|
| # | Still gates what, now that everything ships |
|---|---|
| G0.1 | **[ANSWERED — §4.1: 1 process x 1 replica]** **Production topology** — replicas, `--workers`/`WEB_CONCURRENCY`, `LUMEN_WSGI_WORKERS`. Via `kubectl get deploy -o yaml`, or `GET /metrics/debug`, which already prints workers × replicas and the live `WSGI_*` thread count. **Decides what we test against**: at 1×1 the multi-process paths (dead-PID reaping, `livesum` aggregation, flock election) are never exercised in production, so their only coverage is the test suite — which raises the bar on the Phase 1 tests rather than lowering it |
| G0.2 | **Is Redis deployed?** `redis.enabled`, or an external `redis.url` / `rate_limiting.storage_url`. **Decides whether Phase 5 also carries a provisioning task**, and whether its fail-open path is the normal case or the exception |
| G0.3 | **[ANSWERED — §4.1: 2.27.2 / pg17, no toolkit; measured, not inferred]** **Timescale version + is `timescaledb_toolkit` installed?** `SELECT extname, extversion FROM pg_extension WHERE extname LIKE 'timescale%'`. **Decides**: exact `percentile_agg` p95 vs hand-rolled bucket counts; hierarchical continuous aggregates; and — newly — whether adding a column to a compressed hypertable is restricted, which sets how firmly compression must trail the schema work |
| G0.4 | **Size and growth of `request_logs`** — `SELECT pg_size_pretty(pg_total_relation_size('request_logs')), count(*), min(time), max(time) FROM request_logs`. **Decides** the retention window and, more urgently, how long the entity-aggregate's **full-history backfill** will run, since it cannot happen inside the migration transaction |
| G0.5 | **[PARTLY ANSWERED — §4.1: yes, `/metrics` is enabled with a token]** **Does a Prometheus/Grafana stack scrape this cluster, at what interval?** **No longer decides ordering.** Still decides whether the ServiceMonitor needs the token work below, and what scrape interval the snapshot refresh should be tuned against |
| **G0.6** | **Are the backends' `/metrics` reachable, and what engines/versions are they?** **Weakened, not retired, by the `http_sd` design in §10.1.** Under `http_sd` Prometheus scrapes the backends and Lumen never sends the endpoint API key anywhere, so a misidentified backend costs a down-looking target rather than a leaked credential. What still gates: reachability *from Prometheus*, and positively knowing each endpoint's engine — today only SGLang is detected, and vLLM is a fall-through indistinguishable from OpenAI, Azure or any OpenAI-compatible proxy |

**[GATE] G0.6 blocks Phase 6.** The rest are inputs to be written into this document as they arrive;
they no longer hold work up.

### 4.1 Answers received

**2026-08-18 — production has Prometheus enabled.** Reported from a production config file
(`lumen.yaml`) whose `api:` block reads:

```yaml
api:
  consent: false
  monitoring:
    token: <redacted>
  prometheus:
    enabled: true
    token: <redacted>
```

**What this settles.**

- **G0.5 is half-answered.** `api.prometheus.enabled: true` with a token set means `/metrics` is
  served and *something* is scraping it — nobody sets a scrape token for an endpoint nobody scrapes.
  Still open: **what** scrapes it (Prometheus Operator vs. a static `scrape_config`), at what
  interval, and whether Grafana sits in front. That remainder is what decides the ServiceMonitor
  token work and the snapshot refresh interval, so G0.5 stays open — but the existence question
  behind it is answered *yes*.
- **The HTTP metrics middleware is installed in production.** This is the one that matters most for
  work already merged. Per §2.3 of the LESSONSLEARNED doc, `make_metrics_middleware` is only wrapped
  when `api.prometheus.enabled` is true, and the chart default is `false` — so everything captured
  inside that middleware (`lumen_wsgi_queue_depth`, `lumen_wsgi_queue_wait_seconds`,
  `lumen_rejections_total`, the HTTP counters and histograms) is live in production, not dark. The
  design rule that produced this still holds and is not relaxed: **nothing outside Prometheus may
  depend on the middleware being installed**, because dev, tests and any operator who flips the flag
  off must keep working.
- **The T0 bridge is unconditional, and that remains the load-bearing fact.** `asgi.py` wraps the
  app in `DisconnectAwareWSGIMiddleware` with no flag, and `create_app` registers
  `_record_queue_wait` / `_stash_url_rule` outside the `if prom_cfg.get("enabled")` block. So
  `started_at`, `queue_wait`, `send_blocked` and the `disconnect` outcome are populated on the
  `request_logs` row regardless of this setting. Production having Prometheus on is a bonus, not a
  precondition.

**What this does *not* settle.**

- **G0.6 is untouched.** It asks about the *model backends'* `/metrics` (vLLM / SGLang
  `num_requests_waiting`), not Lumen's own. Lumen exporting metrics says nothing about whether
  Prometheus can reach a backend, nor which engine any endpoint runs. **[GATE] G0.6 still blocks
  Phase 6.**
- **`multiproc_dir` is absent from the reported block**, and the chart only renders that key when
  `api.prometheus.multiprocDir` is non-empty. Two readings, and they differ materially:
  1. Production runs one WSGI process per pod (`wsgiProcesses: 1`), in which case single-process
     mode is correct and the Phase 1 multi-process paths are exercised only by the test suite —
     the G0.1 answer, arrived at sideways.
  2. Production runs more than one and `multiproc_dir` was never set, in which case **`/metrics` is
     already wrong today**: each worker keeps its own in-memory registry and a scrape returns
     whichever worker answered, so counters appear to jump backwards at random.
  Reading 2 is a live production defect independent of this plan. Distinguish with
  `GET /metrics/debug`, which prints workers × replicas — one request settles it.
- **`consent: false`** is unrelated to metrics (it exempts API requests from the graylist
  model-consent requirement) and is recorded here only because it arrived in the same block.

**Which file is `lumen.yaml`.** The repo has no file by that name; the `api:` block above is
**schema-identical** in the two candidates, so the name alone cannot decide it. Discriminate by what
sits beside `api:` at the top level:

- `image:`, `replicaCount:`, `ingress:`, `serviceMonitor:` → it is a **Helm values override**, fed to
  `helm -f lumen.yaml`. `chart/templates/config-secret.yaml` then renders it into the app's
  `config.yaml` inside a Secret. Note the case shift on the one key that differs: values use
  `multiprocDir`, the rendered config uses `multiproc_dir`.
- `app:`, `llm:`, `models:`, `rate_limiting:` → it is **the app's `config.yaml` itself**, mounted
  directly and read by `create_app` via `CONFIG_YAML`.

Either way the read site is the same: `lumen/__init__.py:119` for the middleware decision and
`lumen/blueprints/metrics/routes.py:33` for the endpoint's auth.

**2026-08-18 — G0.1 answered, G0.3 half-answered.** From `GET /metrics/debug` on production
(`v1.25.0`) and the production compose image:

```
worker processes: 1 (WEB_CONCURRENCY=unset)  replicas: 1 (LUMEN_REPLICAS=unset)
wsgi thread pool: 64 per process (LUMEN_WSGI_WORKERS=auto), 58 spawned
engine options: pool_size=60 max_overflow=20 pool_timeout=10 pool_recycle=1800
postgres max_connections: 100
pool budget: (pool_size+max_overflow) x workers x replicas = 80 of 100
QueuePool: size=60 checked_in=49 checked_out=1 overflow=-10 max_overflow=20
postgres image: timescale/timescaledb:2.27.2-pg17
```

- **[ANSWERED] G0.1 — production is 1 × 1.** The absent `multiproc_dir` is therefore *correct*,
  and reading 2 above (a silently broken `/metrics`) is ruled out. The consequence stands as the
  table row wrote it: the Phase 1 multi-process paths — dead-PID reaping, `livesum` aggregation,
  flock election — are **never exercised in production**, so the test suite is their only coverage.
  That raises the bar on those tests; it does not license deleting them, because `wsgiProcesses`
  is a one-line change away and the chart now supports it.
- **[ANSWERED] G0.3 (version half) — Timescale 2.27.2 on PG17.** This is comfortably past 2.13,
  **which is the release that flipped `timescaledb.materialized_only` to default `true`**. The §8
  warning about a continuous aggregate hiding the last hour of every user's `/usage` is therefore
  a live defect-in-waiting on this exact deployment, not a version-dependent maybe. The
  `materialized_only = false` decision in §8(b) is confirmed as required.
- **[OPEN] G0.3 (toolkit half).** `timescale/timescaledb` is the community image; the toolkit ships
  in `timescale/timescaledb-ha`. So `percentile_agg` is **probably absent** and §8(f) takes the
  hand-rolled fixed-bucket path. Confirm before writing the aggregate — one query settles it:
  `SELECT extname, extversion FROM pg_extension WHERE extname LIKE 'timescale%';`

**[FINDING — pre-existing, outside this plan's scope] The connection budget does not survive a
rolling deploy.** One pod holds **50 open connections at idle** (`pool_size + overflow = 60 + (-10)`;
SQLAlchemy's `_overflow` starts at `-pool_size`), and `QueuePool` never shrinks below what it has
opened. `max_connections` is 100, of which `superuser_reserved_connections` (default 3) is not
available to the app. During any rolling update the draining pod and the starting pod are both up,
so the steady-state floor alone is 2 × 50 = 100 > 97 usable, and the *ceiling* is 2 × 80 = 160.
The failure mode is `FATAL: sorry, too many connections` on the new pod during deploys. Not caused
by this work and not fixed by it — but this plan adds a background refresher that also draws from
that pool, so it is recorded here rather than left implicit. Mitigations are all one-liners
(`maxSurge: 0` or the `Recreate` strategy, a smaller `pool_size`, or a larger `max_connections`);
picking one is the operator's call.

**[FINDING — this is what the plan is for] The WSGI thread pool has a 58-of-64 high-water mark.**
a2wsgi runs `ThreadPoolExecutor(max_workers=64, thread_name_prefix="WSGI")`, and CPython's executor
spawns a thread **only when no idle thread is available** and never reaps one. So "58 spawned" is a
monotonic high-water mark: at some point since this pod started, 58 requests were simultaneously
in flight. Six more and requests begin sitting in the executor's unbounded work queue — which is
exactly what `lumen_wsgi_queue_depth` and `lumen_wsgi_queue_wait_seconds` (Phase 1, and **live in
production** per the Prometheus answer above) were added to measure. Note the pool is sized by
`resolve_wsgi_workers`'s `auto` path, which clamps `pool_size + max_overflow = 80` down to
`MAX_AUTO_WSGI_WORKERS = 64` — so the thread ceiling, not the DB pool, is the binding constraint,
and `queue_wait` rather than `preflight` is where a class-start burst will show up first.

**2026-08-18 — G0.3 closed by measurement, not by reading release notes.** `timescale/timescaledb:2.27.2-pg17`
was pulled and run locally; every claim below is a transcript, not an inference.

- **[ANSWERED] `timescaledb_toolkit` is absent, and not even installable.**
  `pg_available_extensions` lists exactly one matching row — `timescaledb 2.27.2` — so
  `CREATE EXTENSION timescaledb_toolkit` cannot succeed: the community image ships no control file
  for it. **§8(f) takes the hand-rolled fixed-bucket path; `percentile_agg` is off the table**
  unless the deployment moves to `timescale/timescaledb-ha`, which is a separate decision with its
  own cost.

- **[CONFIRMED] `materialized_only` defaults to `true`, and the consequence is reproducible.**
  A continuous aggregate created without the option reports `materialized_only = t`. Seeded with one
  five-day-old row (cost 10) and one row in the current bucket (cost 99), then refreshed with
  `end_offset => 1 hour` exactly as the existing policy would:

  ```
   src  | sum        <- raw table
   raw  | 109
   cagg |  10        <- the aggregate. The current bucket is simply not there.
  ```

  This is the §8(b) defect, demonstrated: **91% of that user's spend is invisible through the
  aggregate.** `ALTER MATERIALIZED VIEW ... SET (timescaledb.materialized_only = false)` then
  returns 109, matching raw. The `[DECISION]` in §8(b) is confirmed by experiment rather than
  assumed, and the equality test in §8(a) step 2 **must** seed a row inside the current bucket —
  seeded with history alone it passes at 10 = 10 and certifies the regression.

- **[CORRECTED] Compression is *not* a one-way door on 2.27.2.** Against a hypertable with three
  genuinely compressed chunks (verified via `timescaledb_information.chunks.is_compressed`, not
  assumed from a `compress_chunk` call that silently matched nothing):

  | statement | result |
  |---|---|
  | `ADD COLUMN ttft double precision` | ok |
  | `ADD COLUMN qw double precision DEFAULT 0` | ok |
  | `ADD COLUMN oc text NOT NULL DEFAULT 'ok'` | ok |
  | `ADD COLUMN bad text NOT NULL` | **ERROR:** `cannot add column with NOT NULL constraint without default to a hypertable that has columnstore enabled` |

  Only the last is refused, it is refused loudly, and it is the same restriction `y9z0a1b2c3d4`
  already documents — now phrased in 2.27's columnstore vocabulary. §8(e) is resolved to its first
  branch and the Gate 0 preamble's "decompress/recompress migration" claim is downgraded to a
  preference. **A first attempt at this test proved nothing**: `show_chunks(older_than => '1 day')`
  matched zero chunks because the default 7-day chunk interval put both seeded rows in one
  still-open chunk, so the `ADD COLUMN`s ran against an *uncompressed* table and trivially passed.
  Worth recording as the shape of a test that looks green and asserts nothing.

- **[VERIFIED] The migration chain applies cleanly on the exact production version.**
  `tests/integration` against 2.27.2-pg17: **14 passed**, the only skips being the Redis suite with
  no `LUMEN_TEST_REDIS_URL`. Nothing in `f7a8b9c0d1e2` or its predecessors is version-sensitive here.

- **[FINDING] CI tests a different Timescale than production runs.**
  `.github/workflows/test.yml` pins `timescale/timescaledb:latest-pg17` — a floating tag — while
  production pins `2.27.2-pg17`. The failure mode is precise and this plan is standing in it: the
  2.13 `materialized_only` flip is exactly the class of change that a floating tag adopts silently,
  so CI would validate §8 against semantics production does not have (or, after the next release,
  against semantics production does not have *yet*). Pin CI to the production version.

**2026-08-18 — two continuous-aggregate behaviours found during implementation, both measured.**
Neither is in the Timescale docs in a form that would have warned us, and both silently produce the
*exact* regression §12 exists to prevent, from directions the contract did not anticipate.

- **A refresh whose window END is in the future disables real-time aggregation for the current
  bucket.** The obvious month-by-month backfill loop refreshes `[month_start, month_start + 1 month)`,
  and on the final iteration that end is in the future. Timescale then advances the aggregate's
  watermark past `now()`, and real-time aggregation only unions raw rows *above* the watermark — so
  the current bucket is served from materialised data alone, frozen at refresh time. Measured:

  ```
  watermark after refresh | 2026-08-18 16:00:00+00   (now() was 15:19)
  raw 7 | cagg 2                                     <- 5 of 7 invisible
  ```

  Clamping each window end to `now()` leaves the watermark behind the current bucket and the later
  insert appears (6 = 6). **The backfill command, run to protect history, would otherwise have
  broken the present.** Note the first attempt at reproducing this did *not* fail: with
  `start => NULL` the watermark only advances to the end of existing data. It takes an explicit
  window over the current month to push it forward — which is precisely what the month loop does.

- **Rows older than the policy's `start_offset` become invisible, not merely un-materialised.**
  Once a policy job runs it advances the watermark, and real-time aggregation scans raw rows only
  above it. History that was never materialised sits below the watermark and simply vanishes from the
  view — a 40-day-old row disappeared from a 30-day `start_offset` aggregate the moment the
  background job first fired. This is contract (c) confirmed empirically rather than argued:
  **the query rewrite shows near-empty per-user history until the backfill has run**, and since
  `entrypoint.sh` runs `flask db upgrade` at container start, the aggregate and its policy go live
  the moment the code deploys. The raw-table fallback in §12(a) is therefore mandatory, not the
  "if they must ship together" contingency the contract phrased it as.

- **A background policy job racing an explicit refresh fails outright rather than waiting.**
  `LockNotAvailable` (SQLSTATE `55P03`), not a block-and-proceed. With a 1-minute schedule on
  `request_metrics_1m`, a 13-month backfill will collide; without a retry it aborts partway and
  leaves the operator with a half-filled aggregate and no record of which months completed.

- **`COMMENT ON MATERIALIZED VIEW` does not work on a continuous aggregate.** Despite the
  `CREATE MATERIALIZED VIEW` spelling, the object is `pg_class.relkind = 'v'` — a plain view over a
  hidden materialisation hypertable. Postgres answers `"..." is not a materialized view`.
  Use `COMMENT ON VIEW`.

---

## 5. Phase 1 — multi-process integrity

*Make the metrics that already exist true at N processes × M replicas. No new metrics.*

This phase is what earns the right to say the system "supports scaling beyond one process". It is
small and it is a prerequisite for everything after it.

### Changes

1. **Chart: mount an `emptyDir` at `api.prometheus.multiproc_dir`** and set it by default when
   `wsgiProcesses > 1`. Add `wsgiProcesses` to `chart/values.yaml` + `chart/values.schema.json` (CLAUDE.md §5),
   wire it to both `WEB_CONCURRENCY` (so `db_pool.detect_workers()` and uvicorn agree on one number)
   and `uvicorn --workers`.
2. **`entrypoint.sh`: wipe the multiproc dir before `exec uvicorn`.** Stale `*.db` files from a
   previous run's PIDs are summed into every counter otherwise — a restarted pod reports lifetime
   totals from two lives.
3. **Reap dead PIDs, do not merely mark them on clean shutdown.** Registering
   `prometheus_client.multiprocess.mark_process_dead` at shutdown handles SIGTERM and nothing else —
   **a worker killed by SIGKILL (uvicorn's post-grace-period kill, or the OOM killer) never runs it**,
   and its gauge file keeps contributing its last value to every `livesum` aggregate for the rest of
   the **pod's** lifetime, because the dir wipe in item 2 runs once per pod start, not per worker
   respawn. A worker that dies holding `queue_depth=5` leaves the fleet depth 5 too high
   indefinitely — during a burst, which is when workers are most likely to be OOM-killed and when the
   gauge matters most. **Add a reconciliation pass**: scan the multiproc dir for PID-suffixed files
   whose PID is no longer alive (`os.kill(pid, 0)`) and `mark_process_dead` them. Run it at worker
   startup **and** on each scrape (a directory listing plus a handful of signal-0 probes), and
   **before** `MultiProcessCollector(...)` is constructed in
   `lumen/blueprints/metrics/routes.py:192`, not after.

   **Wrap every `mark_process_dead` call.** **[FACT]** the library's implementation
   (`prometheus_client/multiprocess.py:177-183`) does an unguarded `glob` + `os.remove` with no
   `try`/`except`. Two processes reaping the same dead PID concurrently means the loser raises
   `FileNotFoundError` **into the scrape** — an unhandled 500 on `/metrics`. With N workers plus a
   ServiceMonitor scraping every pod, concurrent reaps are routine, and they cluster right after an
   OOM kill: the scrape breaks exactly when a worker just died. Catch `(FileNotFoundError, OSError)`
   per call, or serialise the reap behind the same non-blocking flock as item 7.

   **PID reuse is worse than "a stale file survives".** Files are named `gauge_{mode}_{pid}.db`, so a
   respawned worker that draws the same PID opens the **same mmap** and inherits the dead worker's
   values — a worker killed holding `queue_depth=5` gives its successor a permanent +5 offset on
   every `inc`/`dec`. An mtime cross-check does not help, because the new process rewrites mtime.
   **Do not "unlink your own pid's files at startup"** — an earlier draft said to, and it is wrong
   twice. (i) A blanket unlink of `*_{pid}.db` also removes **counter** files, violating the
   asymmetry stated two items above: a dead worker's counter increments are real history, and
   dropping them makes the summed counter *decrease*, which Prometheus reads as a reset. (ii) The
   only per-worker startup seam is `lumen/__init__.py`, where importing the middleware **constructs
   the metric objects** — `prometheus_client/values.py` opens and caches the mmap handle eagerly at
   construction — so unlinking there deletes files the process is still writing to, and that worker's
   metrics never appear in any scrape again, silently. **Instead call
   `mark_process_dead(os.getpid(), path)` before importing the middleware**: it removes exactly the
   live-gauge files and nothing else.
4. **Document the invariant at the definition site** in `lumen/blueprints/metrics/middleware.py`: every `Gauge` added from
   here on declares an explicit `multiprocess_mode`; `Counter`/`Histogram` need nothing. State the
   asymmetry explicitly so nobody "fixes" it later: **dead PIDs' counter files are summed, and that
   is correct** — those increments are real history. Only `livesum`/`liveall` gauges must exclude
   them. The two cases look alike and the wrong fix silently loses counts.
5. **Fix `_normalize_path` cardinality** — label by the matched Flask url_rule with a single
   `"<unmatched>"` bucket. **[FACT]** Today it only collapses `/\d+`, so a scanner hitting `/.env`,
   `/wp-admin`, … mints an unbounded set of label values in every process's memory *and* in the TSDB.
   This gets N× worse with N processes and is the one pre-existing bug that Phase 1 makes urgent.

   **This is not a one-line change to `_normalize_path`, and the obvious fix does not work.** The
   middleware captures `path` *before* calling `wsgi_app`, when routing has not happened yet — but
   `request.url_rule` is **also unavailable after `wsgi_app` returns**, because Flask's `wsgi_app`
   runs `ctx.pop(error)` in its `finally` before returning (verified against the installed Flask).
   The label closures therefore run with no request context at all. **Correct approach:** stash
   `environ["lumen.url_rule"] = request.url_rule.rule if request.url_rule else None` while the
   context is still current, and read it from `environ` in the counter and latency closures, falling
   back to `"<unmatched>"`. Keep raw `_normalize_path` for the context-anomaly log messages, which
   want the real path.

   **Use `teardown_request`, not `after_request`.** `after_request` is skipped when a non-`Exception`
   `BaseException` unwinds the request; `teardown_request` runs regardless. The middleware's existing
   `except BaseException` path plus the `"<unmatched>"` fallback would cover the gap, but there is no
   reason to leave one.

   **Bound `method` in the same change — the label set has three dimensions, not one.**
   **[FACT]** `_http_requests` is labelled `["method", "path_template", "status"]`
   (`lumen/blueprints/metrics/middleware.py:22-26`) and `method` comes straight from
   `environ["REQUEST_METHOD"]` (`:117`). HTTP methods are RFC token grammar, so a scanner sending
   `PROPFIND`, `TRACK`, `FOOBARBAZ`, … mints a new series per method — the same unbounded growth,
   the same N× amplification across processes. Allow-list against the standard methods with an
   `"<other>"` bucket. **The Phase 1 exit gate must scan junk *methods* as well as junk paths**,
   otherwise it passes while the bug it exists to close stays open through the other dimension.
6. **Chart: a `ServiceMonitor`** (guarded by `serviceMonitor.enabled`, default false), scraping every
   pod. Multi-replica aggregation is Prometheus's job; it cannot do it if nothing is scraped.
7. **Elect a single health-probe runner.** **[FACT]** `lumen/services/health.py:27` holds a module-global
   `ThreadPoolExecutor(max_workers=8)` — one **per process** — and `BACKGROUND_WORKER`
   (`lumen/__init__.py:470`) is a process-wide env var that uvicorn's children all inherit, so it
   cannot mean "extra workers only". At `wsgiProcesses=4` with 10 endpoints that is 40 probes across
   32 threads every 60 s, aimed at the same GPU servers the students are queued behind, and the
   probes compete with real traffic precisely during a burst.

   **[DECISION] Elect, don't tolerate.** An earlier draft said "accept and document"; that was the
   wrong call once the per-process executor was accounted for. A `fcntl.flock` on a well-known file
   at the top of the probe pass is ~5 lines, needs no election service and no Redis, and is
   **naturally scoped to the pod** (a shared `emptyDir`), which is exactly the right granularity —
   one probe pass per pod, every pod probing. Non-holders skip the pass and read the DB result the
   holder wrote.

   **The lock must be non-blocking.** `fcntl.LOCK_EX | fcntl.LOCK_NB` — try, and skip the pass if
   someone holds it. A *blocking* flock would be strictly worse than the problem it solves:
   `lumen/services/health.py:81` commits inside the pass, that commit can block up to `pool_timeout` on an exhausted
   pool, and every other process would then queue behind a hung holder — stalling all health probing
   during exactly the burst when health data matters.

   **Do not add a "deadline after which the holder releases".** An earlier draft did; it recreates
   the problem. Nothing in the pass is preemptible — `future.result(timeout=_PROBE_TIMEOUT)`
   (`lumen/services/health.py:60`) is serial and legitimately runs 10 s × N endpoints,
   `_probe_executor` abandons stuck threads rather than killing them (documented at `:22-25`), and a
   blocked `commit()` cannot be interrupted from another thread. A watchdog could only unlock the
   flock **while the holder is still running**, admitting a second concurrent pass against the same
   GPU endpoints and raising the odds of the `StaleDataError` already handled at `:82`.
   **Instead: bound the pass by construction** (the probes already are; give the `commit()` a short
   statement timeout) and use **lock-file mtime staleness** — a non-holder that finds the heartbeat
   older than 3× the interval takes over.

   **Two mechanical details that decide whether it works:**
   - **Open mode.** Opening the lock file `"w"` truncates *before* the `flock` attempt, so every
     non-holder's failed attempt wipes the holder's heartbeat. Use `os.open(..., O_CREAT | O_RDWR)`
     or `"a+"`.
   - **Path.** Use a fixed container path (`/tmp/lumen-health.lock`), **not** the multiproc emptyDir:
     that volume only exists when `multiprocDir` is configured, and all uvicorn workers share one
     container anyway — a plain container path is already pod-scoped, so the emptyDir buys nothing.

### Tests

| Test | File | Asserts |
|---|---|---|
| Multiproc aggregation | `tests/unit/test_metrics_multiprocess.py` (new) | With `PROMETHEUS_MULTIPROC_DIR` set to a tmpdir, two child processes each increment `lumen_http_requests_total`; a third process's `MultiProcessCollector` reports the **sum** |
| Stale-PID hygiene | same | A dead PID's counter file still sums (correct for counters); after `mark_process_dead`, its `livesum` gauge file does not |
| Startup wipe | `tests/unit/test_entrypoint_multiproc.py` (new) | `entrypoint.sh` removes `*.db` under the dir before exec (shell-level assertion, or a Python re-implementation of the same guard) |
| Gauge mode guard | `tests/unit/test_metrics_middleware.py` (extend) | Static check: every `Gauge(...)` constructed in `lumen/` passes `multiprocess_mode` — same static-analysis shape as the existing `tests/unit/test_no_stream_with_context.py` |
| Path cardinality | `tests/unit/test_metrics_middleware.py` (extend) | 50 requests to distinct unmatched paths produce **one** `path_template` label value |
| Chart render | `tests/unit/test_chart_values.py` (new) | `helm template` with `wsgiProcesses: 4` renders the volume, the mount, `WEB_CONCURRENCY=4`; `chart/values.schema.json` accepts it |

### **[GATE] Phase 1 exit**

- `helm template --set wsgiProcesses=4` renders a pod that, when run, reports **one** set of HTTP
  counters equal to the sum of its four processes — verified by hand against a local 4-worker run.
- A pod restart does not increase any counter's reported total.
- `/metrics` label-value count for `path_template` is bounded by the number of Flask url_rules,
  proven by scanning a running instance with 100 junk paths.
- **Until this gate passes, `wsgiProcesses > 1` is not a supported configuration** and should be
  documented as such.

---

## 6. Phase 2 — the missing queue numbers

*Close the Q1 blind spot. Correct at N processes from the first commit.*

This is the highest value-per-line change in the whole plan and it is unchanged by the topology
argument — the queue is per-process, and per-process queues sum.

### Changes

1. **Stamp T0** — `time.monotonic()` **and** a wall-clock `started_at` into `environ` in
   `_DisconnectAwareWSGIResponder.__call__` (`lumen/services/wsgi_disconnect.py`), immediately before
   `loop.run_in_executor(...)`. **Read T1** at the top of the WSGI call in the worker thread.
   `local_queue_wait = T1 − T0`.

   **[DECISION] The capture must not live in the Prometheus middleware.** **[FACT]**
   `lumen/__init__.py:99-104` installs `make_metrics_middleware` **only when
   `api.prometheus.enabled`**, and the chart default is `enabled: false`
   (`chart/values.yaml:139-141`). Capturing T0/T1 there would leave `queue_wait`, `preflight` and
   `started_at` NULL on every row in the default deployment, making the headline historical query
   unanswerable for exactly the installations most likely to need it. T0 goes in
   `lumen/services/wsgi_disconnect.py` (always in the request path, via `asgi.py`) and T1 in a Flask
   `before_request` (always registered). Only the *histograms* live in `lumen/blueprints/metrics/middleware.py`.
2. **Explicit depth counters** around the submit — an `itertools.count`-free pair of `inc`/`dec` on a
   lock-free counter, **not** `executor._work_queue.qsize()` (private API, and it excludes the
   items already handed to threads).
3. **New metrics**, all in `lumen/blueprints/metrics/middleware.py` next to the existing three:
   - `lumen_wsgi_queue_wait_seconds` — Histogram, no labels, buckets `0.001 … 60`.
   - `lumen_wsgi_queue_depth` — Gauge, `multiprocess_mode='livesum'`.
   - `lumen_wsgi_threads_busy` / `lumen_wsgi_threads_total` — Gauges, `livesum` / `livesum`.
   - `lumen_rejections_total{reason, source, model}` with
     `reason ∈ {rate_limit, coin_budget, no_access, needs_consent, no_healthy_endpoint}`.
     `model` is empty for `rate_limit` — the body is unparsed at that point, and saying so is honest.
4. **Distinguish the two 429s.** Coin exhaustion gets `code: insufficient_quota` (matching the
   OpenAI taxonomy the API otherwise follows) instead of sharing `rate_limit_exceeded`. Add
   `Retry-After` to both paths — **[DECISION]** this is a genuine burst mitigation, not cosmetics:
   300 OpenAI-SDK clients given a bare 429 retry on independent schedules and can synchronise into a
   storm; the SDK honours `Retry-After`.
5. **Short-circuit already-disconnected queued work.** If the disconnect `Event` is set when the work
   item finally starts, return a 499-shaped response without running preflight, and count it as
   `lumen_rejections_total{reason="queue_shed"}`. **[FACT]** the disconnect pump is created *before*
   `run_in_executor`, so the flag is already accurate for queued requests — the information exists
   and is currently discarded.
6. **Shed before the gateway does, and record it.** A request that waits 400 s in the Q1 queue and
   then streams for 200 s hits the 600 s `gateway.timeout` (`chart/values.yaml`) and is cut from
   outside. Lumen records **nothing** about why: the client vanishes, and it is indistinguishable
   from any other disconnect. At the start of the upstream call, compare elapsed time against the
   budget — **`gateway.timeout − (T2 − T0)`, i.e. minus `queue_wait` *and* `preflight`**, not
   `queue_wait` alone; under the burst that makes preflight large, omitting it hands the request a
   few seconds of budget that do not exist. If the budget is spent, fail fast with 429 +
   `Retry-After` rather than starting a generation that cannot be delivered.

   **The 429 must happen in the VIEW, not "at the start of the upstream call".** **[FACT]** On both
   streaming paths the upstream call lives inside a generator that does not begin executing until
   body iteration — by which time Flask has already called `start_response`, and a2wsgi has already
   queued `http.response.start` with **status 200** to the client
   (`a2wsgi/wsgi.py:244-256`; chat at `lumen/blueprints/chat/routes.py:247`, API at
   `lumen/blueprints/api/routes.py:584`). A check placed where an earlier draft said would leave only
   an in-band SSE `error` event on a 200 — which the OpenAI SDK does not treat as a retryable 429 and
   which carries no `Retry-After`, defeating the anti-synchronisation argument in item 4. **Put the
   admission check in the views, before the `Response(...)` is constructed** — and compute the spend
   as `time.monotonic() - environ["lumen.t0_monotonic"]`, i.e. **from T0**. An earlier draft said
   "using T1", which is `queue_wait` alone and therefore exactly the under-count this item's own
   decision calls wrong: everything the view does after `before_request` — auth, model lookup,
   `check_coin_budget`, the conversation query, the pool checkout — is post-T1 preflight. Measuring
   from T0 captures it. State plainly that the check still excludes the *generator's* preflight,
   which happens later.

   **Two cases, and be honest that this catches one of them.** The admission check catches requests
   whose budget is *already* spent at admission. A request that starts with 100 s of budget and then
   streams for 200 s is still cut mid-generation by the gateway, and Lumen's disconnect detection
   fires — so it records `disconnect`, which is exactly the conflation this item claims to fix.
   Closing that requires a **mid-stream deadline check** at the existing inter-chunk poll
   (`lumen/services/llm.py:807`, which already runs per chunk and costs nothing extra): when elapsed
   exceeds the budget, end the stream and record `outcome='timeout'` **before** the gateway's cut can
   be mistaken for a disconnect. Give it a **safety margin**: the check only runs *between* chunks
   (bounded by `LLM_READ_TIMEOUT`) and the gateway's clock starts before Lumen's T0, so a budget of
   exactly the gateway's own value loses the race and still records `disconnect`.

   **The budget is a new config key — it does not exist in the app today.** **[FACT]**
   `gateway.timeout` is a Helm value consumed only by `chart/templates/httproute.yaml`, guarded by
   `gateway.enabled` (default false), and it is never rendered into `config.yaml` — grep
   `chart/templates/` confirms no `gateway` key reaches the app. In the default ingress deployment
   the real cut comes from ingress annotations, so copying `gateway.timeout` would be wrong rather
   than merely missing. **Add an explicit `api.request_budget_seconds`**, hot-loaded (see
   `lumen/services/config_watcher.py` for the pattern), mirrored into `chart/values.yaml` and
   `values.schema.json` per CLAUDE.md §5, documented as "must match whatever fronts Lumen". Do not
   derive it from `gateway.timeout`.
7. **DB pool checkout-wait histogram** — `lumen_db_pool_wait_seconds`. Today the pool is observable
   only as a *depth*, so "the pool is full" and "the pool is full **and requests are queued behind
   it**" look identical. §13a.1's write-spike measurement depends on this and referenced it as though
   it already existed.

   **[FACT] An earlier draft said `lumen/services/pool_tracker.py` "already hooks `checkout`/`checkin`,
   so the wait is a subtraction away". That is wrong.** Verified against SQLAlchemy 2.0.52: the
   complete `PoolEvents` set is `checkin, checkout, close, close_detached, connect, detach,
   first_connect, invalidate, reset, soft_invalidate` — **there is no pre-checkout event**.
   `checkout` fires *after* the connection has been acquired, so time blocked inside
   `QueuePool._do_get` is not the difference between any two event timestamps; it is invisible to the
   event API entirely.

   **Corrected approach:** time the acquisition itself by subclassing the pool and wrapping
   `_do_get`. That is a larger change than "a subtraction away", it touches a semi-private
   SQLAlchemy method, and it therefore needs a test that fails loudly if that signature moves. Size
   it accordingly. The metric and an `observe_pool_wait()` helper already exist in
   `lumen/blueprints/metrics/middleware.py`, awaiting a call site.
8. **Load-test harness:** split TTFT from total elapsed in `loadtesting/locustfile.py`, and add a
   step load shape (0 → N in seconds). A ramp does not reproduce a class start.

### Tests

| Test | File | Asserts |
|---|---|---|
| Queue wait measured | `tests/unit/test_wsgi_queue_metrics.py` (new) | With `workers=1` and two concurrent requests where the first sleeps 200 ms, the second's observed `queue_wait` ≥ 150 ms and the first's ≈ 0 |
| Depth rises and returns | same | Depth gauge > 0 while requests are parked; **returns to exactly 0** after all complete — including the exception path |
| Depth is decremented on every exit | same | Parametrised over: normal return, view raises, `_StalledClient`, client disconnect mid-stream. This is the leak-prone part |
| Multi-process sum | `tests/unit/test_metrics_multiprocess.py` (extend) | Two processes each with depth 3 report `lumen_wsgi_queue_depth == 6` under `livesum` |
| Shed on pre-known disconnect | `tests/integration/test_disconnect.py` (extend) | A request whose client disconnects while queued never reaches the view; the counter increments |
| Rejection taxonomy | `tests/routes/test_metrics_routes.py` (extend) | Over-limit ⇒ `reason="rate_limit"` + `Retry-After`; zero-coin ⇒ `reason="coin_budget"` + `insufficient_quota` + `Retry-After`; the two are distinguishable in both body and metric |
| No token-path cost | `tests/unit/test_llm_functions.py` (extend) | Streaming 500 chunks performs **zero** `Histogram.observe` calls (assert via a patched observe) |

### **[GATE] Phase 2 exit**

- Locust step load, 300 users, dummy backend, `wsgiWorkers=10`: `lumen_wsgi_queue_depth` peaks near
  290, `queue_wait` p95 tracks it, **both return to zero** within seconds of the run ending. A depth
  gauge that does not return to zero is a leak and blocks the phase.
- The same run at `wsgiProcesses=4` reports a single fleet depth, not four series.
- Run the burst twice in the same process; the second run's baseline is 0, not the first run's peak.

---

## 7. Phase 3 — schema and a database CI

*The columns that unlock every retrospective question — and the CI that can actually test them.*

**[FACT] The prerequisite is real.** `tests/conftest.py:34` builds the schema with `db.create_all()`
on SQLite, `.github/workflows/test.yml` provisions **no services**, and `tests/unit/test_migrations.py`
only checks the Alembic graph shape without a database. **Nothing in the suite has ever executed the
hypertable, the continuous aggregate, or a single line of the `/api/usage/*` SQL** — those endpoints
return empty on the dialect check before reaching the query. Anything aggregate-shaped built without
fixing this ships untested.

### Changes

1. **CI service container.** Add `timescale/timescaledb:latest-pg17` as a service to
   `.github/workflows/test.yml`, plus a `postgres` pytest marker and a session fixture that runs
   `flask db upgrade` (not `create_all`) against it. Existing SQLite tests are untouched and still
   run; the new marker is additive.
2. **New nullable columns on `request_logs`**, with column comments (CLAUDE.md §5):
   - `queue_wait` (Float) — T1 − T0, Lumen's own admission wait.
   - `ttft` (Float) — first chunk of **any** kind, including reasoning deltas.
   - `ttft_visible` (Float) — first *content* delta; today's `t_first`. Two columns, because on a
     reasoning model these differ by tens of seconds and conflating them makes a thinking model look
     like a queued one.
   - `send_blocked` (Float) — accumulated time blocked handing chunks to the server. Separates
     "slow client" from "slow backend", which `duration` currently conflates.
   - `outcome` (String(16)) — **enum limited to what can actually be written: `ok` and `disconnect`.**

     **[FACT] An earlier draft listed six values, four of which are unwritable, and defended
     `billing_error` on reasoning that is exactly backwards.** `request_logs` rows are only ever
     created inside `update_stats`, which ends at `db.session.flush()`
     (`lumen/services/llm.py:565-568`) — the `commit()` belongs to the caller. So:
     - **`billing_error` is unreachable.** The failing commit is the one that would have persisted
       the row; the rollback takes the flushed `RequestLog` with it
       (`lumen/services/llm.py:845-853`, `lumen/blueprints/api/routes.py:530-537`). The counter fires
       and the row does not exist. Including the value would *guarantee* the disagreement with the
       metric that including it was meant to prevent.
     - **`upstream_error` is unreachable.** Those paths return or re-raise before any `update_stats`
       (`lumen/blueprints/api/routes.py:378-380`, `lumen/services/llm.py:860-871`).
     - **`stalled_client` is indistinguishable from `disconnect`.** `_StalledClient` unwinds through
       a2wsgi's `finally: iterable.close()` into `GeneratorExit` → `_abort()` →
       `record_stream_abort(...)` with the default `reason="disconnect"`, and `send` has already
       called `self.disconnected.set()`. `record_stream_abort`'s own docstring says "nothing
       distinguishable is available at this seam."
     - **`timeout`** depends on §6 item 6 shipping the mid-stream check; add the value in that change,
       not this one.

     **[DECISION]** Ship `ok` and `disconnect`. Any further value must arrive together with the code
     that writes it. Making `upstream_error`/`billing_error` real requires a **best-effort insert on a
     fresh session after rollback**, which is new failure-path work and breaks the "statement count
     unchanged" property below — price it separately or not at all. `stalled_client` requires the
     responder to publish a distinct flag through a shared mutable holder (`_StalledClient` is caught
     in `__call__`, not in the generator, so an exception type cannot carry it).

   Nullable **with a server default**, per the precedent documented in
   `migrations/versions/e6f7a8b9c0d1_add_request_logs_aborted.py` — Timescale rejects propagating a non-defaulted NOT NULL
   column to populated chunks. **No backfill**, with the reason in the migration docstring.
   **Defaults, stated rather than left to the implementer:** `server_default='0'` on every Float
   column; `outcome` nullable with **no** default, where NULL means "written before this migration,
   unknown" — a default of `'ok'` would silently assert success about rows nobody measured.
   `send_blocked` is `0.0` on non-streaming paths (there is no send loop to block in), not NULL —
   NULL there would be indistinguishable from "not measured".
3. **Populate on all four paths** — chat stream, API stream, API non-stream, audio. TTFT is chat-only
   today, so the entire `/v1` surface is currently a latency blind spot.
4. **Composite index `(model_config_id, time DESC)`** — the access pattern every operator query uses.
5. **`lumen_llm_ttft_seconds{model, source}`** and `lumen_llm_duration_seconds{model, source, stream}`
   histograms, observed **once per request** at the end.
6. **`docs/dbschema.md`** updated (CLAUDE.md §5). **`CHANGELOG.md`** unreleased section.
7. **Do not touch `request_logs.time`** — it stays the completion timestamp and the partition key.
   Redefining it would silently shift every existing chart.
8. **`started_at` (TIMESTAMPTZ) — store the absolute start, do not derive it.**

   **[FACT] An earlier draft of this plan derived start as `time − duration − queue_wait`. That is
   wrong**, and wrong in the direction that matters. Verified in `lumen/services/llm.py`:
   - `t0 = time.time()` is set at `:752`, **after** the `with app.app_context():` preflight block
     (`:725-750`) that does the model lookup, endpoint selection, cache-salt derivation and timeout
     resolution. So `duration = T5 − T2`, excluding all preflight.
   - `RequestLog.time = datetime.now(timezone.utc)` is stamped inside `update_stats` (`:554-555`),
     called at `:848` **after** `duration` was computed (`:832`) and **after** `subtract_coins`.
     So `time ≈ T5 + billing_delay`.

   Therefore `time − duration − queue_wait = T0 + preflight + billing_delay`, **not `T0`.** The
   derived "waiting" interval is shifted right and shortened by the two unmeasured gaps — and
   `preflight` contains a DB pool checkout bounded by `pool_timeout` (10 s), so **the error is
   largest exactly during the burst this plan exists to diagnose.** The same gap makes the
   `user_perceived_wait = T4c − T0` SLI uncomputable from the stored columns.

   **[DECISION]** Store `started_at` as an absolute timestamp stamped at T0 and carried through on
   the same INSERT. The waiting interval becomes
   `[started_at, started_at + queue_wait + preflight + ttft_visible]` — a direct comparison, no
   reconstruction, no unmeasured gaps. One more column on a statement that already runs.

   **Type note:** `started_at` is `TIMESTAMPTZ`, matching `time` rather than the naive-UTC
   convention in CLAUDE.md §5. This is a deliberate, documented exception: the column exists to be
   compared and subtracted against `time` **on the same row**, and mixing naive and aware timestamps
   in that arithmetic is a Postgres footgun. Record the reason in the column comment and in
   `docs/dbschema.md`.
9. **Also store `preflight` (T2 − T1).** It is the only remaining unmeasured span in the request, it
   is where DB pool contention shows up, and without it "the queue was fine and the model was fine
   but requests were still slow" has no explanation.

### Implementation contract — decisions this plan makes so nobody has to stop and ask

These five were found by round-2 review as places an engineer would have to invent a design. They
are decided here.

**(a) One clock. All spans are `time.monotonic()` — and the conversion is SIX sites, not four.**
**[FACT]** T2 today is `t0 = time.time()` (`lumen/services/llm.py:752`, wall-clock; likewise
`lumen/blueprints/api/routes.py:368,462,641`), while this plan stamps T0/T1 with `time.monotonic()`.
**Subtracting them is meaningless**, and `queue_wait + preflight + ttft_visible` would mix three
clocks. §14's "monotonic for durations" is therefore **not** a review-checklist aspiration — it is a
required Phase 3 change.

**[FACT] The dangerous part is that `t0` escapes its function.** An earlier draft scoped the
conversion to "the four paths' own `duration` lines". That is wrong and would have shipped a
catastrophe. `record_stream_abort` computes its own duration **from the caller's `t0`**:

```
lumen/services/llm.py:686     duration=time.time() - started_at     # started_at IS the caller's t0
lumen/services/llm.py:772     started_at=t0                          # chat stream
lumen/blueprints/api/routes.py:475   started_at=t0                   # API stream
```

Convert `t0` to monotonic without touching `:686` and **every aborted row stores
`duration ≈ 1.76e9`** — about 55 years. That is the exact row set this plan exists to study
(disconnects during a class-start burst), it poisons `lumen_llm_duration_seconds` permanently into
`+Inf`, it makes any `AVG(duration)` on `/usage` garbage the moment one student closes a tab, and
**nothing raises**. The same applies to `t_first = time.time() - t0` (`llm.py:819`), which is
user-visible: it flows to `Message.time_to_first_token` and is rendered in the chat UI, and §7 item 2
defines `ttft_visible` as "today's `t_first`".

**TEN sites, not six — and an earlier draft of this very fix listed six.** The six obvious ones are
*subtractions*; the variables they subtract from are *assigned* four lines elsewhere:

```
assignments   lumen/services/llm.py:752            t0 = time.time()
              lumen/blueprints/api/routes.py:368   t0 = _time.time()
              lumen/blueprints/api/routes.py:462   t0 = _time.time()
              lumen/blueprints/api/routes.py:641   t0 = _time.time()
subtractions  lumen/services/llm.py:686, :819, :832
              lumen/blueprints/api/routes.py:377, :523, :649
```

Convert only the six and you get `duration = monotonic() − wall` ≈ **−1.76e9 on EVERY request on
every path** — not merely on aborted ones. That is strictly worse than the bug this fix exists to
prevent: `output_speed` silently becomes 0.0 behind `llm.py:842`'s `if duration > 0` guard,
`Message.time_to_first_token` goes negative in the chat UI, and `lumen_llm_duration_seconds` collapses
into the lowest bucket. Nothing raises.

**Guard it as a PRESENCE test, not a subtraction test.** An earlier draft specified "no `time.time()`
may appear in a subtraction" — which passes green in exactly the broken state above, because the four
survivors are plain assignments. **The invariant is: `time.time()` may not appear *at all* in
`lumen/services/llm.py` or `lumen/blueprints/api/routes.py`**, with the single exception of the
`wall_started_at` stamp. Shape it like `tests/unit/test_no_stream_with_context.py`.

**Note what does *not* break:** converting `:819` leaves `Message.time_to_first_token` and the chat UI
unchanged — it is a span on both sides, so the stored value is identical. An earlier draft raised an
alarm there that was unfounded.

**(b) `started_at` is stamped with `datetime.now(timezone.utc)`, not `utcnow()`.** CLAUDE.md §5
mandates `lumen.timeutils.utcnow()`, which returns **naive** UTC. Writing naive into a `TIMESTAMPTZ`
column makes Postgres interpret it against the session `TimeZone` — a silent, deployment-dependent
offset bug. `request_logs.time` already does the right thing (`lumen/services/llm.py:555`); `started_at` follows it
for the same reason and the same documented exception.

**(c) Threading the values into the generators — including the abort path.** `update_stats` is
called from five sites, **two of which are context-free streaming generators**
(`lumen/services/llm.py:848`, `lumen/blueprints/api/routes.py:533`) that must never touch `request`.
The pattern already exists and is documented: `client_disconnect_event()`
(`lumen/services/wsgi_disconnect.py:338-352`) is called in the view while the context is live and
captured into the generator's closure. Do exactly that — read `started_at`/`queue_wait` from
`request.environ` in the view, pass them as parameters into
`send_message_stream`/`_do_chat`/`_do_audio`, and add them to the `update_stats` signature.

**The fifth call site is the one that matters most and an earlier draft omitted it.**
`update_stats` is also reached at `lumen/services/llm.py:641` inside `record_aborted_request`, via
`record_stream_abort` (`:684`). **Both signatures must be threaded too.** Miss them and every
*aborted* row gets `started_at`/`queue_wait` NULL — the disconnect rows the headline "who was
waiting" query needs most, and the ones §6 item 5 and the abort-share SLI are about.

**Absent-key behaviour, stated to resolve a contradiction.** Falling back to `None` keeps the
Werkzeug dev server, the Flask test client and direct unit-test calls working (same reasoning as
`client_disconnect_event`'s docstring) — but note two consequences an earlier draft did not
reconcile:
- `None` inserts SQL NULL and **bypasses** the `server_default='0'` above. That is correct — "not
  measured" must stay distinguishable from "zero wait" — but it must be deliberate.
- It makes the Phase 3 test "all four paths write non-null `queue_wait`" **unsatisfiable under
  `test_client`**, which bypasses `asgi.py` entirely so T0 is never stamped. Move that assertion to
  the real-uvicorn harness (`tests/integration/test_disconnect.py:205-226`); the `test_client` suites
  assert `ttft`/`outcome` only.

**(d) Rename to avoid a collision — at BOTH call sites.**
`record_stream_abort(..., started_at=...)` (`lumen/services/llm.py:654`) already uses `started_at` to
mean **T2**. Rename it to `stream_t0`; two meanings of `started_at` one function apart is a bug
waiting to be written. It has **two** callers — `lumen/services/llm.py:772` **and**
`lumen/blueprints/api/routes.py:475`. Renaming only the first leaves the second raising `TypeError`
*inside* `_abort()`, which runs from the `except GeneratorExit` handler — so the `TypeError` replaces
the `GeneratorExit`, Python raises `RuntimeError: generator ignored GeneratorExit`, and **the abort
is never billed**. Only caught by tests if the API stream path is covered in
`tests/integration/test_disconnect.py`; check that it is.

**(e) `send_blocked` needs a mechanism, and the obvious one is wrong three ways.** **[FACT]**
`_DisconnectAwareWSGIResponder.send` (`lumen/services/wsgi_disconnect.py:241-269`) calls
`future.result(self.send_timeout)` and accumulates nothing, so the column as specified would be
permanently 0. The naive fix — "time each `future.result()`, publish into `environ`, read at billing
time" — fails on all three counts:

1. **It records zero for the stalled client.** The largest block of the request's life happens on the
   `except concurrent.futures.TimeoutError` branch, which raises `_StalledClient` without
   accumulating. So the column reads ~0 for exactly the outcome it exists to identify. **Accumulate
   in a `finally` around `future.result()`, so the timeout branch is counted.**
2. **The view cannot read it.** Billing on both streaming paths happens **in the generator**
   (`lumen/services/llm.py:846-853`, `lumen/blueprints/api/routes.py:531-536`), and at view time
   nothing has been sent yet — a value captured into the closure per contract (c) is `0.0` forever.
   **Capture a mutable holder** (a small dataclass or one-element list) published in `environ`
   alongside `ENVIRON_KEY`, so the generator reads the live total at billing time. Note the responder
   and the generator are on the *same* worker thread (`send` runs inside `run_in_executor`), so this
   is a timing problem, not a threading one.
3. **It excludes the tail.** Billing precedes the final chunks and `data: [DONE]`, by design, so the
   recorded value always omits them. Say so in the column comment.

**And be honest about what it measures:** time to enqueue onto a bounded `asyncio.Queue`, which also
absorbs event-loop scheduling latency. Under a 300-user burst that is *not* purely "slow client" —
which is what a naive column comment would claim. **If this lands after Phase 3, cut the column**
rather than shipping a permanently-zero one that reads as "no client backpressure ever".

**Per-path semantics of `preflight` — document, do not pretend uniformity.** It is *not* purely DB
contention on every path:
- **Audio** (`lumen/blueprints/api/routes.py:587-679`): includes `upload.read()` at `:605`, i.e. multipart parsing of
  the whole body, which for a large file dominates. High audio `preflight` is an upload, not a pool.
- **Chat stream**: there are effectively *two* preflights — the view's (`lumen/blueprints/chat/routes.py:213-237`) and
  the generator's second lookup (`lumen/services/llm.py:725-750`) — and `T2 − T1` spans both plus the handoff.
Record this in the column comment and in `docs/dbschema.md`, or an operator will read audio latency
as database pressure.

### Tests

| Test | File | Asserts |
|---|---|---|
| Migration round-trip on Timescale | `tests/integration/test_migrations_postgres.py` (new, `@pytest.mark.postgres`) | `upgrade` then `downgrade` clean against a populated hypertable; the new columns propagate to existing chunks |
| Hypertable still a hypertable | same | `timescaledb_information.hypertables` still lists `request_logs` after the migration |
| Continuous aggregate intact | same | `request_counts_hourly` refreshes and returns rows after the column addition |
| All four paths populate | `tests/routes/test_api_*.py`, `tests/routes/test_chat_routes.py` (extend) | Each path writes a row with non-null `ttft`, `ttft_visible`, `queue_wait`, `outcome` |
| Reasoning split | `tests/unit/test_llm_functions.py` (new case) | A stream of 3 reasoning deltas then content sets `ttft` at the first reasoning delta and `ttft_visible` at the content delta; `ttft < ttft_visible` |
| Abort sets outcome | `tests/integration/test_disconnect.py` (extend) | A mid-stream disconnect writes `outcome='disconnect'`, `aborted=True`, and a non-null `ttft` |
| The headline query | `tests/integration/test_usage_queries_postgres.py` (new) | Against seeded data, "distinct users waiting for model X at instant T" returns the hand-computed answer |
| **`started_at` is really T0** | `tests/integration/test_started_at_accuracy.py` (new) | A **real request through the full stack**, with T0 independently recorded by a test-only hook, asserts `\|started_at − recorded_T0\| < 0.1 s`. **Without this the seeded-data test above passes vacuously** — it proves the SQL is right, never that the data is. This is the test that would have caught the derivation bug. **Harness — reuse, do not invent:** Flask's `test_client()` bypasses `asgi.py` entirely, so T0 would never be stamped and the test would assert nothing. `tests/integration/test_disconnect.py:205-226` **already solves this**: it imports `asgi` with `lumen.create_app` patched to return the test app, then runs **real uvicorn** on an ephemeral port, deliberately so the test exercises the wiring production uses rather than a copy that can drift. Reuse that fixture and record the independent T0 from a test-only callback at the same point in `_DisconnectAwareWSGIResponder.__call__` |
| Preflight is non-zero and bounded | same | A request whose preflight includes a contended pool checkout records `preflight > 0`; `started_at + preflight + ttft ≈ first byte observed by the client` |
| Statement count unchanged | `tests/unit/test_llm_functions.py` (extend) | `update_stats` issues the **same number** of statements as before the change (columns ride the existing INSERT) |

### **[GATE] Phase 3 exit**

- CI is green with the Postgres+Timescale service, and **at least one test fails if the hypertable is
  replaced by a plain table** — proof the new suite actually exercises Timescale rather than passing
  vacuously.
- `flask db upgrade` → `downgrade` → `upgrade` on a database seeded with `seed_analytics.py` leaves
  `/usage` rendering identically.
- The added statement count per request is **zero**.

---

## 8. Phase 4 — decouple `/metrics`, introduce the LiveState seam

### Changes

1. **`LumenDBCollector` serves a background-refreshed snapshot.** **[FACT]** it currently queries the
   database on every scrape — a `GROUP BY` over `model_stats` (one row per entity × model × source)
   plus two `COUNT(*)` over `entities` — taking a pooled connection to do it. During the burst it
   competes for the pool it is reporting on and can block up to `pool_timeout`, so **`/metrics`
   degrades exactly when it is needed**. Refresh from a daemon thread on the same pattern as
   `lumen/services/health.py` every 30–60 s; `collect()` becomes a pure in-memory read.
   Export `lumen_metrics_snapshot_age_seconds` so staleness is visible rather than silent.
   *This is a cost reduction and it belongs early.*
2. **`lumen/services/live_state.py`** — the seam. Interface, deliberately minimal:
   ```python
   admit(model_key: str, entity_id: int) -> None
   release(model_key: str, entity_id: int) -> None
   snapshot() -> dict[str, ModelLive]     # inflight, unique_users
   topology() -> dict                     # scope, processes, replicas
   ```
   `LocalLiveState` only in this phase. Called exactly twice per request, from the same places that
   already own request lifecycle.
3. **Cache the `models_page` per-model counts.** **[FACT]** `lumen/blueprints/models_page/routes.py` runs two uncached
   `COUNT(*)` scans of `request_logs` per model-page view, while the API's equivalent is cached 30 s
   40 lines away in `lumen/blueprints/api/routes.py`. 300 students opening a model page during the incident is 600
   sequential scans of a growing hypertable at the worst possible moment. Copy the existing pattern,
   or read the snapshot.

### Implementation contract — decisions this plan makes so nobody has to stop and ask

Seven places where §8 as written reads as buildable but leaves a design decision to the implementer.
They are decided here. The module is `lumen/services/metrics_snapshot.py`; the seam is
`lumen/services/live_state.py`.

**(a) The refresher runs in EVERY process and is NEVER flock-elected.**
**[FACT]** Phase 1 item 7 has just turned `lumen/services/health.py` into a single-runner election
(`lumen/services/health.py:111-167`), and §8 item 1 says "the same pattern as `lumen/services/health.py`".
Copying that pattern here is a **fleet-breaking bug**: the election's non-holders `return 0` and do
no work (`lumen/services/health.py:239-242`), so N−1 workers would hold an empty snapshot forever.
Prometheus scrapes land on whichever worker the socket gives them, so `lumen_model_requests_total`
would alternate between the real cumulative value and absent — and a counter that disappears and
reappears lower is read as a **reset**, so `rate()` invents a spike on every recovery. The result is
sawtooth garbage on the exact series the operator uses to judge a burst.

**[DECISION] The general rule, stated so it is not re-litigated per daemon: elect only work whose
result lands in the DB.** Health probing qualifies — the holder writes `ModelEndpoint.healthy` and
every non-holder reads the same row back (`lumen/services/health.py:222-230`). Snapshot refresh does
not: its result is an in-memory object that only the refreshing process can see. **Never elect
in-memory state.** Put that sentence in the module docstring next to a pointer at `health.py`, or
the next reader will "unify the two daemons".

**Corollary — start it outside the `_run_background` guard.** **[FACT]** `lumen/__init__.py:549-555`
starts `start_health_checker` / `start_coin_refiller` / `start_config_watcher` only when
`BACKGROUND_WORKER != "false"`, which is the switch an operator is told to use to keep extra workers
from duplicating shared work. That switch is correct for shared work and wrong for per-process state:
setting it would silently give those workers an empty snapshot — the same failure as (a) by a
different route. `start_snapshot_refresher(app)` is called unconditionally.

**(b) One `app.app_context()` per pass, entered inside the loop and exited BEFORE the sleep.**
CLAUDE.md §5's absolute rule is phrased around `yield`, so a `while True:` daemon reads as exempt —
and hoisting the context out of the loop ("why push it 1440 times a day?") is the natural
optimisation that reproduces the 1.22.0 idle-in-transaction leak, one leaked connection per process,
held across a 60 s sleep. The rule's actual content is *no session and no Flask context may outlive
the work that needs it*; a sleep is a `yield` in every way that matters. `lumen/services/health.py:248-255`
already has the correct shape (`with app.app_context():` inside the loop, `time.sleep` outside it)
but only incidentally, with no comment saying why. **[DECISION]** Copy that shape, and this time
write the comment. Also copy the `try/except Exception: logger.exception(...)` wrapper at
`lumen/services/health.py:253-254`: an unhandled exception kills the thread and the snapshot then
ages forever with nothing logged after the first traceback.

**How the test proves it — assert during the sleep, not after the pass.** A test that checks
`pool.checked_out == 0` after a refresh completes passes under the hoisted-context bug too, because
the session is idle-but-checked-out only between passes. Give the module a `_sleep = time.sleep`
indirection, patch it, and from inside the patched sleep — i.e. on the refresher thread, while it is
"parked" — assert **both**:
- `db.engine.pool.checkedout() == 0`, and
- no app context is bound on that thread (`flask.globals.app_ctx` unbound), which catches the variant
  that pushes the context outside the loop but happens to have released the session.

Add a static guard in the shape of `tests/unit/test_no_stream_with_context.py`: in
`lumen/services/metrics_snapshot.py`, `app.app_context()` may appear only inside the loop body, and
`_sleep` may not appear inside a `with app.app_context():` block.

**(c) The snapshot is one frozen dataclass, and every future field declares its query cost.**

```python
@dataclass(frozen=True)
class MetricsSnapshot:
    primed: bool                                    # False until the first successful pass; see (e)
    captured_at: float                              # time.monotonic(), the only input to the age gauge
    captured_wall: datetime                         # naive UTC via lumen.timeutils.utcnow(), for display only
    model_usage: dict[tuple[str, str], ModelUsage]  # (model_name, source) -> requests, input, output, cost
    endpoint_health: tuple[tuple[str, str, bool], ...]   # (model_name, endpoint_url, healthy)
    users_active: int
    users_total: int
```

That is exactly the three statements `LumenDBCollector.collect()` runs today —
`lumen/blueprints/metrics/routes.py:67-78`, `:121-124`, `:135-140` — and nothing more. `collect()`
becomes a pure read of this object.

**[DECISION] Field-addition rule.** Phases 7 and 8 both want to add fields. Each addition states, in
the PR: (i) the exact statement it adds, (ii) whether that statement is index-backed, (iii) that it
rides the **same pass** — *no per-model loop*, ever, because an N+1 across models turns a bounded
3-statement pass into an unbounded one. A pass is capped at **5 statements**. Anything that reads
`request_logs` requires Phase 3 item 4's composite index `(model_config_id, time DESC)` and must say
so.

**Worked example, which also settles §8 item 3.** The models-page counts
(`lumen/blueprints/models_page/routes.py:49-60`, two uncached `COUNT(*)` per page view) become a
snapshot field, not a second 30 s cache — a second cache is a third source of truth for one number.
Both counts come from **one** statement: `GROUP BY model_config_id` over
`request_logs WHERE time >= now() - 24h`, with a `FILTER (WHERE time >= now() - 1h)` for the hour
bucket. One index-backed statement for all models, satisfying the rule.

**[DECISION] Refresh interval, chosen against the scrape interval — because per-process refresh can
*increase* total DB load.** Today the cost is 3 statements per **scrape per pod** (one worker serves
each scrape). After this change it is 3 statements per **process** per interval, i.e.
`processes × replicas × 3 / interval`. The rule:

> `refresh_interval ≥ processes × scrape_interval`, floor 30 s, default **60 s**, with ±10% jitter so
> the N workers of a pod do not align their passes into one burst against the pool.

At 4 workers and a 30 s scrape the rule says 120 s; take the 60 s default only with eyes open that it
is 2× today's statement rate. **That trade is still right**, and the reason is not "it is only 3
statements": the pass is bounded, off the request path, and never blocks a scrape, whereas today's
queries take a pooled connection *during* the burst and can block up to `pool_timeout` — §8 item 1's
whole premise. Staleness bound is `refresh_interval + scrape_interval`; it must be shorter than the
shortest window anyone alerts on.

**[DECISION] No lazy refresh-on-read.** `/metrics` never triggers a pass, not even when the snapshot
is stale. A refresh-if-stale branch puts the DB back on the scrape path on exactly the scrape that
follows a slow period — the failure this phase exists to remove.

**(d) The refresher starts even when `api.prometheus.enabled` is false.**
**[FACT]** `/metrics` 404s when the flag is off (`lumen/blueprints/metrics/routes.py:33`) and the
chart default is off, but `LumenDBCollector` is registered unconditionally today
(`lumen/__init__.py:110-114`), Phase 7a's `/admin/status` reads this same snapshot, and §8 item 3's
model-page counts do too. **[DECISION] Always start it.** The snapshot is the application's own cache
of its own state; gating it on a metrics flag would make an admin page and a user-facing page change
behaviour when an operator turns on Prometheus — the identical mistake Phase 2 item 1 already
rejected for T0/T1 capture (`lumen/__init__.py:128-139`, chart default `enabled: false`). Cost when
Prometheus is off: 3 statements per minute per process.

**(e) Cold start is a synchronous prime, and an unprimed snapshot emits NO `lumen_model_*` series.**
Without a prime, the first `refresh_interval` after every rolling restart has an empty snapshot, and
a fleet mid-restart mixes primed and unprimed workers — gaps, and the same counter-reset artefact as
(a). **[DECISION]** Run one pass synchronously in `create_app` before starting the thread, inside the
app context that already exists at `lumen/__init__.py:511`, wrapped in the same `try/except` +
`WARNING` shape as the `sync_*_from_yaml` calls there (a database that has not been migrated yet must
not stop the app from booting).

**[DECISION] If the prime fails, `collect()` yields the pool gauges and the age gauge and **nothing
else**.** Emitting zeros is strictly worse than emitting nothing: absent → present is an ordinary new
series to Prometheus, whereas present(1e6) → present(0) → present(1e6) is two resets and a fabricated
rate spike. `lumen_metrics_snapshot_age_seconds` is still emitted while unprimed (measured from
process start), because that gauge is precisely the signal an operator alerts on for this state.

**(f) The `admit`/`release` contract.** §8 item 2's signature is not sufficient — `release` cannot
find the right entry without a handle, and a handle is what makes Phase 5's multiplicity fix possible.

```python
@dataclass(frozen=True)
class LiveTicket:
    model_key: str
    entity_id: int
    request_id: str     # uuid4().hex
    deadline: float     # unix seconds; see below

admit(model_key: str, entity_id: int) -> LiveTicket
release(ticket: LiveTicket) -> None
snapshot() -> dict[str, ModelLive]   # inflight, unique_users
topology() -> dict
```

- **`admit` is called in the VIEW**, while the request context is live, *after* every rejection path
  (rate limit, coin budget, access, consent, no healthy endpoint) and after Phase 2 item 6's budget
  check — a rejected request must never appear in flight. Concretely, alongside the existing
  capture-into-the-closure lines: `lumen/blueprints/chat/routes.py:246-247` and
  `lumen/blueprints/api/routes.py:457-465`. The ticket is captured into the generator's closure,
  exactly as `client_disconnect_event()` already is (`lumen/services/wsgi_disconnect.py:538-553`,
  and per Phase 3 contract (c)).
- **`release` is called in the generator**, in a `finally:` wrapping the whole body of `generate()`
  (`lumen/blueprints/chat/routes.py:249`, `lumen/blueprints/api/routes.py:465`). On the two
  non-streaming paths (API non-stream, audio) it is a `try/finally` in the view around the upstream
  call.
- **`release` must never touch `db.session` or `current_app`.** It runs context-free. Both backends
  satisfy this (a dict under a lock; a Redis client). Anything a future implementation needs from
  config is read at `admit` time and carried on the ticket.

**[DECISION] The `finally` is the fast path, not the correctness argument — the deadline is.**
**[FACT]** the codebase already documents that a disconnected client's response generator **may never
be closed** under uvicorn + a2wsgi, so `GeneratorExit` and therefore `finally` may never run:
`send_message_stream`'s docstring at `lumen/services/llm.py:819-823` ("GeneratorExit alone is not enough … that handler never fires in
production") and the comment at `lumen/blueprints/chat/routes.py:281`. An abandoned generator that
never releases would inflate the count permanently. Therefore **every ticket carries a deadline** —
`time.time() + api.request_budget_seconds + 60` (Phase 2 item 6's new key; fall back to 660 s until
it lands) — and **both** backends exclude expired tickets from every number they report.
`LocalLiveState` is not exempt: without the deadline its dict leaks forever on exactly the disconnect
path this plan is about. Prune on every `admit()` and every `snapshot()`.

**[DECISION] "Live" means admitted → request completion, NOT admitted → first token.** This is the
choice that moves the headline number by ~10× during a long generation, so it is made here rather
than discovered in review:
1. It is the number Phase 9 needs. A generating request holds a backend sequence slot; a cap on
   "still waiting for the first token" caps nothing.
2. It keeps the per-request Redis budget at **two** commands (§14). Splitting the lifetime at first
   token needs a third state transition and therefore a third command, on the token path's edge.
3. The *waiting* question already has better answers: retrospectively from Phase 3's
   `started_at` + `ttft_visible`, live-and-aggregate from `lumen_llm_ttft_seconds`, and — for the
   only place a user sees it — from Phase 6's upstream queue depth, which is the backend's own
   running/queued count and is the *actual* queue position. Lumen's own in-flight count never was.

**Consequences that must be applied, not just noted:**
- **The UI label matches the meaning.** §11's tile reads *"12 requests in flight, 9 distinct users
  (this process — 1 of 4 processes × 2 replicas)"*. It must **not** say "waiting".
- **§11's chat "waiting for the model (N ahead of you)" is sourced from Phase 6**, not from
  `LiveState`. §11 already hedges it with "if the backend reports a queue"; this makes that binding
  explicit, and it means 7a's live tiles ship without that string.

**(g) `lumen_metrics_snapshot_age_seconds` is emitted from `LumenDBCollector`, not as a
`prometheus_client` Gauge.** **[FACT]** under `PROMETHEUS_MULTIPROC_DIR` a `Gauge` must declare a
`multiprocess_mode` (Phase 1 item 4), and every available mode is wrong here: `livesum` reports 4× the
age at 4 workers (a 30 s-old snapshot alerts as 120 s), `livemax` reports the worst worker's age but
detached from the values in the payload, `mostrecent` reports whichever worker wrote last.
**[DECISION]** Yield it as a `GaugeMetricFamily` from `collect()`, computed as
`time.monotonic() - snapshot.captured_at` on the process serving the scrape. That is *consistent*
because it then describes **the same snapshot whose sample values are in the same response** — an age
that does not belong to the payload it annotates is worse than no age at all. It is also already the
established convention in that collector: `lumen_db_pool_connections`
(`lumen/blueprints/metrics/routes.py:150-168`) is likewise per-serving-process and deliberately not
multiproc-aggregated. **[FACT, worth stating once]** that does mean only one worker's pool is ever
reported; that is pre-existing and out of scope here, but do not "fix" it by moving these gauges into
multiproc files — the same aggregation problem applies.

---

### Tests

| Test | File | Asserts |
|---|---|---|
| Scrape issues no SQL | `tests/routes/test_metrics_routes.py` (extend) | 20 consecutive `/metrics` requests execute **zero** statements (SQLAlchemy event listener counting `before_cursor_execute`) |
| Snapshot age exported | same | Gauge present and increasing between refreshes |
| Refresher releases its session | `tests/unit/test_db_teardown.py` (extend) | After a refresh cycle, pool `checked_out == 0` — the 1.22.0 leak class, guarded |
| Stale snapshot degrades visibly | same | With the refresher stopped, the age gauge grows and the values do not silently change |
| LiveState balance | `tests/unit/test_live_state.py` (new) | `admit`/`release` parametrised over every exit path leaves `inflight == 0` and `unique_users == 0` |
| Topology honesty | same | `topology()["scope"] == "local"` with no Redis; `processes`/`replicas` reflect `WEB_CONCURRENCY`/`LUMEN_REPLICAS` |
| Model page cached | `tests/routes/test_models_routes.py` (extend) | Second page load within the TTL issues no new `COUNT(*)` |

### **[GATE] Phase 4 exit**

- Scraping `/metrics` at 1 s for 60 s during a Locust burst causes **no** measurable change in pool
  checkouts and no change in request latency p95.
- `LiveState` counters return to zero after a 300-user burst that includes disconnects, upstream
  errors and stalled clients.

---

## 9. Phase 5 — the Redis backend

**No longer conditional** — this phase ships regardless of topology, which is what makes the Phase 4
interface justified rather than speculative (§2). G0.2 only decides whether it also carries a
provisioning task.

### Changes

1. **`RedisLiveState`** behind the Phase 4 interface. Per model: a Redis SET of entity ids for
   in-flight, `SADD`/`SREM`/`SCARD`. One `INCR`/`DECR` for in-flight count, or derive it from the set.
   Pipelined into one round trip per call site ⇒ **two round trips per request**, both off the token
   path.
2. **Selection at startup**, from the Redis URL that rate limiting already resolves — no new config
   key, no second URL to keep in sync.
3. **Fail-open wrapper.** `socket_timeout=0.25`, `socket_connect_timeout=0.25`, every call inside
   `try/except`, failure ⇒ local value + `lumen_live_state_errors_total{op}`.
4. **TTL self-healing.** Members expire above `gateway.timeout` (600 s), so a SIGKILL mid-request
   cannot leave a set permanently inflated. A periodic reconciliation against local state, logged
   when it corrects anything.
5. **Chart:** document that `wsgiProcesses > 1` **or** `replicaCount > 1` requires Redis (the values
   comment currently says replicas only). Fix the three pre-existing Redis chart bugs found in the
   proposal's §8 while in the file — `existingSecret`/`existingSecretKey` read by no template, the
   `auth.existingSecret` password-less URL, and `auth.password` landing in plaintext in the rendered
   config Secret. Add the `rate_limiting.storage_url` omission from `RESTART_REQUIRED` in
   `lumen/services/config_watcher.py`.

### Implementation contract — decisions this plan makes so nobody has to stop and ask

**(a) The data structure in §9 item 1 (and in §3's summary) is wrong twice. Replace it.**

**[FACT] Redis sets have no per-member TTL.** Expiry in Redis is per **key**; `SADD` members live and
die with the key. §9 item 4's "members expire above `gateway.timeout`" is not implementable on a SET,
and the only thing that *is* implementable — `EXPIRE` on the whole key — is worse than the leak it
was meant to fix: at 600 s it deletes the live members of a busy model, silently zeroing the count
during precisely a long burst.

**[FACT] `SREM` under-counts the double-submitting student.** With member = `entity_id`, a user with
three concurrent requests is one member, and the **first** completion `SREM`s them while two are
still in flight. A class-start burst is full of exactly this user — the one who hits send twice and
reloads the tab. The design under-reports the population it exists to measure.

**[DECISION] One sorted set per model.** Key `lumen:live:{model_key}`, member
`"{entity_id}:{request_id}"`, score = the ticket's deadline as unix seconds (Phase 4 contract (f)).

| operation | commands | when |
|---|---|---|
| `admit` | `ZADD key {deadline} {member}` | 1 command, in the view |
| `release` | `ZREM key {member}` | 1 command, in the generator's `finally` |
| prune | `ZREMRANGEBYSCORE key -inf {now}` | in `snapshot()` only — see below |
| inflight | `ZCOUNT key {now} +inf` | `snapshot()` |
| unique users | `ZRANGEBYSCORE key {now} +inf` then dedupe the `entity_id` prefix in Python | `snapshot()` |

Why this shape:
- **Multiplicity is correct by construction** — three concurrent requests from one user are three
  members with one shared prefix, so `inflight` is 3 and `unique_users` is 1, and releasing one
  removes exactly one.
- **Expiry is by score, not by TTL**, so a SIGKILLed process's orphans stop being *counted* the
  instant their deadline passes (every read is bounded by `{now}`), independently of when they are
  physically removed. That is §9 item 4's self-healing, actually implemented.
- **No key `EXPIRE` is needed at all**: Redis deletes a sorted set automatically when its last member
  is removed, so an idle model leaves nothing behind.
- **[DECISION] Derive in-flight; do not keep a separate `INCR`/`DECR` counter** (§9 item 1 offers
  both). A plain counter has nowhere to store a deadline, so a SIGKILL inflates it permanently — the
  one failure mode §3's TTL bullet exists to prevent — and it would be a third command per request.

**(b) The cost of the unique-user count is O(members), not O(1). Say it out loud, because it changes
two budgets.** There is no Redis primitive for "count distinct prefixes in a sorted set", so
`unique_users` requires transferring the live members and deduping in Python: O(N) in commands' worth
of bytes and in client CPU, N = live members for that model.

- **Per-request budget survives intact, but only because prune moved to the reader.** `admit` = 1
  command, `release` = 1 command — **exactly 2, as §14 requires, literally**. Do **not** pipeline
  `ZREMRANGEBYSCORE` into `admit`: that is the obvious tidy-up and it silently makes the budget 3.
- **Snapshot cost, with Phase 4's per-process refresh, is `processes × replicas × models × members`
  per interval.** At 300 concurrent requests over 10 models that is ~3000 short members moved per
  pass per process — tens of kilobytes, single-digit milliseconds. Acceptable. It is also why
  **[DECISION] `snapshot()` is the only caller permitted to read members; no request path may issue
  `ZRANGEBYSCORE`.**
- **[DECISION] One round trip per pass, not one per model.** Pipeline every model's
  prune + `ZCOUNT` + `ZRANGEBYSCORE` into a single pipeline, or a 10-model deployment pays 30 round
  trips a minute per process for no reason.
- **The escape hatch, documented but not built:** if live members ever exceed ~10⁴ on one model,
  switch `unique_users` to a per-model HASH of `entity_id → refcount` (`HINCRBY` ±1, `HLEN` for an
  O(1) unique count) and accept a third command per request. Do not do this speculatively — it
  reintroduces the orphan problem the scores solve, since a HASH field cannot carry a deadline.

**(c) `LocalLiveState` needs the same multiplicity fix, and the same deadline.** A `set[entity_id]`
has the identical `SREM` bug in-process, and a bare `dict[entity_id, count]` cannot expire orphans
because there is nowhere to hold a deadline. **[DECISION]** `dict[model_key, dict[request_id, tuple[entity_id, deadline]]]`
under one lock: `inflight = len(inner)`, `unique_users = len({entity_id for ...})`, expired entries
excluded from both and pruned on `admit()` and `snapshot()`. This is deliberately the same triple the
sorted set holds, which is what makes a single parametrised suite runnable against both backends and
what guarantees the fallback does not change the *meaning* of a number when Redis drops out — only
its scope, which `topology()` reports.

**(d) Fail-open, stated precisely enough to be symmetric.** An `admit` whose Redis call raises or
times out still **returns a usable `LiveTicket`** and records it in the local fallback state;
`release(ticket)` on a failing Redis is a no-op plus `lumen_live_state_errors_total{op="release"}`.
Never return a ticket that `release` cannot accept — an asymmetric fail-open turns a Redis blip into
a permanent count inflation, which is the bug wearing the costume of the fix.

**Key namespace:** the URL comes from rate limiting (`lumen/__init__.py:168-171`,
`RATELIMIT_STORAGE_URI`) per §9 item 2, so two Lumen deployments pointed at one Redis with the same
db index will merge their counts. Fixed prefix `lumen:live:`; document the caveat next to the chart
note in §9 item 5 rather than inventing a config key for it.

**(e) Which of §9's existing tests are wrong under the corrected structure.**

| §9 test as written | verdict | replacement |
|---|---|---|
| Fleet-wide unique users — "`SCARD` is the **union**, not the sum" | **Only passes in the broken design.** There is no `SCARD` any more, and the member count *is* the in-flight count, not the unique-user count | Two processes admit overlapping entity sets **and one entity admits three concurrent requests**; assert `snapshot()[model].unique_users == len(union of entity ids)` **and** `inflight == total tickets`. Releasing one of the three leaves `unique_users` unchanged — that assertion is the entire point of the phase's redesign |
| TTL set — "every key written carries a TTL > `gateway.timeout`" | **Wrong, and inverted.** No key carries a TTL; a correct implementation would fail this test, and an implementation that passed it would have reintroduced whole-key expiry that drops live members | Every member's score is ≥ `now + api.request_budget_seconds`; a member whose score has passed is excluded from `inflight` and `unique_users` **before any prune runs**, and is physically gone after the next `snapshot()` |
| Bounded call count — "≤ 2 Redis commands per request" | **Survives, but only because prune is the reader's job.** Keep it | Keep, extend over the abort/disconnect path, and add the counterpart: `snapshot()` issues **zero** commands per request and **one round trip** per pass |
| Reconciliation — "an orphaned member is removed and the correction is logged" | Keep, restate | Simulate a killed process holding three tickets: **before** the deadline they still count (honest — nothing can know yet); **after** it they are excluded and removed on the next pass. Log **once per pass with a count**, not once per member — 300 orphans after a pod kill would otherwise emit 300 lines into the incident |
| Both fail-open tests, and the timeout test | Unchanged | Add: an `admit` that fails against Redis returns a usable ticket, and `release(ticket)` on it does not raise |
| — | **New** | One parametrised suite in `tests/unit/test_live_state.py` run against **both** `LocalLiveState` and `RedisLiveState`, asserting identical numbers for the same admit/release/expire sequence — including multiplicity and deadline. Without it the two implementations drift and the fallback silently changes the number's meaning |

### Tests

| Test | File | Asserts |
|---|---|---|
| Fleet-wide unique users | `tests/integration/test_live_state_redis.py` (new, `@pytest.mark.redis`) | Two processes admit overlapping entity sets; `SCARD` is the **union**, not the sum |
| Fail-open on outage | `tests/unit/test_live_state.py` (extend) | With a Redis client whose every call raises, `admit`/`release`/`snapshot` all succeed, the request completes, the error counter increments, `topology()["scope"] == "local"` |
| Fail-open on timeout | same | A client that sleeps 5 s does not add 5 s to the request — the wrapper's 0.25 s timeout bounds it |
| Bounded call count | same | One proxied request performs **≤ 2** Redis commands, asserted with a counting fake, including on the abort path |
| TTL set | same | Every key written carries a TTL > `gateway.timeout` |
| Reconciliation | same | An orphaned member (simulated killed process) is removed and the correction is logged |

### **[GATE] Phase 5 exit**

- With Redis stopped mid-burst, **no request fails and no latency percentile moves**; only the admin
  page degrades, and it says so.
- Two pods × two processes report one unique-user count equal to the true union, verified against
  `request_logs` after the run.

---

## 10. Phase 6 — upstream queue depth

**[FACT]** From Lumen's side, "queued behind 40 sequences" and "the model is slow" are the same
number. Only the backend can tell them apart. **[FACT]** `lumen/services/model_sync.py` already probes
SGLang's `/get_server_info` at the server root by stripping `/v1`, and already detects the backend
type — but **discards it**.

### 10.1 Primary mechanism: Lumen tells Prometheus what to scrape (`http_sd`)

**[DECISION] Adopt Prometheus HTTP service discovery, with Lumen as the discovery source.** This
supersedes both options the proposal weighed. Credit where due: it came from the project's senior
developer, unprompted and with no knowledge of this design — which is worth noting, because it
independently reached the same safety conclusion as this plan's own review (§16 round 3, F9) by a
much cheaper route.

Lumen already knows every backend: they are rows in `model_endpoints`. So instead of Lumen polling
the backends, or an operator hand-maintaining scrape config, Lumen publishes a target list and
Prometheus polls *that*:

```yaml
- job_name: backends
  http_sd_configs:
    - url: https://lumen/metrics/targets
      refresh_interval: 60s
      # Lumen's metrics auth is a BEARER token, not basic auth
      # (lumen/blueprints/metrics/routes.py:_metrics_auth_required).
      authorization:
        credentials: <api.prometheus.token>
  metrics_path: /metrics
```

returning `Content-Type: application/json`:

```json
[{"targets": ["spark0:8000"],
  "labels": {"lumen_model": "ornith-1.0-35b", "engine": "vllm", "host": "spark0"}}]
```

Add a model to Lumen, and it appears in monitoring on the next poll — no Prometheus reload, no
restart, no chart change.

**Why this beats what the plan had.** The proposal's Option A (a ServiceMonitor on the model
Deployments) only ever covered models the chart itself deploys; endpoints configured in `config.yaml`
that live outside the cluster were structurally uncovered. `http_sd` covers every row in
`model_endpoints` by construction.

**And it removes the credential risk outright.** §16/F9's objection to Option B was that a Lumen-side
scraper could send `Authorization: Bearer <endpoint api_key>` to a host that is not a model server.
Under `http_sd`, **Prometheus** scrapes the backends and Lumen never sends the key anywhere. The
worst outcome of a misidentified backend becomes a target that reads as down, not a leaked
credential. This substantially de-risks **G0.6**, though it does not retire it: whether backend
`/metrics` is reachable *from Prometheus* still has to be true.

**Three things to get right.**

1. **Emit only positively identified engines** — `engine in ("vllm", "sglang")`. Hosted providers
   (OpenAI, Anthropic, Azure, another Lumen) expose no `/metrics` and would sit in the target list as
   permanently-down, indistinguishable from a crashed model. This is the same "positive evidence
   only, never a fall-through" rule F9 arrived at, and it requires the `backend` column below.
2. **Name the label `lumen_model`, never `model_name`.** vLLM emits its own `model_name` label; on a
   collision Prometheus renames the scraped one to `exported_model_name`, which silently breaks every
   join. Keeping both also makes it visible when the served name and Lumen's alias have drifted.
3. **Emit only backends that have been healthy at least once.** A brand-new endpoint with a typo'd
   URL otherwise appears as a down target, identical to a backend that crashed.
   **[DECISION] This needs a new column** — `first_healthy_at` (nullable timestamp, set once by the
   health checker). Filtering on `last_checked_at IS NOT NULL` is wrong: a misconfigured endpoint is
   still *checked*, it just always fails. And filtering on `healthy = true` is worse — it would drop
   a crashed backend out of service discovery and destroy the `up == 0` alert, which is the signal
   you actually want.

**Route:** `GET /metrics/targets`, behind the existing `_metrics_auth_required`. It exposes internal
hostnames and ports to anyone holding the metrics token, which is acceptable at that trust level —
but it must never include `api_key`, and a test should assert that.

### 10.2 Secondary, and only if the in-app surface needs it

**Correction to the proposal's reasoning.** It recommended "Lumen polls the backends" as *primary*,
on the grounds that Grafana cannot answer "how many distinct users were waiting for model X at 09:05"
because it does not know who the users are. That argument does not survive contact with the design:
user identity comes from `request_logs` (§7), and the backend gauges carry no user identity in either
architecture. The join was never between "backend depth" and "user" — it is between
`request_logs.started_at` and time.

What Lumen-side polling still genuinely buys is narrower, and worth doing only if these are wanted:

- **`/admin/status` without a Prometheus dependency** — which matters exactly when G0.5 says no
  Prometheus stack exists (§11).
- **Historical upstream depth in Lumen's own database**, joinable to `request_logs` in SQL.

If built, it is the background scraper as previously specified: modelled on `lumen/services/health.py`
(bounded executor, hard deadline, best-effort), parsing a **configured allowlist** of gauge names per
backend type into the Phase 4 snapshot — names are a moving target across vLLM versions, so
configuration, never constants, and an unknown name degrades to "unknown" rather than to a crash or a
silently wrong gauge. Note it re-acquires the credential risk that `http_sd` avoids, so it must not
send the endpoint API key to a `/metrics` path.

### 10.3 Supporting work, either way

- Persist the detected `backend` type and the concurrency capacity (`--max-num-seqs`, or the SGLang
  equivalent) on `model_endpoints`, so depth has a denominator. **[FACT]** vLLM's `/metrics` exposes
  `num_requests_running`/`num_requests_waiting` but **no capacity gauge**, so for the most likely
  backend the denominator must be operator-configured — with `model_sync` never overwriting a
  manually set value.
- Add `first_healthy_at`, set once by the health checker (§10.1 item 3).
- Add health-probe **latency** to `lumen/services/health.py`, which records only a boolean today. A
  backend whose probe latency has quadrupled is degrading before it flips unhealthy.
- The Lumen-self ServiceMonitor added in Phase 1 stays; it is orthogonal. `http_sd` removes the need
  for a *second* ServiceMonitor on the model Deployments.

### Tests

- `/metrics/targets` requires the token; returns valid `http_sd` JSON; **never contains any
  `api_key`** (assert against the raw body).
- Hosted-provider endpoints are excluded; only `vllm`/`sglang` appear.
- An endpoint that has never been healthy is excluded; one that *was* healthy and is now down is
  **included** (so `up == 0` still fires).
- The label is `lumen_model`, and `model_name` appears nowhere in the emitted labels.
- URL → target derivation strips `/v1` and yields `host:port`, including for a URL with a path prefix.
- If the Lumen-side scraper is built: allowlist parse against captured fixtures from both engines;
  unknown metric name ⇒ gauge absent plus one warning, not an exception; scraper thread releases its
  DB session (`tests/unit/test_db_teardown.py`).

**[GATE] Phase 6 exit** — a model added to Lumen appears as a live Prometheus target within one
`refresh_interval`, with no Prometheus restart; and saturating a real backend beyond its
`max_num_seqs` moves `num_requests_waiting` for that target. **[GATE] G0.6 still applies**, now in its
weaker form: backend `/metrics` must be reachable *from Prometheus*, and the engine of each endpoint
must be positively known rather than inferred.

### Implementation contract — decisions this plan makes so nobody has to stop and ask

**(a) `backend` is a three-valued column, and `vllm` requires positive evidence.**
**[FACT]** `lumen/services/model_sync.py:80-95` tags `sglang` only when `/get_server_info` returns a
body containing an SGLang-specific key — the code comments say a 200 alone is not proof. vLLM is
never tagged; it is the **fall-through** at `:97-108`, which is equally reached by OpenAI, Azure,
another Lumen, or any OpenAI-compatible proxy.

**[DECISION]** `model_endpoints.backend` is `'sglang' | 'vllm' | 'unknown'`, defaulting to `unknown`.
`vllm` is set only on positive evidence — an unauthenticated `GET {root}/metrics` that returns a body
containing `vllm:`-prefixed metric families. Never by elimination. An endpoint that fails to identify
stays `unknown` and is simply absent from service discovery; a permanently-down target is a worse
outcome than an unmonitored one, because it trains operators to ignore the alert.

**(b) Service discovery never carries a credential, and the payload is asserted to.**
`GET /metrics/targets` emits `targets` and `labels` only. `model_endpoints.api_key` must not appear,
directly or in a URL. **[DECISION]** a test asserts the raw response body contains no endpoint API
key, using a key value seeded specifically to be searched for. This is cheap and it guards the one
mistake that would matter: the endpoint is behind the metrics token, so it is trusted, but a
credential in a scrape-target list gets copied into Prometheus config and then into git.

**(c) The label is `lumen_model`. Never `model_name`.**
vLLM emits its own `model_name` label. On collision Prometheus renames the scraped one to
`exported_model_name`, which silently breaks every join written against it. Keeping Lumen's alias
under a distinct name also makes drift visible when the served name and the configured alias diverge.
A test asserts `model_name` appears in no emitted label set.

**(d) Emit only endpoints that have been healthy at least once — which needs a new column.**
A brand-new endpoint with a typo'd URL otherwise appears as a down target, indistinguishable from a
backend that crashed. Neither existing field expresses "has worked":
- `last_checked_at IS NOT NULL` is wrong — a misconfigured endpoint is still *checked*, it just
  always fails.
- `healthy = true` is worse — it drops a crashed backend out of discovery and destroys the `up == 0`
  alert, which is the signal the whole job exists to produce.

**[DECISION]** Add `model_endpoints.first_healthy_at` (nullable timestamp, naive UTC per CLAUDE.md),
set once by the health checker on the first successful probe and never cleared. Discovery filters on
`first_healthy_at IS NOT NULL`.

**(e) The capacity denominator is operator-configured, and `model_sync` never overwrites it.**
**[FACT]** vLLM's `/metrics` exposes `num_requests_running` and `num_requests_waiting` but **no
capacity gauge**; SGLang exposes `max_running_requests`. So for the most likely backend the
denominator cannot be discovered. **[DECISION]** `model_endpoints.max_concurrency` (nullable int),
set by an admin. Where the backend does report it, `model_sync` may fill it **only when it is NULL**,
never replacing a value a human set. A NULL denominator renders as "unknown", never as a guess —
saturation displayed against an invented capacity is worse than no saturation figure.

**(f) The Lumen-side scraper (§10.2) is REQUIRED if the chat queue indicator is wanted.**
An earlier draft deferred it as optional, which silently contradicted §11's contract. **[FACT]**
under `http_sd`, **Prometheus** scrapes the backends, and Lumen has no Prometheus query client
anywhere in the tree — the codebase only ever *exposes* metrics. So `num_requests_waiting` lives in
Prometheus and never enters Lumen's process. §11(d) names it as the only honest source for "N ahead
of you", so with the scraper deferred that feature is unbuildable and the "elapsed time only"
fallback becomes permanent by accident.

**[DECISION]** Pick one deliberately, and record which:
- **Build the scraper** (§10.2) as part of Phase 6 — it is the prerequisite for the chat indicator
  and for `/admin/status` working without Prometheus. It must not send the endpoint API key to a
  `/metrics` path, which is the risk `http_sd` otherwise removes.
- **Or drop "N ahead of you"** from §11 and from this phase's stated benefits, leaving chat with the
  elapsed-time indicator permanently.

What is not acceptable is leaving the two sections contradicting each other, or putting a Prometheus
HTTP query on the chat request path — which this plan forbids elsewhere for exactly this reason.

**(g) Emit `__scheme__` and `__metrics_path__`, not a bare `host:port`.**
Endpoint URLs are arbitrary base URLs, and `_sglang_root` strips only a trailing `/v1`, leaving any
path prefix in place. For `https://spark0:8000/gateway/v1`, a bare `host:port` target under a job
with `metrics_path: /metrics` makes Prometheus scrape **`http://spark0:8000/metrics`** — wrong scheme
and wrong path — and the target reads permanently down, which §10(a) identifies as the worst
available outcome. Derive the port explicitly with a scheme default, and emit both meta-labels per
target. IPv6 literals need bracketing.

**(h) Exclude models that are not servable.** `ModelConfig.active` and `.disabled` already gate
routing (`lumen/services/llm.py:307-318`). An operator who disables a model and shuts its backend
down otherwise gets a permanently-down target — the same failure (d) exists to prevent, reached by
another route. Filter on `active AND NOT disabled`.

**(i) Who writes these columns, and what survives a URL edit.** **[FACT]** the only backend-detection
code path runs from the admin config editor against unsaved JSON and never touches `model_endpoints`
rows; endpoint rows are reconciled from YAML **keyed by URL**, so changing a URL is a delete + insert
that carries across only `api_key` and `model_name`. Left alone, `backend` stays `unknown` forever
(so discovery is empty and Phase 6 monitors nothing), and fixing a port typo silently resets
`max_concurrency` and `first_healthy_at`.
**[DECISION]** the detection probe runs in the **health checker** pass, which already writes
`ModelEndpoint` rows and is already elected; `max_concurrency` is a `config.yaml` endpoint field
synced through the reconciler (with the `chart/values.yaml` + `values.schema.json` updates CLAUDE.md
requires), not a DB-only field with no UI to set it; and the reconciler must carry `backend`,
`first_healthy_at` and `max_concurrency` across a URL change.
**[DECISION]** allow an explicit per-endpoint `backend:` in `config.yaml` that **wins over
detection**, and surface `unknown` with its reason on the admin models page. Otherwise a vLLM behind
an auth-all ingress, or started with `--disable-log-stats`, sits unmonitored and nothing says why.
**[OPEN]** whether `first_healthy_at` is backfilled for existing endpoints at migration time —
without it, every currently-working backend is invisible to discovery until its next probe.

**Tests.** Token required; valid `http_sd` JSON shape; no API key in the body; hosted providers,
`unknown` backends and disabled/inactive models excluded; an endpoint that was healthy and is now
down is still **included**; the label is `lumen_model`; and target derivation yields the correct
`__scheme__`/`__metrics_path__`/port triple for an **https, path-prefixed** URL, not merely
`host:port`.


---

## 11. Phase 7 — the views

**[DECISION] Split into 7a and 7b — a dependency, not a scheduling preference.** Every phase ships,
so this one no longer jumps the queue on a G0.5 answer. But §11 as written consumes Phase 8's
1-minute aggregate and Phase 8's percentile decision, so it cannot all be built at once:

- **7a — live tiles**, reading the Phase 4 snapshot and LiveState only. Depends on Phase 4 alone, and
  must issue **zero SQL** (§14 already asserts this). Buildable as soon as Phase 4 lands.
- **7b — historical charts**, which need `request_metrics_1m` from §12.2 and the exact-vs-approximate
  percentile decision from G0.3. Strictly after Phase 8's aggregate work.

Building 7b early would mean charting raw `request_logs` — reintroducing the burst-time hypertable
scans Phase 4 exists to remove, on the one page most likely to be open *during* the incident. A
`period=all` p95 over raw per-user rows is a full-history scan per page load.

- **`/admin/status`**, reclaiming the dead `/admin/analytics` redirect slot (its 346-line orphaned
  template is a stale near-copy of `usage.html`). Live tiles from the Phase 4 snapshot + LiveState —
  **never** from the database — plus historical charts from the Phase 8 1-minute aggregate. Chart.js
  4 as everywhere else. Every live tile carries the `topology()` label.
- **Chat:** when nothing has arrived yet, an elapsed indicator and, if the backend reports a queue,
  "waiting for the model (N ahead of you)". This is the single change that most improves the
  class-start experience — a student who knows they are queued does not reload, and every reload is
  another rate-limit bucket entry and another thread.
- **Model detail:** "typical wait right now" (median TTFT, last 5 min) from the snapshot, replacing
  the two uncached `COUNT(*)` scans.
- **`/usage` single-user:** median and p95 TTFT and abort share — free from the Phase 3 columns, and
  it answers the question a student actually has.
- **Nav** in all four theme headers; consider a shared admin-nav partial while there.

**Accessibility (CLAUDE.md §6) — the non-obvious ones for this page:**
auto-refreshing numeric tiles are **`aria-live="off"`** with an explicit Refresh button, and a
*separate* small `role="status"` region announces only **state transitions** ("model X is now
queued"). A polite live region updating every 5 s is unusable with a screen reader. Every `<canvas>`
gets `role="img"` + `aria-label` + text fallback; every table a `<caption class="visually-hidden">`;
queue state never colour-only; timestamps as `<span class="local-datetime" data-utc="…Z">`.

**Tests:** `tests/ui/test_accessibility.py` extended to cover `/admin/status` (it already runs the
compliance sweep); `tests/routes/test_admin_routes.py` for `@admin_required` on both the page and
`/admin/api/status`, and that the JSON endpoint executes **zero** SQL statements; a snapshot test that
the topology label renders "this process" when `LocalLiveState` is active. Re-capture affected
screenshots in `docs/img/` and update the matching `docs/guides/` pages (CLAUDE.md §5).

### Implementation contract — decisions this plan makes so nobody has to stop and ask

**(a) 7a and 7b are separate deliverables with different dependencies. Do not merge them.**
7a (live tiles) depends on Phase 4 only. 7b (historical charts) needs `request_metrics_1m` from §12.2
and the percentile decision from G0.3. Building 7b early means charting raw `request_logs` — a
`period=all` p95 over per-user rows is a full-history scan **per page load**, on the one page most
likely to be open during an incident, reintroducing exactly the burst-time scans Phase 4 removed.

**(b) 7a issues zero SQL BEYOND the `admin_required` identity lookup.**
An earlier draft said "zero SQL", full stop. That is **unsatisfiable by construction**: **[FACT]**
`admin_required` (`lumen/decorators.py`) does `db.session.get(Entity, session["entity_id"])` on every
call, and a fresh session per request makes that a real SELECT. The `/metrics` precedent does not
carry over — `_metrics_auth_required` is a bearer-token comparison with no DB access at all. A test
asserting zero would fail on the first commit, and the cheapest way to green it is to weaken the
assertion or drop the decorator: one erases the invariant, the other opens an admin endpoint.

**[DECISION]** the constraint is **no data query**: exactly one statement per request, and it targets
`entities`. The `before_cursor_execute` test asserts the count *and* the statement's target table, so
a data query added later cannot hide behind the auth lookup. §14's invariant row carries the same
wording. The page auto-refreshes on a timer, so one data query here becomes a query every few seconds
per open admin tab, during the incident.

**(c) Every live number renders with the topology it was computed from.**
Use `LiveState.topology()`. A tile reading "12 users waiting" that is silently per-process at 4
processes is not a smaller number, it is a **wrong** number, and the failure is invisible precisely
because it looks plausible. Render "(this process — 1 of 4 × 2 replicas)" or the fleet-wide
equivalent. **[DECISION]** when `LocalLiveState` is active at more than one process, the tile must say
so in text, not only in a tooltip.

**(d) "N ahead of you" in chat is Phase 6 data, not Phase 4 data.**
Per §8's contract, "live" means admitted→completion, so `LiveState`'s in-flight count includes users
who are happily streaming — it is not a queue position. The honest source for "N ahead of you" is the
backend's own `num_requests_waiting` (Phase 6). **[DECISION]** until Phase 6 lands, chat shows an
elapsed-time indicator only, with no position claim. A wrong queue position is worse than none: a
student told "3 ahead" who then waits four minutes learns the number is a lie and reloads, which is
the behaviour the indicator exists to prevent.

**(e) A model has many endpoints; say which number is shown.**
**[FACT]** `lumen/blueprints/models_page/routes.py` shows a model owning a list of endpoints. Depth
summed across endpoints tells a student "40 ahead" when they are behind 10 on the endpoint they will
actually land on — and which endpoint that is cannot be known before dispatch (round-robin selects
later). **[DECISION]** display the **maximum across the *healthy* subset**, worded to the student as an
upper bound ("at most N ahead"), never the sum. Two corrections an earlier draft missed:
`get_next_endpoint` round-robins over **healthy endpoints only**, so an unhealthy-but-loaded endpoint
must not enter the max; and round-robin makes the landing endpoint roughly uniform, not worst-case,
so a bare "40 ahead" shown to a student who lands on the idle endpoint and waits five seconds teaches
exactly the same "this number is a lie" lesson that (d) is written to avoid. The upper-bound wording
is what keeps it honest in both directions.

**(f) The chat indicator is client-side elapsed time.**
Server-emitted heartbeat frames would interact with proxy buffering and with the context-free
generator rule, for a number the browser already knows. **[DECISION]** if any new SSE event type is
ever added, it is emitted from the chat blueprint, never from `lumen/services/llm.py`'s shared
`send_message_stream` — `lumen/blueprints/api/routes.py` emits raw OpenAI chunks, and a new event type
leaking into `/v1` would break every OpenAI-client parser.

**(g) Accessibility decisions that are easy to get wrong here** (CLAUDE.md §6):
auto-refreshing numeric tiles are **`aria-live="off"`** with an explicit Refresh button, plus a
*separate* small `role="status"` region announcing only **state transitions** ("model X is now
queued"). A polite live region updating every 5 seconds is unusable with a screen reader — it reads
the whole dashboard aloud continuously. Every `<canvas>` gets `role="img"` + `aria-label` + a text
fallback; every table a `<caption class="visually-hidden">`; queue state never colour-only; timestamps
as `<span class="local-datetime" data-utc="…Z">`.

**(h) Reclaiming `/admin/analytics`.** It is a bare redirect to `/usage`, and its 346-line template is
an orphaned older copy of `usage.html`. **[DECISION]** delete the orphan in the same change that adds
`admin/status.html`, so the repo does not carry two near-identical analytics templates.

**Nav:** a third `{% if is_admin %}` item in **all four** theme headers
(`themes/{default,illinois,uic,uis}/templates/theme/header.html`). Factor them into a shared partial
in the same change — four copies is how the next page gets added to three of them.

**Tests:** `@admin_required` on page and JSON endpoint; `/admin/api/status` issues exactly one
statement and it targets `entities` (see (b) — a bare "zero SQL" cannot hold behind an auth lookup);
the topology label renders when `LocalLiveState` is active; `tests/ui/test_accessibility.py` covers the
new page; screenshots in `docs/img/` re-captured and `docs/admin/` updated (CLAUDE.md).


---

## 12. Phase 8 — lifecycle: aggregates, compression, retention

**Blocked on [GATE] G0.3/G0.4.** **Order is not negotiable** — this is the most likely way the whole
effort causes a user-visible regression.

1. **Entity-dimensioned continuous aggregate first.** **[FACT]** `/usage`'s per-user charts read raw
   `request_logs` precisely because `request_counts_hourly` has no `entity_id` column. Enabling
   retention before this exists would **silently truncate every individual's "All Time" history while
   the org-wide charts kept going** — an asymmetry that would be reported as data loss.
2. **`request_metrics_1m`** — 1-minute buckets on `(model_config_id, source)` with
   `end_offset => 1 minute`, carrying counts, token sums, abort counts and duration/TTFT
   sums-and-maxima. The hourly aggregate lags by ≥ 1 hour and is useless for "what happened during
   the 9 a.m. lab". Build hierarchically on top of it **only if** G0.3 confirms the Timescale version
   supports it.
3. **Then compression.** `add_compression_policy('request_logs', INTERVAL '7 days')`,
   `segmentby = model_config_id, source`, `orderby = time DESC`. Chunks are already 7 days, so it
   aligns naturally.
4. **Then retention.** **[DECISION] 13 months**, not 90 days — this is an academic deployment where
   term-over-term comparison is the natural analysis, and G0.4 will almost certainly show the storage
   cost of being generous is trivial. Revisit only if G0.4 contradicts it.
5. **Percentiles:** exact via `percentile_agg` if G0.3 says the toolkit is present; otherwise
   fixed-bucket counts in the aggregate — ugly, exact, dependency-free.
6. Lifetime totals survive retention because `entity_stats`/`model_stats` are cumulative and written
   synchronously. **Say so in the UI** next to any truncated chart.
7. Policies live in a dialect-guarded Alembic migration, not in hot-reloaded config. DDL from a config
   watcher is a bad idea. **[AMENDED — retention is the exception, see (j)]**

**Tests** (all `@pytest.mark.postgres`): per-entity `/usage` queries return identical results before
and after being rewritten onto the new aggregate; a **retention-drop simulation** — seed rows older
than the window, drop the chunks, assert per-user charts still render from the aggregate and
`entity_stats` lifetime totals are unchanged; compression round-trip leaves query results identical;
the 1-minute aggregate is within one bucket of the raw table for a synthetic burst.

**[GATE] Phase 8 exit** — retention is enabled **only after** a dry run on a copy of production
demonstrates no per-user chart changes. If it changes anything, the phase stops.

### Implementation contract — decisions this plan makes so nobody has to stop and ask

**(a) The step that protects per-user history is the QUERY REWRITE, not the aggregate.**
Creating `request_counts_hourly_by_entity` protects nothing on its own. **[FACT]** five per-entity
branches in `lumen/blueprints/profile/routes.py` read raw `request_logs` — at `:444` (summary), `:588`
(requests), `:630` (tokens), `:670` (models) and `:714` (heatmap) — because the existing aggregate has
no `entity_id`. Enabling retention while those still read raw truncates **every individual user's
"All Time" history** while the org-wide charts, fed by the aggregate, keep going. That asymmetry
would be reported as data loss, and it is reachable by following §12's numbered list literally.

**[DECISION] The order is: (1) create the entity aggregate → (2) rewrite all five queries onto it and
prove them equal → (3) compression → (4) retention.** Step 2 is a numbered step with its own
acceptance test, not a line in a test table.

**(b) The bucket is one hour. A daily bucket silently breaks the heatmap.**
**[FACT]** the heatmap does `EXTRACT(HOUR FROM bucket)` (`lumen/blueprints/profile/routes.py:721-722`).
On a daily bucket every row collapses to hour 0 and the 7×24 grid becomes a single column — no error,
just a wrong chart. Daily is the cheaper and therefore tempting choice, so this is stated rather than
left to judgement.

**[DECISION]** `request_counts_hourly_by_entity`, `time_bucket('1 hour', time)`, grouped by
`bucket, entity_id, model_config_id, source`.

**Enumerate the columns; "the same measures as the existing aggregate" loses the ones this project
added.** **[FACT]** the existing aggregate selects only `COUNT(*)`, `SUM(input_tokens)`,
`SUM(output_tokens)`, `SUM(cost)` — it does not even carry `duration`. §11 promises per-user median
and p95 TTFT and abort share, and once retention drops raw chunks those are **unrecoverable** unless
the aggregate carries them. So it must also carry `SUM(duration)`, an abort count
(`COUNT(*) FILTER (WHERE outcome = 'disconnect')`), and the TTFT measure chosen by (f) —
`percentile_agg(ttft_visible)` if the toolkit is present, fixed-bucket counts if not.

**And specify the refresh properties, or the rewrite loses the last hour of everyone's data.**
`timescaledb.materialized_only` defaults to **true** on recent Timescale, so a non-real-time
aggregate returns nothing newer than the last materialisation — and the existing policy's
`end_offset => 1 hour, schedule_interval => 1 hour` would put that up to two hours behind. The raw
queries being replaced are exact to the millisecond. A student who runs 40 requests in a 9 a.m. lab
and opens `/usage` at 09:50 would see **zero** — a bigger and far more frequent complaint than the
truncation this phase exists to prevent, because it hits every active user every day.
**[DECISION]** set `materialized_only = false` (real-time aggregation, so recent rows come from raw),
or an `end_offset` under a minute with a matching `schedule_interval`; G0.3's version answer decides
which is available. **The equality test in (a) step 2 must include data inside the current bucket** —
run against historical rows only, it passes while certifying the regression. Cardinality is bounded by *active* user-hours, not users × models × hours: a student uses
one or two models in an hour, so a 300-student class produces a few hundred rows per hour, not tens of
thousands. **Verify against G0.4 before enabling retention.**

**(c) The full-history backfill cannot run inside the migration.**
**[FACT]** the existing migration creates its aggregate `WITH NO DATA`
(`migrations/versions/i9j0k1l2m3n4_timescaledb_tracking.py:66`), and `seed_analytics.py` calls
`refresh_continuous_aggregate` as a standalone `CALL` on an AUTOCOMMIT connection — because it cannot
run in a transaction block. A policy-created aggregate materialises only its recent window, so
per-user "All Time" stays empty until a full refresh runs.

**[DECISION]** the migration creates the aggregate `WITH NO DATA` and adds the policy; the
full-history refresh is a separate `flask` CLI command, run deliberately by an operator, with its
expected runtime estimated from G0.4 first. The command must open its own **AUTOCOMMIT** connection
(a `flask` command using `db.session` fails with "cannot run inside a transaction block") and should
refresh **month by month** rather than one `CALL ... (NULL, NULL)`, which over 13 months of a
production hypertable is a single long transaction with unbounded memory.

**The backfill must run BEFORE the code that reads the aggregate is deployed.** **[FACT]**
`entrypoint.sh:8` runs `flask db upgrade` at container start, so the migration lands automatically —
and a policy-created aggregate holds only its `start_offset` window. Deploying the query rewrite in
the same release therefore guarantees that every user's All Time, Month and Week charts read
near-empty from the moment of deploy until a human remembers a CLI command nothing gates on. That is
the *identical* user report this phase's ordering exists to prevent, and it is certain rather than
conditional. **[DECISION]** the ordering in (a) is: create aggregate → **run the backfill and verify
row counts** → deploy the rewrite. If they must ship together, the rewritten queries fall back to raw
whenever the aggregate's earliest bucket is later than the requested window start, and the fallback
is deleted once the backfill is confirmed. **Do not** put an unbounded materialisation on the deploy
path — on a production-sized hypertable it can run for hours while `flask db upgrade` blocks container
start (`entrypoint.sh:8`).

**(d) `start_offset` and retention must agree — and getting it wrong ERASES data silently.**
An earlier draft said a refresh reaching into dropped chunks "errors out". It does not: refreshing a
window whose raw chunks are gone recomputes that window as **empty and deletes the materialised
rows**. There is no error for an operator to notice; the users simply lose history.

Two guards follow, and the second matters more:
**[DECISION]** set `start_offset` to a finite window comfortably inside the retention window, and
state both numbers adjacent in the migration so the relationship is visible when either changes.
**[DECISION]** the (c) backfill CLI must **refuse** to refresh any window starting before the
retention boundary, unless given an explicit `--force`, and must print the row count it is about to
affect. Without that guard the CLI is a foot-gun: it is documented, it looks idempotent, and an
operator running it a year after retention is enabled would erase every user's pre-retention history
in one call — strictly worse than the truncation this whole phase is built to avoid.

**(e) Compression is a one-way door for `request_logs` schema changes.**
Adding a column to a hypertable with compressed chunks is restricted and version-dependent; the
practical consequence is a decompress/recompress migration over the whole retention window.
An earlier draft made compression wait for "anything Phase 9 would want", which blocks a
non-negotiable step of Phase 8 on a phase §13 explicitly declares out of scope — leaving the engineer
to stop, add speculative columns (CLAUDE.md §2 forbids), or ignore the contract.

**[DECISION] Bind it to G0.3, with both branches stated:**
- **If the deployed Timescale supports `ADD COLUMN` on compressed hypertables** (roughly 2.10+, for
  nullable columns): compression proceeds now, and any later phase adds nullable columns freely.
- **If it does not:** compression waits until the `request_logs` schema is settled, and that delay is
  a stated cost of running the older version — not a dependency on an undesigned phase.

**[RESOLVED — §4.1, measured on 2.27.2] The first branch.** Nullable `ADD COLUMN`, `ADD COLUMN`
with a `DEFAULT`, and `ADD COLUMN NOT NULL DEFAULT` were all executed against a hypertable with
three genuinely compressed chunks and all three succeeded. Only `NOT NULL` *without* a default is
refused, and it is refused loudly (`cannot add column with NOT NULL constraint without default to a
hypertable that has columnstore enabled`) — the same restriction `y9z0a1b2c3d4` already documents,
now stated in 2.27's columnstore vocabulary. So compression is **not** a one-way door on this
deployment, the heading above overstates it, and later phases may add nullable columns freely.
Keep the ordering as a preference for a different reason: recompressing a chunk to service an
`ADD COLUMN` still costs I/O over the retention window, and doing the schema work first avoids
paying it.

**(f) Percentiles: exact if the toolkit is present, hand-rolled buckets if not.**
Continuous aggregates hold `COUNT`/`SUM`/`MAX`, which gives means and worst cases but not p95. If
G0.3 confirms `timescaledb_toolkit`, use `percentile_agg`. If not, store fixed-bucket counts in the
aggregate — ugly, exact, dependency-free. **Do not** compute p95 from raw rows for long windows; that
is the full-history scan this phase exists to eliminate.

**(g) Retention window: 13 months, and it is a policy decision the team owns.**
Chosen because this is an academic deployment where term-over-term comparison is the natural
analysis, and the storage cost of being generous is small. **Revisit against G0.4** — every storage
figure in this plan is an estimate until that query is run.

**(g2) The entity aggregate needs its own retention decision, and G0.4 must measure it.**
`add_retention_policy` on the hypertable does **not** apply to its continuous aggregates. Left
unstated, per-user hourly rows accumulate forever — which is what makes "All Time" work, but it also
makes the stated 13-month policy false for precisely the most identity-bearing data in the system,
with the data-protection implication that per-user rows outlive the raw rows they came from.
**[DECISION]** state it explicitly — indefinite retention for the aggregate, or its own longer window
— and **include the aggregate in G0.4's size query**, which currently measures `request_logs` only.
The figure justifying "the storage cost of being generous is trivial" is otherwise measuring the
wrong table.

**(h) Lifetime totals survive retention, and the UI should say so.**
`entity_stats` and `model_stats` are cumulative all-time counters written synchronously with every
request, so dropping raw chunks loses no one's lifetime usage or cost. Say this next to any chart that
truncates, or the first user to notice will report it as data loss.

**Tests** (all `@pytest.mark.postgres`): each rewritten per-entity query returns results identical to
the raw-table version over the same window, per endpoint; a **retention-drop simulation** — seed rows
older than the window, drop the chunks, assert per-user charts still render from the aggregate and
`entity_stats` totals are unchanged; the heatmap returns 24 distinct hours from the aggregate (the
guard for (b)); compression round-trip leaves query results identical; the 1-minute aggregate is
within one bucket of the raw table for a synthetic burst.

**(j) [AMENDMENT] Retention is an operator command, not a migration — the [GATE] is otherwise
unenforceable by construction.**
Item 7 above puts the lifecycle policies in an Alembic migration. That is right for compression and
wrong for retention, and the reason is mechanical rather than stylistic. **[FACT]** `entrypoint.sh:8`
runs `flask db upgrade` at container start. A migration calling `add_retention_policy` therefore
takes effect on the next deploy, unattended — which means the `[GATE]` at the end of this section
("retention is enabled **only after** a dry run on a copy of production demonstrates no per-user
chart changes") can never actually gate anything: the policy is already live before a human is in a
position to look. A gate that the deploy path walks straight through is worse than no gate, because
the document claims a safeguard that does not exist.

Compression stays a migration: it destroys nothing, it is reversible, and 2.27.2 accepts `ADD COLUMN`
on compressed chunks (§4.1), so it forecloses nothing either.

**[DECISION]** retention is enabled by `flask enable-retention`, which **defaults to `--dry-run`**
and prints what it would drop — row count, time range, each aggregate's earliest bucket, and whether
that bucket falls outside the aggregate's `start_offset`. Only `--force` calls
`add_retention_policy`. It refuses outright while `request_counts_hourly_by_entity` is empty, since
retention then destroys history that exists in no aggregate — the precise failure (a) is built to
prevent, arrived at from the other direction. This pairs with the (d) guard on the backfill command:
the two commands are the only ways to lose history, and each now refuses the losing move by default.

**(i) The rewrite must preserve the SQLite early-return.** All five per-entity endpoints
short-circuit today on `dialect.name != "postgresql"`, and the rewrite must keep doing so — a
continuous aggregate does not exist on SQLite, and the development and test paths run there.

**[GATE]** retention is enabled only after a dry run on a copy of production shows no per-user chart
changes. If anything changes, the phase stops.


---

## 13. Phase 9 — admission control (separate decision)

Listed for completeness and explicitly **not** part of the observability work.

A bounded per-model in-flight cap near the backend's `max_num_seqs`, a bounded queue with a deadline,
and a fast `429 + Retry-After` beyond it. It converts the invisible unbounded Q1 queue into a
bounded, measured, explainable one, and a student told "the model is busy, try again in 30 s" is far
better served than one who watches a spinner for ten minutes and is then cut by the 600 s gateway.

**[DECISION] This is a policy change, not a metrics change** — it moves the failure mode from
"everyone waits" to "some are rejected quickly", and needs the operators' agreement, not an
engineer's. It also is the second thing in this plan that genuinely requires Redis at M replicas (a
distributed semaphore). Do it after Phases 1–7 have produced the data to size the cap, or not at all.

---

## 13a. Burst pathologies this plan did not originally address

Surfaced by adversarial review. Each is a real load pathology that measurement alone will not fix;
each is listed with the phase that should at minimum *measure* it.

1. **The synchronized-completion write spike.** 300 streams that started together finish together,
   and each completion fires `update_stats` — five statements plus `subtract_coins` plus, on the API
   path, the `APIKey` update, in one transaction. That is well over a thousand statements arriving at
   Postgres within a few seconds, against a pool that Phase 4 has just stopped `/metrics` from
   competing for. **The plan decoupled the scrape from the DB and left the completion path
   untouched.** *Action:* measure it first — Phase 3's per-request columns plus the pool-wait
   histogram will show whether commit latency is a real component of `duration` under burst. Only if
   it is, consider batching the rollup updates or moving billing to a short queue — and treat that as
   a billing-correctness change with its own review, not a metrics change.
2. **No connection reuse upstream.** Every call site builds a fresh `openai.OpenAI(...)` inside a
   `with`, so a new httpx pool is created and destroyed per request: TCP + TLS on every single call.
   300 simultaneous handshakes to one backend is measurable latency and CPU at exactly the wrong
   moment. *Action:* Phase 3 makes `connect` visible (it is the gap between `preflight` end and
   `ttft`). A long-lived per-endpoint client is the obvious win **if the data justifies it**, and it
   carries its own concurrency and credential-rotation implications — measure, then decide.
3. **`pool_tracker`'s cost scales with concurrency, not duration.** It captures a 25-frame stack on
   *every* checkout, several times per request. Its per-request cost is negligible against an LLM
   call; its cost during a 300-request burst is the one always-on instrumentation that grows with the
   thing being diagnosed. **[DECISION] Do not remove it** — it caught a production leak nothing else
   could, twice. *Action:* measure its share under the Phase 2 load test, and add a sampling switch
   only if the measurement justifies it. **[OPEN]** for the team.
4. **Gateway-budget exhaustion is invisible.** Handled as Phase 2 item 6 above.

---

## 14. Standing invariants, enforced by test

The pattern already exists in this repo — `tests/unit/test_no_stream_with_context.py` and
`tests/unit/test_wsgi_disconnect_body.py` are static/behavioural guards against regressions that bit
production. These join them.

| Invariant | Enforced by |
|---|---|
| **No DB write per token, chunk, or interval of a stream.** Accumulate in the generator frame; write once | `tests/unit/test_llm_functions.py` — statement counter across a 500-chunk stream |
| **No `Histogram.observe` per token.** `observe` takes a lock; 50 tok/s × 300 streams is 15 000 lock acquisitions/s for a number nobody reads | `tests/unit/test_metrics_middleware.py` — patched `observe` counter |
| **No Redis call per token.** ≤ 2 per request | `tests/unit/test_live_state.py` — counting fake client |
| **No Redis on the critical path.** Every failure mode of Redis leaves proxying unaffected | `tests/unit/test_live_state.py` — raising and sleeping fake clients |
| **Every `Gauge` declares `multiprocess_mode`** | static scan over `lumen/`, same shape as `tests/unit/test_no_stream_with_context.py` |
| **Every live counter returns to zero** after any exit path | `tests/unit/test_live_state.py`, `tests/unit/test_wsgi_queue_metrics.py`, parametrised over normal / raise / disconnect / stall / timeout |
| **`/metrics` executes zero SQL; `/admin/api/status` executes exactly one statement, the `admin_required` identity lookup against `entities`** (a bare "zero" is unsatisfiable — see §11's contract) | `tests/routes/test_metrics_routes.py`, `tests/routes/test_admin_routes.py` — `before_cursor_execute` listener |
| **No DB session and no Flask context spans a `yield`** (existing CLAUDE.md rule; the snapshot refresher and the backend scraper are both new generators-adjacent daemons) | `tests/unit/test_no_stream_with_context.py` (existing), `tests/unit/test_db_teardown.py` extended to the new threads |
| **`time.monotonic()` for durations, wall clock only for stored timestamps** | review checklist; the existing LLM path uses `time.time()` throughout, which is NTP-step-sensitive — a wart worth not replicating |
| **No user identity in any Prometheus label** | static scan of label names against a denylist (`entity`, `user`, `email`, `api_key`) |

---

## 15. Corrections to the proposal

Verified against `196f642`; the proposal was written against an earlier tree.

1. **§5.3 is stale.** The claim that `lumen_http_request_duration_seconds` stops its clock when
   `wsgi_app()` returns, with a 10 s top bucket, **has already been fixed on this branch.**
   `lumen/blueprints/metrics/middleware.py:27-36` now runs buckets to 300 s with a comment explaining exactly this, and the
   observation happens in `_ContextCheckingBody.close()` — the change the proposal recommends. Remove
   this item from Phase 0; it is done.
2. **§8's "Redis is already an installed dependency" understates and overstates at once.** The Python
   package is installed unconditionally (`pyproject.toml`, `flask-limiter[redis]`), but the chart
   ships it **disabled**, so in a default deployment Redis is not running at all. "Not a new
   dependency decision" is true only if Gate 0 finds it already deployed.
3. **§8's central premise is a chart default, not production config** — the point you raised. The
   conclusion happens to survive for the *storage* question (§1.1 strengthens it) but not for the
   *live gauge* question, which is why §2 introduces the seam rather than a dict.
4. **§2.1's "multi-process aggregation is wired but currently moot" is too generous.** It is wired at
   the read side only. Without the volume mount, the startup wipe and explicit `multiprocess_mode` on
   every new gauge, turning on `--workers` would produce **wrong numbers**, not merely per-process
   ones. That is Phase 1.
5. **The coin refiller is multi-process safe**, contrary to the implication in §8's open question.
   `lumen/services/token_refill.py:101-113` is a compare-and-set on `last_refill_at`; a second process's pass is a
   no-op. The health checker's N× probe load is the real multi-process wart, and it is benign.
6. **Timescale is not optional.** §9's framing treats the hypertable as a Postgres-only enhancement;
   `CREATE EXTENSION ... CASCADE` in the migration makes it a hard boot requirement. This is worth
   stating in `docs/architecture.md` — an operator pointing Lumen at a plain Postgres today gets a
   failed migration, not a degraded feature.

---

## 16. Adversarial review — what changed

This plan was reviewed by GLM 5.2 (via `opencode`, run against this repo with its own independent
code dive) under a brief that told it a review finding nothing is a failed review, and instructed it
not to trust this document's `[FACT]` citations. It read the code and checked them. Findings, with
this plan's disposition:

| # | Finding | Disposition |
|---|---|---|
| **C1** | `time − duration − queue_wait` does not equal T0. `duration` starts *after* preflight (`lumen/services/llm.py:752` vs the context block at `:725-750`) and `time` is stamped *after* billing (`:848` → `:554`), so the derived start is `T0 + preflight + billing_delay` — and the error peaks during bursts, when pool contention makes preflight largest | **Accepted; this was a real bug in the plan.** Phase 3 now stores absolute `started_at` plus `preflight` instead of deriving. §7 items 8–9 |
| **M1** | `mark_process_dead` never runs under SIGKILL, so a crashed worker's `livesum` gauge file poisons the fleet number for the pod's remaining life; the dir wipe is per-pod, not per-worker | **Accepted.** Phase 1 item 3 now specifies a dead-PID reaping pass at worker start and on scrape |
| **M2** | Capturing T1 in the Prometheus middleware would NULL `queue_wait` whenever Prometheus is disabled — which is the chart default | **Accepted; a design error.** Phase 2 item 1 now requires unconditional capture; only histograms live in `lumen/blueprints/metrics/middleware.py` |
| **M3** | The seeded-data "headline query" test proves the SQL, never the data, and would have passed with C1 shipped | **Accepted.** New `tests/integration/test_started_at_accuracy.py` asserts `started_at` against an independently recorded T0 |
| **M4** | `lumen/services/health.py:27`'s executor is per-process, so N processes means N×8 probe threads at the backends every 60 s — the plan's "accept and document" underweighted this | **Accepted.** Phase 1 item 7 now elects a single runner via `fcntl.flock` on a pod-scoped file |
| **m1** | `LiveState` is an interface with one implementation if Gate 0 finds 1×1 — the speculative generality CLAUDE.md §2 forbids | **Accepted.** §2 now ships a plain class at 1×1 and adds the abstraction with the second implementation |
| **m2** | The `url_rule` cardinality fix is not a one-liner; `request.url_rule` is unset when the middleware captures the path | **Accepted, remedy corrected.** The review proposed reading `url_rule` after `wsgi_app` returns; verified against the installed Flask that `wsgi_app` runs `ctx.pop()` in its `finally` **before** returning, so the context is gone there too. Phase 1 item 5 now stashes the rule into `environ` from inside the request |
| **m3** | Column defaults unspecified; `send_blocked` ambiguous on non-streaming paths | **Accepted.** Defaults now stated explicitly, including why `outcome` gets none |
| **m4** | Counter files from dead PIDs are summed *correctly*; not saying so invites a wrong "fix" | **Accepted.** Phase 1 item 4 now states the asymmetry |
| **Idea 3** | Synchronized-completion write spike — the plan decoupled the scrape and ignored the completion path | **Accepted as §13a.1**, measure-first |
| **Idea 4** | No upstream connection reuse; 300 simultaneous TLS handshakes under burst | **Accepted as §13a.2**, measure-then-decide |
| **Idea 5** | Gateway-budget exhaustion recorded as an ordinary disconnect | **Accepted as Phase 2 item 6** — shed before the gateway, distinct `timeout` outcome |
| **Idea 6** | `pool_tracker`'s stack capture is the one instrumentation whose cost scales with concurrency | **Accepted as §13a.3**, left `[OPEN]` — it has twice caught leaks nothing else could |
| **Idea 7** | If Gate 0.5 finds no Prometheus stack, Phases 1–6 have no consumer and `/admin/status` should ship right after Phase 4 | **Accepted.** §11 now opens with that conditional |

Two things the review confirmed rather than challenged, worth recording because they were the
load-bearing claims: **Timescale is mandatory** (`i9j0k1l2m3n4:38` + `entrypoint.sh:8`), and
**Phase 8's ordering constraint** — entity-dimensioned aggregate strictly before retention — which it
independently called the most important risk decision in the document.

The review also caught that both documents cite the LLM module as `lumen/services/llm.py` when it is
`lumen/services/llm.py`. Line numbers are correct; the paths are not.

### Round 2

The revised plan went back to the same reviewer under a brief warning that agreeableness is the
round-2 failure mode. It re-verified against the installed Flask and the four LLM code paths.

**On the one finding this plan overrode:** round 1 claimed `request.url_rule` is readable after
`wsgi_app` returns; this plan rejected that. Round 2 read Flask 3.1.3 and **confirmed the rejection** —
`ctx.pop(error)` runs in `wsgi_app`'s `finally` before it returns — and confirmed the `environ`
replacement works, while pointing out `teardown_request` is strictly safer than `after_request`.
Adopted.

| # | Finding | Disposition |
|---|---|---|
| **A1** | `preflight = T2 − T1` subtracts a wall-clock T2 (`lumen/services/llm.py:752`) from a monotonic T1 — three different clocks across `queue_wait`/`preflight`/`ttft` | **Accepted.** Converting `t0` to `time.monotonic()` on all four paths is now a required Phase 3 change, not a §14 aspiration. Contract (a) |
| **A2** | Nothing said how `started_at`/`queue_wait` reach `update_stats`, and two of its five call sites are context-free generators that must not touch `request` | **Accepted.** Contract (c) specifies the `client_disconnect_event()` capture-into-closure pattern the codebase already documents |
| **A3** | `send_blocked` has no measurement mechanism — `send()` accumulates nothing, so the column ships permanently 0 | **Accepted.** Contract (e) specifies the accumulation, or cuts the column rather than shipping a zero that reads as "no backpressure ever" |
| **A4** | A blocking flock is worse than the problem: `lumen/services/health.py:81` commits inside the pass and can block on an exhausted pool, stalling all probing during a burst | **Accepted.** `LOCK_NB` + pass deadline + heartbeat |
| **A5** | `record_stream_abort(started_at=...)` (`lumen/services/llm.py:654`, called at `:772`) already means T2 | **Accepted.** Rename to `stream_t0` in the same change |
| **A6** | The `started_at` accuracy test needs an ASGI harness the plan never specified | **Accepted, and corrected** — `tests/integration/test_disconnect.py:205-226` already runs real uvicorn against `asgi.app`; reuse it |
| **B1** | Gateway shedding catches only budget-already-spent-at-T2; a stream that exhausts the budget mid-generation is still recorded as `disconnect` — the conflation the item claimed to fix | **Accepted.** Phase 2 item 6 now adds a mid-stream deadline check at the existing per-chunk poll, and says plainly what the admission check alone does not cover |
| **B2** | Budget formula omitted `preflight` | **Accepted.** Now `gateway.timeout − (T2 − T0)` |
| **B3** | Dead-PID reaping is fooled by PID reuse | **Accepted as a documented limitation** |
| **B4** | §13a.1 referenced a pool-wait histogram no phase defines | **Accepted.** Now Phase 2 item 7 |
| **C1** | `outcome` enum lacks `billing_error`, which `lumen/services/llm.py:845`'s `phase` already distinguishes and the abort counter already emits | **Accepted** |
| **C2/C3** | `preflight` is not uniform: audio's is dominated by `upload.read()` multipart parsing; the chat stream has two preflights spanned by one column | **Accepted** — documented per-path rather than pretending uniformity |
| **C4** | `TIMESTAMPTZ` + the mandated `utcnow()` (naive) is a silent timezone bug | **Accepted.** Contract (b) requires `datetime.now(timezone.utc)`, matching what `request_logs.time` already does |

**Cuts proposed, and the response.** Round 2 suggested cutting §3 (the Redis analysis), §16, §13a.3
and Phase 9. **Declined for §3** — it is the direct answer to the question that commissioned this
work, and compressing it would lose the reasoning rather than the words. **Declined for §13a.3** —
an `[OPEN]` with a named measurement is how a known cost stays known. §16 and Phase 9 are one table
and one paragraph respectively; they stay unless the team wants a leaner document.

**Buildability, per round 2:** Phase 1 was executable with minor questions; Phases 2–3 had four
places an engineer would have to stop and design (A1, A2, A3, A6). All four are now decided in the
implementation contract above. That was the point of the round.

### Round 3

Two reviews, run against material nobody had checked: (3a) the round-2 fixes, and (3b) Phases 4–8,
which no round had assessed. Both found more than rounds 1 and 2 did. This is the third consecutive
round in which **the corrections were the weakest text in the document** — the pattern is now
established well enough to treat as a rule (see the lessons doc, §1.1).

#### 3a — the round-2 fixes. Two criticals.

| # | Finding | Disposition |
|---|---|---|
| **C1** | Contract (a) scoped the monotonic conversion to "the four paths' own `duration` lines" and missed that `t0` **escapes its function**: `record_stream_abort` computes `duration = time.time() - started_at` at `lumen/services/llm.py:686` from the caller's `t0`. Converting without it writes **`duration ≈ 1.76e9`** — ~55 years — on every aborted row, silently. `t_first` (`:819`) is a second such site and is user-visible in the chat UI | **Accepted; verified myself.** Contract (a) now enumerates all six sites and mandates a grep-shaped static test |
| **C2** | Four of the six `outcome` values are unwritable. `billing_error` especially: `update_stats` only `flush()`es, the caller `commit()`s, so the failing commit rolls back the very row that would record the failure. The round-2 argument for including it was exactly backwards | **Accepted.** Enum cut to `ok` and `disconnect`; any further value must arrive with the code that writes it |
| **M3** | The 429 in §6 item 6 is impossible where specified — on both streaming paths, `start_response` has already committed **200** before the generator runs, leaving only an in-band SSE error the OpenAI SDK will not retry | **Accepted.** Admission check moves into the views, before `Response(...)`, using T1 |
| **M4** | The budget it compares against does not exist in the app: `gateway.timeout` is Helm-only, consumed by `httproute.yaml` under `gateway.enabled` (default false), never rendered into `config.yaml` | **Accepted.** New hot-loaded `api.request_budget_seconds`, plus a safety margin for the inter-chunk check |
| **M5** | Contract (e) records **zero** for the stalled client (the timeout branch raises without accumulating), and the view cannot read the value because streaming billing happens in the generator | **Accepted.** Accumulate in a `finally`; publish a mutable holder in `environ` |
| **M6** | Contract (c) omitted the fifth `update_stats` call site (`record_aborted_request` via `record_stream_abort`) — so aborted rows, the ones the headline query needs most, would carry NULLs. Its `None` fallback also made the Phase 3 "all four paths populate" test unsatisfiable under `test_client` | **Accepted.** Both signatures threaded; the assertion moves to the real-uvicorn harness |
| **M7** | `mark_process_dead` does an unguarded `os.remove`; concurrent reaps 500 the scrape — clustering right after an OOM kill | **Accepted.** Wrapped, and ordered before `MultiProcessCollector` |
| **M8** | The cardinality fix bounds `path_template` and leaves `method` unbounded, so the Phase 1 gate would pass with the bug open | **Accepted.** Allow-list methods; the gate now scans junk methods too |
| **M9** | The flock's "pass deadline" cannot be implemented — nothing in the pass is preemptible, so releasing the lock admits a concurrent second pass. Plus: `"w"` truncates before `flock`, and the proposed emptyDir path does not exist at defaults | **Accepted.** Deadline dropped for mtime-staleness takeover; open mode and a fixed container path specified |
| **m10–m13** | `record_stream_abort` has two callers (renaming one leaves a `TypeError` inside `_abort()` → `RuntimeError: generator ignored GeneratorExit` → abort never billed); PID reuse inherits the dead worker's mmap rather than merely leaving a stale file; `send_blocked = 0.0` on non-streaming paths is justified by the wrong reason; chat `preflight` also spans a `send()` of the response-start | **All accepted** |

#### 3b — Phases 4–8 buildability. The structural finding matters more than any single item.

**Phases 4–8 are not buildable as written.** §7 has an implementation contract because round 2 forced
one into existence; Phases 4–8 have none, which is why they *read* as buildable. Fourteen findings,
of which these change the design rather than the prose:

- **F1 (critical):** Phase 4 says to model the snapshot refresher on `lumen/services/health.py` — but
  Phase 1 item 7 has just turned that file into a **single-runner election**. Copying it would leave
  N−1 workers serving an empty snapshot; Prometheus reads absent-then-present as a counter **reset**,
  producing sawtooth garbage in the multi-process deployment Phase 1 exists to enable. The refresher
  must run in **every** process and must never be flock-elected: election is for work whose result
  lands in the DB, not for in-memory snapshots. Also unaddressed: per-process refresh means Phase 4
  can *increase* total DB load rather than reduce it.
- **F2 (critical):** Phase 5's Redis structure is wrong. **Redis sets have no per-member TTL**, and
  `SREM` on a user's first of three concurrent requests removes them while two are still in flight —
  under-reporting exactly the double-submitting student the burst scenario is full of. `SCARD` is
  also offered as both unique-users and in-flight, which are different numbers. Needs a sorted set
  keyed `{entity_id}:{request_id}` with expiry as score; `LocalLiveState` needs the same multiplicity
  fix (a counter, not a set).
- **F3:** `admit`/`release` has no contract — call sites, disconnect-path release, and whether
  "waiting" ends at TTFT or at completion are all undecided. The last changes the headline number by
  ~10× during a long generation, and it drives Phase 7's "N ahead of you" and Phase 9's cap sizing.
- **F4/F5:** Phase 8's ordering omits the step that actually protects per-user history — **rewriting
  the `/usage` per-entity queries onto the new aggregate**. Creating the aggregate protects nothing
  on its own, so following §12's numbered list literally still truncates every user's "All Time"
  chart. And the entity aggregate itself is unspecified: bucket width (daily collapses the heatmap's
  `EXTRACT(HOUR …)` to hour 0), and a full-history backfill that **cannot run inside an Alembic
  transaction**.
- **F7:** Phase 7 consumes Phase 8's 1-minute aggregate and its percentile decision, while the G0.5
  clause authorises moving Phase 7 *earlier*. Split it: 7a live tiles (may jump the queue, zero SQL),
  7b historical charts (strictly after §12.2).
- **F9:** Phase 6's "already detects the backend type" is half true — only SGLang is ever tagged;
  vLLM is the fall-through, indistinguishable from OpenAI, Azure, or any OpenAI-compatible proxy.
  Guessing "not-sglang ⇒ vllm" would send scrapes, possibly carrying the endpoint's API key, to
  arbitrary third-party hosts. And vLLM exposes no capacity gauge, so the denominator must be
  operator-configured.
- **F6, F8, F10–F14:** the refresher's real leak mode is a context spanning `sleep` (CLAUDE.md's rule
  says `yield`, so it reads as silent — worth generalising the rule); nobody owns the snapshot's
  schema while three phases add to it; compression is a one-way door for further `request_logs`
  columns; and several smaller unmade decisions.

**[DECISION] Phases 4–8 do not start until each has an implementation contract in the shape of §7's.**
**Phases 4 and 5 now have one** (§8 and §9). Phase 4's settles: the refresher runs per-process and
unelected *and outside the `BACKGROUND_WORKER` guard*; one `app_context()` per pass, exited before the
sleep; a frozen `MetricsSnapshot` dataclass with a field-addition rule and a refresh interval derived
from the scrape interval; a synchronous prime at startup; the `admit`/`release` contract including a
`LiveTicket` carrying a deadline, with "live" defined as admitted→completion; and the age gauge
emitted from the collector rather than as a multiprocess Gauge. Phase 5's replaces the unimplementable
SET-with-per-member-TTL design with a sorted set scored by deadline, states the honest O(members) cost
of the unique-user count, and lists which existing tests the correction invalidates.

**Phases 6, 7 and 8 still have none**, and §10's `http_sd` rewrite has not been through a contract
pass either.
