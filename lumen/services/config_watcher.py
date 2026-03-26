import logging
import os
import threading
import time

import yaml

from lumen.commands import sync_models_from_yaml, sync_groups_from_yaml

logger = logging.getLogger(__name__)

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

                app_cfg = new_data.get("app", {})
                app.config["APP_NAME"] = app_cfg.get("name", "Lumen")
                app.config["APP_TAGLINE"] = app_cfg.get("tagline", "")
                app.config["DEV_USER"] = app_cfg.get("dev_user", "")
                app.config["GITHUB_URL"] = app_cfg.get("github_url", "https://github.com/ncsa/lumen")

                logs_cfg = app_cfg.get("logs", {})
                werkzeug_level = logging.WARNING if not logs_cfg.get("access", True) else logging.INFO
                logging.getLogger("werkzeug").setLevel(werkzeug_level)
                logging.getLogger("uvicorn.access").setLevel(werkzeug_level)
                app.config["LOG_MODEL_HEALTH"] = logs_cfg.get("model", False)

                oauth2_cfg = new_data.get("oauth2", {})
                app.config["OAUTH2_PARAMS"] = oauth2_cfg.get("params") or {}

                chat_cfg = new_data.get("chat", {})
                app.config["CHAT_CONVERSATION_REMOVE_MODE"] = chat_cfg.get("remove", "hide")
                try:
                    sync_models_from_yaml(new_data)
                except Exception as e:
                    logger.warning("config_watcher: sync_models_from_yaml failed: %s", e)
                try:
                    sync_groups_from_yaml(new_data)
                except Exception as e:
                    logger.warning("config_watcher: sync_groups_from_yaml failed: %s", e)

            app.logger.info("config.yaml reloaded")
        except Exception as e:
            logger.warning("config_watcher error: %s", e)


def start_config_watcher(app, config_path):
    t = threading.Thread(target=_watcher, args=(app, config_path), daemon=True)
    t.start()
