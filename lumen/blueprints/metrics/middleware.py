import logging
import re
import time

from flask.globals import _cv_app

# Use the default registry (no registry= kwarg) so prometheus_client's multiprocess
# mode is automatically engaged when PROMETHEUS_MULTIPROC_DIR is set.
from prometheus_client import Counter, Histogram

from lumen.services.ctx_probe import record_context_anomaly

logger = logging.getLogger(__name__)

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
        start = time.time()
        try:
            return _ContextCheckingBody(
                wsgi_app(environ, _start_response), method, path, baseline_ctx
            )
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
