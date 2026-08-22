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
column. See plans/ and lumen/services/wsgi_disconnect.py.
"""
import re
from pathlib import Path

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
