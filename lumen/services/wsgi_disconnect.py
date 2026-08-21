"""ASGI→WSGI bridge that delivers client disconnects to the WSGI application.

Production serves Flask through ``a2wsgi.WSGIMiddleware`` under uvicorn (see
``asgi.py``). a2wsgi's ``WSGIResponder`` never watches for the ASGI
``http.disconnect`` event, and uvicorn's ``send()`` silently no-ops once the
peer is gone instead of raising. The consequence, verified empirically: when a
client disconnects mid-stream the WSGI response generator is never closed,
``GeneratorExit`` never fires, and the app streams an entire LLM response into a
dead socket — pinning a worker thread and burning upstream GPU time — while the
abort accounting that exists precisely to observe this never runs.

This module fixes the missing signal. It puts a :class:`threading.Event` into
the WSGI environ under :data:`ENVIRON_KEY`, set the moment the ASGI server
reports ``http.disconnect``. Streaming views capture it via
:func:`client_disconnect_event` while the request context is still live (the
response generators run context-free — see CLAUDE.md) and poll ``is_set()``
between chunks.

``receive()`` has EXACTLY ONE consumer, and it must stay that way
-----------------------------------------------------------------
``receive()`` is a single-consumer channel: every message it yields goes to
whoever happens to be awaiting it, and it is gone for everyone else. The
obvious implementation — leave ``a2wsgi.Body`` reading ``receive()`` for the
request body and add a small watcher task alongside it looking for
``http.disconnect`` — puts two consumers on that channel, and they steal each
other's messages at random: the watcher swallows ``http.request`` body chunks
that ``Body`` needed, and ``Body`` swallows the ``http.disconnect`` the watcher
was waiting for.

The failure mode is nasty because it is invisible in the obvious places. A GET
has no body to steal, so every GET keeps working and a smoke test passes. Only
requests with a body break, and they break *quietly*: the body arrives
truncated or with a hole in the middle rather than raising, so the symptom
surfaces far downstream as a JSON parse error, a short upload, or a request
that simply hangs waiting for bytes that were handed to the wrong reader. Every
endpoint that motivated this module (``/v1/chat/completions``, ``/chat/stream``,
``/v1/audio/*``) is a POST with a body.

So there is one pump task, and it is the only caller of ``receive()``. It
dispatches ``http.request`` messages into a queue that ``wsgi.input`` reads
from, and sets the disconnect Event on ``http.disconnect``. Never add a second
``receive()`` caller anywhere in this chain. Enforced by
``tests/unit/test_wsgi_disconnect_body.py``.

The other half: stalled readers, which no flag can catch
---------------------------------------------------------
The disconnect Event above handles a *clean* disconnect — the peer sent a FIN
and the server told us about it. It does nothing for a client that is simply not
reading: a half-open TCP connection (closed laptop, dropped NAT entry) or an app
that opened a stream and stopped consuming it. No ``http.disconnect`` is ever
delivered there, and — more fundamentally — the flag could not help even if it
were. Polling happens in the streaming view's loop body, but a blocked writer is
suspended *inside* ``yield``: a2wsgi's ``WSGIResponder.send`` pushes each chunk
across threads with ``asyncio.run_coroutine_threadsafe(...).result()``, and
upstream of it uvicorn's ``send`` awaits ``flow.drain()``. Neither has a timeout,
so once the socket buffer, uvicorn's write buffer and the 10-slot ``send_queue``
are all full, the WSGI thread parks in ``send`` forever. The generator never gets
control back, so it can never look at any flag. ``workers`` such clients is a
total request-serving outage with nothing logged.

So ``send`` is overridden here to bound that hand-off (:data:`DEFAULT_SEND_TIMEOUT`,
overridable with :data:`SEND_TIMEOUT_ENV`). On expiry it cancels the pending put,
sets the *same* disconnect Event the pump sets — one consistent "client is gone"
signal for the whole system — logs a warning, and raises. Raising is the point:
the exception unwinds a2wsgi's ``wsgi()`` into its ``finally: iterable.close()``,
which throws ``GeneratorExit`` at the suspended ``yield`` and so runs the
existing abort accounting. ``__call__`` then swallows that one exception rather
than letting it reach uvicorn's ``run_asgi``, which would log a full traceback
for what is a routine event; see the comment there for the rest of the trade.

Note this is the *write* side. It is deliberately not the pump's queue — that is
the *read* side (request body) and has nothing to do with a stalled reader.

Queue accounting: T0, depth, and shedding
-----------------------------------------
This is also the only place in the request path that can see the WSGI thread
pool's *queue*. A request is submitted to the pool here and does not start
executing until a worker is free, so the wait is invisible to Flask: by the time
``before_request`` runs, the queue time has already been spent and nothing
recorded it. So ``__call__`` stamps T0 into the environ immediately before the
submit (:data:`T0_ENVIRON_KEY`, :data:`STARTED_AT_ENVIRON_KEY`) and a Flask
``before_request`` subtracts it from its own T1.

Depth is counted explicitly rather than read from ``executor._work_queue`` —
private API, and it excludes the items already handed to threads, so a
completely saturated pool with nothing left to hand out reads as idle. See
:func:`current_queue_depth` / :func:`current_threads_busy`.

Having both the depth counters and the disconnect Event here also makes one
optimisation possible: the pump is created *before* the submit, so a request
that has been sitting in the queue already knows its client hung up. It is
answered 499 without ever entering the application (:func:`queue_shed_total`).

``_PumpedBody`` and ``_DisconnectAwareWSGIResponder`` are thin derivations of
a2wsgi 1.10.10's ``Body`` and ``WSGIResponder``; the bounded send queue and its
backpressure semantics are inherited unchanged — only the unbounded wait on it
is replaced.
"""

