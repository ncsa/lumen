import math
import os
import re
import shlex
import shutil
import tempfile
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
from types import SimpleNamespace
from urllib.parse import urlparse

import click
import yaml
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from flask import current_app
from flask.cli import with_appcontext
from flask_migrate import upgrade as migrate_upgrade
from sqlalchemy import create_engine, delete, func, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.pool import NullPool

from lumen.models.api_key import APIKey
from lumen.models.entity import Entity
from lumen.models.entity_stat import EntityStat
from lumen.models.model_alias import ModelAlias
from lumen.models.model_config import ModelConfig
from lumen.models.model_endpoint import ModelEndpoint
from lumen.models.model_stat import ModelStat
from lumen.models.request_log import RequestLog
from lumen.services.erasure import _erase_dependents, _ghost_keys, get_or_create_ghost, groups_losing_owner, merge_stats_into
from lumen.services.llm import _bump
from lumen.services.stats_cutover import lock_fold_state
from lumen.services.stats_fold import TOTALS, fold_increments, window_sums
from lumen.services.timescale_health import extension_versions
from lumen.timeutils import as_utc_naive

from .extensions import db

# The entity-dimensioned continuous aggregate the per-entity /usage queries read.
# `check-aggregate` reports whether it covers every request_logs row. request_logs
# itself is never pruned (#167), so this is about chart speed, not about saving data.
ENTITY_AGGREGATE = "request_counts_hourly_by_entity"

# Every continuous aggregate Lumen creates, children before the aggregates built on
# them: refreshing a hierarchical aggregate before its child would rebuild it from the
# child's stale rows. request_metrics_1m is built on request_logs, not on another
# aggregate; its place at the end only keeps the walk stable. Aggregates missing from a
# database are skipped; ones not listed here come last.
AGGREGATE_REFRESH_ORDER = (
    ENTITY_AGGREGATE,
    "request_counts_daily_by_entity",
    "request_counts_hourly",
    "request_counts_hourly_total",
    "request_counts_daily",
    "request_metrics_1m",
)

# The aggregates behind the /usage charts, which ``flask upgrade`` keeps backfilled. The
# per-entity daily one and the org-wide hourly one are built on the entity aggregate; the
# other org-wide ones on hourly (hourly_total, the heatmap's per-hour totals, and daily).
USAGE_AGGREGATES = (ENTITY_AGGREGATE, "request_counts_daily_by_entity", "request_counts_hourly",
                    "request_counts_hourly_total", "request_counts_daily")

# request_logs key columns the chunk walk can move rows along. The column name is
# interpolated into SQL, so every helper that takes one checks it against this set.
MOVABLE_COLUMNS = frozenset({"model_config_id", "entity_id"})

# Deduplicate deprecation warnings; the config watcher re-runs sync every 5s.
_warned: set = set()


def _warn_once(key, msg, *args):
    if key not in _warned:
        _warned.add(key)
        current_app.logger.warning(msg, *args)


def write_config_yaml(config_path, data):
    """Write ``data`` (a top-level config dict) to config_path as YAML.

    Each top-level key is dumped as its own document fragment (preserving key order
    and keeping blocks visually separated), the previous file is backed up to
    ``<config>.bak``, and the new content is written atomically via a temp file.
    Shared by the admin config editor and project creation.

    If you add a new secret-bearing key to the schema, add it to
    :data:`SENSITIVE_KEYS` in :mod:`lumen.services.config_watcher` so it is
    masked before being sent to the admin browser (see ``config_api_get``).
    """
    parts = [
        yaml.dump({k: v}, Dumper=yaml.SafeDumper, default_flow_style=False, allow_unicode=True, sort_keys=False)
        for k, v in data.items()
    ]
    fd, tmp_path = tempfile.mkstemp(suffix=".yaml")
    try:
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(parts))
        # Back up the current config before overwriting so a partial or
        # malformed save can be recovered from <config>.bak. Best-effort:
        # in containers only config.yaml itself is bind-mounted, so the
        # directory may not be writable — a failed backup must not block
        # the save.
        if os.path.exists(config_path):
            try:
                shutil.copy2(config_path, config_path + ".bak")
            except OSError as e:
                current_app.logger.warning(
                    "write_config_yaml: could not write backup %s.bak: %s", config_path, e
                )
        shutil.copyfile(tmp_path, config_path)
    finally:
        os.unlink(tmp_path)


def _normalize_model_url(url):
    """Expand a bare HuggingFace repo id (`org/name`) to a full huggingface.co URL.

    Common HuggingFace host variants (huggingface.com, www.) are rewritten to
    huggingface.co so the README lookup recognizes them; any other URL is used
    as given."""
    if not url:
        return None
    if "://" not in url and re.fullmatch(r"[\w.-]+/[\w.-]+", url):
        return f"https://huggingface.co/{url}"
    parsed = urlparse(url)
    if parsed.netloc.lower() in ("huggingface.com", "www.huggingface.co", "www.huggingface.com"):
        url = parsed._replace(netloc="huggingface.co").geturl()
    return url


