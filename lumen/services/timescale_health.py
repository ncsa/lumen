"""Read-only health check of the TimescaleDB objects Lumen relies on (#173).

Nothing else notices at runtime when the extension, the ``request_logs``
hypertable, a continuous aggregate or one of its background jobs is missing or
broken: a failing refresh job is silent, and an outage longer than an
aggregate's ``start_offset`` leaves a gap its policy never refills. :func:`check`
reads the catalog in at most four statements and :func:`problems` turns the
result into one message per problem.

A retention policy on ``request_logs`` is never an expected job. It is reported
through its own flag, with the same meaning as ``_retention_job`` in
``lumen/commands.py``, so the startup tripwire and this check cannot disagree.
Jobs are matched on ``(proc_name, target)``, so the 30-day policy on
``request_metrics_1m`` can never be read as pruning ``request_logs``.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy import text

HYPERTABLE = "request_logs"
AGGREGATES = ("request_counts_hourly", "request_counts_hourly_by_entity", "request_metrics_1m")

#: Every background job the migration chain creates, by a stable name.
EXPECTED_JOBS = {
    **{f"refresh:{view}": ("policy_refresh_continuous_aggregate", view) for view in AGGREGATES},
    "compression:request_logs": ("policy_compression", HYPERTABLE),
    "retention:request_metrics_1m": ("policy_retention", "request_metrics_1m"),
}

EXTENSION_MISSING = (
    "TimescaleDB is required, but the timescaledb extension is not installed in this "
    "database. Run 'flask db upgrade' against a TimescaleDB image."
)
HYPERTABLE_MISSING = (
    "request_logs is not a TimescaleDB hypertable; the usage charts and their "
    "continuous aggregates depend on it."
)
REQUEST_LOGS_RETENTION = (
    "request_logs has a retention policy, but request_logs must never be pruned. "
    "Remove it with SELECT remove_retention_policy('request_logs');"
)

_EXTENSION_SQL = text(
    "SELECT e.extversion, a.default_version FROM pg_available_extensions a "
    "LEFT JOIN pg_extension e ON e.extname = a.name WHERE a.name = 'timescaledb'"
)
_HYPERTABLE_SQL = text(
    "SELECT EXISTS (SELECT 1 FROM timescaledb_information.hypertables "
    "WHERE hypertable_name = :t)"
)
_AGGREGATES_SQL = text(
    "SELECT view_name, materialized_only, materialization_hypertable_name "
    "FROM timescaledb_information.continuous_aggregates"
)
_JOBS_SQL = text(
    "SELECT j.job_id, j.proc_name, j.hypertable_name, s.last_run_status, "
    "s.total_failures, s.last_successful_finish "
    "FROM timescaledb_information.jobs j "
    "LEFT JOIN timescaledb_information.job_stats s ON s.job_id = j.job_id "
    "ORDER BY j.job_id"
)


@dataclass(frozen=True)
class Aggregate:
    view: str
    materialized_only: bool
    materialization_hypertable: Optional[str]


@dataclass(frozen=True)
class Job:
    job_id: int
    proc_name: str
    #: The hypertable or continuous aggregate the job acts on. A job reported
    #: against a materialisation hypertable is mapped back to its view.
    target: Optional[str]
    last_run_status: Optional[str]
    total_failures: Optional[int]
    last_successful_finish: Optional[datetime]


@dataclass(frozen=True)
class TimescaleHealth:
    installed_version: Optional[str]
    available_version: Optional[str]
    request_logs_hypertable: bool = False
    #: The expected aggregates that exist, by view name.
    aggregates: tuple[Aggregate, ...] = ()
    #: The expected jobs that exist, by their ``EXPECTED_JOBS`` name.
    jobs: tuple[tuple[str, Job], ...] = ()
    request_logs_retention: bool = False

    @property
    def extension_installed(self) -> bool:
        return self.installed_version is not None

    def job(self, name: str) -> Optional[Job]:
        return dict(self.jobs).get(name)

    def aggregate(self, view: str) -> Optional[Aggregate]:
        return next((a for a in self.aggregates if a.view == view), None)


def check(conn) -> TimescaleHealth:
    """Read the catalog in at most four read-only statements. ``conn`` is a session or connection.

    Without the extension the ``timescaledb_information`` views do not exist, so
    only the first statement runs.
    """
    row = conn.execute(_EXTENSION_SQL).first()
    installed, available = (row[0], row[1]) if row else (None, None)
    if installed is None:
        return TimescaleHealth(installed_version=None, available_version=available)

    hypertable = bool(conn.execute(_HYPERTABLE_SQL, {"t": HYPERTABLE}).scalar())

    all_aggregates = [Aggregate(view, bool(mat_only), mat_table)
                      for view, mat_only, mat_table in conn.execute(_AGGREGATES_SQL).all()]
    # A cagg policy may be reported against its materialisation hypertable rather
    # than the view; both name the same aggregate.
    view_of = {a.materialization_hypertable: a.view for a in all_aggregates
               if a.materialization_hypertable}

    jobs: dict[str, Job] = {}
    retention_on_request_logs = False
    for job_id, proc_name, hypertable_name, status, failures, last_success in conn.execute(_JOBS_SQL).all():
        target = view_of.get(hypertable_name, hypertable_name)
        if (proc_name, target) == ("policy_retention", HYPERTABLE):
            retention_on_request_logs = True
            continue
        for name, expected in EXPECTED_JOBS.items():
            if (proc_name, target) == expected and name not in jobs:
                jobs[name] = Job(job_id, proc_name, target, status, failures, last_success)

    return TimescaleHealth(
        installed_version=installed,
        available_version=available,
        request_logs_hypertable=hypertable,
        aggregates=tuple(a for a in all_aggregates if a.view in AGGREGATES),
        jobs=tuple((name, jobs[name]) for name in EXPECTED_JOBS if name in jobs),
        request_logs_retention=retention_on_request_logs,
    )


def problems(health: TimescaleHealth) -> list[str]:
    """One message per problem, in a stable order; empty when everything is healthy."""
    if not health.extension_installed:
        return [EXTENSION_MISSING]
    found = []
    if health.available_version and health.installed_version != health.available_version:
        found.append(
            f"TimescaleDB extension is {health.installed_version} but the server has "
            f"{health.available_version}. Run ALTER EXTENSION timescaledb UPDATE; as the "
            "first command of a new psql -X session."
        )
    if not health.request_logs_hypertable:
        found.append(HYPERTABLE_MISSING)
    for view in AGGREGATES:
        agg = health.aggregate(view)
        if agg is None:
            found.append(f"Continuous aggregate {view} is missing.")
        elif agg.materialized_only:
            found.append(
                f"Continuous aggregate {view} has materialized_only = true, so it does not "
                "return the buckets after its last refresh."
            )
    for name in EXPECTED_JOBS:
        job = health.job(name)
        if job is None:
            proc, target = EXPECTED_JOBS[name]
            found.append(f"Background job {name} ({proc} on {target}) is missing.")
        elif job.last_run_status == "Failed":
            found.append(
                f"Background job {name} (job {job.job_id}) failed its last run; "
                f"{job.total_failures or 0} failures in total."
            )
    if health.request_logs_retention:
        found.append(REQUEST_LOGS_RETENTION)
    return found
