"""Python reference for the SQL bucketing.

The dashboard buckets rows in SQL (`bucket_index()`, `status_rank()`,
`rank_status()` in `monitor.buckets`). These plain-Python versions say what
that SQL must return, and the tests compare the two.
"""

from monitor.buckets import DEGRADED, STATUS_PRIORITY, UNKNOWN, DayGrid, Grid


def worst(statuses) -> str:
    """The worst of `statuses` by `STATUS_PRIORITY`; `unknown` when there are none.

    A status Lumen reports that is not in `STATUS_PRIORITY` (e.g. `starting`)
    ranks below ok; a bucket with only such statuses is degraded, so the strip
    draws what uptime counts: the model reported, but was not ok.
    """
    found = list(statuses)
    if not found:
        return UNKNOWN
    result = max(found, key=lambda s: STATUS_PRIORITY.get(s, 0))
    return result if result in STATUS_PRIORITY else DEGRADED


def bucket_statuses(points, grid: Grid | DayGrid) -> list[str]:
    """Worst status per bucket of `(time, status)` points; points outside the grid are ignored."""
    buckets: list[list[str]] = [[] for _ in range(grid.count)]
    for at, status in points:
        i = grid.index(at)
        if i is not None:
            buckets[i].append(status)
    return [worst(b) for b in buckets]
