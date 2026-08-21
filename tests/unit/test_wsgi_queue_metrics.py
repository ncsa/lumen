"""Guard the queue instrumentation of the ASGI→WSGI bridge.

Three separate things live here, and they share a harness because they share a
cause: the WSGI thread pool's queue is invisible to Flask. A request submitted to
a saturated pool has already spent its wait by the time ``before_request`` runs,
so unless the bridge stamps T0 the wait is unmeasurable — and unless the bridge
counts depth explicitly it is unobservable, because
``ThreadPoolExecutor._work_queue.qsize()`` drops to zero the moment an item is
handed to a thread and so reports a fully saturated pool as idle.

The counter tests are the important ones, and specifically the "returns to
exactly 0" half. A depth gauge that only goes up is worse than no gauge: it
reads as a permanent overload, and the natural response to it is to add capacity
that changes nothing. The leak-prone part is the set of *exit* paths, which is
why they are parametrised — a normal return, a raising view, the ``_StalledClient``
unwind and a client that vanishes while queued all leave the responder by
different routes, and three of them are exception paths.

``send_blocked`` is tested against a *stalled* client on purpose. Accumulating
only on ``future.result()``'s success would leave it ~0 for exactly the outcome
it exists to identify, since a stalled client spends its entire life on the
timeout branch.
"""

import asyncio
import threading
import time
from datetime import timedelta

import pytest

from lumen.services.wsgi_disconnect import (
    SEND_BLOCKED_ENVIRON_KEY,
    SEND_TIMEOUT_ENV,
    STARTED_AT_ENVIRON_KEY,
    T0_ENVIRON_KEY,
    DisconnectAwareWSGIMiddleware,
    configured_threads_total,
    current_queue_depth,
    current_threads_busy,
    queue_shed_total,
)

EMPTY_BODY = [{"type": "http.request", "body": b"", "more_body": False}]

# A client that hangs up immediately. Delivered while the request is still
# waiting for a worker thread, so the disconnect Event is already set when the
# work item finally starts.
DISCONNECT_WHILE_QUEUED = EMPTY_BODY + [{"type": "http.disconnect"}]

# How long a fake server stays unresponsive. Bounded so a regression fails on an
# assertion rather than wedging the suite.
STALL_RELEASE = 10.0

# The send bound under test, shrunk so the stall costs a fraction of a second.
TEST_SEND_TIMEOUT = "0.5"

# Comfortably past a2wsgi's 10-slot send queue plus the sender's in-flight
# message, so a stalled server actually blocks the writer.
CHUNKS = 40


def _scope(path):
    return {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "root_path": "",
        "path": path,
        "query_string": b"",
        "headers": [(b"content-length", b"0")],
        "server": ("testserver", 80),
        "client": ("198.51.100.7", 4242),
    }


class _Client:
    """One scripted ASGI client driving the middleware."""

    def __init__(self, path, messages=None, stall_send=False):
        self.path = path
        self.messages = list(EMPTY_BODY if messages is None else messages)
        self.stall_send = stall_send
        self.sent = []
        self.error = None

    async def run(self, middleware):
        pending = list(self.messages)
        stalling = [self.stall_send]

        async def receive():
            if pending:
                return pending.pop(0)
            # A real server simply says nothing more; the pump is cancelled when
            # the response finishes.
            await asyncio.Event().wait()

        async def send(message):
            self.sent.append(message)
            if stalling[0]:
                stalling[0] = False
                # Never set; the wait_for bounds the damage.
                await asyncio.wait_for(asyncio.Event().wait(), STALL_RELEASE)

        try:
            await middleware(_scope(self.path), receive, send)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # a raising view must not fail the harness
            self.error = exc


def _drive(app, clients, workers, coordinator=None, timeout=30.0):
    """Run ``clients`` concurrently against one middleware and thread pool.

    ``coordinator`` is an extra coroutine run alongside them; it is how the
    tests sample the counters and release a parked request, since everything
    else is either on the event loop or on a worker thread.
    """
    middleware = DisconnectAwareWSGIMiddleware(app, workers=workers)

    async def main():
        coros = [client.run(middleware) for client in clients]
        if coordinator is not None:
            coros.append(coordinator())
        await asyncio.wait_for(asyncio.gather(*coros), timeout)

    try:
        asyncio.run(main())
    finally:
        middleware.executor.shutdown(wait=False)
    return clients


def _park_coordinator(release, sampled, settle=0.2):
    """Wait until one request is running and another is queued, then sample.

    Waiting for *both* conditions matters: depth alone is briefly 1 before the
    first request reaches a thread, and sampling there would pass without ever
    having exercised a queue.
    """

    async def coordinator():
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if current_queue_depth() >= 1 and current_threads_busy() >= 1:
                break
            await asyncio.sleep(0.005)
        sampled["depth"] = current_queue_depth()
        sampled["busy"] = current_threads_busy()
        # Give the event loop time to deliver any http.disconnect to the queued
        # request's pump before that request is allowed to start.
        await asyncio.sleep(settle)
        release.set()

    return coordinator


