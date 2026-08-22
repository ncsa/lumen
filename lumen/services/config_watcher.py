import logging
import os
import threading
import time

import bleach
import yaml
from markupsafe import Markup

from lumen.commands import sync_models_from_yaml

_ANNOUNCEMENT_ALLOWED_TAGS = {"a", "b", "br", "em", "i", "li", "ol", "p", "strong", "ul"}
_ANNOUNCEMENT_ALLOWED_ATTRS = {"a": ["href", "title", "target"]}

logger = logging.getLogger(__name__)

# Lumen 2.0 reads config version 3 only. Older configs must be migrated by hand:
# the users:, projects:, clients:, and groups: sections were removed (groups and
# memberships live in the database), model access is ownership-based (no access/
# model_access keys), and OAuth auto-assignment moved to a top-level group_rules:.
REQUIRED_CONFIG_VERSION = 3
CONFIG_VERSION_ERROR = (
    "config.yaml must declare 'version: 3'. Lumen 2.0 removed the users:, projects:, "
    "clients:, and groups: sections (groups and memberships are managed in the "
    "database), removed the access/model_access keys (model access is ownership-"
    "based, managed on the model detail page), and moved OAuth auto-assignment "
    "rules to a top-level 'group_rules:' section."
)


def config_version_ok(yaml_data: dict) -> bool:
    """True if the config declares the required version."""
    return int(yaml_data.get("version", 1) or 1) >= REQUIRED_CONFIG_VERSION

# Shown when an early-access model is acknowledged; overridable via defaults.models.early_access_message.
DEFAULT_EARLY_ACCESS_MESSAGE = (
    "**Early access:** This model is offered as an early-access preview. "
    "It may change, produce inconsistent results, or be removed at any time without notice."
)

# Every key the 'app' section may contain, read either here, in _apply_theme, or
# in create_app. An unrecognised key is almost always a stale name left behind by
# a schema change, and it fails silently because every read is a .get() with a
# default: `app.database_url` survived the move to `app.database.url` for six
# weeks, ignored, which left the entire test suite running against the developer's
# dev database and dropping its tables on every run. Warn rather than fail, so a
# config written for a newer version still boots.
# Keep in step with the keys chart/templates/config-secret.yaml emits under
# 'app:' — tests/unit/test_config_watcher.py parses that template and fails if a
# key it ships is missing here, which is how config_editor and email_themes went
# unlisted: every Helm deploy warned that a key it had just written was ignored,
# and an operator who deleted config_editor to silence the warning would have
# re-enabled the read-write admin config editor (it defaults to true).
KNOWN_APP_KEYS = frozenset({
    "announcement", "config_editor", "database", "debug", "dev_user",
    "email_themes", "encryption_key", "github_url", "graylist_default_notice",
    "logs", "name", "secret_key", "tagline", "theme",
})


def _positive_seconds(value, default: float, key: str) -> float:
    """A positive float from config, or ``default`` with a warning.

    Zero or negative is not a lenient setting: httpx reads it as an *immediate*
    expiry, so `read_timeout: 0` fails every upstream call instantly rather than
    disabling the bound. The Helm schema rejects those values, but editing
    config.yaml directly is the documented hot-reload path and had no such guard.
    Mirrors ``_send_timeout`` in ``wsgi_disconnect``.
    """
    if value is None:
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = None
    if parsed is None or parsed <= 0:
        logger.warning(
            "config.yaml: llm.%s must be a positive number, got %r — using %s",
            key, value, default,
        )
        return default
    return parsed


def _non_negative_int(value, default: int, key: str) -> int:
    """A non-negative int from config, or ``default`` with a warning.

    Zero is meaningful here (never retry), so it must survive — which is why this
    cannot use the ``value or default`` idiom used elsewhere in this module.
    """
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = -1
    if parsed < 0:
        logger.warning(
            "config.yaml: llm.%s must be zero or greater, got %r — using %s",
            key, value, default,
        )
        return default
    return parsed


