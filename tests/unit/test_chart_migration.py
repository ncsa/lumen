"""The Helm chart runs the upgrade as a Job and gates app pods on it.

Renders the chart with `helm template` and checks the two halves of the two-step
start: the `<fullname>-migrate-<revision>` Job runs the image's `upgrade` mode, and
the Deployment's `wait-for-migrations` initContainer runs the image's gate mode.
Both must see the database exactly as the app container does (same env, same
config mount), and both must go through the image's entrypoint, which is where
the one-shot modes get their default PGCONNECT_TIMEOUT.

Needs the `helm` binary. GitHub's ubuntu runners ship it; outside CI the module
skips when it is missing, in CI it fails so the check cannot silently vanish.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
CHART = REPO / "chart"

_HELM = shutil.which("helm")
if _HELM is None:
    if os.environ.get("CI"):
        pytest.fail("helm is not installed; the chart migration tests need it", pytrace=False)
    pytest.skip("helm is not installed", allow_module_level=True)

_SECRET = "x" * 32
_REQUIRED = {
    "config.secretKey": _SECRET,
    "config.encryptionKey": _SECRET,
    "oauth2.clientId": "client",
    "oauth2.clientSecret": "secret",
    "postgresql.auth.password": "pw",
}
#: Turns on every optional env var and mount of the app container.
_FULL = {
    "wsgiProcesses": "2",
    "api.prometheus.enabled": "true",
    "api.prometheus.multiprocDir": "/tmp/prom",
    "config.themes.campus.pvc": "campus-theme",
}


def _render(extra=None):
    values = {**_REQUIRED, **(extra or {})}
    cmd = [_HELM, "template", "rel", str(CHART)]
    for key, value in values.items():
        cmd += ["--set", f"{key}={value}"]
    out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    docs = [d for d in yaml.safe_load_all(out) if d]
    return {(d["kind"], d["metadata"]["name"]): d for d in docs}


def _deployment(docs):
    return docs[("Deployment", "rel-lumen")]


def _job(docs):
    jobs = [d for (kind, _), d in docs.items() if kind == "Job"]
    assert len(jobs) == 1
    return jobs[0]


def _app_container(docs):
    (app,) = [c for c in _deployment(docs)["spec"]["template"]["spec"]["containers"] if c["name"] == "lumen"]
    return app


def _init(pod_spec, name):
    (container,) = [c for c in pod_spec["initContainers"] if c["name"] == name]
    return container


@pytest.fixture(scope="module", params=["defaults", "full"])
def docs(request):
    return _render(_FULL if request.param == "full" else None)


def test_job_runs_upgrade_mode_through_entrypoint(docs):
    (migrate,) = _job(docs)["spec"]["template"]["spec"]["containers"]
    assert migrate["args"] == ["upgrade"]
    # A `command` would bypass entrypoint.sh and with it the PGCONNECT_TIMEOUT default.
    assert "command" not in migrate
    assert migrate["image"] == _app_container(docs)["image"]


def test_gate_runs_wait_for_migrations_mode_through_entrypoint(docs):
    pod = _deployment(docs)["spec"]["template"]["spec"]
    assert [c["name"] for c in pod["initContainers"]] == ["wait-for-db", "wait-for-migrations"]
    gate = _init(pod, "wait-for-migrations")
    # No --timeout: the gate waits as long as the Job takes.
    assert gate["args"] == ["wait-for-migrations"]
    assert "command" not in gate
    assert gate["image"] == _app_container(docs)["image"]
    assert gate["imagePullPolicy"] == _app_container(docs)["imagePullPolicy"]


def test_gate_and_job_share_the_app_env(docs):
    app_env = _app_container(docs)["env"]
    gate = _init(_deployment(docs)["spec"]["template"]["spec"], "wait-for-migrations")
    (migrate,) = _job(docs)["spec"]["template"]["spec"]["containers"]
    assert gate["env"] == app_env
    assert migrate["env"] == app_env
    # The chart sets no PGCONNECT_TIMEOUT, so the entrypoint's default applies.
    assert "PGCONNECT_TIMEOUT" not in {e["name"] for e in app_env}


def test_gate_has_the_app_mounts_and_job_all_but_themes(docs):
    app_mounts = _app_container(docs)["volumeMounts"]
    gate = _init(_deployment(docs)["spec"]["template"]["spec"], "wait-for-migrations")
    assert gate["volumeMounts"] == app_mounts

    job_pod = _job(docs)["spec"]["template"]["spec"]
    (migrate,) = job_pod["containers"]
    assert migrate["volumeMounts"] == [m for m in app_mounts if not m["name"].startswith("theme-")]
    # Every mount is backed by a volume of the same source as the Deployment's.
    deploy_volumes = {v["name"]: v for v in _deployment(docs)["spec"]["template"]["spec"]["volumes"]}
    for volume in job_pod["volumes"]:
        assert volume == deploy_volumes[volume["name"]]
    assert {m["name"] for m in migrate["volumeMounts"]} == {v["name"] for v in job_pod["volumes"]}


def test_full_values_render_the_optional_mounts():
    docs = _render(_FULL)
    gate = _init(_deployment(docs)["spec"]["template"]["spec"], "wait-for-migrations")
    assert {m["name"] for m in gate["volumeMounts"]} == {"config", "theme-campus", "prometheus-multiproc"}
    (migrate,) = _job(docs)["spec"]["template"]["spec"]["containers"]
    assert {m["name"] for m in migrate["volumeMounts"]} == {"config", "prometheus-multiproc"}
    assert {"name": "PROMETHEUS_MULTIPROC_DIR", "value": "/tmp/prom"} in migrate["env"]


def test_job_waits_for_db_like_the_deployment(docs):
    deploy_wait = _init(_deployment(docs)["spec"]["template"]["spec"], "wait-for-db")
    assert _job(docs)["spec"]["template"]["spec"]["initContainers"] == [deploy_wait]


def test_job_name_carries_release_revision(docs):
    # `helm template` always renders revision 1; the template must use the revision.
    assert _job(docs)["metadata"]["name"] == "rel-lumen-migrate-1"
    assert "-migrate-{{ .Release.Revision }}" in (CHART / "templates" / "migrate-job.yaml").read_text()


def test_job_pod_is_not_selected_by_the_service(docs):
    selector = docs[("Service", "rel-lumen")]["spec"]["selector"]
    labels = _job(docs)["spec"]["template"]["metadata"]["labels"]
    assert not selector.items() <= labels.items()
    assert _job(docs)["spec"]["template"]["spec"]["restartPolicy"] == "Never"


def test_job_values_defaults(docs):
    spec = _job(docs)["spec"]
    assert spec["backoffLimit"] == 6
    assert spec["ttlSecondsAfterFinished"] == 86400
    (migrate,) = spec["template"]["spec"]["containers"]
    assert migrate["resources"] == _app_container(docs)["resources"]


def test_job_values_override():
    docs = _render({
        "migration.backoffLimit": "2",
        "migration.ttlSecondsAfterFinished": "",
        "migration.resources.limits.memory": "2Gi",
    })
    spec = _job(docs)["spec"]
    assert spec["backoffLimit"] == 2
    assert "ttlSecondsAfterFinished" not in spec
    (migrate,) = spec["template"]["spec"]["containers"]
    assert migrate["resources"] == {"limits": {"memory": "2Gi"}}