@pytest.mark.timeout(60)
def test_queue_wait_is_visible_behind_a_busy_worker():
    """T0 must predate the worker thread, not the view.

    With a single worker the second request cannot start until the first lets
    go, and the whole point of stamping T0 in the bridge is that this gap is
    recoverable afterwards. Stamped anywhere inside Flask it would read as zero.
    """
    observed = {}
    release = threading.Event()

    def app(environ, start_response):
        path = environ["PATH_INFO"]
        observed[path] = {
            "t0": environ[T0_ENVIRON_KEY],
            "t1": time.monotonic(),
            "started_at": environ[STARTED_AT_ENVIRON_KEY],
        }
        if path == "/first":
            release.wait(timeout=10)
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"ok"]

    _drive(
        app,
        [_Client("/first"), _Client("/second")],
        workers=1,
        coordinator=_park_coordinator(release, {}),
    )

    first_wait = observed["/first"]["t1"] - observed["/first"]["t0"]
    second_wait = observed["/second"]["t1"] - observed["/second"]["t0"]
    assert first_wait < 0.1, f"an unqueued request must not show a wait: {first_wait}"
    assert second_wait >= 0.15, (
        f"the queued request's wait was not observable: {second_wait}"
    )


def test_started_at_is_timezone_aware_utc():
    """It is compared against request_logs.time, which is TIMESTAMPTZ.

    Naive UTC here would be reinterpreted by Postgres against the session
    TimeZone — silent and deployment-dependent. This is the documented exception
    to CLAUDE.md's ``utcnow()`` rule, so it needs a test saying so.
    """
    observed = {}

    def app(environ, start_response):
        observed["started_at"] = environ[STARTED_AT_ENVIRON_KEY]
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"ok"]

    _drive(app, [_Client("/")], workers=2)

    started_at = observed["started_at"]
    assert started_at.tzinfo is not None, "started_at must be timezone-aware"
    assert started_at.utcoffset() == timedelta(0), "started_at must be UTC"


def test_configured_threads_total_reports_the_pool_size():
    """Depth is meaningless without it: 8 queued behind 4 threads or behind 40?"""
    DisconnectAwareWSGIMiddleware(lambda environ, start_response: [], workers=7)
    assert configured_threads_total() == 7


@pytest.mark.timeout(90)
@pytest.mark.parametrize("mode", ["normal", "raises", "stalled", "disconnect"])
def test_queue_counters_rise_and_return_to_zero(mode, monkeypatch):
    """Depth is non-zero while a request is queued and exactly zero afterwards.

    Parametrised over the four ways a request leaves the responder. Three are
    exception paths, and each one that forgets to decrement leaks a unit of
    depth per request for the life of the process.
    """
    if mode == "stalled":
        monkeypatch.setenv(SEND_TIMEOUT_ENV, TEST_SEND_TIMEOUT)

    assert current_queue_depth() == 0, "a previous test leaked queue depth"
    assert current_threads_busy() == 0, "a previous test leaked a busy thread"

    release = threading.Event()
    sampled = {}
    entered = []

    def app(environ, start_response):
        path = environ["PATH_INFO"]
        entered.append(path)
        if path == "/first":
            release.wait(timeout=10)
            start_response("200 OK", [("Content-Type", "text/plain")])
            return [b"ok"]
        if mode == "raises":
            raise RuntimeError("view exploded")
        start_response("200 OK", [("Content-Type", "text/plain")])
        if mode == "stalled":
            return (b"chunk-%04d;" % i for i in range(CHUNKS))
        return [b"ok"]

    second = _Client(
        "/second",
        messages=DISCONNECT_WHILE_QUEUED if mode == "disconnect" else None,
        stall_send=(mode == "stalled"),
    )
    _drive(
        app,
        [_Client("/first"), second],
        workers=1,
        coordinator=_park_coordinator(release, sampled),
    )

    assert sampled["depth"] >= 1, "the queued request was never counted"
    assert sampled["busy"] >= 1, "the running request was never counted"
    assert current_queue_depth() == 0, f"queue depth leaked on the {mode} path"
    assert current_threads_busy() == 0, f"busy threads leaked on the {mode} path"

    if mode == "raises":
        assert isinstance(second.error, RuntimeError)
    else:
        assert second.error is None
    if mode == "disconnect":
        assert entered == ["/first"], "a shed request must never reach the view"


