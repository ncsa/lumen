"""The Helm chart writes the api.keys TTL settings into config.yaml.

`maxTtl` renders as `max_ttl`, falling back to the deprecated `ttl` and then to
"0"; `maxAdminTtl`, `maxMonitorTtl` and `defaultTtl` render only when set, so the
server defaults apply otherwise. The schema rejects anything but "", 0 or
`0`/`<n>h|d|w`.

Renders the chart with `helm template`; the helm check (skip locally, fail in CI)
lives in test_chart_migration, whose helpers this module reuses.
"""
import subprocess

import pytest
import yaml

from tests.unit.test_chart_migration import _render


def _api_keys(extra=None, values_file=None):
    docs = _render(extra, values_file=values_file)
    config = yaml.safe_load(docs[("Secret", "rel-lumen-config")]["stringData"]["config.yaml"])
    return config["api"]["keys"]


def test_nothing_set_renders_no_limit():
    assert _api_keys() == {"max_ttl": "0"}


def test_legacy_ttl_becomes_max_ttl():
    assert _api_keys({"config.api.keys.ttl": "30d"}) == {"max_ttl": "30d"}


def test_all_four_set():
    assert _api_keys({
        "config.api.keys.maxTtl": "90d",
        "config.api.keys.maxAdminTtl": "1d",
        "config.api.keys.maxMonitorTtl": "0",
        "config.api.keys.defaultTtl": "30d",
        "config.api.keys.ttl": "5d",
    }) == {
        "max_ttl": "90d",
        "max_admin_ttl": "1d",
        "max_monitor_ttl": "0",
        "default_ttl": "30d",
    }


def test_only_default_ttl():
    assert _api_keys({"config.api.keys.defaultTtl": "30d"}) == {"max_ttl": "0", "default_ttl": "30d"}


def test_integer_zero_max_ttl_wins_over_legacy_ttl(tmp_path):
    values = tmp_path / "values.yaml"
    values.write_text("config:\n  api:\n    keys:\n      maxTtl: 0\n      ttl: 30d\n")
    assert _api_keys(values_file=values) == {"max_ttl": "0"}


@pytest.mark.parametrize("field", ["maxTtl", "maxAdminTtl", "maxMonitorTtl", "defaultTtl", "ttl"])
def test_bad_value_fails_schema_validation(field):
    with pytest.raises(subprocess.CalledProcessError) as exc:
        _render({f"config.api.keys.{field}": "30x"})
    assert f"config.api.keys.{field}" in exc.value.stderr