def apply_hot_config(app, yaml_data: dict):
    """Apply hot-reloadable yaml settings to app.config. Called at startup and on config reload."""
    app_cfg = yaml_data.get("app", {})
    if unknown := sorted(str(k) for k in (set(app_cfg) - KNOWN_APP_KEYS)):
        logger.warning(
            "config.yaml: unrecognised key(s) under 'app': %s. They are ignored — check "
            "for a setting renamed by a schema change (app.database_url, for example, "
            "became app.database.url).",
            ", ".join(unknown),
        )
    app.config["APP_NAME"] = app_cfg.get("name", "Lumen")
    app.config["APP_TAGLINE"] = app_cfg.get("tagline", "")
    raw_announcement = app_cfg.get("announcement", "") or ""
    app.config["APP_ANNOUNCEMENT"] = Markup(
        bleach.clean(raw_announcement, tags=_ANNOUNCEMENT_ALLOWED_TAGS, attributes=_ANNOUNCEMENT_ALLOWED_ATTRS, strip=True)
    )
    _dev_raw = app_cfg.get("dev_user", "")
    if isinstance(_dev_raw, dict):
        app.config["DEV_USER"] = _dev_raw.get("email", "")
        app.config["DEV_USER_GROUPS"] = _dev_raw.get("groups") or []
    else:
        app.config["DEV_USER"] = _dev_raw or ""
        app.config["DEV_USER_GROUPS"] = []
    if app.config["DEV_USER"]:
        logger.warning(
            "DEV LOGIN ENABLED — /devlogin bypasses OAuth and logs in as '%s'. "
            "Never set app.dev_user in production. (Active only while app.debug is true.)",
            app.config["DEV_USER"],
        )
    app.config["GITHUB_URL"] = app_cfg.get("github_url", "https://github.com/ncsa/lumen")

    logs_cfg = app_cfg.get("logs", {})
    werkzeug_level = logging.WARNING if not logs_cfg.get("access", True) else logging.INFO
    logging.getLogger("werkzeug").setLevel(werkzeug_level)
    logging.getLogger("uvicorn.access").setLevel(werkzeug_level)
    # The SQLAlchemy engine/pool logs a DEBUG line per connection checkout/checkin
    # (e.g. "Connection ... being returned to pool", "rollback-on-return"), which
    # is noise at normal levels. Only keep it when app.database.logging is debug.
    # The pool warnings travel under SQLAlchemy's legacy names AND — because the
    # app's pool subclass lives here — under `lumen.services.pool_tracker.TimingQueuePool`
    # (SQLAlchemy names instance loggers from the concrete class' module).
    db_logging = (app_cfg.get("database") or {}).get("logging", "info")
    db_level = logging.DEBUG if str(db_logging).lower() == "debug" else logging.INFO
    for _name in ("sqlalchemy.engine", "sqlalchemy.pool"):
        logging.getLogger(_name).setLevel(db_level)
    try:
        from lumen.services.pool_tracker import TimingQueuePool

        logging.getLogger(f"{TimingQueuePool.__module__}.{TimingQueuePool.__name__}").setLevel(db_level)
    except Exception:
        pass
    app.config["LOG_MODEL_HEALTH"] = logs_cfg.get("model", False)

    oauth2_cfg = yaml_data.get("oauth2", {})
    app.config["OAUTH2_PARAMS"] = oauth2_cfg.get("params") or {}
    app.config["OAUTH2_ALLOW_UNVERIFIED_EMAIL"] = bool(oauth2_cfg.get("allow_unverified_email", False))
    app.config["EMAIL_THEMES"] = app_cfg.get("email_themes") or {}
    api_cfg = yaml_data.get("api", {})
    app.config["API_REQUIRE_MODEL_CONSENT"] = api_cfg.get("consent", True)

    # Bounds on upstream LLM calls. read_timeout is the maximum gap *between
    # chunks* of a streaming response, not the total call duration — a
    # twenty-minute generation is fine as long as tokens keep arriving, which is
    # what keeps the default safe. max_retries applies to non-streaming
    # calls; a streaming call must never be auto-retried, because the retry
    # restarts the whole generation while the first may still be draining.
    # A key present but blank ("read_timeout:") parses as None; treat it as absent.
    llm_cfg = yaml_data.get("llm") or {}
    app.config["LLM_CONNECT_TIMEOUT"] = _positive_seconds(llm_cfg.get("connect_timeout"), 5.0, "connect_timeout")
    app.config["LLM_READ_TIMEOUT"] = _positive_seconds(llm_cfg.get("read_timeout"), 300.0, "read_timeout")
    # Non-streaming calls need their own, much larger bound: read_timeout is a
    # between-chunks gap for a stream, but for a single-response call the same
    # setting caps the entire generation, and a long completion or a large audio
    # transcription legitimately takes minutes.
    app.config["LLM_REQUEST_TIMEOUT"] = _positive_seconds(llm_cfg.get("request_timeout"), 600.0, "request_timeout")
    app.config["LLM_MAX_RETRIES"] = _non_negative_int(llm_cfg.get("max_retries"), 1, "max_retries")

    # The in-app config editor is on by default; Helm sets it false for git-managed configs.
    app.config["CONFIG_EDITOR"] = bool(app_cfg.get("config_editor", True))

    # Global defaults for models and token (coin) pools, overridable per scope.
    defaults_cfg = yaml_data.get("defaults") or {}
    models_defaults = defaults_cfg.get("models") or {}
    # Legacy app.graylist_default_notice feeds the global ack_message when not set under defaults.models.
    ack_message = models_defaults.get("ack_message") or app_cfg.get("graylist_default_notice") or None
    app.config["MODEL_DEFAULTS"] = {
        "ack_message": ack_message,
        "early_access_message": models_defaults.get("early_access_message") or DEFAULT_EARLY_ACCESS_MESSAGE,
    }
    tokens_defaults = defaults_cfg.get("tokens") or {}
    _td_max = tokens_defaults.get("max", 0)
    app.config["TOKEN_DEFAULTS"] = {
        "max": _td_max,
        "refresh": tokens_defaults.get("refresh", 0),
        "starting": tokens_defaults.get("starting", _td_max),
    }

