"""Client-disconnect behaviour measured against a *real* server, not the test client.

Why this file exists (and why it cannot live in tests/routes/):

Every other streaming test in this suite drives Flask through the Werkzeug test
client, which always closes the response iterable — so the ``except GeneratorExit``
abort paths in ``llm.py`` and ``api/routes.py`` are exercised and pass. Production
does not work that way. ``asgi.py`` serves Flask through ``a2wsgi.WSGIMiddleware``
under uvicorn, and that bridge never delivers ASGI ``http.disconnect`` to the WSGI
app: a2wsgi's responder only reads ``receive()`` for the request *body*, and
uvicorn's ``send()`` returns silently once the peer is gone instead of raising.
Nothing signals the WSGI layer, so a client that vanishes mid-stream leaves the
response generator running to completion — burning upstream GPU time nobody will
read, pinning a WSGI worker thread, and never writing the ``request_logs`` row that
is supposed to make mid-stream disconnects observable.

The existing tests are not wrong about what they assert; they assert it against a
server that behaves differently from the one we deploy. So these tests boot the
real ASGI stack (``asgi.py``'s wiring, uvicorn, an ephemeral port, a background
thread) and speak to it over a raw socket, which is the only way to control the
disconnect precisely enough to observe the defect.

The assertions are deliberately written against observable behaviour — did the
stubbed upstream stop producing? was the abort logged? were the DB connection and
the app context released? — not against any particular disconnect-detection
mechanism, so they stay valid however the fix is built.

Two constraints worth knowing before editing this file:

* **One app, one engine.** The server runs in this interpreter on a background
  thread and is handed the session-scoped test app (``lumen.create_app`` is
  patched for the one import of ``asgi``), so the served application shares this
  process's engine and connection pool. That is what makes ``pool.checkedout()``
  a meaningful assertion and lets the fixtures' rows be visible to the server.
  Letting ``asgi.py`` build its own app would put a second engine — and a second
  set of writers — on the same SQLite file, and the pool assertion would be
  measuring the wrong pool. It also keeps the ASGI wrapping identical to
  production rather than a copy of ``asgi.py`` that can drift.
* **Nothing may still be running when a test returns.** The test DB is a shared
  file and the autouse ``clean_db`` fixture wipes it between tests, so a
  generator still draining on a worker thread would race it. The ``upstream``
  fixture's teardown shuts the stub down and waits for the pool to drain before
  the test finishes.
"""

import importlib
import json
import socket
import struct
import sys
import threading
import time
import types
from http import HTTPStatus

import openai
import pytest
import uvicorn
from sqlalchemy import select

# How long the stub upstream keeps producing, and how fast. Long enough that a
# disconnect a few chunks in leaves most of the generation ahead of us.
_CHUNK_DELAY = 0.15
_TOTAL_CHUNKS = 40
# Chunks the client reads before pulling the plug.
_CHUNKS_BEFORE_DISCONNECT = 3
# How long we watch the stub after the disconnect. ~20 chunks' worth: on a server
# that ignores the disconnect the counter climbs by roughly that much.
_OBSERVE_AFTER_DISCONNECT = 3.0
# Slack for chunks already in flight when the client vanished, plus the one poll
# interval a disconnect check can lag by.
_INFLIGHT_TOLERANCE = 3
# Every network read is bounded so a regression fails instead of hanging CI.
_SOCKET_TIMEOUT = 20.0


# ---------------------------------------------------------------------------
# The stub upstream
# ---------------------------------------------------------------------------

class _Delta:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.delta = _Delta(content)


class _Chunk:
    """Shaped for both consumers: ``api/routes.py`` calls ``model_dump()`` and reads
    ``.usage``; ``llm.py`` reads ``.usage`` and ``.choices[0].delta.content``."""

    usage = None

    def __init__(self, text):
        self.choices = [_Choice(text)]
        self._text = text

    def model_dump(self):
        return {"choices": [{"delta": {"content": self._text}}]}