def _normalize_end_date(value, model_name=None):
    """Coerce a config end_date (date, datetime, or ISO string) to naive-UTC datetime.

    A bare date is stored as midnight UTC at the start of the following day, so
    the model remains usable throughout the named date. Explicit datetimes stay
    exact. Unparseable values are dropped with a warning."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day) + timedelta(days=1)
    text_value = str(value)
    bare_iso_date = re.fullmatch(r"\d{4}-\d{2}-\d{2}", text_value) is not None
    parsed_rfc822_date = False
    try:
        parsed = datetime.fromisoformat(text_value)
    except ValueError:
        # The admin config editor round-trips YAML dates through JSON, which
        # Flask serializes in RFC 822 form ("Thu, 31 Dec 2026 00:00:00 GMT").
        try:
            parsed = parsedate_to_datetime(text_value)
            parsed_rfc822_date = parsed.hour == parsed.minute == parsed.second == parsed.microsecond == 0
        except ValueError:
            _warn_once(("end-date", model_name), "invalid end_date '%s' on model '%s'; ignoring", value, model_name)
            return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    if bare_iso_date or parsed_rfc822_date:
        parsed += timedelta(days=1)
    return parsed


def _normalize_knowledge_cutoff(value, model_name=None):
    """Clamp a knowledge cutoff to YYYY-MM (the column is String(7)).

    models.dev sometimes reports YYYY-MM-DD; keep just the year-month. Values
    that are not YYYY-MM or YYYY-MM-DD are dropped with a warning."""
    if not value:
        return None
    value = str(value)
    if re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])(?:-\d{2})?", value):
        return value[:7]
    _warn_once(("knowledge-cutoff", model_name),
               "invalid knowledge_cutoff '%s' on model '%s'; expected YYYY-MM, ignoring", value, model_name)
    return None


def _apply_model_fields(config, model_def):
    config.input_cost_per_million = model_def["input_cost_per_million"]
    config.output_cost_per_million = model_def["output_cost_per_million"]
    if "audio_cost_per_hour" in model_def:
        config.audio_cost_per_hour = model_def["audio_cost_per_hour"]
    elif "audio_cost_per_minute" in model_def:
        # Legacy per-minute pricing -> per-hour (×60).
        legacy = model_def.get("audio_cost_per_minute")
        _warn_once(("audio-cost", model_def.get("name")),
                   "deprecated 'audio_cost_per_minute' on model '%s'; use 'audio_cost_per_hour' instead", model_def.get("name"))
        config.audio_cost_per_hour = (legacy * 60) if legacy is not None else None
    else:
        config.audio_cost_per_hour = None
    _apply_model_access(config, model_def)
    config.description = model_def.get("description") or None
    config.url = _normalize_model_url(model_def.get("url"))
    config.supports_function_calling = model_def.get("supports_function_calling")
    config.input_modalities = model_def.get("input_modalities") or None
    config.output_modalities = model_def.get("output_modalities") or None
    config.context_window = model_def.get("context_window") or None
    config.max_output_tokens = model_def.get("max_output_tokens") or None
    config.supports_reasoning = model_def.get("supports_reasoning")
    config.knowledge_cutoff = _normalize_knowledge_cutoff(model_def.get("knowledge_cutoff"), model_def.get("name"))
    config.notice = model_def.get("notice") or None
    config.ack_message = model_def.get("ack_message") or None
    config.end_date = _normalize_end_date(model_def.get("end_date"), model_def.get("name"))


def _apply_model_access(config, model_def):
    """Set config.needs_ack / early_access / disabled from a model definition.

    Bridges the legacy `active:` boolean (active: false -> disabled) with a
    deprecation warning. Never touches owner_entity_id — model
    ownership is DB-managed via the admin UI, not config.yaml.
    """
    config.needs_ack = bool(model_def.get("needs_ack", False))
    config.early_access = bool(model_def.get("early_access", False))

    disabled = model_def.get("disabled")
    if disabled is None and model_def.get("active") is False:
        _warn_once(("active", model_def.get("name")),
                   "deprecated 'active: false' on model '%s'; use 'disabled: true' instead", model_def.get("name"))
        disabled = True
    config.disabled = bool(disabled)


def _reconcile_endpoints(config, model_def):
    # Keep every row, including duplicate URLs. Prefer a currently active row
    # before reusing a retired one, preserving IDs and historical references.
    existing_by_url = {}
    for ep in sorted(config.endpoints, key=lambda ep: (not ep.active, ep.id)):
        existing_by_url.setdefault(ep.url, []).append(ep)

    for ep_def in model_def.get("endpoints", []):
        candidates = existing_by_url.get(ep_def["url"], [])
        if not candidates:
            db.session.add(ModelEndpoint(
                model_config_id=config.id,
                url=ep_def["url"],
                api_key=ep_def["api_key"],
                model_name=ep_def.get("model") or None,
                active=True,
                healthy=False,
            ))
        else:
            existing_ep = candidates.pop(0)
            if not existing_ep.active:
                existing_ep.healthy = False  # require a fresh probe on reactivation
            existing_ep.active = True
            existing_ep.api_key = ep_def["api_key"]
            existing_ep.model_name = ep_def.get("model") or None

    for remaining in existing_by_url.values():
        for ep in remaining:
            ep.active = False
            ep.healthy = False  # retired rows are never probed again; drop stale health


def _deactivate_removed_models(yaml_model_names):
    stmt = select(ModelConfig)
    if yaml_model_names:
        stmt = stmt.where(ModelConfig.model_name.notin_(yaml_model_names))
    deactivated_ids = [c.id for c in db.session.execute(stmt).scalars().all()]
    if not deactivated_ids:
        return
    db.session.execute(
        update(ModelEndpoint).where(ModelEndpoint.model_config_id.in_(deactivated_ids)).values(active=False, healthy=False)
    )
    db.session.execute(
        update(ModelConfig).where(ModelConfig.id.in_(deactivated_ids)).values(disabled=True)
    )
    db.session.expire_all()


# Maximum length of an alias/ID (must match the model_aliases.alias column).
_ALIAS_MAX_LEN = 128
# POST /v1/models/<id>/acknowledge would be ambiguous for an id ending in this.
RESERVED_MODEL_ID_SUFFIX = "/acknowledge"


def model_alias_config_errors(models) -> list[str]:
    """Validate the ``aliases`` of every model definition.

    Names are exact and case-sensitive. Neither a model name nor an alias may
    end in ``/acknowledge``: ``POST /v1/models/<id>/acknowledge`` would be
    ambiguous for it. Returns human-readable error strings
    (empty when valid). The canonical names are collected from the whole list
    first so an alias can never collide with a model name that appears later.

    Shared by the startup watcher, the CLI sync, and the admin config editor so
    an invalid alias configuration is rejected everywhere before any writes.
    """
    errors: list[str] = []
    canonical_names = {
        m.get("name") for m in models
        if isinstance(m, dict) and isinstance(m.get("name"), str) and m.get("name")
    }
    seen_aliases: set[str] = set()
    for i, model in enumerate(models):
        if not isinstance(model, dict):
            continue
        name = model.get("name")
        label = name if isinstance(name, str) and name else f"models[{i}]"
        if isinstance(name, str) and name.endswith(RESERVED_MODEL_ID_SUFFIX):
            errors.append(f"{label}: model name cannot end in '{RESERVED_MODEL_ID_SUFFIX}' (reserved for the acknowledge endpoint)")
        aliases = model.get("aliases")
        if aliases is None:
            continue
        if not isinstance(aliases, list):
            errors.append(f"{label}: 'aliases' must be a list")
            continue
        for j, alias in enumerate(aliases):
            if not isinstance(alias, str) or not alias.strip():
                errors.append(f"{label}: aliases[{j}] must be a non-empty string")
                continue
            if len(alias) > _ALIAS_MAX_LEN:
                errors.append(f"{label}: alias '{alias}' exceeds {_ALIAS_MAX_LEN} characters")
                continue
            if alias.endswith(RESERVED_MODEL_ID_SUFFIX):
                errors.append(f"{label}: alias '{alias}' cannot end in '{RESERVED_MODEL_ID_SUFFIX}' (reserved for the acknowledge endpoint)")
                continue
            if alias == name:
                errors.append(f"{label}: alias '{alias}' cannot equal the model's own name")
                continue
            if alias in canonical_names:
                errors.append(f"{label}: alias '{alias}' collides with model name '{alias}'")
                continue
            if alias in seen_aliases:
                errors.append(f"{label}: duplicate alias '{alias}'")
            seen_aliases.add(alias)
    return errors


def validate_model_aliases(data):
    """Raise ``ValueError`` when the config's model aliases are invalid.

    Central validation for the CLI sync and the config watcher, both of which
    call :func:`sync_models_from_yaml` and must reject a bad config before any
    writes. See :func:`model_alias_config_errors`.
    """
    models = data.get("models", []) if isinstance(data, dict) else []
    errors = model_alias_config_errors(models)
    if errors:
        raise ValueError("; ".join(errors))


def _reconcile_aliases(config, model_def):
    """Upsert the model_aliases rows for ``config`` from ``model_def['aliases']``.

    Removes aliases no longer declared, adds newly declared ones, and retargets
    an alias that moved from another model (the alias is unique, so a move must
    update the existing row rather than insert a duplicate).
    """
    desired = set(model_def.get("aliases", []) or [])
    existing = {a.alias: a for a in config.aliases}
    for alias in list(existing):
        if alias not in desired:
            db.session.delete(existing[alias])
    for alias in desired:
        row = db.session.execute(select(ModelAlias).filter_by(alias=alias)).scalar_one_or_none()
        if row is None:
            db.session.add(ModelAlias(alias=alias, model_config_id=config.id))
        elif row.model_config_id != config.id:
            row.model_config_id = config.id


def sync_models_from_yaml(yaml_data):
    """Upsert ModelConfig and ModelEndpoint rows from yaml_data. Must run inside an app context."""
    validate_model_aliases(yaml_data)
    for model_def in yaml_data.get("models", []):
        config = db.session.execute(select(ModelConfig).filter_by(model_name=model_def["name"])).scalar_one_or_none()
        if not config:
            config = ModelConfig(model_name=model_def["name"])
            db.session.add(config)
        _apply_model_fields(config, model_def)
        db.session.flush()  # ensure config.id exists before reconciling endpoints
        _reconcile_endpoints(config, model_def)
        _reconcile_aliases(config, model_def)

    _deactivate_removed_models({m["name"] for m in yaml_data.get("models", [])})
    db.session.commit()


@click.command("init-db")
@with_appcontext
def init_db_cmd():
    """Sync ModelConfig and ModelEndpoint from config.yaml."""
    yaml_data = current_app.config["YAML_DATA"]
    sync_models_from_yaml(yaml_data)
    click.echo("Database synced from config.yaml.")


#: PostgreSQL advisory lock key shared by reassign-model, erase-entity and
#: check-stats, so none of them runs while another one does. Any fixed bigint no
#: other code in the database uses (token_refill uses 7_172_093_405_172).
STATS_WALK_LOCK_KEY = 7_172_093_405_173

#: What reassign-model and erase-entity print when the lock is taken.
WALK_LOCK_HELD = "Error: a reassign-model, erase-entity or check-stats is already running; retry when it finishes."


@contextmanager
def stats_walk_lock():
    """Hold the advisory lock shared by reassign-model, erase-entity and check-stats.

    Yields whether it got the lock; it never waits. On PostgreSQL the lock is
    session-level, on a connection of its own committed right after the lock
    call, so it stays held while the command commits on db.session, and a
    crashed command releases it when its connection drops. Callers take it
    before lock_fold_state, never after. On SQLite it yields True: reassign-model
    and erase-entity move every row under the fold lock there, so there is no
    unlocked walk to keep check-stats away from.
    """
    if db.engine.dialect.name != "postgresql":
        yield True
        return
    conn = db.engine.connect()
    try:
        acquired = conn.execute(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": STATS_WALK_LOCK_KEY}
        ).scalar_one()
        conn.commit()
    except Exception:
        # The lock may be held without us knowing; dropping the connection frees it.
        conn.invalidate()
        conn.close()
        raise
    if not acquired:
        conn.close()
        yield False
        return
    try:
        yield True
    finally:
        _release_stats_walk_lock(conn)


def _release_stats_walk_lock(conn):
    """Unlock and close; if the unlock fails, drop the connection so the lock dies with it."""
    try:
        conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": STATS_WALK_LOCK_KEY})
        conn.commit()
    except Exception:
        conn.invalidate()
    finally:
        conn.close()


@click.command("reassign-model")
@click.argument("from_id", type=int)
@click.argument("to_id", type=int)
@with_appcontext
def reassign_model_cmd(from_id, to_id):
    """Move all conversations, stats and request history from one model to another.

    FROM_ID and TO_ID are model_configs.id values. The FROM row in model_configs is
    left as it is.

    Not atomic on PostgreSQL: request_logs moves one chunk per transaction, so a
    failure partway leaves its history split between FROM and TO. It is safe to
    re-run: the same command picks up where the last one stopped. Run it off-hours
    for a model with a large history, and retire FROM in config.yaml first so no new
    requests are logged against it while it runs.

    On PostgreSQL reassign-model, erase-entity and check-stats do not run at the
    same time: check-stats skips, the other two exit with an error.
    """
    with stats_walk_lock() as acquired:
        if not acquired:
            click.echo(WALK_LOCK_HELD)
            raise SystemExit(1)
        _reassign_model(from_id, to_id)


def _reassign_model(from_id, to_id):
    """reassign-model itself, run under stats_walk_lock."""
    from lumen.models.conversation import Conversation
    from lumen.models.request_log import RequestLog

    if from_id == to_id:
        click.echo(f"Error: FROM and TO are the same model_config id ({from_id}).")
        raise SystemExit(1)
    src = db.session.get(ModelConfig, from_id)
    dst = db.session.get(ModelConfig, to_id)
    if not src:
        click.echo(f"Error: model_config id {from_id} not found.")
        raise SystemExit(1)
    if not dst:
        click.echo(f"Error: model_config id {to_id} not found.")
        raise SystemExit(1)
    from_name, to_name = src.model_name, dst.model_name
    # End the read transaction: on PostgreSQL the chunk walk below can run for hours,
    # and a session left idle in transaction that long is reaped by the server's
    # idle_in_transaction_session_timeout before the conversations step uses it.
    db.session.commit()

    click.echo(f"Reassigning from '{from_name}' (id={from_id}) to '{to_name}' (id={to_id})")

    postgres = db.engine.dialect.name == "postgresql"
    if postgres:
        with db.engine.connect() as conn:
            retention = _retention_job(conn)
            if retention is not None:
                click.echo(f"Error: request_logs has a retention policy (job {retention[0]}, "
                           f"config {retention[1]}).\n"
                           f"  Refreshing the aggregates over windows whose raw chunks were\n"
                           f"  dropped would erase their history. Run 'flask check-aggregate'.")
                raise SystemExit(1)
            # request_counts_hourly_total has neither model_config_id nor entity_id, so
            # neither this walk nor erase-entity's returns it. That is correct: moving
            # rows to another model or entity leaves every hour's totals unchanged.
            boundaries = {name: _retention_boundary(conn, name)
                          for name in _aggregates_with_column(conn, "model_config_id")}
            # Read before anything moves: afterwards FROM has no raw rows left to date it.
            start = _reassign_start(conn, "model_config_id", from_id, boundaries)
        log_count = _move_request_logs("model_config_id", from_id, to_id)

    result = db.session.execute(update(Conversation).where(Conversation.model == from_name).values(model=to_name))
    conv_count = result.rowcount
    click.echo(f"  conversations updated: {conv_count}")
    db.session.commit()

    # The fold state is held only around the model_stats merge, from here to the
    # commit: a fold pass in between could add FROM's request_logs to a FROM row
    # this has already merged away. The chunk walk above runs without it (it can
    # take hours); a pass during the walk folds each row under whichever model it
    # has at that moment, and FROM's rows are merged below. FROM's remaining
    # request_logs move under the lock too, so no pass folds them into FROM after
    # the merge: all of them on SQLite, which has no chunk walk, and on
    # PostgreSQL the few logged while the walk ran. Taken first in the
    # transaction (see lock_fold_state).
    lock_fold_state()
    if postgres:
        db.session.execute(text("SET LOCAL timescaledb.max_tuples_decompressed_per_dml_transaction = 0"))
    moved = db.session.execute(
        update(RequestLog).where(RequestLog.model_config_id == from_id).values(model_config_id=to_id)
    ).rowcount
    log_count = log_count + moved if postgres else moved

    # For model_stats, merge rows that might collide on the unique constraint
    existing_dst_stats = {
        (s.entity_id, s.source): s
        for s in db.session.execute(select(ModelStat).filter_by(model_config_id=to_id)).scalars().all()
    }
    src_stats = db.session.execute(select(ModelStat).filter_by(model_config_id=from_id)).scalars().all()
    stats_merged = 0
    stats_moved = 0
    for stat in src_stats:
        key = (stat.entity_id, stat.source)
        if key in existing_dst_stats:
            dst_stat = existing_dst_stats[key]
            for name in TOTALS:
                for column in (name, f"folded_{name}"):
                    setattr(dst_stat, column, getattr(dst_stat, column) + getattr(stat, column))
            if stat.last_used_at and (not dst_stat.last_used_at or stat.last_used_at > dst_stat.last_used_at):
                dst_stat.last_used_at = stat.last_used_at
            db.session.delete(stat)
            stats_merged += 1
        else:
            stat.model_config_id = to_id
            stats_moved += 1
    click.echo(f"  model_stats moved: {stats_moved}, merged: {stats_merged}")
    click.echo(f"  request_logs updated: {log_count}")

    db.session.commit()

    if postgres:
        if start is None:
            click.echo("  no request_logs rows for FROM; aggregates unchanged")
        else:
            _refresh_reassigned(start, boundaries)
    click.echo("Done.")


def _check_column(column):
    """Refuse a key column outside ``MOVABLE_COLUMNS`` before it reaches any SQL."""
    if column not in MOVABLE_COLUMNS:
        raise ValueError(f"request_logs rows cannot be moved along column {column!r}")


def _move_request_logs(column, from_id, to_id):
    """Move request_logs rows with ``column = FROM`` to TO one chunk per transaction, oldest first.

    Also recompresses every chunk that is flagged compressed but has rows in its
    uncompressed heap, whether this run moved rows in it or not, so a re-run repairs
    a chunk left half-done by an interrupted run. Returns the number of rows moved.
    """
    _check_column(column)
    done = []
    moved_total = 0
    with db.engine.connect() as conn:
        chunks = conn.execute(text(
            "SELECT format('%I.%I', chunk_schema, chunk_name), range_start, range_end, is_compressed "
            "FROM timescaledb_information.chunks WHERE hypertable_name = 'request_logs' "
            "ORDER BY range_start"
        )).all()
        conn.rollback()
        for chunk, range_start, range_end, compressed in chunks:
            try:
                moved = _move_chunk(conn, column, from_id, to_id, range_start, range_end)
            except DBAPIError as exc:
                click.echo(f"Error: moving request_logs rows in {chunk} failed: {exc.orig}")
                click.echo(f"  Chunks done: {', '.join(done)}" if done else "  No chunk was done.")
                click.echo("  History is split between FROM and TO until the same command is re-run.")
                raise SystemExit(1)
            if moved:
                moved_total += moved
                click.echo(f"  {chunk}: {moved} request_logs rows moved")
            if compressed:
                try:
                    if _recompress_chunk(conn, chunk):
                        click.echo(f"  {chunk}: recompressed")
                except DBAPIError as exc:
                    click.echo(f"Error: recompressing {chunk} failed: {exc.orig}")
                    click.echo("  Re-running the same command finishes it.")
                    raise SystemExit(1)
            done.append(chunk)
    return moved_total


def _move_chunk(conn, column, from_id, to_id, range_start, range_end):
    """Move FROM's rows inside one chunk's time range in one transaction. Returns the row count."""
    _check_column(column)
    params = {"f": from_id, "t": to_id, "s": range_start, "e": range_end}
    with conn.begin():
        if not conn.execute(text(
            "SELECT EXISTS (SELECT 1 FROM request_logs "
            f"WHERE {column} = :f AND time >= :s AND time < :e)"
        ), params).scalar():
            return 0
        # model_config_id is a compression segmentby column, so an UPDATE on it decompresses
        # every batch of FROM in the chunk. entity_id is not segmentby: FROM's rows are
        # spread across every batch, so an UPDATE on it can decompress the whole chunk.
        # Either way, past the per-transaction decompression
        # limit (100k by default) TimescaleDB aborts it. SET LOCAL lifts the limit for
        # this one transaction only, and COMMIT puts it back. Never a plain SET: it
        # outlives the transaction and leaks an unlimited guard to the next user of this
        # pooled connection. Never on _autocommit_connection() either: there SET LOCAL
        # has no transaction block to apply to and fails.
        conn.execute(text("SET LOCAL timescaledb.max_tuples_decompressed_per_dml_transaction = 0"))
        return conn.execute(text(
            f"UPDATE request_logs SET {column} = :t "
            f"WHERE {column} = :f AND time >= :s AND time < :e"
        ), params).rowcount


def _recompress_chunk(conn, chunk):
    """Recompress a compressed chunk that has rows in its uncompressed heap.

    An UPDATE on a compressed chunk writes the rows it touched back uncompressed and
    leaves the chunk flagged compressed, so the compression policy never revisits it.
    ``recompress => true`` folds the heap back in (measured on 2.27.2: heap 0 after);
    ``if_not_compressed => true`` alone would skip the chunk because of that flag.
    Returns whether it recompressed.
    """
    with conn.begin():
        # chunk is format('%I.%I') from the catalog, so it is already quoted.
        if not conn.execute(text(f"SELECT EXISTS (SELECT 1 FROM ONLY {chunk})")).scalar():
            return False
        conn.execute(text("SELECT compress_chunk(CAST(:c AS regclass), recompress => true)"), {"c": chunk})
    return True


def _retention_boundary(conn, name):
    """Start of the hour holding ``now() - drop_after`` of a retention policy on ``name``.

    None without a policy. Floored to an hour, in UTC, so refresh windows starting
    there stay aligned to every aggregate's buckets.
    """
    boundary = conn.execute(text(
        "SELECT now() - CAST(config->>'drop_after' AS interval) FROM timescaledb_information.jobs "
        "WHERE proc_name = 'policy_retention' AND hypertable_name = :n"
    ), {"n": name}).scalar()
    if boundary is None:
        return None
    return boundary.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def _reassign_start(conn, column, from_id, boundaries):
    """Earliest time ``column = FROM`` still shows up, or None if it shows up nowhere.

    Checks raw request_logs and the materialisation hypertable of every aggregate in
    ``boundaries`` (inside that aggregate's retention), so a re-run after an interrupted
    refresh still refreshes the months whose buckets are materialised under FROM. Every
    aggregate in ``boundaries`` must have ``column``: see ``_aggregates_with_column``.
    """
    _check_column(column)
    found = [conn.execute(text(
        f"SELECT MIN(time) FROM request_logs WHERE {column} = :f"
    ), {"f": from_id}).scalar()]
    for name, boundary in boundaries.items():
        bucket = _bucket_column(conn, name)
        found.append(conn.execute(text(
            f'SELECT MIN("{bucket}") FROM {_materialization_hypertable(conn, name)} '
            f'WHERE {column} = :f AND (CAST(:b AS timestamptz) IS NULL OR "{bucket}" >= :b)'
        ), {"f": from_id, "b": boundary}).scalar())
    found = [t for t in found if t is not None]
    return min(found) if found else None


def _refresh_reassigned(start, boundaries):
    """Refresh every aggregate month by month from the month of ``start`` to now.

    In the order of ``boundaries``, which ``_aggregates_with_column`` gives in refresh
    order, so a hierarchical aggregate is refreshed after the child it is built on.

    The policies only reprocess their own short windows, so without this every older
    bucket keeps FROM forever. An aggregate with its own retention starts at that
    boundary instead: refreshing older windows rebuilds rows retention drops again.
    """
    month = start.astimezone(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    now = datetime.now(timezone.utc)
    with _autocommit_connection() as conn:
        for name, boundary in boundaries.items():
            first = max(month, boundary) if boundary is not None else month
            click.echo(f"  refreshing '{name}' from {first:%Y-%m-%d} to {now:%Y-%m-%d}, month by month")
            try:
                _refresh_months(conn, name, first, now)
            except _RefreshFailed as failed:
                click.echo(f"Error: refreshing '{name}' for {failed.month:%Y-%m} failed: {failed.error.orig}")
                click.echo("  request_logs is moved; re-run the same command to finish the refresh.")
                raise SystemExit(1)


#: The largest cost difference check-stats reads as equal: one unit of the
#: Numeric(12, 6) columns, which SQLite sums as floats.
_COST_TOLERANCE = Decimal("0.000001")


def _stats_drift(model, key_columns, expected_rows):
    """The ``model`` rows whose fold part differs from ``expected_rows``.

    Returns (key, differences, last_used_at) for each, where ``differences``
    maps every name in ``TOTALS`` to what ``request_logs`` holds beyond the
    row's ``folded_*`` column (zero when equal), and ``last_used_at`` is the
    latest ``request_logs`` time when the row's own is older, else None.
    """
    expected = {tuple(getattr(r, c) for c in key_columns): r for r in expected_rows}
    recorded = {
        tuple(getattr(s, c) for c in key_columns): s
        for s in db.session.execute(select(model)).scalars()
    }
    drift = []
    for key in sorted(expected.keys() | recorded.keys()):
        want, have = expected.get(key), recorded.get(key)
        differences = {
            name: (getattr(want, name) if want else 0) - (getattr(have, f"folded_{name}") if have else 0)
            for name in TOTALS
        }
        if abs(differences["cost"]) <= _COST_TOLERANCE:
            differences["cost"] = 0
        last = as_utc_naive(want.last_used_at) if want else None
        if last is not None and have is not None and have.last_used_at is not None and have.last_used_at >= last:
            last = None
        if last is not None or any(differences.values()):
            drift.append((key, differences, last))
    return drift


@click.command("check-stats")
@click.option("--fix", is_flag=True, help="Add each difference to the totals.")
@with_appcontext
def check_stats_cmd(fix):
    """Compare the folded usage totals with request_logs since the stats cutover.

    For every model_stats / entity_stats row, the part the fold added (the
    folded_* columns) must equal the request_logs sums of [cutover_at,
    folded_until) over the rows billing marked stats_deferred, and
    last_used_at must not be older than the latest of those requests.
    Unmarked rows were bumped by billing directly and are not compared. A
    difference means requests the fold missed, such as a billing transaction
    that committed more than stats.fold_lag after its timestamp. Reports every
    difference and exits 1; with --fix adds it to the totals instead. Holds the
    fold state meanwhile, so no fold pass runs in the middle. Scans every
    request_logs row since the cutover. Does nothing before the cutover.

    On PostgreSQL reassign-model, erase-entity and check-stats do not run at
    the same time: check-stats skips, the other two exit with an error. While
    either of the other two moves request_logs, rows already moved count for
    their target, whose folded part is merged only at the end, so a check then
    would report differences that are not drift, and --fix would count them
    twice. A skipped run prints "Skipped" and exits 0.
    """
    with stats_walk_lock() as acquired:
        if not acquired:
            click.echo("Skipped: a reassign-model or erase-entity (or another check-stats) is running; "
                       "run check-stats after it finishes.")
            return
        _check_stats(fix)


def _check_stats(fix):
    """check-stats itself, run under stats_walk_lock."""
    cutover_at, folded_until = lock_fold_state()
    if cutover_at is None:
        db.session.rollback()
        click.echo("No stats cutover yet: billing still updates model_stats and entity_stats; nothing to check.")
        return

    per_model, per_entity = window_sums(cutover_at, folded_until)
    keys = {ModelStat: ("entity_id", "model_config_id", "source"), EntityStat: ("entity_id",)}
    drift = [
        (model, key, differences, last)
        for model, rows in ((ModelStat, per_model), (EntityStat, per_entity))
        for key, differences, last in _stats_drift(model, keys[model], rows)
    ]
    click.echo(f"Checked request_logs from {cutover_at} to {folded_until} UTC.")
    for model, key, differences, last in drift:
        changes = [f"{name} {value:+}" for name, value in differences.items() if value]
        if last is not None:
            changes.append(f"last_used_at -> {last}")
        where = ", ".join(f"{column}={value}" for column, value in zip(keys[model], key))
        click.echo(f"  {model.__tablename__} {where}: {', '.join(changes)}")
        if fix:
            _bump(model, dict(zip(keys[model], key)),
                  *fold_increments(model, SimpleNamespace(**differences)), last_used_at=last)

    if not drift:
        db.session.rollback()
        click.echo("model_stats and entity_stats match request_logs.")
    elif fix:
        db.session.commit()
        click.echo(f"Fixed {len(drift)} row(s).")
    else:
        db.session.rollback()
        click.echo(f"{len(drift)} row(s) differ from request_logs. Run 'flask check-stats --fix' to repair them.")
        raise SystemExit(1)


@click.command("erase-entity")
@click.argument("entity_id", type=int)
@click.option("--yes", is_flag=True, help="Erase without asking for confirmation.")
@with_appcontext
def erase_entity_cmd(entity_id, yes):
    """Hard-delete a user for GDPR erasure. Cannot be undone; run it off-hours.

    Deletes the user's conversations, messages, auth requests, balance, limits, model
    consents, group and project memberships. Moves the user's request history, usage
    stats and models (disabled) to the shared ghost user, and the user's API keys too,
    revoked and anonymised. Groups the user owned are left without an owner.

    On PostgreSQL request_logs moves one chunk per transaction, and each UPDATE can
    decompress a whole chunk, hence off-hours. Safe to re-run: an interrupted run
    leaves every row owned by the user or the ghost, and re-running with the same id,
    even after the user is deleted, finishes the aggregate refresh.

    On PostgreSQL reassign-model, erase-entity and check-stats do not run at the
    same time: check-stats skips, the other two exit with an error.
    """
    with stats_walk_lock() as acquired:
        if not acquired:
            click.echo(WALK_LOCK_HELD)
            raise SystemExit(1)
        _erase_entity(entity_id, yes)


def _erase_entity(entity_id, yes):
    """erase-entity itself, run under stats_walk_lock."""
    postgres = db.engine.dialect.name == "postgresql"
    if postgres:
        with db.engine.connect() as conn:
            retention = _retention_job(conn)
            if retention is not None:
                click.echo(f"Error: request_logs has a retention policy (job {retention[0]}, "
                           f"config {retention[1]}).\n"
                           f"  Refreshing the aggregates over windows whose raw chunks were\n"
                           f"  dropped would erase their history. Run 'flask check-aggregate'.")
                raise SystemExit(1)
            # request_counts_hourly_total has neither entity_id nor model_config_id, so
            # neither this walk nor reassign-model's returns it. That is correct: moving
            # rows to the ghost entity leaves every hour's totals unchanged.
            boundaries = {name: _retention_boundary(conn, name)
                          for name in _aggregates_with_column(conn, "entity_id")}

    user = db.session.get(Entity, entity_id)
    if user is None:
        # Either never existed, or erased by a run whose aggregate refresh did not
        # finish: buckets materialised under the id are all that can be left.
        start = None
        if postgres:
            with db.engine.connect() as conn:
                start = _reassign_start(conn, "entity_id", entity_id, boundaries)
        if start is None:
            click.echo(f"Entity {entity_id} does not exist and nothing is left under it: nothing to do.")
            return
        click.echo(f"Entity {entity_id} is already deleted; finishing its aggregate refresh.")
        _refresh_reassigned(start, boundaries)
        click.echo("Done.")
        return
    if user.entity_type != "user":
        click.echo(f"Error: entity {entity_id} is a {user.entity_type}, not a user; only users can be erased.")
        raise SystemExit(1)
    owned = db.session.execute(
        select(Entity.name).where(Entity.entity_type == "project", Entity.owner_entity_id == entity_id)
        .order_by(Entity.name)
    ).scalars().all()
    if owned:
        click.echo(f"Error: user {entity_id} owns these projects: {', '.join(owned)}.\n"
                   f"  Transfer their ownership first.")
        raise SystemExit(1)

    orphaned = groups_losing_owner(entity_id)
    click.echo(f"Erasing user {entity_id}: {user.name} <{user.email}>")
    if orphaned:
        click.echo(f"  groups that will be left without an owner: {', '.join(orphaned)}")
    if not yes and not click.confirm("This deletes the user and cannot be undone. Continue?"):
        click.echo("Aborted.")
        raise SystemExit(1)

    # No new request is logged against the user from here on, and existing sessions
    # and keys stop working (login_required and api_key_required reject inactive users).
    user.active = False
    db.session.commit()
    ghost_id = get_or_create_ghost().id
    # End the read transaction before the chunk walk, as reassign-model does.
    db.session.commit()

    if postgres:
        with db.engine.connect() as conn:
            # Read before anything moves: afterwards the user has no raw rows left to date it.
            start = _reassign_start(conn, "entity_id", entity_id, boundaries)
        log_count = _move_request_logs("entity_id", entity_id, ghost_id)

    # One transaction from here to the entity delete, holding the fold state
    # throughout: a fold pass in between could add the user's request_logs to a
    # user stats row this has already merged away, and the delete would then take
    # those counts with it. The chunk walk above runs without it (it can take
    # hours); a pass during the walk folds each row under whichever entity owns it
    # at that moment. Taken first in the transaction (see lock_fold_state).
    lock_fold_state()
    if postgres:
        # Rows logged during the walk, by a request already in flight at deactivation.
        # SET LOCAL lasts only for this transaction, so the sweep UPDATE right after
        # must stay in it, with no commit or rollback in between.
        db.session.execute(text("SET LOCAL timescaledb.max_tuples_decompressed_per_dml_transaction = 0"))
    result = db.session.execute(update(RequestLog).where(RequestLog.entity_id == entity_id)
                                .values(entity_id=ghost_id))
    log_count = log_count + result.rowcount if postgres else result.rowcount
    click.echo(f"  request_logs moved to the ghost: {log_count}")

    moved, merged, entity_merged = merge_stats_into(ghost_id, entity_id)
    click.echo(f"  model_stats moved: {moved}, merged: {merged}; entity_stats merged: {entity_merged}")

    models = db.session.execute(
        select(ModelConfig).where(ModelConfig.owner_entity_id == entity_id).order_by(ModelConfig.model_name)
    ).scalars().all()
    in_yaml = {m.get("name") for m in current_app.config.get("YAML_DATA", {}).get("models", [])}
    for model in models:
        # Re-own rather than leave to ON DELETE SET NULL, which would make a private model public.
        model.disabled = True
        model.owner_entity_id = ghost_id
        click.echo(f"  model disabled and moved to the ghost: {model.model_name}")
        if model.model_name in in_yaml:
            click.echo(f"  Warning: '{model.model_name}' is still in config.yaml; the next sync re-enables it.\n"
                       f"    Only its owner, the inactive ghost, can use it. Remove it from config.yaml.")

    click.echo(f"  api_keys revoked, anonymised and moved to the ghost: {_ghost_keys(entity_id, ghost_id)}")

    counts, orphaned = _erase_dependents(entity_id)
    for table, count in counts.items():
        click.echo(f"  {table} deleted: {count}")
    for name in orphaned:
        click.echo(f"  group left without an owner: {name}")

    if not postgres:
        # SQLite enforces no foreign keys here: a leftover row would point at a deleted id.
        left = {
            "request_logs": db.session.scalar(select(func.count()).select_from(RequestLog)
                                              .where(RequestLog.entity_id == entity_id)),
            "api_keys": db.session.scalar(select(func.count()).select_from(APIKey)
                                          .where(APIKey.entity_id == entity_id)),
        }
        if any(left.values()):
            db.session.rollback()
            click.echo(f"Error: rows still reference user {entity_id}: {left}. Nothing was deleted.")
            raise SystemExit(1)

    try:
        db.session.execute(delete(Entity).where(Entity.id == entity_id))
        db.session.commit()
    except IntegrityError as exc:
        # On PostgreSQL the RESTRICT foreign keys from request_logs refuse the delete
        # while a row still references the user: one logged after the sweep above.
        db.session.rollback()
        click.echo(f"Error: deleting user {entity_id} failed: {exc.orig}")
        click.echo("  Nothing in this last step was kept; the user is still there, inactive.\n"
                   "  Most likely a request_logs row was logged after the move: re-run the same command.")
        raise SystemExit(1)
    click.echo(f"  user {entity_id} deleted")

    if postgres:
        if start is None:
            click.echo("  no request_logs rows for the user; aggregates unchanged")
        else:
            _refresh_reassigned(start, boundaries)
    click.echo("Done.")


# --------------------------------------------------------------------------- #
# TimescaleDB lifecycle (Phase 8): aggregate backfill and coverage check.      #
# --------------------------------------------------------------------------- #

def _autocommit_connection():
    """Open a connection that is *not* inside a transaction block.

    ``CALL refresh_continuous_aggregate`` cannot run inside a transaction, and a
    ``flask`` command using ``db.session`` is already in one — the CALL then fails
    with "cannot run inside a transaction block" in the middle of an operator's
    maintenance window. Same technique as ``seed_analytics.py``.
    """
    return db.engine.connect().execution_options(isolation_level="AUTOCOMMIT")


def _in_refresh_order(names):
    """``names`` in ``AGGREGATE_REFRESH_ORDER``, then any it does not list, alphabetically."""
    known = [name for name in AGGREGATE_REFRESH_ORDER if name in names]
    return known + sorted(name for name in names if name not in AGGREGATE_REFRESH_ORDER)


def _continuous_aggregates(conn):
    """Names of every continuous aggregate in this database, in refresh order."""
    return _in_refresh_order(conn.execute(text(
        "SELECT view_name FROM timescaledb_information.continuous_aggregates ORDER BY view_name"
    )).scalars().all())


def _aggregates_with_column(conn, column):
    """Names of the continuous aggregates grouped by ``column``, in refresh order.

    Only these hold buckets keyed on it, so only these need checking and refreshing
    after rows move along it.
    """
    _check_column(column)
    return _in_refresh_order(conn.execute(text(
        "SELECT a.view_name FROM timescaledb_information.continuous_aggregates a "
        "JOIN information_schema.columns c "
        "ON c.table_schema = a.view_schema AND c.table_name = a.view_name "
        "WHERE c.column_name = :c ORDER BY a.view_name"
    ), {"c": column}).scalars().all())


def _bucket_column(conn, view_name):
    """Name of the aggregate's ``time_bucket`` column, which is always its first."""
    return conn.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = :v AND ordinal_position = 1"
    ), {"v": view_name}).scalar()


