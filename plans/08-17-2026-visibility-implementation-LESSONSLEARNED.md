# Lessons Learned — the visibility planning effort

**Date:** 2026-08-17
**Companion to:** `08-17-2026-propsoal-for-usage-and-resource-metric-visibility.md` (proposal) and
`08-17-2026-visibility-implementation-plan.md` (plan).

This document exists because the planning effort produced more transferable knowledge than the plan
itself contains. Two kinds: **process lessons** about how the work went wrong and was caught, and
**repo facts** that were expensive to establish and are easy to get wrong again.

Read Part 2 before touching anything in the request path, the metrics surface, or `request_logs`.

---

## Part 1 — Process lessons

### 1.1 Corrections have a higher defect rate than the text they correct

The single most useful finding of the whole effort. Three adversarial review rounds ran against the
plan. Round 1 found a critical bug and several majors. Those were fixed. **Round 2 then found six
defects in round 1's fixes** — including one that broke the headline fix outright (a monotonic
timestamp subtracted from a wall-clock one).

The reason is structural, not carelessness: original text is written slowly and reviewed; corrections
are written fast, under the satisfaction of having just been told what was wrong, and are then
treated as *resolved* rather than as new untested text. **A fix is the least-reviewed prose in any
document.**

**Apply it:** when a review round completes and the fixes land, the fixes are now the highest-risk
section. Review them specifically, and scope a round at them rather than re-reviewing the whole
document. Never treat "we addressed the findings" as equivalent to "the findings are resolved."

### 1.2 Defaults are not production config

The proposal's central architectural argument — that process-local memory is exactly correct, so
Redis is unnecessary — rested entirely on `replicaCount: 1` and a single uvicorn process. Both were
read out of `chart/values.yaml` and `entrypoint.sh`. **Neither was ever verified against the running
deployment.** A whole section of reasoning was conditional on an unchecked premise, and it was not
labelled as conditional.

**Apply it:** any design decision that depends on deployment topology, scale, or configuration must
either cite the *actual* deployment or be explicitly marked as an assumption with a gate to verify
it. In this repo, `GET /metrics/debug` prints workers × replicas, pool topology and live thread
counts — one authenticated request answers most of these questions.

### 1.3 Prefer storing an absolute instant over deriving it from offsets

The plan originally reconstructed a request's start as `time − duration − queue_wait`. It read as
obviously correct. It was wrong, because **two spans in the request lifecycle were unmeasured** —
preflight (excluded from `duration`) and billing (included before `time` is stamped) — and a
derivation silently assumes the spans you did not measure are zero.

Worse, the error was *proportional to load*: preflight contains a DB pool checkout, so the
reconstruction was least accurate exactly during the incident it existed to diagnose.

**Apply it:** if you need to know when something happened, record when it happened. Derivations from
durations are only sound when the timeline is fully partitioned by measured spans, and proving that
is harder than adding a column.

### 1.4 One clock, or the sum is meaningless

The plan introduced `time.monotonic()` for new spans into a codebase whose LLM path times everything
with `time.time()`. The intent was good (monotonic is immune to NTP steps). The result was a sum,
`queue_wait + preflight + ttft`, drawing on **three different clocks** — and a subtraction of a
wall-clock instant from a monotonic one, which is not a duration at all, just a number.

**Apply it:** introducing a second clock into an existing timing chain is not a local improvement. It
is a migration: convert the whole chain or none of it. Keep wall-clock strictly for stored instants
and monotonic strictly for spans, and never let the two meet in an arithmetic expression.

### 1.5 A test that seeds its own data proves the query, not the pipeline

The plan's headline test was "seed rows, run the interval-overlap query, assert the hand-computed
answer." It would have passed with the broken derivation in §1.3 shipped — because the seeded rows
were consistent with themselves. It validated SQL, and nothing else.

**Apply it:** for any value computed by production code and later queried, at least one test must
drive the **real code path end-to-end** and compare against an independently captured ground truth.
Ask of every test: what bug could exist that this test would not notice? If the answer is "the one
this feature is about", the test is decoration.

### 1.5a A `server_default` silently swallows an explicit `None`

The timing columns are nullable *and* carry `server_default='0'` — nullable because "not measured"
must stay distinguishable from "measured as zero", and defaulted because a populated TimescaleDB
hypertable will not accept a new column without one.

Those two requirements interact in a way that destroys the first. SQLAlchemy **omits** an attribute
set to `None` from the INSERT when the column has a default, so the database fills in the default:

```python
s.add(T(v=None))        # stored as 0.0   <-- "not measured" became "measured as zero"
s.add(T(v=sa.null()))   # stored as NULL
```

Verified directly against the installed SQLAlchemy. A subagent found it because one of its tests
asserted `0.0 is None`; without that test the column would have shipped looking correct, with every
unmeasured request indistinguishable from a request that waited exactly zero seconds — the precise
distinction the migration docstring spends a paragraph defending.

**Apply it:** whenever a column is *both* nullable and defaulted, writing NULL requires an explicit
`sa.null()`. And more generally: if two schema requirements pull in opposite directions, write the
test that proves which one won.

### 1.6 Verify `[FACT]` claims against HEAD, not against an earlier read

The proposal asserted that the HTTP latency histogram was blind to streaming responses with a 10 s
top bucket. That had **already been fixed** on the working branch — buckets to 300 s, observation
moved into `_ContextCheckingBody.close()`. A whole recommended work item was already done.

**Apply it:** planning documents go stale against active branches within days. Re-verify before
acting, and record the commit the facts were checked against (this plan pins to `196f642`). When
handing a plan to a fresh session, that pin is the first thing to re-check.

### 1.7 Cite repo-relative paths, always

Both documents cited `llm.py:752`. The file is at `lumen/services/llm.py`. An external reviewer's
first three attempts to read it failed, and it had to glob for the file. Line numbers were correct
throughout; only the paths were wrong, which is the most annoying possible combination.

**Apply it:** `lumen/services/llm.py:752`, never `llm.py:752`. A cheap self-check before handing over
any document: extract every cited path and confirm it resolves.

### 1.8 Adversarial review works, but only if the brief forbids agreeableness

What made the review rounds productive was an explicit brief that: told the reviewer a review finding
nothing is a failed review; instructed it **not to trust the document's own `[FACT]` citations** and
to check them against code; named the specific soft spots to attack; and demanded evidence be labelled
VERIFIED vs INFERRED.

Round 2 additionally had to be warned that a *revised* document invites "good, this addresses my
concerns," which is worthless. Naming the failure mode in the prompt prevented it — round 2 was
harsher than round 1.

Also worth doing: give the reviewer the one place you **overrode** it and ask who was right. Round 2
ruled against its own predecessor after reading the Flask source, which is far stronger evidence than
either side asserting.

### 1.9 Check for evidence of progress, not liveness

`opencode run` failed three distinct ways in one session, each time looking fine from outside:
it exited **0** while printing `Error: No healthy endpoints`; it exited **0** after dying on its own
context mid-review, having produced only a tool trace; and three parallel instances in one repo hung
for 35 minutes, having burned **48 seconds of CPU each**.

A process being alive says nothing. What distinguishes working from hung is *evidence of progress*:
CPU time consumed, output bytes growing, files changing on disk. `ps -o time` and a file size, taken
twice, answer in seconds what an elapsed-time indicator never will. Check the **content** of a tool's
output before building on it, especially for wrapped CLIs and anything proxying a network service.

Corollary for delegated work: **verify the artifact, not the report.** A subagent reported full `helm
template` verification of the chart work; the changes it verified included passing `--workers 1` to
every default deployment (adding a supervisor process where there was none) and setting
`PROMETHEUS_MULTIPROC_DIR` to a directory that was not mounted at one process — a combination it
tested and recorded as correct. Both were caught by reading the diff, not the summary.

### 1.10 Exit code 0 does not mean success

`opencode run` exited **0** while printing `Error: No healthy endpoints for model 'glm-5.2'` and
writing an empty result. This was briefly reported as "the review is running." Check the *content* of
a tool's output, not just its status, before building on it — especially for wrapped CLIs and
anything that proxies a network service.

---

## Part 2 — Repo facts that are expensive to rediscover

Each of these cost real effort to establish. All were verified against `196f642`.

### 2.1 TimescaleDB is mandatory on PostgreSQL, not an enhancement

`migrations/versions/i9j0k1l2m3n4_timescaledb_tracking.py:37` runs
`CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE` unconditionally on the Postgres branch, and
`entrypoint.sh:8` runs `flask db upgrade` before `exec uvicorn`. **A Postgres deployment without the
extension fails the migration and the container does not start.** `request_logs` is a hypertable with
a continuous aggregate `request_counts_hourly`. SQLite gets a plain table and every `/api/usage/*`
endpoint short-circuits on the dialect check.