import asyncio
import concurrent.futures
import contextvars
import dataclasses
import functools
import logging
import os
import sys
import threading
import time
import typing
from datetime import datetime, timezone

from a2wsgi.wsgi import Body, WSGIMiddleware, WSGIResponder, build_environ
from flask import has_request_context, request

logger = logging.getLogger(__name__)

#: WSGI environ key holding the ``threading.Event`` set on client disconnect.
ENVIRON_KEY = "lumen.client_disconnected"

#: WSGI environ key holding ``time.monotonic()`` taken immediately before the
#: request is handed to the WSGI thread pool (T0). Monotonic because it is only
#: ever used as one end of a subtraction.
T0_ENVIRON_KEY = "lumen.t0_monotonic"

#: WSGI environ key holding the wall-clock instant of that same moment.
STARTED_AT_ENVIRON_KEY = "lumen.started_at"

#: WSGI environ key holding this request's :class:`SendBlocked` holder.
SEND_BLOCKED_ENVIRON_KEY = "lumen.send_blocked"

#: Environment variable overriding :data:`DEFAULT_SEND_TIMEOUT`, in seconds.
#: Follows the ``LUMEN_WSGI_WORKERS`` precedent in ``db_pool.py``: server
#: plumbing belongs in the environment, not in ``config.yaml``. Read once per
#: request, so a restart is not needed to change it. Non-numeric or non-positive
#: values warn and fall back to the default (there is deliberately no way to
#: disable the bound — an unbounded wait is the bug this exists to fix).
SEND_TIMEOUT_ENV = "LUMEN_WSGI_SEND_TIMEOUT"

#: Seconds a WSGI thread may wait to hand one response chunk to the server
#: before the client is declared gone.
#:
#: This is a stall detector, not a QoS policy, so it is set well past any real
#: client. What has to drain before a thread even begins waiting is the kernel
#: socket buffer plus uvicorn's 64 KiB high-water mark plus ten queued chunks;
#: a genuinely awful 20 kbit/s mobile link clears that in well under a minute,
#: so 300 s leaves more than an order of magnitude of headroom. It also sits
#: comfortably inside the 600 s gateway request budget (``chart/values.yaml``),
#: so a stalled thread is reclaimed before the request would have been cut off
#: anyway, and far below the ~15 min a Linux kernel takes to abandon
#: retransmissions to a vanished peer — which is the *best* case today, since a
#: peer that is alive but advertising a zero window is never abandoned at all.
DEFAULT_SEND_TIMEOUT = 300.0

# The pump reads at most one message ahead of the WSGI thread, so the ASGI
# server's own read backpressure still governs how much of a large upload is
# buffered in this process.
_BODY_QUEUE_SIZE = 1

