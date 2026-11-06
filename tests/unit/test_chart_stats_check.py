"""The Helm chart runs `flask check-stats --fix` weekly as a CronJob.

The fold misses a request whose billing commits more than `stats.fold_lag` late;
the scheduled check adds it back. The CronJob's pod must reach the database exactly
as the migration Job does (same env, config mount, prometheus multiproc dir, image),
go through the image's entrypoint, and never overlap itself.

Renders the chart with `helm template`; the helm check (skip locally, fail in CI)
lives in test_chart_migration, whose helpers this module reuses.
"""
import pytest

from tests.unit.test_chart_migration import _FULL, _app_container, _deployment, _job, _render


def _cronjob(docs):
    cronjobs = [d for (kind, _), d in docs.items() if kind == "CronJob"]
    assert len(cronjobs) == 1
    return cronjobs[0]


def _container(cronjob):
    (container,) = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"]
    return container


@pytest.fixture(scope="module", params=["defaults", "full"])
def docs(request):
    return _render(_FULL if request.param == "full" else None)


def test_cronjob_runs_check_stats_fix_weekly_by_default(docs):
    cronjob = _cronjob(docs)
    assert cronjob["metadata"]["name"] == "rel-lumen-stats-check"
    spec = cronjob["spec"]
    assert spec["schedule"] == "17 3 * * 0"
    assert spec["concurrencyPolicy"] == "Forbid"
    assert spec["failedJobsHistoryLimit"] >= 1
    container = _container(cronjob)
    assert container["args"] == ["check-stats", "--fix"]
    # A `command` would bypass entrypoint.sh and its PGCONNECT_TIMEOUT default.
    assert "command" not in container
    assert container["image"] == _app_container(docs)["image"]
    assert container["imagePullPolicy"] == _app_container(docs)["imagePullPolicy"]


def test_failed_run_is_not_retried_or_deleted(docs):
    job_spec = _cronjob(docs)["spec"]["jobTemplate"]["spec"]
    assert job_spec["backoffLimit"] == 0
    assert "ttlSecondsAfterFinished" not in job_spec
    assert job_spec["template"]["spec"]["restartPolicy"] == "Never"


def test_pod_matches_the_migration_job(docs):
    pod = _cronjob(docs)["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    job_pod = _job(docs)["spec"]["template"]["spec"]
    (migrate,) = job_pod["containers"]
    container = _container(_cronjob(docs))
    assert container["env"] == migrate["env"]
    assert container["volumeMounts"] == migrate["volumeMounts"]
    assert container["securityContext"] == migrate["securityContext"]
    assert container["resources"] == migrate["resources"]
    assert pod["volumes"] == job_pod["volumes"]
    assert pod["initContainers"] == job_pod["initContainers"]
    common = {k: v for k, v in job_pod.items() if k not in ("containers", "initContainers", "volumes")}
    assert {k: v for k, v in pod.items() if k in common} == common


def test_full_values_render_the_prometheus_mount():
    container = _container(_cronjob(_render(_FULL)))
    assert {m["name"] for m in container["volumeMounts"]} == {"config", "prometheus-multiproc"}
    assert {"name": "PROMETHEUS_MULTIPROC_DIR", "value": "/tmp/prom"} in container["env"]


def test_pod_is_not_selected_by_the_service(docs):
    selector = docs[("Service", "rel-lumen")]["spec"]["selector"]
    labels = _cronjob(docs)["spec"]["jobTemplate"]["spec"]["template"]["metadata"]["labels"]
    assert not selector.items() <= labels.items()


def test_disabled_renders_no_cronjob():
    docs = _render({"statsCheck.enabled": "false"})
    assert not [d for (kind, _), d in docs.items() if kind == "CronJob"]
    # The rest of the chart is unaffected.
    assert _deployment(docs)


def test_values_override(tmp_path):
    values = tmp_path / "values.yaml"
    values.write_text(
        "statsCheck:\n"
        "  schedule: \"5 4 * * *\"\n"
        "  ttlSecondsAfterFinished: 604800\n"
        "  resources:\n"
        "    limits:\n"
        "      memory: 1Gi\n"
    )
    docs = _render(values_file=values)
    cronjob = _cronjob(docs)
    assert cronjob["spec"]["schedule"] == "5 4 * * *"
    assert cronjob["spec"]["jobTemplate"]["spec"]["ttlSecondsAfterFinished"] == 604800
    assert _container(cronjob)["resources"] == {"limits": {"memory": "1Gi"}}
    # The migration Job keeps the app resources.
    (migrate,) = _job(docs)["spec"]["template"]["spec"]["containers"]
    assert migrate["resources"] == _app_container(docs)["resources"]


_LONG = "a" * 63


@pytest.mark.parametrize("override", [
    {"fullnameOverride": _LONG},
    {"nameOverride": _LONG},
    {"fullnameOverride": "f" * 39 + "-" + "g" * 10},
    {"nameOverride": "a" * 51 + "-stats-check"},
])
def test_long_names_stay_within_kubernetes_limits(override):
    docs = _render(override)
    cronjob = _cronjob(docs)
    name = cronjob["metadata"]["name"]
    # The CronJob controller appends an 11-character suffix to each Job's name.
    assert len(name) <= 52 and name.endswith("-stats-check") and "--" not in name
    labels = cronjob["spec"]["jobTemplate"]["spec"]["template"]["metadata"]["labels"]
    assert all(len(v) <= 63 for v in labels.values())
    selector = docs[("Service", _service_name(docs))]["spec"]["selector"]
    assert not selector.items() <= labels.items()


def _service_name(docs):
    (name,) = [n for (kind, n) in docs if kind == "Service" and not n.endswith(("-postgresql", "-redis"))]
    return name
