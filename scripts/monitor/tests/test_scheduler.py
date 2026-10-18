import os
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from monitor import migrate
from monitor.config import ConfigStore
from monitor.db import make_engine, make_session_factory
from monitor.models import Probe
from monitor.probe import LiveSnapshot
from monitor.scheduler import Job, Scheduler, make_jobs, serve


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def recorder(calls, name, clock, fail=False):
    def run():
        calls.append((name, clock.now))
        if fail:
            raise RuntimeError(f"{name} broke")
    return run


def jobs(calls, clock, failing=()):
    return [
        Job(name, lambda seconds=seconds: timedelta(seconds=seconds), recorder(calls, name, clock, name in failing))
        for name, seconds in (("probe", 60), ("models", 300), ("price", 3600))
    ]


def drive(scheduler, clock, until, step=10):
    while clock.now <= until:
        scheduler.run_pending()
        clock.now += step


def counts(calls):
    return {name: sum(1 for n, _ in calls if n == name) for name in ("probe", "models", "price")}


def test_loops_run_on_their_cadences():
    calls, clock = [], Clock()
    drive(Scheduler(jobs(calls, clock), clock=clock), clock, 7200)
    assert counts(calls) == {"probe": 121, "models": 25, "price": 3}
    assert calls[:3] == [("probe", 0), ("models", 0), ("price", 0)]  # probe first, so models sees a fresh snapshot
    assert [t for n, t in calls if n == "price"] == [0, 3600, 7200]


def test_a_failing_loop_does_not_block_the_others(caplog):
    calls, clock = [], Clock()
    drive(Scheduler(jobs(calls, clock, failing={"probe", "models"}), clock=clock), clock, 600)
    assert counts(calls) == {"probe": 11, "models": 3, "price": 1}  # failing loops also keep their cadence
    assert "probe run failed" in caplog.text and "RuntimeError: models broke" in caplog.text


def test_an_overrun_runs_once_and_keeps_going():
    calls, clock = [], Clock()
    scheduler = Scheduler(jobs(calls, clock)[:1], clock=clock)
    scheduler.run_pending()
    clock.now = 250  # three probes missed
    scheduler.run_pending()
    scheduler.run_pending()
    assert counts(calls)["probe"] == 2
    assert scheduler.jobs[0].next_run == 310


def test_stop_skips_the_remaining_jobs():
    calls, clock = [], Clock()
    scheduler = Scheduler(jobs(calls, clock), clock=clock)
    scheduler.jobs[0].run = lambda: scheduler.stop.set()
    scheduler.run_pending()
    assert calls == []


def test_make_jobs_reads_cadences_from_the_config(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("refresh: {web: 2m, models: 10m, price: 2h}\n")
    store = ConfigStore(path, env={})
    probe, models, price = make_jobs(store, None, LiveSnapshot())
    assert [probe.name, models.name, price.name] == ["probe", "models", "price"]
    assert [j.interval() for j in (probe, models, price)] == [timedelta(minutes=2), timedelta(minutes=10), timedelta(hours=2)]
    path.write_text("refresh: {web: 1m, models: 5m, price: 30m}\n")
    store.reload()  # the price run reloads; the next reschedule uses the new cadence
    assert price.interval() == timedelta(minutes=30)


def test_serve_stops_on_sigterm():
    calls, ran = [], threading.Event()

    def run():
        calls.append(time.monotonic())
        if len(calls) == 2:
            ran.set()

    scheduler = Scheduler([Job("probe", lambda: timedelta(seconds=0.05), run)])
    threading.Thread(target=lambda: ran.wait(5) and os.kill(os.getpid(), signal.SIGTERM)).start()
    before = signal.getsignal(signal.SIGTERM)
    assert serve(scheduler) is True
    assert len(calls) >= 2 and scheduler.stop.is_set()
    assert signal.getsignal(signal.SIGTERM) is before


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_serve_reports_a_dead_scheduler_thread(caplog):
    def broken():
        raise TimeoutError("no cadence")

    scheduler = Scheduler([Job("probe", broken, lambda: None)])
    assert serve(scheduler) is False
    assert "scheduler thread exited unexpectedly" in caplog.text


def _closed_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_monitor_serve_runs_the_loops_and_exits_on_sigterm(tmp_path):
    url = f"sqlite:///{tmp_path / 'monitor.db'}"
    migrate.upgrade(url)
    config = tmp_path / "config.yaml"
    # Lumen refuses connections: probes fail fast, models skips, price fails on Lumen.
    config.write_text(
        f"database_url: {url}\n"
        f"lumen: {{url: 'http://127.0.0.1:{_closed_port()}'}}\n"
        "refresh: {web: 1s, models: 1s, price: 2s, timeout: 0.5s}\n"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("MONITOR_")}
    proc = subprocess.Popen(
        [sys.executable, "-m", "monitor.cli", "--config", str(config), "serve"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    engine = make_engine(url)
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            with make_session_factory(engine)() as session:
                if session.execute(select(func.count()).select_from(Probe)).scalar_one() >= 3:
                    break
            time.sleep(0.2)
        proc.send_signal(signal.SIGTERM)
        output, _ = proc.communicate(timeout=15)
    finally:
        if proc.poll() is None:
            proc.kill()
        engine.dispose()
    assert proc.returncode == 0, output
    assert output.count("probe failed") >= 3
    assert output.count("models run skipped") >= 2
    assert output.count("price run failed") >= 2
    assert "received SIGTERM, shutting down" in output and "scheduler stopped" in output
