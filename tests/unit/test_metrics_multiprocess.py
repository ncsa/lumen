"""Multi-process metric integrity: aggregation, and reaping workers that died.

Under PROMETHEUS_MULTIPROC_DIR every process writes its own mmap files and the
scrape merges them. Two things have to hold for the merged numbers to be true at
N processes: the merge must SUM counters across processes, and a worker that
died without running ``mark_process_dead`` (SIGKILL, OOM kill) must stop
contributing to live gauges — while still contributing its counters, which are
real history.
"""
import os
import subprocess
import sys
import threading

import pytest

# A fresh interpreter per child: prometheus_client picks its value class at
# import time from PROMETHEUS_MULTIPROC_DIR, so a forked child of this test
# process would already have the single-process class bound.
_CHILD = """
import os, sys
from prometheus_client import Counter, Gauge
amount = float(sys.argv[1])
Counter("lumen_mp_probe", "probe counter").inc(amount)
Gauge("lumen_mp_probe_live", "probe gauge", multiprocess_mode="livesum").set(amount)
print(os.getpid())
"""


def _run_child(multiproc_dir, amount):
    """Increment the probe metrics in a separate process; return its (now dead) pid."""
    env = {**os.environ, "PROMETHEUS_MULTIPROC_DIR": str(multiproc_dir)}
    result = subprocess.run(
        [sys.executable, "-c", _CHILD, str(amount)],
        env=env, capture_output=True, text=True, check=True,
    )
    return int(result.stdout.strip())


def _collect(multiproc_dir):
    from prometheus_client import CollectorRegistry
    from prometheus_client.multiprocess import MultiProcessCollector

    registry = CollectorRegistry()
    MultiProcessCollector(registry, path=str(multiproc_dir))
    return registry


def _dead_pid():
    """A pid that is certainly gone: a child that exited and has been reaped."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_two_processes_are_summed_and_dead_gauges_are_dropped(tmp_path):
    """The whole Phase 1 claim in one test.

    Aggregation: a third process's MultiProcessCollector reports the SUM of what
    two other processes recorded. Then the asymmetry: after mark_process_dead,
    the dead worker's counter is STILL summed — those requests really happened —
    while its livesum gauge is gone, because a gauge is a claim about now.
    """
    from prometheus_client.multiprocess import mark_process_dead

    pid_a = _run_child(tmp_path, 3)
    pid_b = _run_child(tmp_path, 4)
    assert pid_a != pid_b

    registry = _collect(tmp_path)
    assert registry.get_sample_value("lumen_mp_probe_total") == 7.0
    assert registry.get_sample_value("lumen_mp_probe_live") == 7.0

    mark_process_dead(pid_a, str(tmp_path))

    registry = _collect(tmp_path)
    assert registry.get_sample_value("lumen_mp_probe_total") == 7.0, (
        "a dead process's counter file must still be summed — its increments are "
        "real history and dropping them silently loses counts"
    )
    assert registry.get_sample_value("lumen_mp_probe_live") == 4.0, (
        "the dead process's livesum contribution must be gone"
    )


def test_reap_marks_every_dead_pid_and_leaves_counters_alone(tmp_path, monkeypatch):
    """The reaper is what covers SIGKILL: nothing ran mark_process_dead, so the
    dead workers' gauge files are still there at scrape time."""
    from lumen.blueprints.metrics.middleware import reap_dead_workers

    pid_a = _run_child(tmp_path, 3)
    pid_b = _run_child(tmp_path, 4)
    assert _collect(tmp_path).get_sample_value("lumen_mp_probe_live") == 7.0

    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))
    reap_dead_workers()

    names = os.listdir(tmp_path)
    assert f"gauge_livesum_{pid_a}.db" not in names
    assert f"gauge_livesum_{pid_b}.db" not in names
    assert f"counter_{pid_a}.db" in names and f"counter_{pid_b}.db" in names
    assert _collect(tmp_path).get_sample_value("lumen_mp_probe_total") == 7.0


def test_reap_does_not_touch_a_live_process(tmp_path, monkeypatch):
    """Reaping the current process's own files would erase live gauges."""
    from lumen.blueprints.metrics.middleware import reap_dead_workers

    mine = tmp_path / f"gauge_livesum_{os.getpid()}.db"
    mine.write_bytes(b"")
    dead = tmp_path / f"gauge_livesum_{_dead_pid()}.db"
    dead.write_bytes(b"")

    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))
    reap_dead_workers()

    assert mine.exists()
    assert not dead.exists()


def test_reap_is_a_noop_without_the_env_var(monkeypatch):
    from lumen.blueprints.metrics.middleware import reap_dead_workers

    monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
    reap_dead_workers()  # must not raise


