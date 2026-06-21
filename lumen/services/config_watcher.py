import logging
import os
import threading
import time

import bleach
import yaml
from markupsafe import Markup

_ANNOUNCEMENT_ALLOWED_TAGS = {"a", "b", "br", "em", "i", "li", "ol", "p", "strong", "ul"}
_ANNOUNCEMENT_ALLOWED_ATTRS = {"a": ["href", "title", "target"]}

from lumen.commands import sync_clients_from_yaml, sync_groups_from_yaml, sync_models_from_yaml

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


def _resolve_path(data, path):
    cur = data
    for part in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


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
                    sync_clients_from_yaml(new_data)
                except Exception as e:
                    logger.warning("config_watcher: sync_clients_from_yaml failed: %s", e)

            app.logger.info("config.yaml reloaded")
        except Exception:
            logger.exception("config_watcher error")


def start_config_watcher(app, config_path):
    t = threading.Thread(target=_watcher, args=(app, config_path), daemon=True)
    t.start()