# (body, more_body) pushed to end the body stream when the client vanished
# mid-upload, so a blocked read returns EOF instead of waiting forever.
_EOF: typing.Tuple[bytes, bool] = (b"", False)

# Queue accounting, maintained by _DisconnectAwareWSGIResponder around the
# submit. Process-wide because there is one thread pool per process; the lock
# covers the queued/running hand-off, which must be one atomic move so a sample
# taken between the two never loses a request.
_counter_lock = threading.Lock()
_queued = 0
_running = 0
_shed_total = 0
_threads_total = 0


def current_queue_depth() -> int:
    """Requests submitted to the WSGI pool that have not started running."""
    return _queued


def current_threads_busy() -> int:
    """Requests currently executing on a WSGI worker thread."""
    return _running


def configured_threads_total() -> int:
    """Worker threads the pool was configured with, or 0 if never constructed.

    Set by the most recently constructed :class:`DisconnectAwareWSGIMiddleware`.
    Production builds exactly one (``asgi.py``); tests that build several see the
    last one's value.
    """
    return _threads_total


def queue_shed_total() -> int:
    """Requests answered 499 because the client had gone before they started."""
    return _shed_total


def _observe_shed(path: str) -> None:
    """Count one shed request in the rejection taxonomy, or do nothing at all.

    Never *triggers* the import of the metrics middleware — the same rule (and
    the same ``sys.modules`` lookup) as ``pool_tracker._observe_wait``:
    prometheus_client binds each metric to its mmap file at construction, so an
    import landing before PROMETHEUS_MULTIPROC_DIR is set produces metrics no
    scrape will ever merge, and this module sits on the hot path of every
    request through the bridge.

    The model label is empty because it genuinely is unknown here: the request
    body has never been parsed — the whole point of shedding is that the
    application never ran — so saying "" is honest, exactly as it is for the
    rate limiter. The source is derived from the path, since ``request_logs``
    only knows "chat" and "api" and there is no request context to ask.

    Swallows everything: a request already being shed must not fail because a
    counter did.
    """
    middleware = sys.modules.get("lumen.blueprints.metrics.middleware")
    if middleware is None:
        return
    try:
        middleware.observe_rejection(
            "queue_shed", "api" if path.startswith("/v1/") else "chat", "",
        )
    except Exception:  # noqa: BLE001 - instrumentation must never escalate
        logger.debug("counting a shed request failed", exc_info=True)


@dataclasses.dataclass
class SendBlocked:
    """Running total of seconds this request's WSGI thread spent inside ``send``.

    A *mutable holder* rather than a float, and published in the environ, because
    the streaming paths bill inside a context-free response generator: at view
    time nothing has been sent yet, so a float captured into the generator's
    closure would read 0.0 forever. The generator holds this object instead and
    reads ``.seconds`` when it bills.

    Mutated in ``send`` and read by the generator on the *same* worker thread
    (``send`` runs inside ``run_in_executor``), so no lock is needed.

    What it measures, honestly: time to enqueue onto a bounded ``asyncio.Queue``
    handed across to the event loop. That absorbs event-loop scheduling latency
    as well as the peer's read rate, so under a burst it is *not* purely "slow
    client". It also excludes whatever is sent after billing — the final chunks
    and ``data: [DONE]``.
    """

    seconds: float = 0.0


class _StalledClient(Exception):
    """Raised in the WSGI thread when a chunk cannot be handed to the server.

    Private on purpose: it is caught by the responder that raised it and never
    escapes this module. Its only job is to unwind the WSGI thread out of the
    application so a2wsgi's ``finally: iterable.close()`` can fire.
    """


def _send_timeout() -> float:
    """Resolve :data:`SEND_TIMEOUT_ENV`, falling back to the default."""
    raw = os.environ.get(SEND_TIMEOUT_ENV, "").strip()
    if not raw:
        return DEFAULT_SEND_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        value = 0.0
    if value <= 0:
        logger.warning(
            "%s=%r is not a positive number of seconds; using %.0fs",
            SEND_TIMEOUT_ENV, raw, DEFAULT_SEND_TIMEOUT,
        )
        return DEFAULT_SEND_TIMEOUT
    return value


