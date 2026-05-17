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


def apply_hot_config(app, yaml_data: dict):
    """Apply hot-reloadable yaml settings to app.config. Called at startup and on config reload."""
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
    app.config["GITHUB_URL"] = app_cfg.get("github_url", "https://github.com/ncsa/lumen")

    logs_cfg = app_cfg.get("logs", {})
    werkzeug_level = logging.WARNING if not logs_cfg.get("access", True) else logging.INFO
    logging.getLogger("werkzeug").setLevel(werkzeug_level)
    logging.getLogger("uvicorn.access").setLevel(werkzeug_level)
    app.config["LOG_MODEL_HEALTH"] = logs_cfg.get("model", False)

    app.config["OAUTH2_PARAMS"] = yaml_data.get("oauth2", {}).get("params") or {}
    app.config["CHAT_CONVERSATION_REMOVE_MODE"] = yaml_data.get("chat", {}).get("remove", "hide")

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


_RESTART_REQUIRED = [
    ("app", "secret_key"),
    ("app", "database_url"),
    ("app", "debug"),
    ("prometheus", "enabled"),
    ("prometheus", "multiproc_dir"),
]


def _check_restart_required(old_data, new_data):
    for section, key in _RESTART_REQUIRED:
        old_val = old_data.get(section, {}).get(key)
        new_val = new_data.get(section, {}).get(key)
        if old_val != new_val:
            logger.warning(
                "config.yaml changed: '%s.%s' requires a restart to take effect",
                section, key,
            )
    old_oauth2 = old_data.get("oauth2", {})
    new_oauth2 = new_data.get("oauth2", {})
    if old_oauth2 != new_oauth2:
        for key in set(list(old_oauth2) + list(new_oauth2)):
            if old_oauth2.get(key) != new_oauth2.get(key):
                logger.warning(
                    "config.yaml changed: 'oauth2.%s' requires a restart to take effect", key
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
