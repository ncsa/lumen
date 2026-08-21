"""Tests for the in-flight request seam.

Two properties carry the weight. Admit/release must balance to zero on *every*
exit path, or the count drifts up forever; and multiplicity must be preserved —
one user with three concurrent requests is three in flight and one unique user,
not one of each.
"""
import logging
import socket
import threading
import time

import pytest
import redis

import lumen.services.live_state as live_state
from lumen.services.live_state import LiveTicket, LocalLiveState, ModelLive, get_live_state


def test_admit_then_release_balances_to_zero():
    state = LocalLiveState()
    ticket = state.admit("gpt-4o", 7)
    assert state.snapshot()["gpt-4o"].inflight == 1
    state.release(ticket)
    assert state.snapshot() == {}


@pytest.mark.parametrize("exit_path", ["normal", "exception", "client_disconnected"])
def test_every_streaming_exit_path_releases(exit_path):
    """The release lives in a ``finally`` around the generator body, so each of
    these — a completed stream, an upstream error, a client that went away
    mid-stream — must leave the state at zero."""
    state = LocalLiveState()

    def generate():
        ticket = state.admit("gpt-4o", 7)
        try:
            if exit_path == "exception":
                raise RuntimeError("upstream blew up")
            yield "chunk"
        finally:
            state.release(ticket)

    gen = generate()
    if exit_path == "exception":
        with pytest.raises(RuntimeError):
            list(gen)
    elif exit_path == "client_disconnected":
        next(gen)
        gen.close()
    else:
        list(gen)

    assert state.snapshot() == {}


@pytest.mark.parametrize("raises", [False, True])
def test_the_non_streaming_path_releases(raises):
    """The API non-stream and audio paths wrap the upstream call in the view."""
    state = LocalLiveState()
    ticket = state.admit("gpt-4o", 7)
    try:
        if raises:
            raise RuntimeError("upstream blew up")
    except RuntimeError:
        pass
    finally:
        state.release(ticket)
    assert state.snapshot() == {}


def test_three_requests_from_one_user_are_three_in_flight_and_one_user():
    """The multiplicity property: in-flight counts requests, unique_users counts
    users. Collapsing them loses the number Phase 9 needs."""
    state = LocalLiveState()
    tickets = [state.admit("gpt-4o", 7) for _ in range(3)]
    tickets.append(state.admit("gpt-4o", 8))

    live = state.snapshot()["gpt-4o"]
    assert live.inflight == 4
    assert live.unique_users == 2

    for ticket in tickets:
        state.release(ticket)
    assert state.snapshot() == {}


def test_models_are_counted_separately():
    state = LocalLiveState()
    state.admit("gpt-4o", 1)
    state.admit("llama", 1)
    snapshot = state.snapshot()
    assert snapshot["gpt-4o"].inflight == 1
    assert snapshot["llama"].inflight == 1


def test_a_ticket_past_its_deadline_stops_being_counted(monkeypatch):
    """A disconnected client's generator may never be closed, so ``finally`` may
    never run. Without the deadline that ticket would inflate the count forever.
    """
    import lumen.services.live_state as live_state

    monkeypatch.setattr(live_state, "_request_budget", lambda: -live_state._DEADLINE_GRACE - 1)
    state = LocalLiveState()
    state.admit("gpt-4o", 7)
    assert state.snapshot() == {}


def test_expired_tickets_are_pruned_on_admit(monkeypatch):
    import lumen.services.live_state as live_state

    monkeypatch.setattr(live_state, "_request_budget", lambda: -live_state._DEADLINE_GRACE - 1)
    state = LocalLiveState()
    state.admit("gpt-4o", 7)
    monkeypatch.setattr(live_state, "_request_budget", lambda: 600.0)
    state.admit("gpt-4o", 8)
    assert len(state._tickets) == 1


def test_release_needs_no_app_or_request_context(app):
    """``release`` runs on whichever thread iterates the response body, outside
    any Flask context, so it must not touch ``current_app`` or ``db.session``."""
    state = LocalLiveState()
    with app.app_context():
        ticket = state.admit("gpt-4o", 7)
    state.release(ticket)  # no context here on purpose
    assert state.snapshot() == {}


def test_release_of_an_unknown_ticket_is_a_no_op():
    state = LocalLiveState()
    state.release(LiveTicket("gpt-4o", 7, "never-admitted", deadline=1e12))
    assert state.snapshot() == {}


