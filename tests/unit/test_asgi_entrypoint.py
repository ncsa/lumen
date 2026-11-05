"""Guard against serving Lumen without its ASGI bridge.

`asgi.py` is the only place `DisconnectAwareWSGIMiddleware` is installed, and that
bridge is the sole source of the `lumen.t0_monotonic` / `lumen.started_at` /
`lumen.send_blocked` environ marks. Serve the bare Flask app instead — which is what
`uvicorn run:app --interface wsgi` does, since uvicorn then wraps it in its own
deprecated `_WSGIMiddleware` — and four things break silently at once:

* `request_logs.queue_wait`, `preflight`, `started_at` and `send_blocked` all store
  NULL, so the one number that separates "we are under-provisioned" from "the model
  is slow" is unavailable;
* client disconnects are never detected, so an abandoned request keeps generating
  upstream and keeps its worker thread;
* `LUMEN_WSGI_SEND_TIMEOUT` is not enforced, so a half-open socket pins a thread;
* the thread-pool size is uvicorn's hard-coded 10 per process, ignoring
  `LUMEN_WSGI_WORKERS`.

This is not hypothetical: `loadtesting/run_loadtest.sh` predated `asgi.py` and was
never revisited when the bridge landed, so a 500-user load test measured a 40-slot
ceiling (4 processes x uvicorn's 10 threads) and recorded NULL for every queue
column. See lumen/services/wsgi_disconnect.py.
"""
import importlib
import logging
import re
import sys
from pathlib import Path

import dotenv
import pytest
from flask import Flask

import lumen
from lumen.services import db_pool, wsgi_disconnect

REPO = Path(__file__).resolve().parents[2]

#: Files that invoke or document a server command. Kept explicit rather than
#: globbed so a new deployment surface has to be added here deliberately.
_SERVING_FILES = (
    "entrypoint.sh",
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.override.yml",
    "loadtesting/run_loadtest.sh",
    "loadtesting/README.md",
    "chart/templates/deployment.yaml",
    "README.md",
)

#: `--interface wsgi` makes uvicorn wrap the app in its own WSGI adapter, which is
#: mutually exclusive with the bridge no matter which module is served.
_INTERFACE_FLAG = re.compile(r"--interface[=\s]+wsgi")

#: A server pointed at `run:app`, whose `app` is the bare Flask object. Note
#: `flask --app run db upgrade` and the `lumen` console script legitimately import
#: run.py -- only handing it to a server is the bug, hence _SERVER_BINARY below.
_BARE_APP = re.compile(r"\brun:app\b")

_SERVER_BINARY = re.compile(r"\b(uvicorn|gunicorn|hypercorn)\b")


def _logical_lines(rel: str, text: str):
    """Yield (first_lineno, joined_text) for each shell/YAML *logical* line.

    Two things this has to get right, both of which a naive per-line scan gets
    wrong in opposite directions:

    * A server invocation is routinely split across backslash continuations -- the
      original `--interface wsgi` bug lived on a continuation line with no
      `uvicorn` token on it. Continuations are joined so the flag and the binary
      land in the same logical line.
    * Comments and prose *explaining* the ban must not trip it (this file's own
      docstring, and the warnings in run_loadtest.sh and the READMEs). Comments are
      stripped, and in Markdown only fenced code blocks are considered at all.
    """
    is_markdown = rel.endswith(".md")
    in_fence = False
    pending, start = "", None

    for lineno, raw in enumerate(text.splitlines(), start=1):
        if is_markdown:
            if raw.lstrip().startswith("```"):
                in_fence = not in_fence
                continue
            if not in_fence:
                continue
        else:
            # Strip whole-line and trailing comments; a '#' inside the commands we
            # care about only ever starts a comment in these files.
            raw = raw.split("#", 1)[0]

        stripped = raw.rstrip()
        if stripped.endswith("\\"):
            pending += stripped[:-1] + " "
            start = start or lineno
            continue

        pending += stripped
        if pending.strip():
            yield (start or lineno), pending.strip()
        pending, start = "", None

    if pending.strip():
        yield (start or 0), pending.strip()


def _serving_invocations():
    """Every logical line in a shipped file that actually runs an ASGI/WSGI server."""
    for rel in _SERVING_FILES:
        path = REPO / rel
        if not path.exists():
            continue
        for lineno, line in _logical_lines(rel, path.read_text()):
            if _SERVER_BINARY.search(line):
                yield rel, lineno, line


