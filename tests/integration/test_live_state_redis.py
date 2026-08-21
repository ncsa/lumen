"""Phase 5's Redis backend, against a real Redis.

These are separated from ``tests/unit/test_live_state.py`` because the
properties under test *are* Redis behaviour. A fake can prove that we issue two
commands; only a real server can prove that the two commands mean what the
design says they mean — that three concurrent requests from one student are
three members with one shared prefix, that a member whose deadline has passed is
excluded from the count by its *score* and not by any key TTL, and that two
processes writing the same sorted set produce the union of their users rather
than the sum.

Set ``LUMEN_TEST_REDIS_URL`` (same shape as ``LUMEN_TEST_POSTGRES_URL`` in
``conftest.py``) to run them; they skip otherwise. Every key this file touches
lives under a per-run prefix, so pointing the variable at a shared Redis cannot
disturb anything else in it.
"""
import os
import time
import uuid

import pytest
import redis

import lumen.services.live_state as live_state
from lumen.services.live_state import LocalLiveState, ModelLive, RedisLiveState

REDIS_URL_ENV = "LUMEN_TEST_REDIS_URL"

pytestmark = pytest.mark.skipif(
    not os.environ.get(REDIS_URL_ENV),
    reason=f"{REDIS_URL_ENV} is not set; skipping Redis live-state tests",
)


def _client():
    return redis.Redis.from_url(
        os.environ[REDIS_URL_ENV],
        decode_responses=True,
        socket_timeout=live_state._REDIS_TIMEOUT,
        socket_connect_timeout=live_state._REDIS_TIMEOUT,
    )


@pytest.fixture(autouse=True)
def isolated_prefix(monkeypatch):
    """A per-test key namespace, so a shared Redis is safe to point at."""
    prefix = f"lumen:live:test-{uuid.uuid4().hex[:12]}:"
    monkeypatch.setattr(live_state, "_KEY_PREFIX", prefix)
    yield prefix
    admin = _client()
    keys = list(admin.scan_iter(match=prefix + "*", count=500))
    if keys:
        admin.delete(*keys)
    admin.close()


@pytest.fixture
def raw():
    """A plain client, for looking at what the backend actually wrote."""
    client = _client()
    yield client
    client.close()


@pytest.fixture
def state():
    backend = RedisLiveState(_client(), fallback=LocalLiveState())
    yield backend
    backend._client.close()


def _make_backend(kind):
    if kind == "local":
        return LocalLiveState()
    return RedisLiveState(_client(), fallback=LocalLiveState())


# --- the parametrised suite: both backends must produce identical numbers ---
#
# Without this the two implementations drift and the fallback silently changes
# what a number means, rather than only how far it reaches.


@pytest.fixture(params=["local", "redis"])
def backend(request):
    return _make_backend(request.param)


def test_admit_then_release_balances_to_zero(backend):
    ticket = backend.admit("gpt-4o", 7)
    assert backend.snapshot()["gpt-4o"] == ModelLive(inflight=1, unique_users=1)
    backend.release(ticket)
    assert backend.snapshot() == {}


def test_multiplicity_is_identical_on_both_backends(backend):
    """The property the rejected SET design got wrong: one user, three requests.

    A ``SREM`` on a bare entity id would drop the user on the first completion
    while two requests were still running — under-reporting exactly the student
    a class-start burst is full of.
    """
    tickets = [backend.admit("gpt-4o", 7) for _ in range(3)]
    tickets.append(backend.admit("gpt-4o", 8))

    assert backend.snapshot()["gpt-4o"] == ModelLive(inflight=4, unique_users=2)

    backend.release(tickets.pop())  # entity 8 leaves
    assert backend.snapshot()["gpt-4o"] == ModelLive(inflight=3, unique_users=1)

    backend.release(tickets.pop())  # one of entity 7's three
    assert backend.snapshot()["gpt-4o"] == ModelLive(inflight=2, unique_users=1)

    for ticket in tickets:
        backend.release(ticket)
    assert backend.snapshot() == {}