async def _no_receive() -> typing.NoReturn:
    """Placeholder for ``Body.receive`` — reaching it means a second consumer."""
    raise AssertionError(
        "wsgi.input is fed by the pump; receive() must have exactly one consumer"
    )


class _PumpedBody(Body):
    """``wsgi.input`` fed from the pump's queue instead of from ``receive()``."""

    def __init__(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue) -> None:
        super().__init__(loop, _no_receive)
        self._queue = queue

    def _receive_more_data(self) -> bytes:
        if not self._has_more:
            return b""
        future = asyncio.run_coroutine_threadsafe(self._queue.get(), loop=self.loop)
        body, more_body = future.result()
        self._has_more = more_body
        return body


async def _pump(
    receive: typing.Callable[[], typing.Awaitable[typing.Any]],
    queue: asyncio.Queue,
    disconnected: threading.Event,
) -> None:
    """The one and only consumer of ``receive()``. See the module docstring."""
    body_open = True
    try:
        while True:
            message = await receive()
            if message["type"] == "http.request":
                if body_open:
                    more_body = message.get("more_body", False)
                    await queue.put((message.get("body", b""), more_body))
                    body_open = more_body
            elif message["type"] == "http.disconnect":
                # Set the flag before the put, which blocks while the queue is
                # full: an app that has stopped reading the body must still see
                # the disconnect.
                disconnected.set()
                if body_open:
                    await queue.put(_EOF)
                return
    except asyncio.CancelledError:
        # Normal teardown: the responder cancels the pump once the response is
        # done. Nobody is waiting on the queue by then.
        raise
    except BaseException:
        # Any other failure would otherwise be silent — the task's exception is
        # never retrieved — while the WSGI thread stays blocked forever in
        # _receive_more_data waiting on a queue nothing will fill again. That is
        # a permanently leaked worker thread per occurrence. Unblock the reader
        # and report the client as gone before propagating.
        disconnected.set()
        if body_open:
            try:
                await queue.put(_EOF)
            except BaseException:
                pass
        raise