def _bucket_width(conn, view_name):
    """The aggregate's ``time_bucket`` width as a ``timedelta``, or None if unreadable.

    Read from TimescaleDB's internal catalog, which ``timescaledb_information`` does not
    expose. Being internal it can change between releases, so any failure to read it
    gives None and the caller refreshes as it would without knowing the width. A failed
    read prints a warning, so a catalog change is not mistaken for a view without a row,
    and aborts an open transaction: call it on an autocommit connection.
    """
    try:
        return conn.execute(text(
            "SELECT CAST(bf.bucket_width AS interval) "
            "FROM _timescaledb_catalog.continuous_agg ca "
            "JOIN _timescaledb_catalog.continuous_aggs_bucket_function bf "
            "ON bf.mat_hypertable_id = ca.mat_hypertable_id "
            "WHERE ca.user_view_schema = 'public' AND ca.user_view_name = :v"
        ), {"v": view_name}).scalar()
    except DBAPIError as exc:
        click.echo(f"  Warning: could not read the bucket width of '{view_name}' from the "
                   f"TimescaleDB catalog ({exc.orig}); refreshing every month in full.")
        return None


#: The origin ``time_bucket`` aligns timestamptz buckets to by default: a Monday, in UTC.
#: Every Lumen aggregate uses the default; one created with its own ``origin`` would not
#: line up with this.
_BUCKET_ORIGIN = datetime(2000, 1, 3, tzinfo=timezone.utc)


