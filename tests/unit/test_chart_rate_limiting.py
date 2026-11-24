"""The Helm chart renders the per-endpoint `rate_limiting` keys into config.yaml.

`chat` is the preferred key; the deprecated `limit` still renders as `chat` and
setting both fails. `embedding` and `audio` render only when set (unset, the app
gives them their own bucket of the chat size). The schema rejects unknown keys
and non-string values.

Reuses `_render` from test_chart_migration, so this module skips without helm
outside CI and fails in CI.
"""
import subprocess

import pytest
import yaml

from tests.unit.test_chart_migration import _render


def _rate_limiting(extra=None):
    docs = _render(extra)
    secret = docs[("Secret", "rel-lumen-config")]
    return yaml.safe_load(secret["stringData"]["config.yaml"])["rate_limiting"]


def test_defaults_render_chat_only():
    assert _rate_limiting() == {"chat": "30 per minute"}


def test_defaults_with_redis_add_storage_url():
    assert _rate_limiting({"redis.url": "redis://cache:6379/0"}) == {
        "chat": "30 per minute",
        "storage_url": "redis://cache:6379/0",
    }


def test_embedding_and_audio_render_when_set():
    assert _rate_limiting({
        "config.rateLimiting.chat": "120 per minute",
        "config.rateLimiting.embedding": "500 per minute",
        "config.rateLimiting.audio": "20 per minute",
    }) == {"chat": "120 per minute", "embedding": "500 per minute", "audio": "20 per minute"}


def test_legacy_limit_renders_as_chat():
    """A 2.5.0 values file that only sets `limit` keeps working on upgrade."""
    assert _rate_limiting({"config.rateLimiting.limit": "45 per minute"}) == {"chat": "45 per minute"}


@pytest.mark.parametrize("extra, message", [
    ({"config.rateLimiting.chat": "120 per minute", "config.rateLimiting.limit": "45 per minute"}, "not both"),
    ({"config.rateLimiting.embeddings": "500 per minute"}, "embeddings"),
    ({"config.rateLimiting.audio": "20"}, "audio"),
], ids=["chat-and-limit", "misspelled-key", "wrong-type"])
def test_invalid_values_fail(extra, message):
    with pytest.raises(subprocess.CalledProcessError) as exc:
        _render(extra)
    assert message in exc.value.stderr