def _apply_theme(app, yaml_data: dict):
    """Switch the active theme from yaml_data. No-op if unchanged or theme dir not found."""
    app_cfg = yaml_data.get("app", {})
    theme_name = app_cfg.get("theme", "default")
    themes_root = app.config.get("THEMES_ROOT", "")
    theme_dir = os.path.join(themes_root, theme_name)
    if not os.path.isdir(theme_dir):
        logger.warning("config_watcher: theme '%s' not found, keeping current theme", theme_name)
        return
    if app.config.get("THEME_NAME") == theme_name:
        return
    app.config["THEME_NAME"] = theme_name
    with open(os.path.join(theme_dir, "theme.yaml")) as _f:
        app.config["THEME"] = yaml.safe_load(_f)
    if app.jinja_env.cache is not None:
        app.jinja_env.cache.clear()
    logger.info("config_watcher: theme switched to '%s'", theme_name)


# Each entry is a dotted path into the config. A single-element path means any
# change to the whole section triggers a restart.
# This list is also consumed by the admin config editor UI.
RESTART_REQUIRED = [
    ("app", "secret_key"),
    # Read once in create_app into ENCRYPTION_KEY; apply_hot_config never
    # re-reads it. Documented as restart-required (and dangerous to rotate)
    # since it was introduced, but absent from this list, so the editor let it
    # be changed with no warning at all.
    ("app", "encryption_key"),
    ("app", "database"),
    ("app", "debug"),
    # Its siblings logs.access and logs.model are applied by apply_hot_config,
    # but the level is set on app.logger in create_app only.
    ("app", "logs", "level"),
    ("oauth2",),
    ("api", "prometheus", "enabled"),
    ("api", "prometheus", "multiproc_dir"),
    # Read once at startup: the limiter's storage is configured on the extension,
    # and get_live_state() resolves its backend from the same URL on first use.
    # Documented as restart-required since it was introduced, but absent from
    # this list, so the config editor offered a hot reload that silently did
    # nothing -- the process kept the old storage while the UI said it had changed.
    ("rate_limiting", "storage_url"),
]
_RESTART_REQUIRED = RESTART_REQUIRED