class _SlowUpstream:
    """A stand-in for the upstream LLM that records how much it actually generated.

    ``produced`` is the whole point: it is the number of chunks the *backend* has
    been asked for, which is what a disconnect is supposed to stop. Watching the
    bytes on the client socket cannot tell us that — the client is gone.
    """

    def __init__(self):
        self.produced = 0
        self.stop = threading.Event()
        self.finished = threading.Event()
        self.closed = threading.Event()

    def chunks(self):
        try:
            for i in range(_TOTAL_CHUNKS):
                if self.stop.wait(_CHUNK_DELAY):
                    return
                self.produced += 1
                yield _Chunk(f"tok{i} ")
        finally:
            self.finished.set()

    # -- the openai.OpenAI(...) surface the proxy paths use -------------------
    def __call__(self, *args, **kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        # Mirrors the real client: closing it aborts the upstream generation
        # (verified in the plan's experiment E2), so the counter freezes here.
        self.stop.set()
        self.closed.set()
        return False

    @property
    def chat(self):
        upstream = self

        class _Completions:
            @staticmethod
            def create(**kwargs):
                return upstream.chunks()

        return type("_Chat", (), {"completions": _Completions()})()


def _wait_for(predicate, timeout):
    """Poll ``predicate`` until true or the deadline passes. Never sleeps blindly."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


def _pool(app):
    """The engine's connection pool. Fetched under a context; usable outside one."""
    from lumen.extensions import db
    with app.app_context():
        return db.engine.pool


@pytest.fixture
def upstream(app, monkeypatch):
    """Replace ``openai.OpenAI`` process-wide so no real network call happens.

    The server runs in this interpreter (a background thread), so a plain
    monkeypatch reaches it. Patching the ``openai`` module itself rather than a
    blueprint's alias covers both proxy paths, which import the same module.
    """
    pool = _pool(app)
    stub = _SlowUpstream()
    monkeypatch.setattr(openai, "OpenAI", stub)
    try:
        yield stub
    finally:
        # monkeypatch is function-scoped but the server thread is not, and on a
        # server that ignores disconnects the generator is still running right
        # now. Let it finish before the test returns: an in-flight generator
        # would otherwise race the autouse clean_db fixture for the SQLite write
        # lock and leave rows behind for the next test (and, since the test DB is
        # a file, for the next run).
        stub.stop.set()
        stub.closed.wait(timeout=10)
        stub.finished.wait(timeout=10)
        _wait_for(lambda: pool.checkedout() == 0, timeout=10)


# ---------------------------------------------------------------------------
# The real server
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def server(app):
    """uvicorn serving the production ASGI wiring on an ephemeral port.

    ``asgi.py`` is imported with ``lumen.create_app`` patched to hand back the
    session-scoped test app, so the served application shares this process's
    engine, pool and fixtures — and so the ASGI wrapping stays whatever
    ``asgi.py`` actually uses rather than a copy of it that could drift.
    """
    import lumen

    original_create_app = lumen.create_app
    lumen.create_app = lambda *a, **k: app
    try:
        sys.modules.pop("asgi", None)
        asgi = importlib.import_module("asgi")
    finally:
        lumen.create_app = original_create_app

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]

    uv = uvicorn.Server(uvicorn.Config(
        asgi.app, log_level="warning", access_log=False, lifespan="off",
    ))
    thread = threading.Thread(target=uv.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()

    deadline = time.monotonic() + 20
    while not uv.started:
        if time.monotonic() > deadline:
            uv.should_exit = True
            pytest.fail("uvicorn did not start within 20s")
        time.sleep(0.02)
    # Started only means the listener is up; confirm it actually accepts.
    with socket.create_connection(("127.0.0.1", port), timeout=5):
        pass

    try:
        yield port
    finally:
        uv.should_exit = True
        thread.join(timeout=20)


def _stream_then_hangup(port, path, headers, payload):
    """POST ``payload``, read until a few SSE events arrive, then RST the socket.

    A raw socket rather than requests/httpx: we need the disconnect to happen at a
    known point mid-stream, and we need it to be a real transport-level close.
    ``SO_LINGER 0`` makes ``close()`` send RST immediately, so there is no doubt the
    peer is gone. Returns the bytes read, for diagnostics when nothing arrives.
    """
    body = json.dumps(payload).encode()
    lines = [f"POST {path} HTTP/1.1", f"Host: 127.0.0.1:{port}",
             "Content-Type: application/json", f"Content-Length: {len(body)}"]
    lines += [f"{k}: {v}" for k, v in headers.items()]
    request = ("\r\n".join(lines) + "\r\n\r\n").encode() + body

    sock = socket.create_connection(("127.0.0.1", port), timeout=_SOCKET_TIMEOUT)
    try:
        sock.sendall(request)
        received = b""
        deadline = time.monotonic() + _SOCKET_TIMEOUT
        while received.count(b"data:") < _CHUNKS_BEFORE_DISCONNECT:
            if time.monotonic() > deadline:
                break
            data = sock.recv(4096)
            if not data:
                break
            received += data
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    finally:
        sock.close()
    return received


# ---------------------------------------------------------------------------
# Fixtures for the two streaming paths
# ---------------------------------------------------------------------------

def _grant_access(app, entity_id, model_config_id):
    """Give the entity an unlimited coin pool; ownerless models are public."""
    from lumen.extensions import db
    from lumen.models.entity_limit import EntityLimit
    with app.app_context():
        db.session.add(EntityLimit(
            entity_id=entity_id, max_coins=-2, refresh_coins=0, starting_coins=0,
        ))
        db.session.commit()


@pytest.fixture
def api_token(app, test_user):
    from lumen.extensions import db
    from lumen.models.api_key import APIKey
    from lumen.services.crypto import hash_api_key
    token = "lk_disconnect_test_token"
    with app.app_context():
        db.session.add(APIKey(
            entity_id=test_user["id"], name="disconnect-test",
            key_hash=hash_api_key(token), active=True,
        ))
        db.session.commit()
    return token


@pytest.fixture
def session_cookie(app, test_user):
    """A real signed session cookie, so the raw socket can log in like a browser."""
    serializer = app.session_interface.get_signing_serializer(app)
    value = serializer.dumps({
        "entity_id": test_user["id"],
        "entity_name": test_user["name"],
        "initials": test_user["initials"],
        "gravatar_hash": test_user["gravatar_hash"],
    })
    return f"{app.config['SESSION_COOKIE_NAME']}={value}"


# ---------------------------------------------------------------------------
# Assertion helpers
# ---------------------------------------------------------------------------

def _wait_for_abort_row(app, source, timeout=10.0):
    """Poll for the ``request_logs`` row that records the mid-stream abort.

    The stub upstream never emits a usage chunk, and the completed path only
    bills when usage arrives, so any row for this source is necessarily the abort
    accounting. See ``_assert_billed_for_abort`` for what must be true of it.
    """
    from lumen.extensions import db
    from lumen.models.request_log import RequestLog
    deadline = time.monotonic() + timeout
    while True:
        with app.app_context():
            rows = db.session.execute(
                select(RequestLog).where(RequestLog.source == source)
            ).scalars().all()
            if rows or time.monotonic() > deadline:
                return rows


def _assert_billed_for_abort(rows, label):
    """The abort row must be marked, and must charge for what was streamed.

    Existence alone used to be the assertion here, which was correct while an
    abort recorded zero cost. It is not correct now, and leaving it that way
    would let the test pass whether the client is billed or not — the one
    dimension where being wrong is exploitable. A client that reads content and
    then hangs up before the usage chunk must still pay for the tokens it read,
    or disconnecting becomes a way to get inference for free.

    Cost is asserted rather than balance because this fixture grants an unlimited
    pool (``max_coins=-2``), which makes ``subtract_coins`` a no-op by design;
    the balance-decrement path is covered by the unit tests.
    """
    row = rows[0]
    assert row.aborted is True, (
        f"{label}: the abort row is not marked aborted, so it is indistinguishable "
        "from a completed request now that aborts carry a real cost"
    )
    assert row.output_tokens >= 1, (
        f"{label}: {row.output_tokens} output tokens billed, but the client read "
        f"{_CHUNKS_BEFORE_DISCONNECT} content chunks before disconnecting"
    )
    assert float(row.cost) > 0.0, (
        f"{label}: the aborted stream was billed {row.cost} — a client that reads "
        "content and hangs up before the usage chunk is getting it for free"
    )


def _leftover_context_count():
    """Anomalies whose signature is 'an app context outlived its request'."""
    from lumen.services.ctx_probe import format_context_anomalies
    report = format_context_anomalies()
    return sum(report.count(kind) for kind in (
        "leftover-context", "cross-thread-pop", "skipped-teardown-pop", "double-push",
    ))


def _assert_stopped_generating(upstream, at_disconnect, label):
    # A fixed wait, not a poll: we are measuring the *absence* of further work,
    # so there is nothing to poll for — only time in which it must not happen.
    time.sleep(_OBSERVE_AFTER_DISCONNECT)
    after = upstream.produced
    assert after <= at_disconnect + _INFLIGHT_TOLERANCE, (
        f"{label}: the client disconnected after {at_disconnect} upstream chunks, but "
        f"{_OBSERVE_AFTER_DISCONNECT}s later the stub upstream had produced {after} "
        f"(of {_TOTAL_CHUNKS}). The server kept pulling the upstream stream for a "
        f"client that is gone — the disconnect never reached the WSGI app."
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_api_stream_disconnect_stops_generation(
    app, server, upstream, test_user, test_model, test_model_endpoint, api_token,
):
    """A `/v1/chat/completions` client that hangs up must stop the backend.

    This is the API half of finding F1. On the deployed stack the abort never
    happens: the generator keeps draining the upstream stream for a socket that no
    longer exists, so the request costs full GPU time, holds a worker thread for
    the rest of the generation, and writes no abort row — making the disconnect
    metric read zero forever, which is worse than having no metric at all.
    """
    from lumen.extensions import db
    _grant_access(app, test_user["id"], test_model["id"])
    pool = _pool(app)
    anomalies_before = _leftover_context_count()
    sessions_before = len(db.session.registry.registry)

    received = _stream_then_hangup(
        server, "/v1/chat/completions",
        {"Authorization": f"Bearer {api_token}"},
        {"model": test_model["model_name"],
         "messages": [{"role": "user", "content": "hi"}], "stream": True},
    )
    at_disconnect = upstream.produced

    # Guard against passing (or failing) for the wrong reason: if the request never
    # streamed, nothing below means anything.
    assert received.startswith(f"HTTP/1.1 {HTTPStatus.OK} ".encode()), (
        f"the request did not succeed; server said: {received[:600]!r}"
    )
    assert received.count(b"data:") >= _CHUNKS_BEFORE_DISCONNECT, (
        f"the stream never started; server said: {received[:600]!r}"
    )

    _assert_stopped_generating(upstream, at_disconnect, "/v1/chat/completions")

    aborted = _wait_for_abort_row(app, "api")
    assert aborted, "no request_logs row was written for the aborted stream"
    _assert_billed_for_abort(aborted, "/v1/chat/completions")

    # Task 1.6 — pool hygiene. A generator abandoned mid-flight is exactly how a
    # connection gets stranded idle-in-transaction and an app context gets left
    # current on a worker thread, poisoning every later request on it.
    # Polled for the same reason the registry check below is: the teardown runs
    # on the server thread, and `pool.checkedout()` is a single cross-thread read
    # of state that is still settling. This one is not the flaky assertion today
    # — the connection is returned by `session.close()`, which runs *before* the
    # `registry.clear()` the next assertion waits on, so by the time that one
    # passes this one already would have — but the ordering is SQLAlchemy's to
    # change, not ours to rely on. A real leak never returns the connection, so
    # the timeout still fails it.
    assert _wait_for(lambda: pool.checkedout() == 0, timeout=10), (
        "a DB connection is still checked out after the abort"
    )
    # Polled, not read once: the registry dict is process-wide (a ScopedRegistry
    # built with a scopefunc stores plain dict entries, not thread-locals) and the
    # request being torn down is on the server thread. ``scoped_session.remove()``
    # calls ``close()`` and then ``clear()`` on consecutive lines, so the moment
    # ``pool.checkedout()`` reaches 0 the entry is still there for a few
    # microseconds. A real leak never clears, so the timeout still fails it.
    assert _wait_for(
        lambda: len(db.session.registry.registry) == sessions_before, timeout=10,
    ), "a session is still registered — its app context was never torn down"
    assert _leftover_context_count() == anomalies_before, (
        "an app context outlived the aborted request (see /metrics/debug's "
        "app-context anomalies section)"
    )


def test_chat_stream_disconnect_stops_generation(
    app, server, upstream, test_user, test_model, test_model_endpoint, session_cookie,
):
    """The same for the web chat path, where a user closing the tab is the common case.

    ``/chat/stream`` goes through ``send_message_stream`` rather than the proxy view,
    so it has its own generator, its own abort accounting and its own nested
    ``llm_stream``; covering only the API path would leave the path most users
    actually trigger untested.
    """
    from lumen.extensions import db
    _grant_access(app, test_user["id"], test_model["id"])
    pool = _pool(app)
    anomalies_before = _leftover_context_count()
    sessions_before = len(db.session.registry.registry)

    received = _stream_then_hangup(
        server, "/chat/stream",
        {"Cookie": session_cookie},
        {"model": test_model["model_name"],
         "messages": [{"role": "user", "content": "hi"}]},
    )
    at_disconnect = upstream.produced

    assert received.startswith(f"HTTP/1.1 {HTTPStatus.OK} ".encode()), (
        f"the request did not succeed; server said: {received[:600]!r}"
    )
    assert received.count(b"data:") >= _CHUNKS_BEFORE_DISCONNECT, (
        f"the stream never started; server said: {received[:600]!r}"
    )

    _assert_stopped_generating(upstream, at_disconnect, "/chat/stream")

    aborted = _wait_for_abort_row(app, "chat")
    assert aborted, "no request_logs row was written for the aborted stream"
    _assert_billed_for_abort(aborted, "/chat/stream")

    # Polled for the same reason the registry check below is: the teardown runs
    # on the server thread, and `pool.checkedout()` is a single cross-thread read
    # of state that is still settling. This one is not the flaky assertion today
    # — the connection is returned by `session.close()`, which runs *before* the
    # `registry.clear()` the next assertion waits on, so by the time that one
    # passes this one already would have — but the ordering is SQLAlchemy's to
    # change, not ours to rely on. A real leak never returns the connection, so
    # the timeout still fails it.
    assert _wait_for(lambda: pool.checkedout() == 0, timeout=10), (
        "a DB connection is still checked out after the abort"
    )
    # Polled, not read once: the registry dict is process-wide (a ScopedRegistry
    # built with a scopefunc stores plain dict entries, not thread-locals) and the
    # request being torn down is on the server thread. ``scoped_session.remove()``
    # calls ``close()`` and then ``clear()`` on consecutive lines, so the moment
    # ``pool.checkedout()`` reaches 0 the entry is still there for a few
    # microseconds. A real leak never clears, so the timeout still fails it.
    assert _wait_for(
        lambda: len(db.session.registry.registry) == sessions_before, timeout=10,
    ), "a session is still registered — its app context was never torn down"
    assert _leftover_context_count() == anomalies_before, (
        "an app context outlived the aborted request"
    )


# ---------------------------------------------------------------------------
# The ASGI bridge actually publishes the timing marks
# ---------------------------------------------------------------------------

class _UsageChunk:
    """Terminal chunk carrying usage, which is what makes the API path bill.

    Deliberately not folded into ``_SlowUpstream``: the disconnect tests depend
    on that stub never completing normally, and a usage chunk would change what
    they exercise.
    """

    def __init__(self):
        self.choices = []
        self.usage = types.SimpleNamespace(prompt_tokens=7, completion_tokens=11)

    def model_dump(self):
        return {"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 11}}


class _CompletingUpstream:
    """Streams a few chunks and then finishes properly, usage and all."""

    def chunks(self):
        for i in range(3):
            yield _Chunk(f"tok{i} ")
        yield _UsageChunk()

    def __call__(self, *args, **kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, *args, **kwargs):
        return self.chunks()


def test_completed_request_records_arrival_through_the_real_bridge(
    app, server, monkeypatch, test_user, test_model, test_model_endpoint, api_token,
):
    """The one link the Flask test client structurally cannot prove.

    ``started_at``/``queue_wait``/``preflight`` are stamped into the WSGI environ
    by ``_DisconnectAwareWSGIResponder`` in ``lumen/services/wsgi_disconnect.py``,
    which only exists on the ASGI path. ``app.test_client()`` bypasses ``asgi.py``
    entirely, so every unit and route test can prove Lumen writes those columns
    *given* the environ keys, and none can prove the bridge ever sets them. A
    typo in a key name would leave every production row NULL while the whole
    suite stayed green — the columns are nullable by design, so nothing raises.

    This drives a real, completed request through real uvicorn and reads the row.
    """
    import http.client
    import json

    from sqlalchemy import select

    from lumen.extensions import db
    from lumen.models.request_log import RequestLog

    monkeypatch.setattr(openai, "OpenAI", _CompletingUpstream())
    _grant_access(app, test_user["id"], test_model["id"])
    with app.app_context():
        db.session.execute(RequestLog.__table__.delete())
        db.session.commit()

    conn = http.client.HTTPConnection("127.0.0.1", server, timeout=30)
    try:
        conn.request(
            "POST", "/v1/chat/completions",
            body=json.dumps({
                "model": test_model["model_name"],
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            }),
            headers={"Authorization": f"Bearer {api_token}",
                     "Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        body = resp.read()  # read to completion; no hangup here
    finally:
        conn.close()

    # Fail for the right reason: a 4xx would make every assertion below vacuous.
    assert resp.status == HTTPStatus.OK, f"request failed: {resp.status} {body[:400]!r}"
    assert b"data:" in body, f"the stream never started: {body[:400]!r}"

    with app.app_context():
        row = db.session.execute(select(RequestLog)).scalars().one()

    assert row.started_at is not None, (
        "the ASGI bridge did not publish lumen.started_at — every production row "
        "would carry a NULL arrival time, silently"
    )
    assert row.queue_wait is not None, "the bridge did not publish lumen.t0_monotonic"
    assert row.preflight is not None, "preflight could not be derived from the bridge marks"

    # Sane magnitudes: a mixed-clock regression lands at ~1.76e9 or goes negative.
    assert 0 <= row.queue_wait < 60, f"implausible queue_wait {row.queue_wait}"
    assert 0 <= row.preflight < 60, f"implausible preflight {row.preflight}"
    assert row.outcome == "ok"

    assert row.started_at <= row.time
    span = (row.time - row.started_at).total_seconds()
    assert 0 <= span < 60, f"implausible arrival-to-completion span {span}"