class _DisconnectAwareWSGIResponder(WSGIResponder):
    """a2wsgi's responder with the ``receive()`` pump and a bounded ``send``.

    ``__call__`` starts the pump, feeds ``wsgi.input`` from the pump's queue, and
    publishes the disconnect Event in the environ. ``send`` bounds the
    cross-thread hand-off that a stalled reader would otherwise block forever.
    ``sender``, ``start_response`` and ``wsgi`` (including its
    ``finally: iterable.close()``) are inherited unchanged.

    It is also where the queue is measured: ``__call__`` stamps T0 and enters the
    depth count immediately before the submit, and ``_run_wsgi`` — the work item
    the pool actually runs — moves the request from queued to busy, sheds it if
    the client left while it waited, and releases the counters in a ``finally``.

    One responder is built per request, so the Event, the resolved timeout, the
    ``SendBlocked`` holder and the counter bookkeeping are per-request state.
    """

    def __init__(self, app: typing.Any, executor: typing.Any, send_queue_size: int) -> None:
        super().__init__(app, executor, send_queue_size)
        self.disconnected = threading.Event()
        self.send_timeout = _send_timeout()
        self.description = "request"
        self.send_blocked = SendBlocked()
        # None | "queued" | "running": which module counter this request is
        # currently held in. Guarded by _counter_lock.
        self._accounted: typing.Optional[str] = None

    def _enter_queue(self) -> None:
        """Count this request as submitted but not yet started."""
        global _queued
        with _counter_lock:
            _queued += 1
            self._accounted = "queued"

    def _enter_thread(self) -> None:
        """Move this request from the queue count to the busy-threads count."""
        global _queued, _running
        with _counter_lock:
            if self._accounted == "queued":
                _queued -= 1
                _running += 1
                self._accounted = "running"

    def _release(self) -> None:
        """Drop this request from whichever counter holds it.

        Idempotent, and called from the worker thread's ``finally`` — the
        request is done writing, so whichever state it was in is released here.
        A counter that only ever goes up is the failure this guards against.
        (``__call__``'s cancellation path must use :meth:`_release_if_queued`
        instead: a cancelled ASGI task must not release a request that is still
        running on a worker thread.)
        """
        global _queued, _running
        with _counter_lock:
            if self._accounted == "queued":
                _queued -= 1
            elif self._accounted == "running":
                _running -= 1
            self._accounted = None

    def _release_if_queued(self) -> None:
        """Release a request that never reached a worker thread, or do nothing.

        ``__call__``'s ``finally`` must NOT use plain ``_release()``. When the
        ASGI task is cancelled, cancelling the ``run_in_executor`` wrapper
        future succeeds even though the underlying work item may already be
        executing on a worker thread — cancelling the asyncio future does not
        cancel the ``concurrent.futures`` item behind it. Releasing the
        ``running`` count here would under-count busy threads for however long
        the abandoned request keeps draining upstream. So this releases the
        ``queued`` ticket only; if the worker thread already picked the item up,
        ``_run_wsgi``'s own ``finally`` owns the release once it truly finishes.
        Idempotent.
        """
        global _queued
        with _counter_lock:
            if self._accounted == "queued":
                _queued -= 1
                self._accounted = None

    def _shed_disconnected(self, environ: typing.Any, start_response: typing.Any) -> None:
        """Answer a queued request whose client already left, without the app."""
        global _shed_total
        with _counter_lock:
            _shed_total += 1
        _observe_shed(environ.get("PATH_INFO", ""))
        logger.info(
            "Client disconnected while %s waited for a WSGI worker; shedding it "
            "without running the application.",
            self.description,
        )
        start_response(
            "499 Client Closed Request",
            [("Content-Type", "text/plain"), ("Content-Length", "0")],
        )
        self.send({"type": "http.response.body", "body": b""})

    def _run_wsgi(self, environ: typing.Any, start_response: typing.Any) -> None:
        """The pool work item: accounting, the disconnect short-circuit, the app.

        Runs on a WSGI worker thread.
        """
        self._enter_thread()
        try:
            if self.disconnected.is_set():
                # The pump is created *before* run_in_executor (see __call__),
                # so anything that actually waited in the queue has an accurate
                # flag here: the client hung up while we were holding its
                # request. Nothing it can receive is worth producing, so skip
                # the application entirely — no preflight, no DB work, no
                # upstream generation.
                self._shed_disconnected(environ, start_response)
                return
            self.wsgi(environ, start_response)
        finally:
            self._release()

    def send(self, message: typing.Optional[typing.Any]) -> None:
        """Hand one ASGI message to the sender task, bounded by a timeout.

        The happy path is exactly the inherited one — the same single blocking
        wait on the same future, with a deadline attached, so normal streaming
        neither busy-loops nor slows down.
        """
        future = asyncio.run_coroutine_threadsafe(
            self.send_queue.put(message), loop=self.loop
        )
        blocked_from = time.monotonic()
        try:
            future.result(self.send_timeout)
        except concurrent.futures.TimeoutError:
            # Do not leave the put pending: it holds a reference to this
            # responder's queue and would deliver a chunk into a response that
            # is already being torn down.
            future.cancel()
            # The same signal the pump raises for a clean disconnect, so
            # everything downstream sees one notion of "client is gone".
            self.disconnected.set()
            logger.warning(
                "Client stopped reading %s after %.0fs (%s); aborting the response "
                "and releasing the WSGI worker thread. Raise %s if legitimate "
                "clients are being cut off.",
                self.description, self.send_timeout,
                "response body" if self.response_started else "response headers",
                SEND_TIMEOUT_ENV,
            )
            raise _StalledClient(self.description) from None
        finally:
            # In a ``finally`` so the timeout branch above is counted too. That
            # branch is where the largest block of a stalled client's life
            # happens; accumulating only on success would report ~0 for exactly
            # the outcome this measurement exists to identify.
            self.send_blocked.seconds += time.monotonic() - blocked_from

    async def __call__(self, scope: typing.Any, receive: typing.Any, send: typing.Any) -> None:
        queue: asyncio.Queue = asyncio.Queue(_BODY_QUEUE_SIZE)
        environ = build_environ(scope, _PumpedBody(self.loop, queue))
        environ[ENVIRON_KEY] = self.disconnected
        environ[SEND_BLOCKED_ENVIRON_KEY] = self.send_blocked
        self.description = "%s %s from %s" % (
            scope.get("method", "?"),
            scope.get("path", "?"),
            (scope.get("client") or ("-",))[0],
        )
        sender = None
        pump = None
        try:
            pump = self.loop.create_task(_pump(receive, queue, self.disconnected))
            sender = self.loop.create_task(self.sender(send))
            context = contextvars.copy_context()
            func = functools.partial(context.run, self._run_wsgi)
            self._enter_queue()
            # T0, stamped as late as possible so that the T1 a Flask
            # ``before_request`` takes measures the wait for a worker thread and
            # nothing else.
            environ[T0_ENVIRON_KEY] = time.monotonic()
            # Timezone-AWARE on purpose, and deliberately not
            # ``lumen.timeutils.utcnow()`` (which returns naive UTC, as CLAUDE.md
            # requires everywhere else): this instant is stored beside and
            # compared against ``request_logs.time``, which is TIMESTAMPTZ.
            # Naive UTC written into a TIMESTAMPTZ column is reinterpreted by
            # Postgres against the session TimeZone — a silent, deployment-
            # dependent offset. Same documented exception as request_logs.time.
            environ[STARTED_AT_ENVIRON_KEY] = datetime.now(timezone.utc)
            try:
                await self.loop.run_in_executor(
                    self.executor, func, environ, self.start_response
                )
            except _StalledClient:
                # ``send`` has already logged, set the disconnect Event and
                # cancelled its pending put; unwinding the WSGI thread ran
                # a2wsgi's ``finally: iterable.close()``, so the generator got
                # its GeneratorExit and did its abort accounting. Nothing is
                # left to do but let ``finally`` cancel the tasks.
                #
                # Returning rather than re-raising is deliberate. Re-raising
                # reaches uvicorn's ``run_asgi``, which logs "Exception in ASGI
                # application" with a full traceback and only then closes the
                # transport — an alarming stack dump for a routine dead client.
                # Returning leaves the response incomplete, which uvicorn
                # answers with a single "ASGI callable returned without
                # completing response." line and the same transport close. Same
                # cleanup, no traceback.
                #
                # And it must return *here*, before the two awaits below:
                # ``send_queue`` is full and the sender task is stuck in the
                # server's write drain, so ``put(None)`` and ``await sender``
                # would block for exactly as long as the send we just gave up on.
                return
            await self.send_queue.put(None)
            # Sender may raise an exception, so we need to await it
            await sender
            if self.exc_info is not None:
                raise self.exc_info[0].with_traceback(
                    self.exc_info[1], self.exc_info[2]
                )
        finally:
            self._release_if_queued()
            if pump and not pump.done():
                pump.cancel()
            if sender and not sender.done():
                sender.cancel()