def test_models_are_counted_separately_on_both_backends(backend):
    backend.admit("gpt-4o", 1)
    backend.admit("llama", 1)
    snapshot = backend.snapshot()
    assert snapshot["gpt-4o"] == ModelLive(inflight=1, unique_users=1)
    assert snapshot["llama"] == ModelLive(inflight=1, unique_users=1)


def test_an_expired_ticket_is_identical_on_both_backends(backend, monkeypatch):
    monkeypatch.setattr(
        live_state, "_request_budget", lambda: -live_state._DEADLINE_GRACE - 1
    )
    backend.admit("gpt-4o", 7)
    assert backend.snapshot() == {}


def test_release_of_an_unknown_ticket_is_a_no_op_on_both_backends(backend):
    backend.release(live_state.LiveTicket("gpt-4o", 7, "never-admitted", deadline=1e12))
    assert backend.snapshot() == {}


# --- fleet-wide behaviour, which only a shared server can show --------------


def test_unique_users_is_the_union_across_processes_not_the_sum(raw, isolated_prefix):
    """Two processes, overlapping users, and one user submitting three times.

    Sum would be 6 users; the union is 4. The in-flight count is the total
    number of requests, which is a different number again — collapsing the two
    is what the redesign exists to prevent.
    """
    process_a = RedisLiveState(_client(), fallback=LocalLiveState())
    process_b = RedisLiveState(_client(), fallback=LocalLiveState())

    a_tickets = [process_a.admit("gpt-4o", e) for e in (1, 2, 3)]
    b_tickets = [process_b.admit("gpt-4o", e) for e in (3, 4)]
    # Entity 3 has one request on A and one on B, plus two more on B.
    b_tickets += [process_b.admit("gpt-4o", 3) for _ in range(2)]

    for reader in (process_a, process_b):
        live = reader.snapshot()["gpt-4o"]
        assert live.unique_users == 4, "unique users must be the union, not the sum"
        assert live.inflight == 7, "in flight counts requests, not users"

    # Releasing one of entity 3's four requests changes the request count and
    # nothing else. This assertion is the entire point of the redesign.
    process_b.release(b_tickets.pop())
    live = process_a.snapshot()["gpt-4o"]
    assert live.inflight == 6
    assert live.unique_users == 4

    for ticket in a_tickets + b_tickets:
        (process_a if ticket in a_tickets else process_b).release(ticket)
    assert process_a.snapshot() == {}
    assert raw.exists(isolated_prefix + "gpt-4o") == 0, (
        "Redis drops a sorted set when its last member goes, so an idle model "
        "must leave nothing behind"
    )


def test_a_member_past_its_deadline_is_excluded_by_score_before_any_prune(
    state, raw, isolated_prefix, monkeypatch
):
    """Self-healing is by score, not by TTL and not by the prune.

    The prune is disabled here (replaced with a harmless read that keeps the
    pipeline's shape) so the only thing that can exclude the orphan is the
    ``now`` bound on the read. A SIGKILLed process's tickets therefore stop
    counting the instant their deadline passes, whatever happens to the prune.
    """
    monkeypatch.setattr(
        live_state, "_request_budget", lambda: -live_state._DEADLINE_GRACE - 1
    )
    for _ in range(3):
        state.admit("gpt-4o", 7)  # a killed process's three orphans
    monkeypatch.undo()

    key = isolated_prefix + "gpt-4o"
    assert raw.zcard(key) == 3, "the orphans are physically present"

    monkeypatch.setattr(
        redis.client.Pipeline,
        "zremrangebyscore",
        lambda self, name, min, max: self.zcard(name),
    )
    assert state.snapshot() == {}, "excluded by score with the prune disabled"
    assert raw.zcard(key) == 3, "and the prune really was disabled"

    monkeypatch.undo()
    assert state.snapshot() == {}
    assert raw.zcard(key) == 0, "physically gone after a pass that does prune"