def _floor_to_bucket(dt, width):
    """Start of the bucket of ``width`` holding ``dt``, in UTC, as ``time_bucket`` aligns it.

    An unknown width (None) floors to the hour, the narrowest bucket of Lumen's usage
    aggregates, and so does a width that is not positive. Calendar widths (months) are
    not supported: they have no fixed length, and no Lumen aggregate uses one.
    """
    dt = dt.astimezone(timezone.utc)
    if width is None or width <= timedelta(0):
        return dt.replace(minute=0, second=0, microsecond=0)
    return dt - (dt - _BUCKET_ORIGIN) % width


def _refresh_end_offset(conn, view_name):
    """``end_offset`` of the aggregate's refresh policy as a ``timedelta``.

    Zero without a policy, or with one refreshing up to now: its last ``end_offset``
    is never materialised by the policy, only by a manual refresh.
    """
    offset = conn.execute(text(
        "SELECT CAST(j.config->>'end_offset' AS interval) FROM timescaledb_information.jobs j "
        # A refresh job names the aggregate's view as its hypertable, not the
        # materialisation hypertable.
        "WHERE j.proc_name = 'policy_refresh_continuous_aggregate' "
        "AND j.hypertable_schema = 'public' AND j.hypertable_name = :v"
    ), {"v": view_name}).scalar()
    return offset or timedelta(0)


