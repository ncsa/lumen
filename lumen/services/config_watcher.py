import logging
import os
import threading
import time

import bleach
import yaml
from markupsafe import Markup

_ANNOUNCEMENT_ALLOWED_TAGS = {"a", "b", "br", "em", "i", "li", "ol", "p", "strong", "ul"}
_ANNOUNCEMENT_ALLOWED_ATTRS = {"a": ["href", "title", "target"]}

from lumen.commands import sync_clients_from_yaml, sync_groups_from_yaml, sync_models_from_yaml, sync_user_groups_from_yaml, sync_user_limits_from_yaml

logger = logging.getLogger(__name__)

# Log the config-version deprecation warning at most once per process.
_version_warned = False


def apply_hot_config(app, yaml_data: dict):
    """Apply hot-reloadable yaml settings to app.config. Called at startup and on config reload."""
    global _version_warned
    if int(yaml_data.get("version", 1) or 1) < 2 and not _version_warned:
        logger.warning(
            "config.yaml is missing 'version: 2'; the legacy (v1) format is deprecated and will be "
            "migrated to v2 on the next save via the editor or Helm redeploy"
        )
        _version_warned = True

    app_cfg = yaml_data.get("app", {})
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
    app.config["LOG_MODEL_HEALTH"] = logs_cfg.get("model", False)

    oauth2_cfg = yaml_data.get("oauth2", {})
    app.config["OAUTH2_PARAMS"] = oauth2_cfg.get("params") or {}
    app.config["OAUTH2_ALLOW_UNVERIFIED_EMAIL"] = bool(oauth2_cfg.get("allow_unverified_email", False))
    app.config["EMAIL_THEMES"] = app_cfg.get("email_themes") or {}
    api_cfg = yaml_data.get("api", {})
    app.config["API_REQUIRE_MODEL_CONSENT"] = api_cfg.get("consent", True)

    # The in-app config editor is on by default; Helm sets it false for git-managed configs.
    app.config["CONFIG_EDITOR"] = bool(app_cfg.get("config_editor", True))

    # Global defaults for models and token (coin) pools, overridable per scope.
    defaults_cfg = yaml_data.get("defaults") or {}
    models_defaults = defaults_cfg.get("models") or {}
    # Legacy app.graylist_default_notice feeds the global ack_message when not set under defaults.models.
    ack_message = models_defaults.get("ack_message") or app_cfg.get("graylist_default_notice") or None
    app.config["MODEL_DEFAULTS"] = {
        "access": models_defaults.get("access", "blocked"),
        "ack_message": ack_message,
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
    ("app", "database"),
    ("app", "debug"),
    ("oauth2",),
    ("api", "prometheus", "enabled"),
    ("api", "prometheus", "multiproc_dir"),
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
                try:
                    sync_groups_from_yaml(new_data)
                except Exception as e:
                    logger.warning("config_watcher: sync_groups_from_yaml failed: %s", e)
                try:
                    sync_user_groups_from_yaml(new_data)
                except Exception as e:
                    logger.warning("config_watcher: sync_user_groups_from_yaml failed: %s", e)
                try:
                    sync_user_limits_from_yaml(new_data)
                except Exception as e:
                    logger.warning("config_watcher: sync_user_limits_from_yaml failed: %s", e)
                try:
                    sync_clients_from_yaml(new_data)
                except Exception as e:
                    logger.warning("config_watcher: sync_clients_from_yaml failed: %s", e)

            app.logger.info("config.yaml reloaded")
        except Exception:
            logger.exception("config_watcher error")


def start_config_watcher(app, config_path):
    t = threading.Thread(target=_watcher, args=(app, config_path), daemon=True)
    t.start()