def test_concurrent_admit_release_balances():
    state = LocalLiveState()

    def worker(entity_id):
        for _ in range(50):
            state.release(state.admit("gpt-4o", entity_id))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert state.snapshot() == {}


def test_topology_is_local_and_reports_the_multiplier(monkeypatch):
    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    monkeypatch.setenv("LUMEN_REPLICAS", "2")
    assert LocalLiveState().topology() == {"scope": "local", "processes": 4, "replicas": 2}


def test_get_live_state_is_process_wide():
    assert get_live_state() is get_live_state()


# --- Phase 5: the Redis backend -------------------------------------------
#
# What is provable without a Redis server lives here: backend selection, the
# fail-open paths, the socket timeout (against a real socket that never
# answers — a fake that sleeps would prove nothing, since the bound is enforced
# by the socket, not by our wrapper), and the two-commands-per-request budget.
# The semantics that depend on how Redis itself behaves — multiplicity, expiry
# by score, the fleet-wide union — are in tests/integration/test_live_state_redis.py
# against a real server, because a fake cannot prove them.


class _BrokenRedis:
    """A Redis that has gone away: every call raises, as the pool does once it
    stops being able to reconnect."""

    def __getattr__(self, name):
        def boom(*args, **kwargs):
            raise redis.exceptions.ConnectionError("redis is gone")
        return boom


class _CountingRedis:
    """Records every command the backend issues, and nothing else."""

    def __init__(self):
        self.commands = []

    def zadd(self, key, mapping):
        self.commands.append(("ZADD", key))
        return 1

    def zrem(self, key, member):
        self.commands.append(("ZREM", key))
        return 1

    def scan_iter(self, match=None, count=None):
        self.commands.append(("SCAN", match))
        return iter(())

    def pipeline(self, transaction=False):
        raise AssertionError("no model keys, so no pipeline should be built")

    def ping(self):
        self.commands.append(("PING", None))
        return True


def _error_count(op):
    return live_state._live_state_errors.labels(op=op)._value.get()