class DisconnectAwareWSGIMiddleware(WSGIMiddleware):
    """Drop-in ``a2wsgi.WSGIMiddleware`` that reports client disconnects."""

    def __init__(self, app: typing.Any, workers: int = 10, send_queue_size: int = 10) -> None:
        super().__init__(app, workers=workers, send_queue_size=send_queue_size)
        # The pool size is only known here, and the depth counters are useless
        # without it — depth 8 means nothing until you know whether the pool has
        # 4 threads or 40.
        global _threads_total
        _threads_total = workers

    async def __call__(self, scope: typing.Any, receive: typing.Any, send: typing.Any) -> None:
        if scope["type"] == "http":
            responder = _DisconnectAwareWSGIResponder(
                self.app, self.executor, self.send_queue_size
            )
            return await responder(scope, receive, send)
        return await super().__call__(scope, receive, send)


def client_disconnect_event() -> threading.Event:
    """Return the current request's disconnect flag.

    Call this from view code while the request context is still live and
    capture the Event into the response generator's closure — the generators
    run context-free and must never touch ``request``.

    Falls back to an Event that is never set when there is no request context or
    the environ key is absent, so the Werkzeug dev server, the Flask test client,
    any non-ASGI deployment, and direct unit-test calls to the streaming helpers
    all keep working unchanged. Never raising matters: the callers are streaming
    views whose failure mode would otherwise be a 500 on a path that is supposed
    to be a pure observability improvement.
    """
    event = request.environ.get(ENVIRON_KEY) if has_request_context() else None
    return event if event is not None else threading.Event()