def test_reap_survives_a_missing_directory(tmp_path, monkeypatch):
    from lumen.blueprints.metrics.middleware import reap_dead_workers

    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path / "gone"))
    reap_dead_workers()  # must not raise


def test_reap_swallows_a_losing_race_on_the_same_pid(tmp_path, monkeypatch):
    """mark_process_dead does an unguarded glob + os.remove.

    Two processes reaping the same pid means the loser gets FileNotFoundError —
    and the reap runs inside the /metrics handler, so an unhandled raise is a 500
    on the scrape, right after a worker died, which is when the scrape matters
    most. Forced deterministically here rather than hoping the race lands.
    """
    from lumen.blueprints.metrics import middleware as mw

    (tmp_path / f"gauge_livesum_{_dead_pid()}.db").write_bytes(b"")
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))

    calls = []

    def losing_race(pid, path=None):
        calls.append(pid)
        raise FileNotFoundError(path)

    monkeypatch.setattr(mw, "mark_process_dead", losing_race)
    mw.reap_dead_workers()  # must not raise
    assert calls, "the dead pid was not even attempted"


def test_concurrent_reaps_of_the_same_pids_do_not_raise(tmp_path, monkeypatch):
    """The real scenario: every worker plus the scrape reaping the same dir."""
    from lumen.blueprints.metrics.middleware import reap_dead_workers

    for _ in range(5):
        pid = _dead_pid()
        for mode in ("livesum", "liveall"):
            (tmp_path / f"gauge_{mode}_{pid}.db").write_bytes(b"")
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))

    errors = []
    barrier = threading.Barrier(8)

    def reap():
        try:
            barrier.wait()
            reap_dead_workers()
        except BaseException as exc:  # noqa: BLE001 — the point is that none escape
            errors.append(exc)

    threads = [threading.Thread(target=reap) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert not [n for n in os.listdir(tmp_path) if n.startswith("gauge_")]


@pytest.mark.parametrize("name", ["counter_1234.db", "gauge_livesum_1234.db"])
def test_pid_suffix_is_recognised(name):
    from lumen.blueprints.metrics.middleware import _PID_SUFFIXED

    assert _PID_SUFFIXED.search(name).group(1) == "1234"


class TestClearOwnStaleGauges:
    """PID reuse: a recycled pid must not inherit its predecessor's gauge values.

    ``reap_dead_workers`` structurally cannot cover this — the pid probes as
    alive, because it is us.
    """

    def test_removes_own_live_gauge_files(self, tmp_path, monkeypatch):
        from lumen.blueprints.metrics.multiproc import clear_own_stale_gauges

        mine = os.getpid()
        for mode in ("livesum", "liveall"):
            (tmp_path / f"gauge_{mode}_{mine}.db").write_bytes(b"")
        monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))

        clear_own_stale_gauges()

        assert not [n for n in os.listdir(tmp_path) if n.startswith("gauge_")]

    def test_keeps_own_counter_file(self, tmp_path, monkeypatch):
        """A blanket unlink would take the counter too, and that is history.

        Losing it makes the summed counter go backwards, which Prometheus reads
        as a counter reset.
        """
        from lumen.blueprints.metrics.multiproc import clear_own_stale_gauges

        mine = os.getpid()
        counter = tmp_path / f"counter_{mine}.db"
        counter.write_bytes(b"")
        (tmp_path / f"gauge_livesum_{mine}.db").write_bytes(b"")
        monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))

        clear_own_stale_gauges()

        assert counter.exists(), "counter file must survive; its increments are real history"

    def test_leaves_other_pids_alone(self, tmp_path, monkeypatch):
        from lumen.blueprints.metrics.multiproc import clear_own_stale_gauges

        other = tmp_path / "gauge_livesum_999999.db"
        other.write_bytes(b"")
        (tmp_path / f"gauge_livesum_{os.getpid()}.db").write_bytes(b"")
        monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))

        clear_own_stale_gauges()

        assert other.exists()

    def test_no_op_and_silent_without_the_env_var(self, tmp_path, monkeypatch):
        from lumen.blueprints.metrics.multiproc import clear_own_stale_gauges

        monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
        clear_own_stale_gauges()  # must not raise

    def test_never_raises_when_the_library_fails(self, tmp_path, monkeypatch):
        """Runs on the startup path — housekeeping must not stop the app booting."""
        from lumen.blueprints.metrics import multiproc

        monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))
        import prometheus_client.multiprocess as pcm

        def boom(*a, **k):
            raise FileNotFoundError("raced")

        monkeypatch.setattr(pcm, "mark_process_dead", boom)
        multiproc.clear_own_stale_gauges()  # must not raise