def _window_too_small(start, end, width):
    """True when ``[start, end)`` is shorter than one bucket of ``width``.

    TimescaleDB rounds a refresh window inward to whole buckets and rejects one that
    holds none ("refresh window too small"). An unknown width (None) is never too small.
    """
    return width is not None and end - start < width


def _materialization_hypertable(conn, view_name):
    """The physical table holding an aggregate's *materialised* rows.

    Not the view. With ``materialized_only = false`` the view unions materialised rows
    with a live scan of the raw rows above the watermark, so ``MIN(bucket)`` read through
    the view reports how far back *raw* goes for as long as the watermark is still at
    -infinity — which is exactly the freshly-migrated state where no backfill has run.
    Only the materialisation hypertable answers what would still be there once retention
    has dropped the raw chunks.
    """
    return conn.execute(text(
        "SELECT materialization_hypertable_schema || '.' || materialization_hypertable_name "
        "FROM timescaledb_information.continuous_aggregates WHERE view_name = :v"
    ), {"v": view_name}).scalar()


def _retention_drop_after(conn):
    """The retention policy's interval on request_logs, or None if retention is off."""
    return conn.execute(text(
        "SELECT config->>'drop_after' FROM timescaledb_information.jobs "
        "WHERE proc_name = 'policy_retention' AND hypertable_name = 'request_logs'"
    )).scalar()


def _retention_job(conn):
    """``(job_id, config)`` of any retention policy on request_logs, or None.

    Matches on the job's existence, not on its interval: a policy added with
    drop_created_before stores that key instead of drop_after. Shared by
    ``check-aggregate`` and the startup tripwire in ``create_app``.
    """
    return conn.execute(text(
        "SELECT job_id, config::text FROM timescaledb_information.jobs "
        "WHERE proc_name = 'policy_retention' AND hypertable_name = 'request_logs'"
    )).first()


def _fmt(dt):
    """Format a timestamp for the terminal. Always UTC — this is an operator tool."""
    if dt is None:
        return "none"
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _next_month(dt):
    year, month = (dt.year + 1, 1) if dt.month == 12 else (dt.year, dt.month + 1)
    return dt.replace(year=year, month=month, day=1)


# TimescaleDB refuses an *overlapping* refresh outright instead of waiting for it: while
# a background continuous-aggregate policy job is mid-refresh, the CALL raises SQLSTATE
# 55P03 (lock_not_available) immediately -- measured at 0.02s on 2.27.2. request_metrics_1m
# has a policy firing every minute and the entity aggregate one every hour, so a 13-month
# month-by-month backfill will collide at least once. Without a retry that aborts the whole
# command partway and leaves the operator with a half-backfilled aggregate.
_LOCK_NOT_AVAILABLE = "55P03"
_REFRESH_RETRY_BUDGET = 60.0
_REFRESH_RETRY_SLEEP = 2.0


def _refresh_window(conn, name, start, end):
    """Refresh one window, waiting out a concurrent refresh. Returns the retry count.

    Matches the SQLSTATE (psycopg's ``sqlstate``) rather than the message text: the
    message is not part of any stability guarantee, and matching it would turn a version
    bump into a silent loss of the retry.
    """
    deadline = time.monotonic() + _REFRESH_RETRY_BUDGET
    retries = 0
    while True:
        try:
            # psycopg 3 binds server-side and the window arguments are polymorphic
            # ("any"), so the server takes whatever type the driver sends: a naive
            # datetime would arrive as timestamp, not the timestamptz every aggregate
            # here buckets on. The casts pin the types in the SQL instead.
            conn.execute(text("CALL refresh_continuous_aggregate("
                              "CAST(:n AS regclass), CAST(:s AS timestamptz), CAST(:e AS timestamptz))"),
                         {"n": name, "s": start, "e": end})
            return retries
        except DBAPIError as exc:
            if exc.orig.sqlstate != _LOCK_NOT_AVAILABLE:
                raise
            if time.monotonic() + _REFRESH_RETRY_SLEEP > deadline:
                raise
            retries += 1
            time.sleep(_REFRESH_RETRY_SLEEP)