Corollary: the docs describe the hypertable as a Postgres-only *feature*. It is a boot requirement.

### 2.2 Redis is installed as a package but not deployed

`flask-limiter[redis]` is an unconditional dependency, so `import redis` always works — but
`chart/values.yaml` ships `redis.enabled: false`, and the only consumer is flask-limiter storage.
The bundled Redis is single-replica, `strategy: Recreate`, `persistence.enabled: false`. "Redis is
already a dependency" is true of the Python package and false of the infrastructure. Do not design
as though it is running.

### 2.3 The Prometheus middleware is only installed when Prometheus is enabled

`lumen/__init__.py:99-104` wraps `app.wsgi_app` with the metrics middleware **only** under
`api.prometheus.enabled`, and the chart default is `false`. Anything captured inside that middleware
is therefore absent in a default deployment. **Never capture a value there that something outside
Prometheus depends on** — this nearly caused `queue_wait` to be NULL on every row in the default
configuration.

### 2.4 Flask pops the request context before `wsgi_app` returns

`Flask.wsgi_app` runs `ctx.pop(error)` in its `finally`, which executes **before** the response
iterable is returned to any WSGI middleware. So in `make_metrics_middleware`'s closures there is no
request context: `request.url_rule`, `request.view_args` and friends are unavailable both before the
call (routing has not happened) and after it (context gone).

To carry a per-request value out to WSGI-level code, stash it into `environ` from a
`teardown_request` hook while the context is live, then read `environ` in the closure. Prefer
`teardown_request` over `after_request`: the latter is skipped when a non-`Exception`
`BaseException` unwinds the request.

### 2.5 Streaming generators run context-free — capture in the view

The banned-`stream_with_context` rule in `CLAUDE.md` has a positive counterpart that is easy to miss:
a streaming generator cannot touch `request` at all. The established pattern is
`client_disconnect_event()` (`lumen/services/wsgi_disconnect.py:338-352`), whose docstring states the
rule outright — call it in the view while the context is live, capture the value into the generator's
closure, and fall back to a benign default when there is no context (dev server, test client, direct
unit-test calls).

Any new per-request value that must reach billing has to travel this way. `update_stats` has **five**
call sites, **two of which are inside context-free generators** (`lumen/services/llm.py:848`,
`lumen/blueprints/api/routes.py:533`), plus the abort path via `record_aborted_request`.

### 2.6 The timing marks in the LLM path are not where you would guess

- `t0` (`lumen/services/llm.py:752`) is set **after** the preflight `app_context()` block
  (`:725-750`) — model lookup, endpoint selection, cache salt, timeout resolution. So `duration`
  excludes all Lumen preflight.
- `duration` ends at `:832`, **before** billing.
- `request_logs.time` is stamped inside `update_stats` (`:554-555`), called at `:848` — **after**
  `subtract_coins`. So `time` is completion **plus billing**, not stream end.
- `t_first` is set inside `if delta.content:` (`:816-819`), so reasoning/thinking deltas do **not**
  stop the clock. On a reasoning model, "time to first token" is time-to-first-*visible*-token.
- `record_stream_abort(..., started_at=...)` (`:654`, called at `:772`) already uses the name
  `started_at` to mean the post-preflight `t0`.

Because the client generator blocks inside `yield` when the client reads slowly, **client download
time is charged to `duration`** and deflates `output_speed` as though the backend were slow.

### 2.7 Multi-process is anticipated in code but not safe to turn on

`db_pool.detect_workers()` reads `WEB_CONCURRENCY` and parses `--workers`; `detect_replicas()` reads
`LUMEN_REPLICAS`. But: the chart mounts no volume at `multiproc_dir`, nothing wipes it at startup,
and `prometheus_client` gauges default to `multiprocess_mode='all'` (one series per PID).
`mark_process_dead` does not run under SIGKILL, so a crashed worker's `livesum` gauge file keeps
contributing for the pod's lifetime.

Known-safe and known-unsafe under N processes:
- **Safe:** the coin refiller — `lumen/services/token_refill.py:101-113` is a compare-and-set on
  `last_refill_at`, so a second process's pass is a no-op. The config watcher — per-process reload is
  correct.
- **Unsafe/wasteful:** the health checker. `lumen/services/health.py:27` holds a module-global
  8-thread executor **per process**, and `BACKGROUND_WORKER` (`lumen/__init__.py:470`) is a
  process-wide env var that uvicorn's children all inherit, so it cannot mean "extra workers only."
  N processes ⇒ N× probe load on the backends.
