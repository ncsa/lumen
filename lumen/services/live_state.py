"""In-flight LLM requests, per model.

Two backends behind one interface. :class:`LocalLiveState` counts this process;
:class:`RedisLiveState` counts the fleet. Which one you get is decided on first
use from the Redis URL rate limiting already resolved — no second config key —
and :meth:`topology` always reports which, because a per-process number
presented as a fleet-wide one misleads exactly the reader it exists for.

"Live" means **admitted → request completion**, not admitted → first token: a
generating request still holds a backend sequence slot, so a count that stops at
the first token counts nothing useful. The UI label must therefore read "in
flight", never "waiting"; the "N ahead of you" number is the backend's own queue
depth (Phase 6), not this.

``admit`` is called in the view, while the request context is live and *after*
every rejection path — a rejected request must never appear in flight. The
returned ticket is captured into the streaming generator's closure and
``release`` is called from a ``finally`` around the generator body.

``release`` runs context-free: it must never touch ``db.session`` or
``current_app``, because it runs on whichever thread iterates the response body,
outside any request context. Anything a backend needs from config is read at
``admit`` time and carried on the ticket.

The ``finally`` is the fast path, not the correctness argument — **the deadline
is**. Under uvicorn + a2wsgi a disconnected client's generator may never be
closed, so ``GeneratorExit`` and therefore ``finally`` may never run (see
``send_message_stream``'s docstring in ``lumen/services/llm.py``). An abandoned
ticket that is never released would inflate the count permanently, so every
ticket carries a deadline and expired tickets are excluded from every number
reported.
"""
import logging
import threading
import time
import uuid
from dataclasses import dataclass

from flask import current_app, has_app_context
from prometheus_client import Counter

from lumen.services.db_pool import detect_replicas, detect_workers

try:  # Redis is optional: without it (or without a Redis URL) the state is per-process.
    import redis
    from redis.backoff import NoBackoff
    from redis.retry import Retry
except ImportError:  # pragma: no cover - redis is a transitive dependency of flask-limiter[redis]
    redis = None

logger = logging.getLogger(__name__)

#: Wall-clock seconds a request is allowed to take before its ticket is presumed
#: abandoned. ``api.request_budget_seconds`` is Phase 2 item 6's key; until it
#: lands the fallback is 600s + the grace below.
_DEFAULT_REQUEST_BUDGET = 600.0

#: Added to the request budget so a request that is merely finishing late is not
#: dropped from the count while it is still running.
#:
#: This grace is also the wall-clock skew tolerance between the process that
#: *mints* a deadline (writer's ``time.time()``, in :func:`_mint`) and the
#: process that *prunes* it (:class:`RedisLiveState.snapshot` removes scores at
#: or below the reader's own ``time.time()``). A reader whose clock *leads* the
#: writer's by more than ``request_budget + grace`` prunes too aggressively and
#: wrongly drops a still-live in-flight ticket — an under-count — so raise
#: ``_DEADLINE_GRACE`` to keep that leading edge inside tolerance. A reader that
#: *lags* prunes too little and holds expired tickets past their deadline, an
#: over-count that self-corrects once the lagging reader catches up; it is the
#: other skew direction and needs no code, only awareness. (Local, per-process
#: state never crosses a clock, so only the Redis backend is affected.)
_DEADLINE_GRACE = 60.0

#: One sorted set per model under this fixed prefix. The URL comes from rate
#: limiting, so two Lumen deployments pointed at the same Redis db will merge
#: their counts — documented rather than fixed with a config key nobody sets.
_KEY_PREFIX = "lumen:live:"

_REDIS_SCHEMES = ("redis://", "rediss://")

#: Short enough that a Redis that has stopped answering costs a request a
#: quarter second, not the request.
_REDIS_TIMEOUT = 0.25

#: SCAN hint. Live models are a handful of keys, so one iteration is the norm.
_SCAN_COUNT = 500

_live_state_errors = Counter(
    "lumen_live_state_errors_total",
    "Live-state operations that fell back to per-process state after a Redis failure",
    ["op"],
)


@dataclass(frozen=True)
class LiveTicket:
    """Handle returned by either backend's ``admit``, needed to release."""

    model_key: str
    entity_id: int
    request_id: str
    deadline: float  # unix seconds


@dataclass(frozen=True)
class ModelLive:
    """One model's live numbers. A user with three concurrent requests is three
    in flight and one unique user."""

    inflight: int
    unique_users: int


def _mint(model_key: str, entity_id: int) -> LiveTicket:
    """Both backends carry the same triple, so a fallback changes a number's
    scope but never its meaning."""
    return LiveTicket(
        model_key=model_key,
        entity_id=entity_id,
        request_id=uuid.uuid4().hex,
        deadline=time.time() + _request_budget() + _DEADLINE_GRACE,
    )