def test_no_uvicorn_interface_wsgi_flag():
    offenders = [
        f"{rel}:{lineno}: {line}"
        for rel, lineno, line in _serving_invocations()
        if _INTERFACE_FLAG.search(line)
    ]
    assert not offenders, (
        "`--interface wsgi` bypasses DisconnectAwareWSGIMiddleware entirely. Serve "
        "`asgi:app` with no --interface flag.\n" + "\n".join(offenders)
    )


def test_no_server_serves_the_bare_flask_app():
    offenders = [
        f"{rel}:{lineno}: {line}"
        for rel, lineno, line in _serving_invocations()
        if _BARE_APP.search(line)
    ]
    assert not offenders, (
        "`run:app` is the bare Flask WSGI app, so serving it skips the ASGI bridge "
        "and every timing mark it publishes. Serve `asgi:app` instead.\n"
        + "\n".join(offenders)
    )


def test_the_guard_detects_the_original_regression():
    """The guard must catch the exact shape of the bug it exists to prevent.

    A guard that has never been observed to fail is not known to work, and this one
    has a real failure mode: the offending flag sat on a backslash continuation with
    no `uvicorn` token of its own.
    """
    regressed = (
        'CONFIG_YAML="$CONFIG_YAML" uv run uvicorn run:app \\\n'
        '    --host "$LUMEN_HOST" --port "$LUMEN_PORT" \\\n'
        '    --workers "$WORKERS" --interface wsgi \\\n'
        "    --log-level warning &\n"
    )
    joined = [line for _, line in _logical_lines("run_loadtest.sh", regressed)]
    assert len(joined) == 1, f"continuations were not joined: {joined}"
    assert _SERVER_BINARY.search(joined[0])
    assert _INTERFACE_FLAG.search(joined[0])
    assert _BARE_APP.search(joined[0])


def test_prose_explaining_the_ban_is_not_flagged():
    """Comments and non-fenced Markdown must not trip the guard."""
    shell_comment = "# Serve asgi:app, NOT run:app --interface wsgi. asgi.py installs the bridge.\n"
    assert not list(_logical_lines("run_loadtest.sh", shell_comment))

    prose = "Serve `asgi:app`, never `uvicorn run:app --interface wsgi`, because reasons.\n"
    assert not list(_logical_lines("README.md", prose))


def test_asgi_module_installs_the_bridge():
    """The positive half: asgi.py must actually wrap the app, not just exist."""
    source = (REPO / "asgi.py").read_text()
    assert "DisconnectAwareWSGIMiddleware" in source
    # The thread-pool size must stay operator-controllable rather than hard-coded.
    assert "resolve_wsgi_workers" in source


@pytest.fixture
def import_asgi(monkeypatch):
    """Import asgi.py against a stub app, returning (module, workers the bridge got)."""
    flask_app = Flask("asgi")
    flask_app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_size": 60, "max_overflow": 20}
    flask_app.logger.setLevel(logging.INFO)
    bridge_workers = []

    def fake_bridge(app, *, workers, **kwargs):
        bridge_workers.append(workers)
        return app

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(lumen, "create_app", lambda: flask_app)
    monkeypatch.setattr(wsgi_disconnect, "DisconnectAwareWSGIMiddleware", fake_bridge)
    monkeypatch.setattr(db_pool, "_warned_invalid_wsgi_workers", False)
    monkeypatch.delitem(sys.modules, "asgi", raising=False)

    def _import():
        module = importlib.import_module("asgi")
        return module, bridge_workers

    yield _import
    sys.modules.pop("asgi", None)


@pytest.mark.parametrize(
    ("setting", "workers", "shown"),
    [(None, 160, "unset"), ("auto", 160, "auto"), ("32", 32, "32")],
)
def test_asgi_logs_resolved_thread_pool_once(import_asgi, monkeypatch, caplog, setting, workers, shown):
    """asgi.py logs the resolved count once at startup, and hands the bridge that count."""
    if setting is None:
        monkeypatch.delenv("LUMEN_WSGI_WORKERS", raising=False)
    else:
        monkeypatch.setenv("LUMEN_WSGI_WORKERS", setting)

    with caplog.at_level(logging.INFO):
        module, bridge_workers = import_asgi()

    records = [r for r in caplog.records if "WSGI thread pool" in r.getMessage()]
    assert len(records) == 1
    assert records[0].getMessage() == f"WSGI thread pool: workers={workers} (LUMEN_WSGI_WORKERS={shown})"
    assert bridge_workers == [workers]
    assert module.wsgi_workers == workers