- **Degraded:** `_rr_counters` round-robin becomes N independent rotations; the `_get_request_rates`
  30 s cache becomes N caches.

### 2.8 The test suite has never executed any Timescale code

`tests/conftest.py:34` builds the schema with `db.create_all()` on SQLite; `.github/workflows/test.yml`
provisions **no services**; `tests/unit/test_migrations.py` only checks the Alembic graph shape without
a database. The hypertable, the continuous aggregate, the refresh policy and every line of
`/api/usage/*` SQL are untested — those endpoints return empty on the dialect check before reaching
the query. Anything aggregate-shaped ships untested until CI grows a Postgres+Timescale service.

### 2.9 There is already a real ASGI integration harness — reuse it

`tests/integration/test_disconnect.py:205-226` imports `asgi` with `lumen.create_app` patched to
return the test app, then runs **real uvicorn** on an ephemeral port — deliberately, so the test
exercises the wiring production uses rather than a copy that can drift. Flask's `test_client()`
bypasses `asgi.py` entirely, so anything stamped in the ASGI layer is invisible to it. Any test of
disconnect behaviour, queue timing or the WSGI bridge belongs on that fixture.

### 2.10 Timescale migration constraints

New columns on `request_logs` must be **nullable with a server default**, or Timescale rejects
propagating them to populated chunks — documented at
`migrations/versions/e6f7a8b9c0d1_add_request_logs_aborted.py:48-53`. Do not backfill; state the
reason in the migration docstring, following the precedent there.

`request_logs.time` is the **only** tz-aware timestamp in the codebase (it is the hypertable
partition key). Everything else is naive UTC via `lumen.timeutils.utcnow()`. Consequence: writing a
naive datetime into a `TIMESTAMPTZ` column makes Postgres interpret it against the session
`TimeZone` — a silent, deployment-dependent offset bug. Any new column on `request_logs` that must be
compared against `time` has to be `TIMESTAMPTZ` **and** stamped with `datetime.now(timezone.utc)`,
which is a deliberate, documented exception to the `utcnow()` rule.

### 2.11 Cost hotspots worth knowing before adding load

- `/metrics` queries the database on **every scrape** (`lumen/blueprints/metrics/routes.py:67-143`) —
  a `GROUP BY` over `model_stats` plus two `COUNT(*)` over `entities` — taking a pooled connection to
  do it. It competes for the pool it is reporting on. This collector already caused a production
  connection leak (fixed in 1.22.0) and carries a comment explaining why it releases its own session.
- `lumen/blueprints/models_page/routes.py:49-60` runs **two uncached `COUNT(*)` scans of
  `request_logs` per model-page view**. The API's equivalent is cached 30 s forty lines away in
  `lumen/blueprints/api/routes.py:108-146`.
- `lumen/services/pool_tracker.py:38` captures a 25-frame stack on **every** pool checkout. Its cost
  scales with concurrency rather than duration. Do not remove it — it caught production leaks nothing
  else could, twice — but know it is there.
- Every request builds a fresh `openai.OpenAI(...)`, so TCP + TLS is paid per call with no keep-alive.

### 2.12 Two unrelated conditions both return 429

The limiter's handler returns `rate_limit_error`/`rate_limit_exceeded`; `check_coin_budget` returns
`TOO_MANY_REQUESTS` with a different body shape entirely, bypassing the error handler. Neither writes
a `request_logs` row, neither logs, and neither sets `Retry-After`. From the outside they are one
undifferentiated `status="429"`.

Also: six chat routes share one 30/min per-user bucket — the chat page itself, uploads, the stream,
the conversation list, message fetch and delete. Under a burst the visible failure may be a 429 on
the **page**, which reads as a total outage.

---

## Part 3 — If you are the session implementing the plan

1. **Re-verify the pin.** The plan's facts are checked against `196f642`. Confirm HEAD before
   trusting any line number.
2. **Gate 0 first.** Five questions, four of them one query or one message each. Two of them
   (production topology, is Redis deployed) determine whether whole phases ship or defer; one
   (does a Prometheus stack exist) can move Phase 7 to the front.
3. **Phase 1 is the safe hand-off.** Self-contained, has a hard exit gate, does not depend on
   Gate 0, and is the prerequisite for everything else.
4. **Respect the standing invariants in the plan's §14** — they encode failures that already
   happened in production twice.
5. **When you fix something a review found, assume your fix is the new weakest point.** See §1.1.