class _RefreshFailed(Exception):
    """A month of ``_refresh_months`` failed; carries what is needed to resume."""

    def __init__(self, month, refreshed, error):
        super().__init__(f"refreshing {month:%Y-%m} failed: {error.orig}")
        self.month = month
        self.refreshed = refreshed
        self.error = error


def _refresh_months(conn, name, start, now):
    """Refresh aggregate ``name`` month by month from ``start`` up to ``now``.

    Prints one line per month and returns the months refreshed as ``YYYY-MM``
    strings. Raises ``_RefreshFailed`` with the failing month and the months already
    refreshed. ``conn`` must be an autocommit connection (``_autocommit_connection``).

    A month window shorter than one bucket (the current month during its first bucket)
    holds no whole bucket, so TimescaleDB would reject it; it is skipped and still
    listed as refreshed. Real-time aggregation serves it until the policy catches up.
    """
    bucket = _bucket_column(conn, name)
    width = _bucket_width(conn, name)
    refreshed = []
    month = start
    while month <= now:
        try:
            nxt = _next_month(month)
            if _window_too_small(month, min(nxt, now), width):
                raw = conn.execute(text(
                    "SELECT COUNT(*) FROM request_logs WHERE time >= :s AND time < :e"
                ), {"s": month, "e": nxt}).scalar()
                click.echo(f"  {month:%Y-%m}: {raw} request_logs rows ... skipped "
                           f"(less than one bucket old; served live by real-time aggregation)")
            else:
                _refresh_month(conn, name, bucket, month, now)
        except DBAPIError as exc:
            raise _RefreshFailed(month, refreshed, exc) from exc
        refreshed.append(f"{month:%Y-%m}")
        month = nxt
    return refreshed


def _refresh_month(conn, name, bucket, month, now):
    """Refresh ``name`` from ``month`` to the next month or ``now``, whichever is first.

    Prints one line with the row counts either side. Raises ``DBAPIError`` once the
    lock retries of ``_refresh_window`` run out. Shared by ``_refresh_months`` and the
    missing-month backfill of ``flask upgrade``.
    """
    nxt = _next_month(month)
    # Never refresh past "now". A window ending in the future materialises the
    # current bucket and moves the aggregate's watermark beyond it, which turns
    # real-time aggregation OFF for that bucket: every request logged after the
    # backfill stays invisible on /usage until a scheduled refresh catches up.
    # Measured on 2.27.2 — with the month end (future) the next insert was
    # missing; with now() the watermark stayed two buckets back and it showed.
    end = min(nxt, now)
    raw = conn.execute(text(
        "SELECT COUNT(*) FROM request_logs WHERE time >= :s AND time < :e"
    ), {"s": month, "e": nxt}).scalar()
    click.echo(f"  {month:%Y-%m}: {raw} request_logs rows ... ", nl=False)
    try:
        retries = _refresh_window(conn, name, month, end)
    except DBAPIError:
        click.echo("FAILED")
        raise
    aggregated = conn.execute(text(
        f'SELECT COUNT(*) FROM "{name}" WHERE "{bucket}" >= :s AND "{bucket}" < :e'
    ), {"s": month, "e": nxt}).scalar()
    click.echo(f"{aggregated} aggregate rows OK"
               + (f" (after {retries} lock retries)" if retries else ""))