def test_reap_marks_each_dead_pid_before_probing_the_next(tmp_path, monkeypatch):
    """The window between "this pid is dead" and "delete its files" must be empty.

    pids are recycled. If the reaper probes every pid first and only then deletes,
    a supervisor can respawn a worker onto a pid that was observed dead moments
    earlier; the new worker constructs its gauges, and the reaper's later
    mark_process_dead removes the *live* worker's files. It never recreates them
    — the mmap still refers to the unlinked inode, so the worker writes to a file
    no MultiProcessCollector glob can see, and its gauges are invisible for the
    rest of the pod's life.

    Asserting on ordering rather than on the race itself: the race needs a pid
    collision to reproduce and would be flaky, but the invariant that makes it
    impossible is exact and cheap to check.
    """
    import lumen.blueprints.metrics.middleware as mw

    dead_pids = [_dead_pid() for _ in range(3)]
    for pid in dead_pids:
        (tmp_path / f"gauge_livesum_{pid}.db").write_bytes(b"")

    calls = []
    real_kill = os.kill

    def spy_kill(pid, sig):
        calls.append(("probe", pid))
        return real_kill(pid, sig)

    monkeypatch.setattr(mw.os, "kill", spy_kill)
    monkeypatch.setattr(mw, "mark_process_dead", lambda pid, path: calls.append(("mark", pid)))
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))

    mw.reap_dead_workers()

    assert len(calls) == 2 * len(dead_pids), calls
    for i in range(0, len(calls), 2):
        probe, mark = calls[i], calls[i + 1]
        assert probe[0] == "probe" and mark[0] == "mark", calls
        assert probe[1] == mark[1], (
            f"pid {probe[1]} was probed but {mark[1]} was marked next; collecting "
            "the dead pids and marking them in a second pass reopens the "
            "recycled-pid window this test exists to close"
        )


def _config_with_prometheus(tmp_path):
    """The suite's own config plus an enabled Prometheus and no multiproc dir.

    Built from the real fixture rather than a minimal dict because create_app
    exits before it reaches the Prometheus block if config.yaml declares no
    active models.
    """
    import yaml

    from tests.conftest import TEST_CONFIG

    data = yaml.safe_load(open(TEST_CONFIG))
    api = dict(data.get("api", {}))
    api["prometheus"] = {"enabled": True, "token": "t" * 32}
    data["api"] = api
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump(data))
    return cfg


def test_multiple_workers_without_a_multiproc_dir_warns(tmp_path, monkeypatch, caplog):
    """The misconfiguration that makes /metrics quietly wrong.

    prometheus_client only enters multiprocess mode when PROMETHEUS_MULTIPROC_DIR
    is set. Without it each worker keeps a private registry, so a scrape returns
    whichever worker happened to answer — counters appear to jump backwards at
    random, which Prometheus reads as a reset. Nothing raises and nothing is
    logged, so the numbers are simply wrong. The chart README already says
    wsgiProcesses > 1 requires multiprocDir; nothing enforced it.
    """
    import logging

    cfg = _config_with_prometheus(tmp_path)
    # Config reads CONFIG_YAML into a class attribute at import time, so the
    # env var is already fixed by the session app fixture; patch the attribute.
    from config import Config
    monkeypatch.setattr(Config, "CONFIG_YAML", str(cfg))
    monkeypatch.setattr(Config, "SQLALCHEMY_DATABASE_URI", f"sqlite:///{tmp_path / 'warn.db'}")
    monkeypatch.setenv("BACKGROUND_WORKER", "false")
    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)

    from lumen import create_app
    with caplog.at_level(logging.WARNING):
        create_app()

    assert any("multiproc_dir" in r.message for r in caplog.records), (
        "four workers with no shared directory must not be silent"
    )


def test_a_single_worker_without_a_multiproc_dir_is_silent(tmp_path, monkeypatch, caplog):
    """Production is 1x1, where single-process mode is exactly right.

    A warning every startup for the correct configuration is how warnings stop
    being read.
    """
    import logging

    cfg = _config_with_prometheus(tmp_path)
    from config import Config
    monkeypatch.setattr(Config, "CONFIG_YAML", str(cfg))
    monkeypatch.setattr(Config, "SQLALCHEMY_DATABASE_URI", f"sqlite:///{tmp_path / 'quiet.db'}")
    monkeypatch.setenv("BACKGROUND_WORKER", "false")
    monkeypatch.setenv("WEB_CONCURRENCY", "1")
    monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)

    from lumen import create_app
    with caplog.at_level(logging.WARNING):
        create_app()

    assert not any("multiproc_dir" in r.message for r in caplog.records)