@pytest.fixture
def blackhole_port():
    """A TCP port that accepts connections and then never sends a byte, so a
    read blocks until the socket timeout fires."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(16)
    held = []

    def accept_forever():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            held.append(conn)  # keep it open, answer nothing

    threading.Thread(target=accept_forever, daemon=True).start()
    yield server.getsockname()[1]
    server.close()
    for conn in held:
        conn.close()


def test_no_redis_url_means_per_process_state(app, monkeypatch, caplog):
    monkeypatch.setitem(app.config, "RATELIMIT_STORAGE_URI", None)
    with app.app_context(), caplog.at_level(logging.INFO, logger="lumen.services.live_state"):
        assert live_state._build_backend() is live_state._live_state
    assert "per-process" in caplog.text


def test_a_non_redis_storage_url_means_per_process_state(app, monkeypatch):
    monkeypatch.setitem(app.config, "RATELIMIT_STORAGE_URI", "memory://")
    with app.app_context():
        assert live_state._build_backend() is live_state._live_state


def test_a_redis_url_selects_the_redis_backend_without_connecting(app, monkeypatch):
    """Nothing is dialled at selection time: a Redis that is down at startup
    must cost a request only the fail-open path it already has."""
    monkeypatch.setitem(app.config, "RATELIMIT_STORAGE_URI", "redis://127.0.0.1:1/0")
    with app.app_context():
        state = live_state._build_backend()
    assert isinstance(state, live_state.RedisLiveState)


def test_the_client_carries_short_timeouts_and_no_retries(app, monkeypatch):
    """redis-py retries three times with backoff by default, which would
    multiply both the 0.25s bound and the two-command budget."""
    monkeypatch.setitem(app.config, "RATELIMIT_STORAGE_URI", "redis://127.0.0.1:1/0")
    with app.app_context():
        state = live_state._build_backend()
    kwargs = state._client.connection_pool.connection_kwargs
    assert kwargs["socket_timeout"] == 0.25
    assert kwargs["socket_connect_timeout"] == 0.25
    assert kwargs["retry"].get_retries() == 0


def test_get_live_state_decides_once_and_keeps_the_choice(app, monkeypatch):
    monkeypatch.setattr(live_state, "_backend", None)
    monkeypatch.setitem(app.config, "RATELIMIT_STORAGE_URI", "redis://127.0.0.1:1/0")
    with app.app_context():
        first = live_state.get_live_state()
        assert live_state.get_live_state() is first
    assert isinstance(first, live_state.RedisLiveState)


def test_a_broken_redis_does_not_fail_the_request(caplog):
    """The whole point of fail-open: an outage degrades the admin page, never a
    /v1/chat/completions."""
    state = live_state.RedisLiveState(_BrokenRedis(), fallback=LocalLiveState())
    before = {op: _error_count(op) for op in ("admit", "release", "snapshot", "topology")}

    with caplog.at_level(logging.WARNING, logger="lumen.services.live_state"):
        ticket = state.admit("gpt-4o", 7)
        # The ticket is usable, and while Redis is down the local fallback is
        # what the numbers come from.
        assert state.snapshot()["gpt-4o"] == ModelLive(inflight=1, unique_users=1)
        assert state.topology()["scope"] == "local"
        state.release(ticket)
        assert state.snapshot() == {}

    assert _error_count("admit") == before["admit"] + 1
    assert _error_count("release") == before["release"] + 1
    assert _error_count("snapshot") == before["snapshot"] + 2
    assert _error_count("topology") == before["topology"] + 1
    assert "fell back to local state" in caplog.text


def test_the_fail_open_admit_returns_a_ticket_release_still_accepts():
    """An asymmetric fail-open turns a blip into a permanent inflation — the bug
    wearing the costume of the fix."""
    state = live_state.RedisLiveState(_BrokenRedis(), fallback=LocalLiveState())
    ticket = state.admit("gpt-4o", 7)
    assert isinstance(ticket, LiveTicket)
    state.release(ticket)  # must not raise
    assert state.snapshot() == {}


def test_repeated_failures_warn_once_per_op_then_drop_to_debug(caplog):
    """300 orphaned requests after a pod kill must not be 300 lines in the
    incident."""
    state = live_state.RedisLiveState(_BrokenRedis(), fallback=LocalLiveState())
    with caplog.at_level(logging.WARNING, logger="lumen.services.live_state"):
        for _ in range(5):
            state.release(state.admit("gpt-4o", 7))
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2  # one for admit, one for release


def test_a_redis_that_never_answers_costs_a_quarter_second_not_the_request(
    app, monkeypatch, blackhole_port
):
    """The 0.25s socket timeout is the bound, and it is the socket that enforces
    it — which is why this test needs a real one."""
    monkeypatch.setitem(
        app.config, "RATELIMIT_STORAGE_URI", f"redis://127.0.0.1:{blackhole_port}/0"
    )
    with app.app_context():
        state = live_state._build_backend()

    started = time.monotonic()
    ticket = state.admit("gpt-4o", 7)
    state.release(ticket)
    elapsed = time.monotonic() - started

    # Two operations at 0.25s each. Anything near 5s means the timeout is not
    # being applied, or redis-py is retrying behind our back.
    assert elapsed < 2.0, f"a dead Redis added {elapsed:.2f}s to the request"
    assert state.snapshot() == {}


def test_a_request_costs_exactly_two_redis_commands():
    client = _CountingRedis()
    state = live_state.RedisLiveState(client, fallback=LocalLiveState())

    def generate():
        ticket = state.admit("gpt-4o", 7)
        try:
            yield "chunk one"
            yield "chunk two"
            yield "chunk three"
        finally:
            state.release(ticket)

    list(generate())
    assert client.commands == [("ZADD", "lumen:live:gpt-4o"), ("ZREM", "lumen:live:gpt-4o")]


def test_an_aborted_stream_also_costs_exactly_two_redis_commands():
    """The abort path is where a per-chunk command or a missing release would
    show up."""
    client = _CountingRedis()
    state = live_state.RedisLiveState(client, fallback=LocalLiveState())

    def generate():
        ticket = state.admit("gpt-4o", 7)
        try:
            yield "chunk one"
            yield "chunk two"
        finally:
            state.release(ticket)

    gen = generate()
    next(gen)
    gen.close()  # client went away mid-stream
    assert client.commands == [("ZADD", "lumen:live:gpt-4o"), ("ZREM", "lumen:live:gpt-4o")]


def test_pruning_is_the_readers_job_and_never_the_request_path():
    """``ZREMRANGEBYSCORE`` in ``admit`` is the obvious tidy-up, and it silently
    makes the per-request budget three."""
    client = _CountingRedis()
    state = live_state.RedisLiveState(client, fallback=LocalLiveState())
    for entity_id in range(20):
        state.release(state.admit("gpt-4o", entity_id))
    assert [c[0] for c in client.commands] == ["ZADD", "ZREM"] * 20
