import logging
import re
import time
import weakref

from flask.globals import _cv_app

# Use the default registry (no registry= kwarg) so prometheus_client's multiprocess
# mode is automatically engaged when PROMETHEUS_MULTIPROC_DIR is set.
from prometheus_client import Counter, Histogram

from lumen.services.ctx_probe import describe_push, record_context_anomaly

logger = logging.getLogger(__name__)

# App contexts already reported as ambient-at-request-start, so a poisoned
# worker thread is reported once rather than on every subsequent request it
# serves. Weak so dead contexts do not pin memory or block id() reuse detection.
_reported_ambient: "weakref.WeakSet" = weakref.WeakSet()

_http_requests = Counter(
    "lumen_http_requests_total",
    "Total HTTP requests",
    ["method", "path_template", "status"],
)
_http_latency = Histogram(
    "lumen_http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["method", "path_template"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)


class _ContextCheckingBody:
    """Wraps the response iterable to verify context hygiene after close().

    The WSGI server's ``close()`` on the response body is the last moment a
    request executes code on its thread. Flask pops its contexts before
    ``wsgi_app`` returns (streaming responses pop when the body generator is
    closed), so the current app context must be back to whatever it was when
    the request started — compared against a baseline rather than None, since
    an ambient context is legitimate (test fixtures, nested dispatch). A
    context left on top of the baseline has escaped teardown: its DB session
    stays registered forever and its connection is stranded. This names the
    request that did it, at the moment it happens.
    """

    def __init__(self, iterable, method, path, baseline_ctx):
        self._iterable = iterable
        self._method = method
        self._path = path
        self._baseline_ctx = baseline_ctx

    def __iter__(self):
        return iter(self._iterable)

    def close(self):
        try:
            close = getattr(self._iterable, "close", None)
            if close is not None:
                close()
        finally:
            ctx = _cv_app.get(None)
            if ctx is not None and ctx is not self._baseline_ctx:
                record_context_anomaly(
                    "leftover-context", id(ctx), len(ctx._cv_tokens),
                    f"left behind by {self._method} {self._path}",
                )
                logger.warning(
                    "app context 0x%x (push depth %d) still current after %s %s "
                    "finished; teardown never ran for it and the DB session "
                    "registered under its scope is stranded",
                    id(ctx), len(ctx._cv_tokens), self._method, self._path,
                )


def make_metrics_middleware(wsgi_app):
    def middleware(environ, start_response):
        path = _normalize_path(environ.get("PATH_INFO", ""))
        method = environ.get("REQUEST_METHOD", "")
        status_holder = ["500"]

        def _start_response(status, headers, exc_info=None):
            status_holder[0] = status.split(" ", 1)[0]
            return start_response(status, headers, exc_info)

        baseline_ctx = _cv_app.get(None)
        # An app context already current when a request STARTS is a poisoned
        # worker thread: some earlier request pushed it and never popped, and
        # every session created while it is current is keyed to it — never torn
        # down. The close()-time check compares against this baseline and so is
        # blind to exactly this case; report it here instead, once per context.
        # (Legitimate in tests, where the client runs inside a fixture context.)
        if baseline_ctx is not None and baseline_ctx not in _reported_ambient:
            _reported_ambient.add(baseline_ctx)
            record_context_anomaly(
                "ambient-context-at-start", id(baseline_ctx), len(baseline_ctx._cv_tokens),
                # app id distinguishes a second Flask app object's context (its
                # sessions key elsewhere) from this app's (sessions key to it and
                # leak); the push provenance names whoever left it behind.
                f"already current when {method} {path} started; "
                f"app=0x{id(baseline_ctx.app):x}; {describe_push(baseline_ctx)}",
            )
            logger.warning(
                "app context 0x%x already current at the start of %s %s — this "
                "worker thread is poisoned; sessions keyed to it are never torn down",
                id(baseline_ctx), method, path,
            )
        start = time.time()
        try:
            body = wsgi_app(environ, _start_response)
        except BaseException:
            # No body, so the close()-time check below will never run; verify
            # here that the raising request did not abandon a context.
            ctx = _cv_app.get(None)
            if ctx is not None and ctx is not baseline_ctx:
                record_context_anomaly(
                    "leftover-context-after-exception", id(ctx), len(ctx._cv_tokens),
                    f"left behind by {method} {path} raising",
                )
                logger.warning(
                    "app context 0x%x still current after %s %s raised; teardown "
                    "never ran for it and its DB session is stranded",
                    id(ctx), method, path,
                )
            raise
        else:
            return _ContextCheckingBody(body, method, path, baseline_ctx)
        finally:
            _http_requests.labels(
                method=method,
                path_template=path,
                status=status_holder[0],
            ).inc()
            _http_latency.labels(
                method=method,
                path_template=path,
            ).observe(time.time() - start)

    return middleware


def _normalize_path(path):
    """Collapse numeric path segments to avoid high-cardinality label explosion."""
    return re.sub(r"/\d+", "/{id}", path)
