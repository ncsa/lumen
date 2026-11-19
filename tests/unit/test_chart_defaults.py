"""The Helm chart's default resources, container securityContext and pod
securityContext.

Every container that runs the Lumen image (the app container, its
`wait-for-migrations` initContainer, the migration Job and the stats-check CronJob)
gets the same hardened securityContext and the same resources by default. A
`securityContext` override is merged over the default, so a partial override keeps
the rest; a `resources` override replaces the default entirely. The pods that run
the image (the Deployment, the migration Job and the stats-check CronJob) default to
the `RuntimeDefault` seccomp profile, and a `podSecurityContext` override is merged
over that default too.

Renders the chart with `helm template`; the helm check (skip locally, fail in CI)
lives in test_chart_migration, whose helpers this module reuses.
"""
from tests.unit.test_chart_migration import _deployment, _init, _job, _render

_DEFAULT_SECURITY_CONTEXT = {
    "runAsNonRoot": True,
    "runAsUser": 1000,
    "allowPrivilegeEscalation": False,
    "capabilities": {"drop": ["ALL"]},
}
_DEFAULT_POD_SECURITY_CONTEXT = {"seccompProfile": {"type": "RuntimeDefault"}}
_DEFAULT_RESOURCES = {
    "requests": {"cpu": "250m", "memory": "512Mi"},
    "limits": {"cpu": 1, "memory": "512Mi"},
}


def _app_image_containers(docs):
    """The containers that run the Lumen image, by a readable name."""
    pod = _deployment(docs)["spec"]["template"]["spec"]
    (app,) = [c for c in pod["containers"] if c["name"] == "lumen"]
    (migrate,) = _job(docs)["spec"]["template"]["spec"]["containers"]
    cronjobs = [d for (kind, _), d in docs.items() if kind == "CronJob"]
    assert len(cronjobs) == 1
    (stats,) = cronjobs[0]["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"]
    return {
        "lumen": app,
        "wait-for-migrations": _init(pod, "wait-for-migrations"),
        "migrate": migrate,
        "stats-check": stats,
    }


def _app_pods(docs):
    """The pod specs that run the Lumen image, by a readable name."""
    cronjobs = [d for (kind, _), d in docs.items() if kind == "CronJob"]
    assert len(cronjobs) == 1
    return {
        "deployment": _deployment(docs)["spec"]["template"]["spec"],
        "migrate": _job(docs)["spec"]["template"]["spec"],
        "stats-check": cronjobs[0]["spec"]["jobTemplate"]["spec"]["template"]["spec"],
    }


def test_defaults():
    containers = _app_image_containers(_render())
    for name, container in containers.items():
        assert container["securityContext"] == _DEFAULT_SECURITY_CONTEXT, name
        assert container["resources"] == _DEFAULT_RESOURCES, name


def test_partial_security_context_is_merged_over_the_default():
    containers = _app_image_containers(_render({"securityContext.readOnlyRootFilesystem": "true"}))
    for name, container in containers.items():
        assert container["securityContext"] == {**_DEFAULT_SECURITY_CONTEXT, "readOnlyRootFilesystem": True}, name


def test_security_context_can_turn_a_default_off():
    containers = _app_image_containers(_render({"securityContext.runAsNonRoot": "false"}))
    for name, container in containers.items():
        assert container["securityContext"] == {**_DEFAULT_SECURITY_CONTEXT, "runAsNonRoot": False}, name


def test_resources_replace_the_default():
    containers = _app_image_containers(_render({"resources.limits.memory": "2Gi"}))
    for name, container in containers.items():
        assert container["resources"] == {"limits": {"memory": "2Gi"}}, name
        # The securityContext is unaffected.
        assert container["securityContext"] == _DEFAULT_SECURITY_CONTEXT, name


def test_pod_security_context_defaults_to_runtime_default_seccomp():
    for name, pod in _app_pods(_render()).items():
        assert pod["securityContext"] == _DEFAULT_POD_SECURITY_CONTEXT, name


def test_partial_pod_security_context_is_merged_over_the_default():
    for name, pod in _app_pods(_render({"podSecurityContext.fsGroup": "1000"})).items():
        assert pod["securityContext"] == {**_DEFAULT_POD_SECURITY_CONTEXT, "fsGroup": 1000}, name