# --------------------------------------------------------------------------- #
# entrypoint.sh modes, run against stub `flask` and `uvicorn` on PATH.         #
# --------------------------------------------------------------------------- #

_STUB = """#!/bin/sh
echo "$(basename "$0") $* PGCONNECT_TIMEOUT=${PGCONNECT_TIMEOUT:-unset}" >> "$STUB_LOG"
if [ "$(basename "$0")" = flask ] && [ "$1 $2 $3" = "--app run wait-for-migrations" ]; then
  exit "${GATE_EXIT:-0}"
fi
"""


@pytest.fixture
def run_entrypoint(tmp_path, monkeypatch):
    """Run entrypoint.sh with ``args``; returns (exit status, the stubs' call log)."""
    import subprocess

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("flask", "uvicorn"):
        stub = bin_dir / name
        stub.write_text(_STUB)
        stub.chmod(0o755)
    log = tmp_path / "calls.log"

    def _run(*args, **env):
        log.write_text("")
        full_env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "STUB_LOG": str(log), **env}
        result = subprocess.run(["sh", str(REPO / "entrypoint.sh"), *args],
                                env=full_env, cwd=tmp_path, timeout=30)
        return result.returncode, log.read_text().splitlines()

    return _run


def test_entrypoint_upgrade_runs_only_flask_upgrade(run_entrypoint):
    status, calls = run_entrypoint("upgrade")
    assert status == 0
    assert calls == ["flask --app run upgrade PGCONNECT_TIMEOUT=10"]


@pytest.mark.parametrize("gate_exit", ["0", "1"])
def test_entrypoint_wait_for_migrations_runs_only_the_gate(run_entrypoint, gate_exit):
    status, calls = run_entrypoint("wait-for-migrations", "--timeout", "30", GATE_EXIT=gate_exit)
    assert status == int(gate_exit), "the gate's exit status must reach the container"
    assert calls == ["flask --app run wait-for-migrations --timeout 30 PGCONNECT_TIMEOUT=10"]


def test_entrypoint_one_shot_modes_keep_an_operator_connect_timeout(run_entrypoint):
    _, calls = run_entrypoint("upgrade", PGCONNECT_TIMEOUT="45")
    assert calls == ["flask --app run upgrade PGCONNECT_TIMEOUT=45"]
    _, calls = run_entrypoint("wait-for-migrations", PGCONNECT_TIMEOUT="45")
    assert calls == ["flask --app run wait-for-migrations PGCONNECT_TIMEOUT=45"]


def test_entrypoint_default_mode_gates_then_serves_with_workers(run_entrypoint):
    """Serving never migrates: it waits at the gate, then hands --workers to uvicorn."""
    status, calls = run_entrypoint("--workers", "4")
    assert status == 0
    assert calls == [
        "flask --app run wait-for-migrations PGCONNECT_TIMEOUT=10",
        "uvicorn asgi:app --host 0.0.0.0 --port 5001 --workers 4 PGCONNECT_TIMEOUT=unset",
    ]
    assert not any("db upgrade" in c or "run upgrade" in c for c in calls)


def test_entrypoint_default_mode_keeps_an_operator_connect_timeout(run_entrypoint):
    _, calls = run_entrypoint(PGCONNECT_TIMEOUT="45")
    assert calls == [
        "flask --app run wait-for-migrations PGCONNECT_TIMEOUT=45",
        "uvicorn asgi:app --host 0.0.0.0 --port 5001 PGCONNECT_TIMEOUT=45",
    ]


def test_entrypoint_default_mode_does_not_serve_when_the_gate_fails(run_entrypoint):
    """A database migrated by a newer image fails the gate; the container must exit."""
    status, calls = run_entrypoint("--workers", "4", GATE_EXIT="1")
    assert status == 1
    assert calls == ["flask --app run wait-for-migrations PGCONNECT_TIMEOUT=10"]