def test_orphans_still_count_until_their_deadline_passes(state, raw, isolated_prefix):
    """Before the deadline nothing can know the process is dead, so counting
    them is the honest answer, not a leak."""
    tickets = [state.admit("gpt-4o", 7) for _ in range(3)]
    # The process holding these is now gone: nobody will ever call release.
    assert state.snapshot()["gpt-4o"] == ModelLive(inflight=3, unique_users=1)

    scores = raw.zscore(
        isolated_prefix + "gpt-4o", f"{tickets[0].entity_id}:{tickets[0].request_id}"
    )
    assert scores >= time.time() + live_state._DEFAULT_REQUEST_BUDGET


def test_the_prune_logs_once_per_pass_with_a_count(state, caplog, monkeypatch):
    """300 orphans after a pod kill must be one line, not 300."""
    monkeypatch.setattr(
        live_state, "_request_budget", lambda: -live_state._DEADLINE_GRACE - 1
    )
    for entity_id in range(5):
        state.admit("gpt-4o", entity_id)
        state.admit("llama", entity_id)
    monkeypatch.undo()

    with caplog.at_level("INFO", logger="lumen.services.live_state"):
        state.snapshot()
    pruned = [r for r in caplog.records if "pruned" in r.message]
    assert len(pruned) == 1
    assert pruned[0].args == (10,)


def test_no_key_carries_a_ttl(state, raw, isolated_prefix):
    """The inverted test. A key TTL would delete a busy model's live members
    mid-burst — precisely when the number matters — so its absence is the
    correct behaviour, and an implementation that set one would be broken.
    """
    state.admit("gpt-4o", 7)
    assert raw.ttl(isolated_prefix + "gpt-4o") == -1


# --- budgets, measured against the real client -----------------------------


def test_a_request_costs_exactly_two_commands_against_a_real_redis(state):
    issued = []
    original = state._client.execute_command

    def counting(*args, **kwargs):
        issued.append(args[0])
        return original(*args, **kwargs)

    state._client.execute_command = counting

    def generate():
        ticket = state.admit("gpt-4o", 7)
        try:
            yield "chunk one"
            yield "chunk two"
        finally:
            state.release(ticket)

    list(generate())
    assert issued == ["ZADD", "ZREM"]


def test_an_aborted_stream_also_costs_exactly_two_commands(state):
    issued = []
    original = state._client.execute_command

    def counting(*args, **kwargs):
        issued.append(args[0])
        return original(*args, **kwargs)

    state._client.execute_command = counting

    def generate():
        ticket = state.admit("gpt-4o", 7)
        try:
            yield "chunk one"
            yield "chunk two"
        finally:
            state.release(ticket)

    gen = generate()
    next(gen)
    gen.close()
    assert issued == ["ZADD", "ZREM"]


@pytest.mark.parametrize("model_count", [1, 12])
def test_snapshot_costs_the_same_round_trips_whatever_the_model_count(
    state, monkeypatch, model_count
):
    """One SCAN to find the live models, then one pipeline holding every model's
    prune and read. A round trip per model would be 30 a minute per process on a
    ten-model deployment, for nothing."""
    for i in range(model_count):
        state.admit(f"model-{i}", i)

    round_trips = []
    original_execute_command = state._client.execute_command
    original_pipeline_execute = redis.client.Pipeline.execute

    def counting_command(*args, **kwargs):
        round_trips.append(args[0])
        return original_execute_command(*args, **kwargs)

    def counting_pipeline(self, *args, **kwargs):
        round_trips.append("PIPELINE")
        return original_pipeline_execute(self, *args, **kwargs)

    state._client.execute_command = counting_command
    monkeypatch.setattr(redis.client.Pipeline, "execute", counting_pipeline)

    live = state.snapshot()
    assert len(live) == model_count
    assert round_trips == ["SCAN", "PIPELINE"]


def test_snapshot_issues_nothing_when_no_model_is_live(state):
    issued = []
    original = state._client.execute_command

    def counting(*args, **kwargs):
        issued.append(args[0])
        return original(*args, **kwargs)

    state._client.execute_command = counting
    assert state.snapshot() == {}
    assert issued == ["SCAN"]


def test_topology_is_fleet_wide_while_redis_answers(state, monkeypatch):
    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    monkeypatch.setenv("LUMEN_REPLICAS", "2")
    assert state.topology() == {"scope": "fleet", "processes": 4, "replicas": 2}