@pytest.mark.timeout(60)
def test_send_blocked_counts_the_timed_out_wait(monkeypatch):
    """The stalled client's block is on the timeout branch, so it must count.

    Accumulating only after a successful ``future.result()`` would report ~0
    here, which is the one answer that would make the column useless.
    """
    monkeypatch.setenv(SEND_TIMEOUT_ENV, TEST_SEND_TIMEOUT)
    observed = {}

    def app(environ, start_response):
        observed["holder"] = environ[SEND_BLOCKED_ENVIRON_KEY]
        start_response("200 OK", [("Content-Type", "text/plain")])
        return (b"chunk-%04d;" % i for i in range(CHUNKS))

    _drive(app, [_Client("/stalled", stall_send=True)], workers=2)

    blocked = observed["holder"].seconds
    assert blocked >= float(TEST_SEND_TIMEOUT) * 0.8, (
        f"the timed-out send was not accumulated: {blocked}"
    )


@pytest.mark.timeout(60)
def test_send_blocked_stays_small_for_a_healthy_client():
    """The counterpart: the holder exists and does not accumulate spuriously."""
    observed = {}

    def app(environ, start_response):
        observed["holder"] = environ[SEND_BLOCKED_ENVIRON_KEY]
        start_response("200 OK", [("Content-Type", "text/plain")])
        return (b"chunk-%04d;" % i for i in range(CHUNKS))

    _drive(app, [_Client("/healthy")], workers=2)

    blocked = observed["holder"].seconds
    assert 0.0 <= blocked < 1.0, f"a healthy stream should barely block: {blocked}"


@pytest.mark.timeout(60)
def test_request_disconnected_while_queued_is_shed_before_the_view():
    """The pump runs before the submit, so a queued request's flag is accurate.

    Nothing the view produces can reach a client that has already gone, so the
    work — preflight, DB lookups, an upstream generation — is pure waste, and
    under a burst it is waste taken from requests that could still be served.
    """
    entered = []
    release = threading.Event()

    def app(environ, start_response):
        entered.append(environ["PATH_INFO"])
        if environ["PATH_INFO"] == "/first":
            release.wait(timeout=10)
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"ok"]

    before = queue_shed_total()
    gone = _Client("/gone", messages=DISCONNECT_WHILE_QUEUED)
    _drive(
        app,
        [_Client("/first"), gone],
        workers=1,
        coordinator=_park_coordinator(release, {}),
    )

    assert entered == ["/first"], "the shed request must never reach the view"
    assert queue_shed_total() == before + 1
    assert gone.sent[0]["type"] == "http.response.start"
    assert gone.sent[0]["status"] == 499


@pytest.mark.timeout(60)
@pytest.mark.parametrize(
    "path, source",
    [("/v1/chat/completions", "api"), ("/chat/stream", "chat")],
)
def test_shed_request_is_counted_in_the_rejection_taxonomy(monkeypatch, path, source):
    """A shed request is a rejection, and the only one nothing in Flask can see.

    The model label is empty because it genuinely is: the body is never parsed —
    that is the whole point of shedding — so the bridge cannot know which model
    was wanted. Guessing would make the per-model breakdown quietly wrong; this
    is the same honest empty the rate limiter reports. ``source`` comes from the
    path because there is no request context to ask.
    """
    recorded = []
    monkeypatch.setattr(
        "lumen.blueprints.metrics.middleware.observe_rejection",
        lambda reason, src, model="": recorded.append((reason, src, model)),
    )
    release = threading.Event()

    def app(environ, start_response):
        if environ["PATH_INFO"] == "/first":
            release.wait(timeout=10)
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"ok"]

    _drive(
        app,
        [_Client("/first"), _Client(path, messages=DISCONNECT_WHILE_QUEUED)],
        workers=1,
        coordinator=_park_coordinator(release, {}),
    )

    assert recorded == [("queue_shed", source, "")]


@pytest.mark.timeout(60)
def test_a_broken_counter_still_sheds_the_request(monkeypatch):
    """Counting must not turn a 499 into an unhandled error on the bridge."""
    def boom(*a, **kw):
        raise RuntimeError("prometheus is unhappy")

    monkeypatch.setattr("lumen.blueprints.metrics.middleware.observe_rejection", boom)
    release = threading.Event()

    def app(environ, start_response):
        if environ["PATH_INFO"] == "/first":
            release.wait(timeout=10)
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"ok"]

    before = queue_shed_total()
    gone = _Client("/gone", messages=DISCONNECT_WHILE_QUEUED)
    _drive(
        app,
        [_Client("/first"), gone],
        workers=1,
        coordinator=_park_coordinator(release, {}),
    )

    assert gone.error is None
    assert queue_shed_total() == before + 1
    assert gone.sent[0]["status"] == 499