def _month_start(dt):
    """First instant, in UTC, of the month holding ``dt``."""
    return dt.astimezone(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _missing_months(conn, name, now):
    """Months whose earliest ``request_logs`` row ``name`` has not materialised.

    Returns ``(start, first_raw, first_bucket)`` per missing month, oldest first, from
    the month of the oldest raw row through the current one. ``start`` is the month's
    first instant, or the aggregate's own retention boundary inside that boundary's
    month; months before the boundary are left out, as ``reassign-model`` does.

    Two indexed probes per month: the earliest raw row and the earliest materialised
    bucket. A month is missing when it has raw rows and no bucket, or when its first
    bucket is later than the bucket of its first raw row, at the aggregate's own bucket
    width (the hour when it cannot be read). Whole-month refreshes plus the
    policy's trailing window can only leave a month uncovered at its head, so this
    finds every gap they can leave; a hole inside a month is not detected.
    """
    oldest = conn.execute(text("SELECT MIN(time) FROM request_logs")).scalar()
    if oldest is None:
        return []
    bucket = _bucket_column(conn, name)
    table = _materialization_hypertable(conn, name)
    boundary = _retention_boundary(conn, name)
    width = _bucket_width(conn, name)
    # A refresh ends at a whole bucket, the policy's at now - end_offset, so the
    # buckets after that are not materialised yet; a month whose only rows are in them
    # is not missing anything. Real-time aggregation serves them.
    horizon = _floor_to_bucket(now - _refresh_end_offset(conn, name), width)
    missing = []
    month = _month_start(oldest)
    while month < horizon:
        nxt = _next_month(month)
        start = max(month, boundary) if boundary is not None else month
        if start < nxt:
            # The bucket holding a retention boundary starts before it once the bucket
            # is wider than the hour the boundary is floored to; probe from that bucket.
            first_raw, first_bucket = conn.execute(text(
                "SELECT (SELECT time FROM request_logs "
                "        WHERE time >= :s AND time < :e ORDER BY time LIMIT 1), "
                f'       (SELECT "{bucket}" FROM {table} '
                f'        WHERE "{bucket}" >= :b AND "{bucket}" < :e ORDER BY "{bucket}" LIMIT 1)'
            ), {"s": start, "b": max(month, _floor_to_bucket(start, width)),
                "e": min(nxt, horizon)}).one()
            if first_raw is not None and (first_bucket is None
                                          or first_bucket > _floor_to_bucket(first_raw, width)):
                missing.append((start, first_raw, first_bucket))
        month = nxt
    return missing


@click.command("backfill-aggregate")
@click.option("--name", default=ENTITY_AGGREGATE, show_default=True,
              help="Continuous aggregate to materialise. Refresh bottom-up: an aggregate "
                   "built on another one reads that child's rows, so backfill the child first "
                   f"({' -> '.join(AGGREGATE_REFRESH_ORDER)}).")
@click.option("--from", "from_month", metavar="YYYY-MM", default=None,
              help="First month to refresh. Defaults to the month of the oldest request_logs row.")
@click.option("--force", is_flag=True,
              help="Refresh months before a retention boundary anyway, erasing those months' "
                   "aggregate rows. Never touches raw request_logs rows. Read the refusal first.")
@with_appcontext
def backfill_aggregate_cmd(name, from_month, force):
    """Materialise a continuous aggregate's full history, month by month.

    A policy-created aggregate holds only its ``start_offset`` window, so per-user
    "All Time" charts stay near-empty until this is run. One
    ``CALL refresh_continuous_aggregate(NULL, NULL)`` over 13 months of a production
    hypertable is a single long transaction with unbounded memory, so this walks
    month by month, oldest first, and prints the row counts either side of each call.

    Aggregates must be refreshed bottom-up, child before parent: one built on another
    aggregate is computed from that child's materialised rows, so backfilling it before
    the child rebuilds it from the child's stale rows.
    """
    if db.engine.dialect.name != "postgresql":
        click.echo(f"backfill-aggregate requires PostgreSQL/TimescaleDB; nothing to do on "
                   f"{db.engine.dialect.name}.")
        return

    with _autocommit_connection() as conn:
        if name not in _continuous_aggregates(conn):
            click.echo(f"Error: '{name}' is not a continuous aggregate in this database.")
            raise SystemExit(1)
        bucket = _bucket_column(conn, name)

        if from_month:
            try:
                start = datetime.strptime(from_month, "%Y-%m").replace(tzinfo=timezone.utc)
            except ValueError:
                click.echo(f"Error: --from must be YYYY-MM, got '{from_month}'.")
                raise SystemExit(1)
        else:
            start = conn.execute(text(
                "SELECT date_trunc('month', MIN(time) AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' "
                "FROM request_logs"
            )).scalar()
            if start is None:
                click.echo("request_logs is empty; nothing to backfill.")
                return

        # Contract (d): refreshing a window whose raw chunks were dropped recomputes it
        # as EMPTY and DELETES the materialised rows, with no error at all. Lumen never
        # adds a retention policy to request_logs (#167), so this is a defensive no-op
        # unless someone added one by hand in SQL — in which case refuse rather than
        # erase aggregate history on top of the raw history already lost.
        drop_after = _retention_drop_after(conn)
        if drop_after is not None:
            boundary = conn.execute(text("SELECT now() - CAST(:d AS interval)"),
                                    {"d": drop_after}).scalar().astimezone(timezone.utc)
            if start < boundary:
                erased = conn.execute(text(
                    f'SELECT COUNT(*) FROM "{name}" WHERE "{bucket}" >= :s AND "{bucket}" < :b'
                ), {"s": start, "b": boundary}).scalar()
                if not force:
                    # The boundary's own month still starts before the boundary, so the
                    # hint has to name the month after it or the re-run is refused too.
                    safe = _next_month(boundary.replace(day=1, hour=0, minute=0,
                                                        second=0, microsecond=0))
                    click.echo(
                        f"Error: refusing to refresh '{name}' from {start:%Y-%m}.\n"
                        f"  retention on request_logs is '{drop_after}', so raw chunks before\n"
                        f"  {_fmt(boundary)} may already have been dropped, and every one that\n"
                        f"  has not will be. Refreshing that window recomputes it as\n"
                        f"  EMPTY and DELETES the materialised rows, with no error: {erased} rows\n"
                        f"  of '{name}' would be erased.\n"
                        f"  Re-run with --from {safe:%Y-%m} to stay inside retention, or --force."
                    )
                    raise SystemExit(1)
                click.echo(
                    f"WARNING: --force given; months before {_fmt(boundary)} are outside retention "
                    f"and their {erased} materialised rows will be erased."
                )

        now = datetime.now(timezone.utc)
        click.echo(f"Backfilling '{name}' from {start:%Y-%m} to {now:%Y-%m}, month by month.")
        try:
            refreshed = _refresh_months(conn, name, start, now)
        except _RefreshFailed as failed:
            click.echo(f"Error: refreshing {failed.month:%Y-%m} failed: {failed.error.orig}")
            if failed.refreshed:
                click.echo(f"  Refreshed OK: {failed.refreshed[0]} .. {failed.refreshed[-1]} "
                           f"({len(failed.refreshed)} months).")
            else:
                click.echo("  No month was refreshed.")
            click.echo(f"  Resume with: flask backfill-aggregate "
                       f"--name {name} --from {failed.month:%Y-%m}")
            raise SystemExit(1)

    if refreshed:
        click.echo(f"Backfill complete: {len(refreshed)} months refreshed "
                   f"({refreshed[0]} .. {refreshed[-1]}).")
    else:
        click.echo("Backfill complete: no months to refresh.")


# The per-month totals ``check-aggregate`` compares across the usage aggregates.
_USAGE_TOTALS = ("requests", "input_tokens", "output_tokens", "cost")


def _watermark(conn, view_name):
    """Where the aggregate's materialisation ends, or None if it has never been refreshed.

    Read through TimescaleDB's internal functions, which ``timescaledb_information`` does
    not expose. A never-refreshed aggregate stores the minimum timestamptz (4714 BC),
    which no ``datetime`` can hold, so anything before year 1 is reported as None.
    """
    return conn.execute(text(
        "SELECT CASE WHEN w >= TIMESTAMPTZ '0001-01-02 00:00:00+00' THEN w END FROM ("
        "  SELECT _timescaledb_functions.to_timestamp("
        "         _timescaledb_functions.cagg_watermark(mat_hypertable_id)) AS w "
        "  FROM _timescaledb_catalog.continuous_agg "
        "  WHERE user_view_schema = 'public' AND user_view_name = :v) s"
    ), {"v": view_name}).scalar()


def _monthly_totals(conn, view_name, before):
    """``{month: (requests, input_tokens, output_tokens, cost)}`` materialised before ``before``.

    Read off the materialisation hypertable, for the reason ``check-aggregate`` gives.
    Months are UTC, which every usage aggregate's buckets nest in.
    """
    bucket = _bucket_column(conn, view_name)
    sums = ", ".join(f"COALESCE(SUM({c}), 0)" for c in _USAGE_TOTALS)
    rows = conn.execute(text(
        f"SELECT date_trunc('month', \"{bucket}\", 'UTC'), {sums} "
        f"FROM {_materialization_hypertable(conn, view_name)} "
        f"WHERE \"{bucket}\" < :b GROUP BY 1"
    ), {"b": before}).all()
    return {row[0].astimezone(timezone.utc): tuple(row[1:]) for row in rows}


def _cross_view_differences(conn):
    """Compare the usage aggregates' per-month totals below the lowest watermark.

    Returns ``(cutoff, differences)``: ``cutoff`` is the first month *not* compared
    (None when any usage aggregate has never been refreshed), ``differences`` lists
    ``(month, column, values)`` with one value per ``USAGE_AGGREGATES`` entry. The
    org-wide aggregates are sums over the entity aggregate's rows, so any difference in
    a month every one of them has fully materialised is drift. The cutoff is the
    minimum watermark across all of them, not any one view's, so a month one view has
    not materialised yet is never compared.
    """
    watermarks = [_watermark(conn, name) for name in USAGE_AGGREGATES]
    if any(w is None for w in watermarks):
        return None, []
    cutoff = _month_start(min(watermarks))
    totals = [_monthly_totals(conn, name, cutoff) for name in USAGE_AGGREGATES]
    zero = (0,) * len(_USAGE_TOTALS)
    differences = []
    for month in sorted(set().union(*totals)):
        per_view = [t.get(month, zero) for t in totals]
        for i, column in enumerate(_USAGE_TOTALS):
            values = [v[i] for v in per_view]
            if len(set(values)) > 1:
                differences.append((month, column, values))
    return cutoff, differences


@click.command("check-aggregate")
@with_appcontext
def check_aggregate_cmd():
    """Report whether the usage aggregates cover request_logs and agree. Read-only.

    ``request_logs`` is never pruned (#167). Exits 1 if a retention policy exists on
    it, if the entity aggregate does not reach back to its oldest row, if any month of
    any usage aggregate is missing or only partly covered (the rule ``flask upgrade``
    backfills by), or if the monthly requests, tokens or cost of the usage
    aggregates differ in any month below the lowest of their watermarks. The /usage
    charts, which read only the aggregates, then miss or misstate that history until
    ``flask upgrade`` or ``flask backfill-aggregate`` is run.
    """
    if db.engine.dialect.name != "postgresql":
        click.echo(f"check-aggregate requires PostgreSQL/TimescaleDB; nothing to do on "
                   f"{db.engine.dialect.name}.")
        return

    # Autocommit: ``_missing_months`` reads the bucket width, and a failed catalog read
    # would abort an open transaction under every read after it.
    with _autocommit_connection() as conn:
        failed = False
        retention = _retention_job(conn)
        if retention is not None:
            click.echo(f"Error: request_logs has a retention policy (job {retention[0]}, "
                       f"config {retention[1]}).\n"
                       f"  request_logs must never be pruned. Remove it with\n"
                       f"  SELECT remove_retention_policy('request_logs');")
            failed = True

        existing = _continuous_aggregates(conn)
        absent = [name for name in USAGE_AGGREGATES if name not in existing]
        for name in absent:
            click.echo(f"Error: '{name}' does not exist. Run 'flask db upgrade' first.")
        if absent:
            raise SystemExit(1)
        # Read the materialisation hypertable, never the view: with
        # materialized_only = false the view answers from raw above the watermark, so a
        # never-refreshed aggregate would look fully covered through it. The question is
        # whether the per-entity charts, which read only the aggregate, see all of
        # history: is its earliest materialised bucket at or before the oldest raw row?
        earliest = conn.execute(text(
            f'SELECT MIN("{_bucket_column(conn, ENTITY_AGGREGATE)}") '
            f'FROM {_materialization_hypertable(conn, ENTITY_AGGREGATE)}'
        )).scalar()
        raw_oldest = conn.execute(text("SELECT MIN(time) FROM request_logs")).scalar()
        click.echo(f"earliest materialised bucket: {_fmt(earliest)}")
        click.echo(f"oldest request_logs row:      {_fmt(raw_oldest)}")

        covered = True
        if raw_oldest is None:
            click.echo("request_logs is empty; nothing to cover.")
        elif earliest is None or earliest > raw_oldest:
            state = "is empty" if earliest is None else "does not cover request_logs"
            click.echo(f"Error: '{ENTITY_AGGREGATE}' {state}. Run "
                       f"'flask backfill-aggregate --from {raw_oldest.astimezone(timezone.utc):%Y-%m}'.")
            covered = False

        # The same per-month rule 'flask upgrade' backfills by, so a month the earliest
        # bucket alone cannot see (an interior month, or one only the refresh policy's
        # trailing window has partly materialised) is reported too.
        now = datetime.now(timezone.utc)
        for name in USAGE_AGGREGATES:
            missing = _missing_months(conn, name, now)
            if not missing:
                if name == ENTITY_AGGREGATE and raw_oldest is not None and covered:
                    click.echo(f"{ENTITY_AGGREGATE} covers request_logs.")
                continue
            click.echo(f"Error: these months of '{name}' are missing or partly covered:")
            for start, first_raw, first_bucket in missing:
                state = "missing" if first_bucket is None else "partly covered"
                click.echo(f"  {start:%Y-%m}: {state} (first request_logs row {_fmt(first_raw)}, "
                           f"first materialised bucket {_fmt(first_bucket)})")
            name_arg = "" if name == ENTITY_AGGREGATE else f"--name {name} "
            click.echo(f"  Run 'flask upgrade', or 'flask backfill-aggregate {name_arg}--from "
                       f"{missing[0][0]:%Y-%m}'.")
            covered = False
        failed = failed or not covered

        # Every month below the lowest watermark is fully materialised in every usage
        # aggregate, so their totals must be equal there.
        cutoff, differences = _cross_view_differences(conn)
        if cutoff is None:
            never = [name for name in USAGE_AGGREGATES if _watermark(conn, name) is None]
            click.echo(f"{', '.join(repr(n) for n in never)} "
                       f"{'has' if len(never) == 1 else 'have'} never been refreshed; "
                       f"no month to compare across the usage aggregates.")
        elif differences:
            click.echo(f"Error: the usage aggregates disagree below {cutoff:%Y-%m} "
                       f"({' / '.join(USAGE_AGGREGATES)}):")
            for month, column, values in differences:
                click.echo(f"  {month:%Y-%m}: {column} {' / '.join(str(v) for v in values)}")
            click.echo(f"  Refresh those months bottom-up: 'flask backfill-aggregate --name <view> "
                       f"--from {differences[0][0]:%Y-%m}' for {', '.join(USAGE_AGGREGATES)}, "
                       f"in that order.")
            failed = True
        else:
            click.echo(f"Usage aggregates agree on every month before {cutoff:%Y-%m}.")

    if failed:
        raise SystemExit(1)


# --------------------------------------------------------------------------- #
# Migration gate: the serve step waits for the upgrade step. Never migrates.   #
# --------------------------------------------------------------------------- #

WAIT_RETRY_SECONDS = 2
WAIT_HINT_SECONDS = 30
# Bounds one PostgreSQL connection attempt (libpq ``connect_timeout``, whole seconds,
# minimum 2) and the read of alembic_version, so a database host that accepts TCP but
# never answers cannot hold the gate past --timeout or silence the upgrade hint.
WAIT_CONNECT_TIMEOUT_SECONDS = 10
UPGRADE_HINT = ("Run the upgrade step first: the image with the 'upgrade' argument, the "
                "'lumen-upgrade' Compose service, or the Helm migration Job.")


def _reject_nan(ctx, param, value):
    # Every comparison with NaN is False, so a NaN timeout would silently never fire.
    if value is not None and math.isnan(value):
        raise click.BadParameter("must be a number of seconds, not NaN.")
    return value


@click.command("wait-for-migrations")
@click.option("--timeout", type=click.FloatRange(min=0), default=None, metavar="SECONDS",
              callback=_reject_nan,
              help="Stop retrying once this many seconds have passed (exit 1); the check in "
                   "progress can add up to 10 seconds. Default: wait forever.")
@with_appcontext
def wait_for_migrations_cmd(timeout):
    """Wait until the database is at this image's migration head. Read-only.

    Exits 0 at head. While the database is behind, empty or unreachable it retries
    every 2 seconds, until --timeout runs out (exit 1) or forever without one. A
    revision this image does not know means a newer image migrated the database:
    that exits 1 at once, since waiting cannot fix it. Each PostgreSQL check gives an
    unresponsive server up to 10 seconds, so the command can exit that much after
    --timeout. Connections made while the app loads are not covered: see
    PGCONNECT_TIMEOUT in docs/admin/commands.md.
    """
    # Flask-Migrate's config and engine, so the script location and the database URL
    # resolve exactly as they do for ``flask db upgrade``.
    migrate = current_app.extensions["migrate"]
    script = ScriptDirectory.from_config(migrate.migrate.get_config())
    expected = tuple(sorted(script.get_heads()))
    known = {rev.revision for rev in script.walk_revisions()}
    engine = migrate.db.engine
    if engine.url.get_backend_name() == "postgresql":
        # Same URL, but every check opens a fresh connection that cannot block forever.
        engine = create_engine(engine.url, poolclass=NullPool, connect_args=_gate_connect_args(engine.url))
    try:
        _wait_for_heads(engine, expected, known, timeout)
    finally:
        if engine is not migrate.db.engine:
            engine.dispose()


def _gate_connect_args(url):
    """libpq arguments that bound one check without changing what it connects to.

    ``connect_args`` replaces a key of the same name from the URL's query string, and
    an explicit ``options`` also replaces ``PGOPTIONS``. So the statement timeout is
    appended to whatever options the URL or the environment already set: dropping a
    ``-c search_path=...`` there would read another schema's alembic_version than the
    one ``flask db upgrade`` writes.

    It goes last on purpose: libpq applies the last value of a repeated ``-c``, so an
    operator's own ``statement_timeout`` is overridden, but only for this one
    read-only query.
    """
    options = url.query.get("options", os.environ.get("PGOPTIONS", ""))
    if isinstance(options, tuple):
        options = " ".join(options)
    return {
        "connect_timeout": WAIT_CONNECT_TIMEOUT_SECONDS,
        "options": f"{options} -c statement_timeout={WAIT_CONNECT_TIMEOUT_SECONDS * 1000}".strip(),
    }


def _wait_for_heads(engine, expected, known, timeout):
    """The retry loop of ``wait-for-migrations``: returns at head, exits 1 otherwise."""
    start = time.monotonic()
    last_hint = start
    last_state = None
    while True:
        try:
            with engine.connect() as conn:
                current = tuple(sorted(MigrationContext.configure(conn).get_current_heads()))
        except DBAPIError as e:
            err = e.orig if e.orig is not None else e
            state = f"database unreachable ({type(err).__name__}: {' '.join(str(err).split())})"
        else:
            unknown = [rev for rev in current if rev not in known]
            if unknown:
                click.echo(f"Error: database is at revision {', '.join(unknown)}, which this image "
                           f"does not know (expected {', '.join(expected)}). A newer image has "
                           f"migrated it; run that image or a newer one.")
                raise SystemExit(1)
            if current == expected:
                click.echo(f"Database is at migration head {', '.join(expected)}.")
                return
            state = (f"database is at revision {', '.join(current) or 'none'}, "
                     f"expected {', '.join(expected)}")

        now = time.monotonic()
        if timeout is not None and now - start >= timeout:
            click.echo(f"Error: timed out after {timeout:g}s waiting for migrations: {state}. "
                       f"{UPGRADE_HINT}")
            raise SystemExit(1)
        if state != last_state:
            click.echo(f"Waiting for migrations: {state}.")
            last_state = state
        if now - last_hint >= WAIT_HINT_SECONDS:
            click.echo(f"Still waiting: {state}. {UPGRADE_HINT}")
            last_hint = now
        time.sleep(WAIT_RETRY_SECONDS)


# --------------------------------------------------------------------------- #
# Upgrade step: every migration, then the post-migration data steps. One-shot. #
# --------------------------------------------------------------------------- #

# Shared with the startup tripwire (``_warn_if_request_logs_has_retention``), so an
# operator searching the logs for it finds both.
REQUEST_LOGS_RETENTION_ERROR = (
    "request_logs has a retention policy (job %s, config %s), but request_logs "
    "must never be pruned. Remove it with "
    "SELECT remove_retention_policy('request_logs'); then run 'flask check-aggregate'."
)


def _backfill_missing_months():
    """Refresh every month of the usage aggregates that ``_missing_months`` reports.

    Covers ``USAGE_AGGREGATES`` in refresh order, so the entity aggregate is filled
    before the org-wide views computed from its materialised rows; an aggregate missing
    from the database is skipped. Oldest month first, each over its whole month up to
    now. Safe to re-run: it probes again and resumes at the first month still missing,
    and re-refreshing a month recomputes it from its source. Exits 1 when a month still
    fails after the lock retries.
    """
    if db.engine.dialect.name != "postgresql":
        click.echo(f"Aggregate backfill: skipped, needs PostgreSQL/TimescaleDB "
                   f"(database is {db.engine.dialect.name}).")
        return
    with _autocommit_connection() as conn:
        if not conn.execute(text(
            "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb')"
        )).scalar():
            click.echo("Aggregate backfill: skipped, TimescaleDB is not installed.")
            return
        # Defensive: Lumen never adds one. Refreshing a window whose raw chunks were
        # dropped would erase its aggregate rows, so leave the aggregate alone, and do
        # not fail the upgrade over a policy only an operator can remove.
        retention = _retention_job(conn)
        if retention is not None:
            current_app.logger.error(REQUEST_LOGS_RETENTION_ERROR, retention[0], retention[1])
            click.echo("Aggregate backfill: skipped, request_logs has a retention policy.")
            return

        now = datetime.now(timezone.utc)
        existing = set(_continuous_aggregates(conn))
        # USAGE_AGGREGATES is already child before parent; walk it, not the catalog.
        for name in (n for n in USAGE_AGGREGATES if n in existing):
            missing = _missing_months(conn, name, now)
            if not missing:
                click.echo(f"Aggregate backfill: no missing months in '{name}'.")
                continue
            click.echo(f"Aggregate backfill: refreshing {len(missing)} missing or partly "
                       f"covered months of '{name}'.")
            bucket = _bucket_column(conn, name)
            for start, _, _ in missing:
                try:
                    _refresh_month(conn, name, bucket, start, now)
                except DBAPIError as exc:
                    click.echo(f"Error: refreshing {start:%Y-%m} of '{name}' failed: {exc.orig}")
                    click.echo("  Re-run 'flask upgrade' to resume at the first month still "
                               "missing.")
                    raise SystemExit(1)


_EXTENSION_OWNER_SQL = text(
    "SELECT r.rolname, current_database() FROM pg_extension e "
    "JOIN pg_roles r ON r.oid = e.extowner WHERE e.extname = 'timescaledb'"
)


def _extension_owner_and_database(session):
    """The role that owns ``timescaledb`` and the current database, from the catalog."""
    return tuple(session.execute(_EXTENSION_OWNER_SQL).one())


def _warn_if_timescaledb_behind():
    """Print the exact ``ALTER EXTENSION timescaledb UPDATE`` command when the extension is behind.

    Advisory only: it never runs the command (the update is one-way and stays a
    deliberate operator step) and never fails the upgrade. Prints nothing on SQLite,
    without the extension, or when the installed version is the server's. A
    database error is rolled back and reported as one note. The ``-U``/``-d``
    values are the extension's owner and the database read from the catalog,
    since the connecting user need not own the extension. Both are shell-quoted, so a
    name with spaces or shell metacharacters stays one literal argument when pasted.
    """
    if db.engine.dialect.name != "postgresql":
        return
    try:
        installed, available = extension_versions(db.session)
        if not (installed and available and installed != available):
            return
        owner, database = _extension_owner_and_database(db.session)
    except DBAPIError as exc:
        click.echo(f"Note: could not check the TimescaleDB extension version: {exc.orig}")
        return
    finally:
        # Read-only: also ends a failed transaction, and leaves none idle.
        db.session.rollback()
    psql = f"psql -X -U {shlex.quote(owner)} -d {shlex.quote(database)} -c 'ALTER EXTENSION timescaledb UPDATE;'"
    click.echo(f"WARNING: TimescaleDB extension is {installed} but the server has {available}. "
               "Back up the database, then run as the first command of a fresh session:")
    click.echo(f"  {psql}")
    click.echo(f"  Compose: docker compose exec postgres {psql}")
    click.echo(f"  Helm (bundled PostgreSQL): kubectl exec -it <fullname>-postgresql-0 -- {psql}")
    click.echo('See "Upgrading TimescaleDB" in docs/admin/commands.md.')


# Run in order after the migrations, each only once the previous one succeeded. Every
# step must be safe to re-run and finish what an earlier, failed run left undone.
POST_MIGRATION_STEPS = (_backfill_missing_months,)


@click.command("upgrade")
@with_appcontext
def upgrade_cmd():
    """Bring the database up to date for this image, then exit.

    Applies every migration (``flask db upgrade`` to head), warns with the exact
    ``ALTER EXTENSION timescaledb UPDATE`` command when the TimescaleDB extension is
    older than the server's (advisory: it never runs it and never fails), then runs
    the post-migration data steps: today, the backfill of the usage aggregates'
    missing months. Stops at the first failure with exit 1; running it again
    resumes. This is the one-shot upgrade step that runs before the app starts.
    """
    click.echo("Applying migrations ...")
    migrate_upgrade()
    _warn_if_timescaledb_behind()
    for step in POST_MIGRATION_STEPS:
        step()
    click.echo("Upgrade complete.")
