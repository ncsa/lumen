"""Tests for config_watcher.restore_config_secrets on round-robin endpoint groups."""
import pytest

from lumen.services.config_watcher import (
    MASK,
    _find_unrestorable_masks,
    restore_config_secrets,
)

URL = "https://api.openai.com/v1"


@pytest.mark.parametrize(
    "third_key, expected_keys, expected_unrestorable",
    [
        # The issue's literal case. Under the `!=` -> `>` mutant this case errors
        # with IndexError at disk_keys[2] instead of failing an assertion; that
        # still kills the mutant and does not mean the test is broken.
        pytest.param(MASK, [MASK, MASK, MASK], 3, id="all_masked"),
        # Under the mutant endpoints 1-2 are silently filled with sk-first and
        # sk-second, so this case fails on its assertion.
        pytest.param("sk-third", [MASK, MASK, "sk-third"], 2, id="new_key"),
    ],
)
def test_restore_leaves_mask_when_round_robin_group_grows(
    third_key, expected_keys, expected_unrestorable
):
    on_disk = {
        "models": [
            {
                "name": "gpt-4o",
                "endpoints": [
                    {"url": URL, "api_key": "sk-first"},
                    {"url": URL, "api_key": "sk-second"},
                ],
            }
        ]
    }
    data = {
        "models": [
            {
                "name": "gpt-4o",
                "endpoints": [
                    {"url": URL, "api_key": MASK},
                    {"url": URL, "api_key": MASK},
                    {"url": URL, "api_key": third_key},
                ],
            }
        ]
    }

    restore_config_secrets(data, on_disk)

    assert [ep["api_key"] for ep in data["models"][0]["endpoints"]] == expected_keys
    assert len(_find_unrestorable_masks(data)) == expected_unrestorable