def _request_budget() -> float:
    if not has_app_context():
        return _DEFAULT_REQUEST_BUDGET
    api_cfg = current_app.config.get("YAML_DATA", {}).get("api", {})
    try:
        return float(api_cfg.get("request_budget_seconds", _DEFAULT_REQUEST_BUDGET))
    except (TypeError, ValueError):
        return _DEFAULT_REQUEST_BUDGET


class LocalLiveState:
    """Per-process live state: a dict under a lock. Also :class:`RedisLiveState`'s
    fail-open fallback, which is why it holds the same triple the sorted set does."""

    def __init__(self):
        self._lock = threading.Lock()
        self._tickets: dict[str, LiveTicket] = {}

    def admit(self, model_key: str, entity_id: int) -> LiveTicket:
        ticket = _mint(model_key, entity_id)
        self.record(ticket)
        return ticket

    def record(self, ticket: LiveTicket) -> None:
        """Adopt a ticket minted elsewhere.

        Only :class:`RedisLiveState`'s fail-open path uses this: an ``admit``
        whose Redis call failed still hands back a usable ticket, and the ticket
        has to land somewhere or the fallback count is a lie.
        """
        with self._lock:
            self._prune(time.time())
            self._tickets[ticket.request_id] = ticket

    def release(self, ticket: LiveTicket) -> None:
        with self._lock:
            self._tickets.pop(ticket.request_id, None)

    def snapshot(self) -> dict[str, ModelLive]:
        now = time.time()
        with self._lock:
            self._prune(now)
            live = list(self._tickets.values())
        by_model: dict[str, list[LiveTicket]] = {}
        for ticket in live:
            by_model.setdefault(ticket.model_key, []).append(ticket)
        return {
            model_key: ModelLive(
                inflight=len(tickets),
                unique_users=len({t.entity_id for t in tickets}),
            )
            for model_key, tickets in by_model.items()
        }

    def topology(self) -> dict:
        """What the numbers cover, so a reader is not misled by a per-process
        count presented as a fleet-wide one."""
        return {
            "scope": "local",
            "processes": detect_workers(),
            "replicas": detect_replicas(),
        }

    def _prune(self, now: float) -> None:
        """Drop tickets past their deadline. Caller holds the lock."""
        expired = [rid for rid, t in self._tickets.items() if t.deadline <= now]
        for rid in expired:
            del self._tickets[rid]


