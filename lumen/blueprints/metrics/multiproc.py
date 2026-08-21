"""Multiprocess-mode housekeeping that must run BEFORE any metric is created.

This is a separate module for one reason, and it is not organisational taste:
``middleware.py`` constructs its ``Counter``/``Histogram`` objects at import
time, and ``prometheus_client`` opens and caches each mmap handle eagerly at
construction. Anything that has to happen before those files are opened
therefore cannot live in that module — importing it to call the function would
already have opened them.
"""

import os


def clear_own_stale_gauges():
    """Drop live-gauge files left by an earlier process that had this same PID.

    PID reuse is not exotic under a supervisor that respawns workers. The files
    are named ``gauge_{mode}_{pid}.db``, so a new worker drawing a recycled pid
    opens the SAME mmap and inherits the dead worker's values: a predecessor
    killed holding ``queue_depth=5`` hands its successor a permanent +5 offset
    on every inc/dec. ``reap_dead_workers`` cannot catch this — the pid probes as
    alive, because it is us — and an mtime cross-check cannot either, because we
    have just rewritten mtime.

    Uses ``mark_process_dead`` and never a blanket unlink of ``*_{pid}.db``.
    A blanket unlink would also remove this pid's COUNTER file, and a dead
    worker's counter increments are real history: dropping them makes the summed
    counter go backwards, which Prometheus reads as a counter reset. See the
    asymmetry note at the top of ``middleware.py``.

    No-ops when PROMETHEUS_MULTIPROC_DIR is unset, and never raises: this runs on
    the startup path, where a housekeeping failure must not stop the app booting.
    """
    if not os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        return
    try:
        # Imported here rather than at module scope so that importing this module
        # stays free of prometheus_client entirely when multiprocess mode is off.
        from prometheus_client.multiprocess import mark_process_dead
        mark_process_dead(os.getpid())
    except (FileNotFoundError, OSError):
        # Nothing to clear, or another process got there first. Both are fine.
        pass
    except Exception:  # pragma: no cover - defensive, startup path
        pass