# Sentinel substituted for secret values before the config is sent to the admin
# browser.  On save, any field still equal to MASK is replaced with the on-disk
# value (see restore_config_secrets) so the real secret is preserved.
# NOTE: assumes no real secret's literal value equals MASK; astronomically
# unlikely for secret_key/tokens, but documented here for the next reader.
MASK = "********"

# Secret-bearing dotted paths masked before the config is sent to the admin
# browser.  Only truthy values are masked — a blank stays blank so the UI can
# distinguish "no secret configured" from "secret hidden".
# *** If you add a new secret to config.yaml, add its dotted path here too. ***
# (models[].endpoints[].api_key is handled separately — see mask_config_secrets.)
SENSITIVE_KEYS = [
    ("app", "secret_key"),
    ("app", "encryption_key"),
    ("app", "database", "url"),
    ("oauth2", "client_secret"),
    ("api", "prometheus", "token"),
    ("api", "monitoring", "token"),
    ("rate_limiting", "storage_url"),
]


def _resolve_path(data, path):
    cur = data
    for part in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _set_path(data, path, value):
    """Walk ``path`` into nested dicts and set the leaf to ``value``.

    No-op if any intermediate key is missing or not a dict — never synthesizes
    intermediate keys, so masking/restoring an absent path leaves structure
    unchanged.  Reuses :func:`_resolve_path` for the parent walk.
    """
    parent = _resolve_path(data, path[:-1])
    if isinstance(parent, dict) and path[-1] in parent:
        parent[path[-1]] = value


def _iter_endpoints(data):
    """Yield ``(model_name, endpoint_dict)`` for every endpoint in every model.

    Shape-guarded: skips non-dict models/endpoints so a hand-edited or partial
    config won't raise.  Used by :func:`mask_config_secrets` and
    :func:`_find_unrestorable_masks`, which walk every endpoint uniformly.
    """
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, list):
        return
    for model in models:
        if not isinstance(model, dict):
            continue
        endpoints = model.get("endpoints")
        if not isinstance(endpoints, list):
            continue
        for ep in endpoints:
            if isinstance(ep, dict):
                yield model.get("name", "?"), ep


def mask_config_secrets(data):
    """Replace every sensitive value in ``data`` with ``MASK`` (in place).

    Walks the dotted paths in :data:`SENSITIVE_KEYS` and the nested
    ``models[].endpoints[].api_key`` list-of-lists via :func:`_iter_endpoints`.
    Only truthy values are masked; blanks are left blank.  Shape-guarded so a
    hand-edited or partial config won't raise mid-mask.
    """
    for path in SENSITIVE_KEYS:
        if _resolve_path(data, path):
            _set_path(data, path, MASK)

    for _name, ep in _iter_endpoints(data):
        if ep.get("api_key"):
            ep["api_key"] = MASK


