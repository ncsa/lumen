# Operator Commands

Lumen ships these `flask` commands for operators. In a deployment, give the command to the image
as its argument; in a local checkout, run `uv run flask --app run <command>`. This page writes them as `flask <command>`
for short. `flask --app run <command> --help` prints the same usage and options as this page.

| Command | What it does |
|---|---|
| [`upgrade`](#upgrade) | Migrates the database and backfills the usage aggregates; the one-shot upgrade step. |
| [`wait-for-migrations`](#wait-for-migrations) | Waits until the database is at this image's migration head. Read-only. |
| [`backfill-aggregate`](#backfill-aggregate) | Materialises a continuous aggregate's full history, month by month. |
| [`check-aggregate`](#check-aggregate) | Reports whether the usage aggregates cover `request_logs` and agree. Read-only. |
| [`check-stats`](#check-stats) | Compares the folded usage totals with `request_logs`; `--fix` repairs them. |
| [`init-db`](#init-db) | Syncs the models in `config.yaml` to the database. |
| [`reassign-model`](#reassign-model) | Moves conversations, stats and request history from one model to another. |
| [`erase-entity`](#erase-entity) | Hard-deletes a user for GDPR erasure. |

`backfill-aggregate` and `check-aggregate` need PostgreSQL/TimescaleDB; on SQLite they print a note
and exit 0. Flask-Migrate also adds `flask db …`; use `flask upgrade` rather than
`flask db upgrade` so the post-migration steps run too.

**`request_logs` is never pruned.** Lumen ships no retention policy and no command that deletes
from it.

## Running commands in a deployment

The image's entrypoint runs any command given as its first argument as
`flask --app run <command> [options]`, and exits with the command's status. Use it for every
command on this page and for `db …`:

```bash
docker run --rm -e CONFIG_YAML=… -e DATABASE_URL=… -v …:/app/instance <image> check-stats --fix
```

With Compose, run it in a one-off container of the `lumen` service. `docker compose run` starts the
services `lumen` depends on first, so the database is up and the upgrade step has run:

```bash
docker compose run --rm lumen check-aggregate
```

With Helm, run it in the running app pod (`<fullname>` is the release's full name). `kubectl exec`
bypasses the entrypoint, so name the app yourself:

```bash
kubectl exec deploy/<fullname> -- flask --app run check-aggregate
```

For a shell in the image, give it `bash`. It starts a shell at once, without waiting for the
database:

```bash
docker run --rm -it <image> bash
```

How the entrypoint reads its arguments:

| Argument | What runs |
|---|---|
| `bash` | A shell. |
| A command, e.g. `upgrade`, `check-stats --fix`, `erase-entity 42 --yes`, `db current` | `flask --app run` with those arguments. `PGCONNECT_TIMEOUT` defaults to `10` (a value you set is kept), so a database host that never answers cannot hang it. |
| Nothing, or options such as `--workers 4` | The server: [`wait-for-migrations`](#wait-for-migrations), then uvicorn with those options. |

## `upgrade`

The one-shot upgrade step: brings the database up to date for this image, then exits.

```bash
flask upgrade
```

The image runs it with the `upgrade` argument (`docker run <image> upgrade`; in Compose the
`lumen-upgrade` service, in Helm the migration Job). The app container never migrates: it waits at
[`wait-for-migrations`](#wait-for-migrations) until this has run. It runs, in order, and stops at
the first failure with exit 1:

1. **Every migration** (`flask db upgrade` to head).
2. **The TimescaleDB extension version check** (advisory). When the extension in the database is
   older than the version the server ships, it prints a WARNING with the exact command to run. It
   never fails the upgrade and never updates the extension: `ALTER EXTENSION … UPDATE` is one-way,
   so it stays a deliberate step (see [Upgrading TimescaleDB](#upgrading-timescaledb)).
3. **The post-migration data steps.** Each is safe to re-run and finishes what an earlier, failed
   run left undone, so running `flask upgrade` again resumes. Today there is one: the **aggregate
   backfill by missing month** of the five usage aggregates.

For the extension check, the `-U` and `-d` values (the role that owns the `timescaledb` extension
and the current database) are read from the catalog and shell-quoted, and the WARNING prints the
plain `psql` command, the Compose form and the Helm form. With extension 2.26.0 on a 2.31.0 server,
owned by `lumen` in database `lumen`:

```text
WARNING: TimescaleDB extension is 2.26.0 but the server has 2.31.0. Back up the database, then run as the first command of a fresh session:
  psql -X -U lumen -d lumen -c 'ALTER EXTENSION timescaledb UPDATE;'
  Compose: docker compose exec postgres psql -X -U lumen -d lumen -c 'ALTER EXTENSION timescaledb UPDATE;'
  Helm (bundled PostgreSQL): kubectl exec -it <fullname>-postgresql-0 -- psql -X -U lumen -d lumen -c 'ALTER EXTENSION timescaledb UPDATE;'
See "Upgrading TimescaleDB" in docs/admin/commands.md.
```

It prints nothing on SQLite, without the extension, or when the versions match. If the catalog
cannot be read, it prints one `Note: could not check the TimescaleDB extension version: …` line and
the upgrade continues.

The backfill walks the usage aggregates child before parent, in the
[refresh order](#backfill-aggregate): `request_counts_hourly_by_entity`,
`request_counts_daily_by_entity`, `request_counts_hourly`, `request_counts_hourly_total`,
`request_counts_daily`.

An aggregate missing from the database is skipped. Migrations create new aggregates `WITH NO DATA`
and rely on this step to fill them.

For each aggregate, the backfill looks at every calendar month from the oldest `request_logs` row
up to the aggregate's refresh horizon: now, less the refresh policy's end offset, rounded down to a
whole bucket. Buckets after the horizon are not materialised yet, and are served from raw rows. A month is *missing* when it has raw rows but no materialised bucket, and
*partly covered* when its first materialised bucket is later than the bucket of its first raw row,
at the aggregate's own bucket width (the refresh policy only ever materialises a trailing window, so
it can leave the head of a month behind). Each such month is refreshed, oldest first, from its first
day, or from the aggregate's retention boundary when that falls inside the month, to the month's end
or up to now, with the same lock retries as `backfill-aggregate`. When every month is covered it
costs two indexed reads per month and refreshes nothing. A month that still fails after the retries
ends the run with exit 1; the next run checks again and starts at the first month still missing. A
hole in the middle of a month is not detected: `flask backfill-aggregate --name NAME --from YYYY-MM`
fixes that by hand.

The backfill is skipped on SQLite and on a database without TimescaleDB. If `request_logs` has a
retention policy it is skipped too: it logs the same ERROR as the startup check and does not fail
the upgrade. Months entirely before an aggregate's own retention boundary, if it has one, are skipped.

## `wait-for-migrations`

Read-only: waits until the database is at the migration head this image ships, then exits 0.

```bash
flask wait-for-migrations [--timeout SECONDS]
```

| Flag | Meaning |
|---|---|
| `--timeout` | Stop retrying once this many seconds have passed and exit 1; the check in progress can add up to 10 seconds. Default: wait forever. |

It compares the heads of the bundled migrations with the revision stamped in the database, connecting
the same way `flask db upgrade` does (including `database.url` from `config.yaml`). While the database
is behind, empty or unreachable it checks again every 2 seconds, logs the current and expected
revisions only when they change, and about every 30 seconds logs a reminder to run the upgrade step
(the image with `upgrade`, the `lumen-upgrade` Compose service, or the Helm migration Job). A
revision this image does not know means a newer image already migrated the database, so it exits
1 at once. It never migrates anything.

Two separate time budgets apply to a PostgreSQL server that accepts connections but never answers:

- **The command's own checks.** Each one gives up after 10 seconds and counts as unreachable, so
  the command can exit up to that much after `--timeout` has passed. Connection options already in
  the database URL (or `PGOPTIONS`), such as `-c search_path=...`, are kept.
- **Loading the app.** Before the command starts, the app opens connections that have no timeout of
  their own. Set `PGCONNECT_TIMEOUT` (for example `10`) so a silent host cannot hold the command
  before its first check; it does not change the 10-second budget above. The image sets it
  to `10` when it runs a command (`docker run <image> wait-for-migrations [--timeout SECONDS]`)
  unless you set it yourself.

The image's default (serve) mode runs this command, with no `--timeout`, before it starts uvicorn,
with the same `PGCONNECT_TIMEOUT` default, for the gate only.

## `backfill-aggregate`

Materialises a continuous aggregate's full history, month by month, oldest first.

```bash
flask backfill-aggregate [--name NAME] [--from YYYY-MM] [--force]
```

| Flag | Meaning |
|---|---|
| `--name` | Aggregate to refresh. Default `request_counts_hourly_by_entity`. Refresh bottom-up (see below). |
| `--from` | First month to refresh. Default: the month of the oldest `request_logs` row. |
| `--force` | Refresh months before a retention boundary anyway, erasing their *aggregate* rows. Raw rows are never touched. |

**Refresh bottom-up.** An aggregate built on another one is computed from that child's
materialised rows, so backfilling it before the child rebuilds it from the child's stale rows. When
you backfill by hand, run `--name` in this order, each over the same `--from`, starting at the
lowest aggregate you changed:

`request_counts_hourly_by_entity` → `request_counts_daily_by_entity` → `request_counts_hourly` →
`request_counts_hourly_total` → `request_counts_daily` → `request_metrics_1m`

(`request_metrics_1m` is built on `request_logs` directly; it comes last only to keep the order
stable.)

Migrations create their aggregates `WITH NO DATA`, and the refresh policy only ever materialises its
own `start_offset` window, so the `/usage` charts, which read only the aggregates, stay near-empty
until the history is backfilled. The upgrade step ([`flask upgrade`](#upgrade)) backfills every
missing month of the usage aggregates, so you rarely need this by hand. It walks month by month
because one `CALL refresh_continuous_aggregate(NULL, NULL)` over a production hypertable is a single
long transaction with unbounded memory, and it never refreshes past `now()`, which would materialise
the current bucket and hide every request logged after the backfill.

The retention refusal only fires if someone added a retention policy to `request_logs` by hand:
refreshing a window whose raw chunks were dropped recomputes it as **empty and deletes the
materialised rows, with no error**. The command refuses such a window and prints how many aggregate
rows it would have erased; `--force` overrides the refusal and erases them.

TimescaleDB refuses an *overlapping* refresh outright rather than queueing behind it, so a month
landing while a scheduled policy job is mid-refresh fails immediately with `SQLSTATE 55P03`
(`LockNotAvailable`). `request_metrics_1m`'s policy fires every minute, so a long backfill will meet
one; each month is retried for up to 60 seconds and the line reports `(after N lock retries)`. Every
month that succeeds is printed with `OK`, except a month younger than one bucket, which prints
`skipped (less than one bucket old; served live by real-time aggregation)`; a month that fails
outright prints the exact `--from` to resume with. Re-running an already-refreshed month is safe:
it recomputes that window from its source and overwrites it, so resuming never double-counts.

It exits 1 with `Error: '<name>' is not a continuous aggregate in this database.` for an unknown
`--name`, and with `Error: --from must be YYYY-MM, got '<value>'.` for a malformed `--from`. With no
`--from` and an empty `request_logs`, it prints `request_logs is empty; nothing to backfill.` and
exits 0.

## `check-aggregate`

Read-only: reports whether the usage aggregates (`request_counts_hourly_by_entity`,
`request_counts_daily_by_entity`, `request_counts_hourly`, `request_counts_hourly_total`,
`request_counts_daily`) cover all of `request_logs` and agree with each other.

```bash
flask check-aggregate
```

It prints the entity aggregate's earliest *materialised* bucket (read from its materialisation
hypertable, not the view, which answers from raw for anything never refreshed) and the oldest
`request_logs` row. It exits 1 when the aggregate is empty or starts after the oldest raw row,
printing the `flask backfill-aggregate --from YYYY-MM` that fixes it; until then the per-user charts
are missing that history. It also checks every month by the rule [`flask upgrade`](#upgrade)
backfills by, and exits 1 listing each month that is *missing* (raw rows, no materialised bucket) or
*partly covered* (its first materialised bucket is later than the bucket of its first raw row), with
both timestamps; `flask upgrade`, or `flask backfill-aggregate [--name <view>] --from YYYY-MM` from
the first listed month, fixes them. The month check runs for all five aggregates, each at
its own bucket width. For every month before the one holding the lowest of their watermarks it then
compares the monthly `requests`, `input_tokens`, `output_tokens` and `cost` of the five aggregates'
materialised rows, and exits 1 listing each month and column that differ: the other views are sums
of the entity aggregate, so a difference is drift. To fix drift, refresh those months bottom-up
with `flask backfill-aggregate --name <view> --from YYYY-MM`, from the first listed month, for each
usage aggregate in the [refresh order](#backfill-aggregate). It also exits 1 if a retention policy
exists on `request_logs`, and prints the `SELECT remove_retention_policy('request_logs');` that removes it.
If one of the five aggregates does not exist, it exits 1 at once with
`Error: '<name>' does not exist. Run 'flask db upgrade' first.`

When every check passes it exits 0, and its last line is
`Usage aggregates agree on every month before YYYY-MM.` (on an install with no requests yet, a note
that the aggregates have never been refreshed). Run it before upgrading.

## `check-stats`

Compares the folded usage totals with `request_logs` since the stats cutover, which the upgrade
sets automatically. Only the rows billing left to the fold (`stats_deferred`) are compared.

```bash
flask check-stats [--fix]
```

| Flag | Meaning |
|---|---|
| `--fix` | Add each difference to the totals. |

Every difference is printed per `model_stats` / `entity_stats` row, and the command exits 1. A
difference means requests the fold missed, such as a billing transaction that committed more than
`stats.fold_lag` after its timestamp. `--fix` adds each difference to the totals instead. Before the
cutover it does nothing. It scans every `request_logs` row since the cutover and holds the fold
state while it does, so on a large install run it at a quiet time: the fold waits until it finishes.
On PostgreSQL `check-stats`, [`reassign-model`](#reassign-model) and [`erase-entity`](#erase-entity)
do not run at the same time: `check-stats` skips, the other two exit with an error. While either of
the other two moves `request_logs`, the check would report differences that are not drift, and
`--fix` would count them twice, so a `check-stats` started then prints `Skipped: a reassign-model or
erase-entity (or another check-stats) is running; run check-stats after it finishes.` and exits 0
without checking anything.

## `init-db`

Syncs the models (`model_configs`, their endpoints and `model_aliases`) from `config.yaml` to the
database, disables the models no longer in the file, then exits. If the aliases are invalid, it
fails before writing anything.

```bash
flask init-db
```

You rarely need it: the app syncs the models on every start, and the config watcher syncs them
whenever `config.yaml` changes (see [Hot Reload](config.md#hot-reload)). Use it to push a model
change to the database without waiting for the watcher or restarting. It does not update in-memory
settings like `APP_NAME` or `CHAT_CONVERSATION_REMOVE_MODE`; those only update when the watcher
picks up the change or the app restarts.

## `reassign-model`

Moves all conversations, stats and request history from one model to another.

```bash
flask reassign-model FROM_ID TO_ID
```

`FROM_ID` and `TO_ID` are `model_configs.id` values. It refuses, with exit 1, when `FROM_ID` and
`TO_ID` are the same model_config id or either id does not exist. The `FROM` row in `model_configs`
is left as it is. The command moves:

- the conversations that use `FROM`,
- its `request_logs` rows,
- its `model_stats` rows, merged into `TO`'s where both have one for the same entity and source.

On PostgreSQL it then refreshes the affected aggregates from `FROM`'s first request onwards.

It is not atomic on PostgreSQL: `request_logs` moves one chunk per transaction, so a failure partway
leaves the history split between `FROM` and `TO`. It is safe to re-run: the same command picks up
where the last one stopped. Run it off-hours for a model with a large history, and retire `FROM` in
`config.yaml` first so no new requests are logged against it while it runs. It refuses to run while
`request_logs` has a retention policy.

On PostgreSQL `reassign-model`, [`erase-entity`](#erase-entity) and [`check-stats`](#check-stats)
do not run at the same time: `check-stats` skips, the other two exit with an error. A
`reassign-model` started while one of them runs exits 1 with `Error: a reassign-model, erase-entity
or check-stats is already running; retry when it finishes.` and changes nothing.

## `erase-entity`

Hard-deletes a user for GDPR erasure. It cannot be undone; run it off-hours.

```bash
flask erase-entity ENTITY_ID [--yes]
```

| Flag | Meaning |
|---|---|
| `--yes` | Erase without asking for confirmation. Only for scripts. |

`ENTITY_ID` is the user's `entities.id`. Run it without `--yes` first: it prints who will be
erased, and which groups will be left without an owner, and asks before it changes anything:

```text
Erasing user 42: Jane Doe <jane@example.edu>
  groups that will be left without an owner: physics-lab
This deletes the user and cannot be undone. Continue? [y/N]:
```

`y` or `yes` (any case) continues; `n`, `no` or Enter prints `Aborted.` and exits 1 with nothing
changed; any other answer asks again. Use `--yes` only in a script that has already checked the id.

It deletes the user's conversations, messages, auth requests, balance, limits, model consents,
group and project memberships. It moves the user's request history, usage stats and models
(disabled) to the shared ghost user, and the user's API keys too, revoked and anonymised. Groups the
user owned are left without an owner. Only users can be erased: it refuses a project, the ghost, a
user who still owns a project (transfer ownership first), and runs only while `request_logs` has no
retention policy.

On PostgreSQL `request_logs` moves one chunk per transaction, and each UPDATE can decompress a whole
chunk, hence off-hours. It is safe to re-run: an interrupted run leaves every row owned by the user
or the ghost, and re-running with the same id, even after the user is deleted, finishes the
aggregate refresh.

On PostgreSQL `erase-entity`, [`reassign-model`](#reassign-model) and [`check-stats`](#check-stats)
do not run at the same time: `check-stats` skips, the other two exit with an error. An
`erase-entity` started while one of them runs exits 1 with `Error: a reassign-model, erase-entity or
check-stats is already running; retry when it finishes.` and changes nothing.

What is deleted and what moves, table by table, is in
[Erasing a user](https://ncsa.github.io/lumen/dbschema/#erasing-a-user-gdpr) in the database schema
reference.

## Upgrading TimescaleDB

TimescaleDB is required: Lumen does not run on a plain `postgres` image. Pin the image tag
(`timescale/timescaledb:2.31.0-pg17` is the tested version) and upgrade on purpose. A new image
only installs the new extension files; the database keeps running the old version until it is
updated, and nothing in Lumen updates it for you: [`flask upgrade`](#upgrade) prints a WARNING
with the exact command for your database, but never runs it. Until then Lumen keeps working, but logs `TimescaleDB extension is <old> but the server has <new>` at
startup and reports `lumen_timescaledb_extension_update_available` as `1` on `/metrics`.

`ALTER EXTENSION … UPDATE` cannot be undone.

1. **Back up the database.**
2. Bump the image tag (`postgresql.image` in the Helm chart, `image:` in `docker-compose.yml`) and
   restart the database.
3. Update the extension. This must be the first command in a fresh session (`-X` skips `.psqlrc`),
   run as the owner of the extension (normally the user that ran the migrations). `lumen` is the
   example owner and database name; the WARNING from [`flask upgrade`](#upgrade) prints these
   commands with your real ones.

    Compose:

    ```bash
    docker compose exec postgres psql -X -U lumen -d lumen -c 'ALTER EXTENSION timescaledb UPDATE;'
    ```

    Helm, with the bundled PostgreSQL (`<fullname>` is the release's full name):

    ```bash
    kubectl exec -it <fullname>-postgresql-0 -- psql -X -U lumen -d lumen -c 'ALTER EXTENSION timescaledb UPDATE;'
    ```

4. Check the version. This must return the new version (`2.31.0`):

    ```sql
    SELECT extversion FROM pg_extension WHERE extname = 'timescaledb';
    ```

5. Restart Lumen. Confirm the startup log no longer shows
   `TimescaleDB extension is <old> but the server has <new>`, and that
   `lumen_timescaledb_extension_update_available` on `/metrics` is `0`.
6. Run [`flask check-aggregate`](#check-aggregate); it must exit 0.

At startup, and then periodically in every worker process (see "Prometheus metrics" in the
[README](https://github.com/ncsa/lumen#prometheus-metrics) for the interval), Lumen checks the
extension version, the `request_logs` hypertable, the continuous aggregates and their background
jobs. It logs a WARNING per problem (ERROR when the extension or the hypertable is missing, or when
`request_logs` has a retention policy) and never blocks startup.
