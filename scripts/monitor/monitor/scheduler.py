"""`monitor serve`: one scheduler thread running the probe, models and price loops.

The loops run one after the other on the same thread, in that order when
several are due, so the models run sees the probe that just finished and the
price run sees its counters. Each loop's cadence is read from the current
config before it is rescheduled, so a config reload (done by the price run)
also changes the cadences. An exception in a loop is logged and the loop runs
again on its next turn; the other loops are not affected.

SIGTERM and SIGINT set a stop event: the loop that is running finishes (every
outbound request is bounded by `refresh.timeout`), then the thread exits. The
dashboard's HTTP server, when given, runs on a thread of its own in the same
process, so it reads the scheduler's live snapshot, and shuts down with it.
"""

import logging
import signal
import threading
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable

import requests
from sqlalchemy.orm import sessionmaker
from werkzeug.serving import BaseWSGIServer

from monitor.collect import collect_prices
from monitor.config import ConfigStore
from monitor.probe import LiveSnapshot, run_probe
from monitor.totals import run_models

log = logging.getLogger(__name__)


@dataclass
class Job:
    name: str
    interval: Callable[[], timedelta]
    run: Callable[[], object]
    next_run: float = 0.0


class Scheduler:
    """Runs due jobs in order on the calling thread until `stop` is set."""

    def __init__(self, jobs: list[Job], stop: threading.Event | None = None, clock=time.monotonic):
        self.jobs = jobs
        self.stop = stop or threading.Event()
        self.clock = clock

    def run_pending(self) -> None:
        for job in self.jobs:
            if self.stop.is_set():
                return
            now = self.clock()
            if now < job.next_run:
                continue
            try:
                job.run()
            except Exception:
                log.exception("%s run failed", job.name)
            # Keep the cadence; after an overrun, start a new one rather than catching up.
            interval = job.interval().total_seconds()
            job.next_run += interval
            if job.next_run <= self.clock():
                job.next_run = self.clock() + interval

    def run(self) -> None:
        log.info("scheduler started: %s", ", ".join(job.name for job in self.jobs))
        while not self.stop.is_set():
            self.run_pending()
            wait = min(job.next_run for job in self.jobs) - self.clock()
            if wait > 0:
                self.stop.wait(wait)
        log.info("scheduler stopped")


def _price(store: ConfigStore, session_factory: sessionmaker) -> None:
    try:
        collect_prices(store, session_factory)
    except (requests.RequestException, ValueError) as exc:
        log.error("price run failed: %s", exc)


def make_jobs(store: ConfigStore, session_factory: sessionmaker, snapshot: LiveSnapshot) -> list[Job]:
    """The probe (`refresh.web`), models (`refresh.models`) and price (`refresh.price`) loops."""
    # Cadences are read from store.current at every reschedule, since the config may reload.
    return [
        Job("probe", lambda: store.current.refresh.web, lambda: run_probe(store, session_factory, snapshot)),
        Job("models", lambda: store.current.refresh.models, lambda: run_models(store, session_factory, snapshot)),
        Job("price", lambda: store.current.refresh.price, lambda: _price(store, session_factory)),
    ]


def serve(scheduler: Scheduler, server: BaseWSGIServer | None = None) -> bool:
    """Run `scheduler` (and `server`) on their own threads until SIGTERM or SIGINT, then wait for them.

    Returns False when a thread died on its own, so the caller can exit non-zero.
    """
    signalled = threading.Event()

    def on_signal(signum, _frame):
        log.info("received %s, shutting down", signal.Signals(signum).name)
        signalled.set()
        scheduler.stop.set()

    previous = {sig: signal.signal(sig, on_signal) for sig in (signal.SIGTERM, signal.SIGINT)}
    thread = threading.Thread(target=scheduler.run, name="scheduler")
    thread.start()
    http = None
    if server is not None:
        http = threading.Thread(target=server.serve_forever, name="http", daemon=True)
        http.start()
        log.info("dashboard listening on http://%s:%d", server.host, server.port)
    try:
        while thread.is_alive() and (http is None or http.is_alive()):
            thread.join(0.5)
    finally:
        scheduler.stop.set()
        if server is not None:
            server.shutdown()
            server.server_close()
        thread.join()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    if not signalled.is_set():
        log.error("%s thread exited unexpectedly", "scheduler" if not thread.is_alive() else "http")
    return signalled.is_set()
