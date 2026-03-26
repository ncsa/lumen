import re
import time

# Use the default registry (no registry= kwarg) so prometheus_client's multiprocess
# mode is automatically engaged when PROMETHEUS_MULTIPROC_DIR is set.
from prometheus_client import Counter, Histogram

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


def make_metrics_middleware(wsgi_app):
    def middleware(environ, start_response):
        path = _normalize_path(environ.get("PATH_INFO", ""))
        method = environ.get("REQUEST_METHOD", "")
        status_holder = ["500"]

        def _start_response(status, headers, exc_info=None):
            status_holder[0] = status.split(" ", 1)[0]
            return start_response(status, headers, exc_info)

        start = time.time()
        try:
            return wsgi_app(environ, _start_response)
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
