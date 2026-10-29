"""Regression guards for the help-doc screenshot inputs (scripts/).

The committed docs/img/ shots must match what the guides describe: chat.png
shows an open conversation with the per-reply and copy-all buttons, and
model-detail.png shows a model with "Also known as" alias badges. These tests
keep the capture script and screenshot.config.yaml from silently drifting away
from that.
"""
from pathlib import Path

import yaml as _yaml

from lumen.commands import model_alias_config_errors

REPO_ROOT = Path(__file__).resolve().parents[2]
SCREENSHOT_CONFIG = REPO_ROOT / "scripts" / "screenshot.config.yaml"
SCREENSHOTS_PY = REPO_ROOT / "scripts" / "screenshots.py"

# The MODEL value the polished-capture instructions in scripts/README.md set
# for the model-detail shot.
README_CAPTURE_MODEL = "qwen2.5-7b-instruct"


def _models():
    data = _yaml.safe_load(SCREENSHOT_CONFIG.read_text())
    return data["models"]


def test_screenshot_config_aliases_are_valid():
    assert model_alias_config_errors(_models()) == []


def _first_active_model_name():
    """The model screenshots.py captures when MODEL is unset (first by name)."""
    return sorted(m["name"] for m in _models())[0]


def test_capture_models_declare_aliases():
    named = {m["name"]: m for m in _models()}
    for name in (_first_active_model_name(), README_CAPTURE_MODEL):
        assert named[name].get("aliases"), (
            f"screenshot model '{name}' declares no aliases, so a recaptured "
            "model-detail.png would not show the alias badges"
        )


def test_chat_capture_shows_both_copy_buttons():
    src = SCREENSHOTS_PY.read_text()
    # The copy-all button is disabled and no reply has a copy button until a
    # conversation is open, so the capture must open one and wait for a reply.
    assert 'page.click(".conv-item")' in src
    assert 'wait_for_selector(".chat-bubble-assistant")' in src
    assert "#send-btn" not in src