def restore_config_secrets(data, on_disk):
    """Substitute the on-disk value for any field in ``data`` still == MASK.

    For dotted paths, the on-disk value at the same path is used.  For
    ``models[].endpoints[].api_key``, models are matched by ``name`` and
    endpoints **by URL** within the model.  This correctly handles remove,
    reorder, and insert of unique-URL endpoints (each URL identifies its key).
    For the documented round-robin pattern (multiple endpoints sharing one
    URL), keys are restored positionally within the URL group — but only when
    the group's endpoint count is unchanged; an added/removed duplicate-URL
    endpoint is ambiguous and left as MASK so the caller's safety check
    rejects the save.  If a model name appears more than once on disk the
    match is also ambiguous, so endpoints for that name are left as MASK.
    Mutates ``data`` in place.
    """
    for path in SENSITIVE_KEYS:
        if _resolve_path(data, path) == MASK:
            disk_value = _resolve_path(on_disk, path)
            if disk_value:
                _set_path(data, path, disk_value)

    incoming_models = data.get("models") if isinstance(data, dict) else None
    disk_models = on_disk.get("models") if isinstance(on_disk, dict) else None
    if not isinstance(incoming_models, list) or not isinstance(disk_models, list):
        return

    # On-disk endpoints collected by model name.  If a name appears more than
    # once on disk, all entries are kept so len(candidates) != 1 signals
    # ambiguity → skip → leave MASK → _find_unrestorable_masks rejects.
    disk_endpoints_by_name: dict[str, list[list]] = {}
    for dm in disk_models:
        if not isinstance(dm, dict):
            continue
        name = dm.get("name")
        eps = dm.get("endpoints")
        if isinstance(name, str) and isinstance(eps, list):
            disk_endpoints_by_name.setdefault(name, []).append(eps)

    for im in incoming_models:
        if not isinstance(im, dict):
            continue
        name = im.get("name")
        ieps = im.get("endpoints")
        if not isinstance(name, str) or not isinstance(ieps, list):
            continue
        candidates = disk_endpoints_by_name.get(name)
        if not candidates or len(candidates) != 1:
            continue  # no match or duplicate names → leave MASK → 400
        deps = candidates[0]

        # Group disk endpoints by URL: url → [api_key, ...] (in order).
        disk_keys_by_url: dict[str, list] = {}
        for dep in deps:
            if isinstance(dep, dict) and isinstance(dep.get("url"), str):
                disk_keys_by_url.setdefault(dep["url"], []).append(dep.get("api_key"))

        # Group incoming endpoints by URL the same way.
        incoming_by_url: dict[str, list] = {}
        for iep in ieps:
            if isinstance(iep, dict) and isinstance(iep.get("url"), str):
                incoming_by_url.setdefault(iep["url"], []).append(iep)

        # Restore: match by URL.  For a unique URL (one disk, one incoming) this
        # is unambiguous regardless of position.  For duplicate URLs (round-
        # robin), restore positionally within the group — but only when the
        # count matches; a changed count (add/remove within the group) is
        # ambiguous → leave MASK → 400.
        for url, ieps_at_url in incoming_by_url.items():
            disk_keys = disk_keys_by_url.get(url)
            if not disk_keys or len(disk_keys) != len(ieps_at_url):
                continue  # no disk match or count mismatch → leave MASK → 400
            for i, iep in enumerate(ieps_at_url):
                if iep.get("api_key") == MASK and disk_keys[i]:
                    iep["api_key"] = disk_keys[i]


def _find_unrestorable_masks(data) -> list[str]:
    """Return dotted-path strings for secrets still == MASK after restore.

    Used to build a precise 400 message naming the field(s) the admin must
    re-enter.  Empty list means every masked secret was restored (or none were
    present).
    """
    still_masked: list[str] = []
    for path in SENSITIVE_KEYS:
        if _resolve_path(data, path) == MASK:
            still_masked.append(".".join(path))

    for name, ep in _iter_endpoints(data):
        if ep.get("api_key") == MASK:
            still_masked.append(f"models[{name}].endpoints[{ep.get('url', '?')}].api_key")
    return still_masked


def _check_restart_required(old_data, new_data):
    for path in RESTART_REQUIRED:
        if _resolve_path(old_data, path) != _resolve_path(new_data, path):
            logger.warning(
                "config.yaml changed: '%s' requires a restart to take effect",
                ".".join(path),
            )


def _watcher(app, config_path):
    last_mtime = None
    while True:
        time.sleep(5)
        try:
            mtime = os.path.getmtime(config_path)
            if last_mtime is None:
                last_mtime = mtime
                continue
            if mtime == last_mtime:
                continue
            last_mtime = mtime

            with open(config_path) as f:
                new_data = yaml.safe_load(f)

            if not config_version_ok(new_data or {}):
                logger.error("config_watcher: reload skipped — %s", CONFIG_VERSION_ERROR)
                continue

            with app.app_context():
                old_data = app.config.get("YAML_DATA", {})
                _check_restart_required(old_data, new_data)
                app.config["YAML_DATA"] = new_data
                apply_hot_config(app, new_data)
                _apply_theme(app, new_data)
                try:
                    sync_models_from_yaml(new_data)
                except Exception as e:
                    logger.warning("config_watcher: sync_models_from_yaml failed: %s", e)

            app.logger.info("config.yaml reloaded")
        except Exception:
            logger.exception("config_watcher error")


def start_config_watcher(app, config_path):
    t = threading.Thread(target=_watcher, args=(app, config_path), daemon=True)
    t.start()