class RedisLiveState:
    """Fleet-wide live state: one sorted set per model, shared by every process.

    Key ``lumen:live:{model_key}``, member ``"{entity_id}:{request_id}"``, score
    the ticket's deadline as unix seconds. That shape is forced by two facts
    about Redis, both of which kill the obvious "a SET of entity ids" design:

    * **Sets have no per-member TTL.** Expiry is per *key*, so the only way to
      expire a member is to expire every member with it — which would zero a
      busy model's count mid-burst, the exact moment the number matters. Here
      expiry is by *score* instead: every read is bounded below by ``now``, so a
      process killed mid-request stops being counted the instant its deadline
      passes, whether or not anything has physically removed it yet.
    * **``SREM`` on a bare entity id under-counts the double-submitting
      student.** One user with three concurrent requests would be one member,
      and the first completion would remove them while two were still in
      flight. With the request id in the member, three requests are three
      members sharing one prefix: in flight 3, unique users 1, and releasing
      one removes exactly one.

    Two Redis commands per request, both off the token path: ``ZADD`` on admit,
    ``ZREM`` on release. Nothing per chunk. That budget only holds because
    pruning is the *reader's* job — do not pipeline ``ZREMRANGEBYSCORE`` into
    ``admit``, which is the obvious tidy-up and silently makes it three.

    Every operation is fail-open: a Redis blip degrades the admin page, never a
    ``/v1/chat/completions``. Failures fall back to per-process state, increment
    ``lumen_live_state_errors_total{op}``, and make :meth:`topology` say so.
    """

    def __init__(self, client, fallback: LocalLiveState | None = None):
        self._client = client
        # Where a fail-open admit parks its ticket, so the degraded number is a
        # smaller truth rather than a wrong one.
        self._local = fallback if fallback is not None else LocalLiveState()
        self._warned: set[str] = set()

    def admit(self, model_key: str, entity_id: int) -> LiveTicket:
        ticket = _mint(model_key, entity_id)
        try:
            self._client.zadd(_key(model_key), {_member(ticket): ticket.deadline})
        except Exception as exc:
            # Fail-open must be symmetric: return a ticket ``release`` can still
            # accept. An admit that raised and returned nothing would turn a
            # Redis blip into a permanent inflation — the bug in the costume of
            # the fix.
            self._degraded("admit", exc)
            self._local.record(ticket)
        return ticket

    def release(self, ticket: LiveTicket) -> None:
        # Unconditional and free: a no-op unless this ticket's admit fell back.
        self._local.release(ticket)
        try:
            self._client.zrem(_key(ticket.model_key), _member(ticket))
        except Exception as exc:
            self._degraded("release", exc)

    def snapshot(self) -> dict[str, ModelLive]:
        """The fleet's live numbers, and the only place that reads members.

        **This is O(live members), not O(1), and the cost is real.** Redis has no
        primitive for "count distinct prefixes in a sorted set", so the unique
        user count means transferring every live member and deduping in Python.
        At 300 concurrent requests over 10 models that is ~3000 short strings per
        pass per process — tens of kilobytes, single-digit milliseconds — which
        is affordable only because no request path is allowed to call this. If
        live members on one model ever pass ~10^4, the escape hatch is a per-model
        HASH of ``entity_id -> refcount`` (``HINCRBY`` ±1, ``HLEN`` for an O(1)
        unique count) at the price of a third command per request; do not do it
        speculatively, because a hash field cannot carry a deadline and so
        reintroduces the orphan problem the scores solve.

        Round trips are two per pass, not per model: one ``SCAN`` to discover
        which models are live, then a single pipeline holding every model's
        prune and read. Pruning happens here, at the reader, which is what keeps
        the request path at two commands.
        """
        now = time.time()
        try:
            # dict.fromkeys because SCAN may hand back the same key twice while
            # the keyspace rehashes, which would double-count the prune log.
            keys = list(dict.fromkeys(
                self._client.scan_iter(match=_KEY_PREFIX + "*", count=_SCAN_COUNT)
            ))
            if not keys:
                return {}
            pipe = self._client.pipeline(transaction=False)
            for key in keys:
                pipe.zremrangebyscore(key, "-inf", now)
                pipe.zrangebyscore(key, f"({now}", "+inf")
            results = pipe.execute()
        except Exception as exc:
            self._degraded("snapshot", exc)
            return self._local.snapshot()

        pruned = sum(results[0::2])
        if pruned:
            # Once per pass with a count, never once per member: 300 orphans
            # after a pod kill would otherwise be 300 lines in the incident.
            logger.info("Live state pruned %d ticket(s) past their deadline.", pruned)

        live = {}
        for key, members in zip(keys, results[1::2]):
            if not members:
                continue
            live[key[len(_KEY_PREFIX):]] = ModelLive(
                inflight=len(members),
                unique_users=len({m.split(":", 1)[0] for m in members}),
            )
        return live

    def topology(self) -> dict:
        """Fleet-wide while Redis answers, and honestly local when it does not —
        a degraded per-process number presented as a fleet one is worse than no
        number at all."""
        try:
            self._client.ping()
        except Exception as exc:
            self._degraded("topology", exc)
            return self._local.topology()
        return {
            "scope": "fleet",
            "processes": detect_workers(),
            "replicas": detect_replicas(),
        }

    def _degraded(self, op: str, exc: BaseException) -> None:
        _live_state_errors.labels(op=op).inc()
        if op in self._warned:
            logger.debug("Live state %s fell back to local state: %s", op, exc)
        else:
            self._warned.add(op)
            logger.warning(
                "Live state %s fell back to local state; further failures log at DEBUG: %s",
                op, exc,
            )


def _key(model_key: str) -> str:
    return _KEY_PREFIX + model_key


def _member(ticket: LiveTicket) -> str:
    return f"{ticket.entity_id}:{ticket.request_id}"


_live_state = LocalLiveState()
_backend: "LocalLiveState | RedisLiveState | None" = None
_backend_lock = threading.Lock()


def _build_backend() -> "LocalLiveState | RedisLiveState":
    """Pick a backend from the Redis URL rate limiting already resolved.

    No second config key to keep in sync, and no new failure mode when one is
    set and the other is not.
    """
    url = str(current_app.config.get("RATELIMIT_STORAGE_URI") or "")
    if redis is None or not url.startswith(_REDIS_SCHEMES):
        logger.info(
            "Live state is per-process: rate_limiting.storage_url is not a Redis URL, so "
            "in-flight counts cover this worker only."
        )
        return _live_state
    try:
        client = redis.Redis.from_url(
            url,
            decode_responses=True,
            socket_timeout=_REDIS_TIMEOUT,
            socket_connect_timeout=_REDIS_TIMEOUT,
            # redis-py retries three times with backoff by default, which would
            # multiply both the 0.25s bound and the two-command budget.
            retry=Retry(NoBackoff(), 0),
        )
    except Exception as exc:  # a malformed URL, nothing else
        logger.warning("Live state is per-process: could not build a Redis client: %s", exc)
        return _live_state
    # Deliberately no connection check here: a Redis that is down at startup must
    # not cost a request anything beyond the fail-open path it already has.
    logger.info(
        "Live state is fleet-wide, over the Redis that rate limiting already uses "
        "(keys under %s). Two deployments sharing one Redis db will merge their counts.",
        _KEY_PREFIX,
    )
    return RedisLiveState(client, _live_state)


def get_live_state() -> "LocalLiveState | RedisLiveState":
    global _backend
    if _backend is None:
        if not has_app_context():
            # Selection needs the resolved rate-limiting URL; with no app context
            # there is nothing to read, so answer locally without deciding.
            return _live_state
        with _backend_lock:
            if _backend is None:
                _backend = _build_backend()
    return _backend
