"""Diagnostics for unbalanced Flask app-context push/pop.

The stranded-DB-session leak traced back to app contexts that never went
through teardown: the /metrics/debug scope report shows ``teardown=NEVER`` for
their registry keys while the context objects themselves are collected within
seconds. ``AppContext.pop()`` only runs teardown functions when its push depth
is 1 (flask/ctx.py), so a context pushed twice and popped once skips teardown
silently while still releasing its contextvar reference — exactly that
signature. These probes log the moment it happens, with the stack that does it.

Patched at class level, deliberately: it must cover every app instance in the
process, and the imbalance may originate outside any one app's code.
"""

import logging
import threading
import time
import traceback
import weakref
from collections import deque

from flask.ctx import AppContext, RequestContext

logger = logging.getLogger(__name__)

# Deep enough to reach past Flask's own plumbing to whoever triggered the push.
_STACK_DEPTH = 20
# Anomalies are rare (a handful per hour at the observed leak rate) but each
# carries a stack, so the ring stays small.
_ANOMALY_RING = 50

_lock = threading.Lock()
_anomalies: deque = deque(maxlen=_ANOMALY_RING)
_anomalies_total = 0
# Push provenance per live app context: ctx -> (thread name, monotonic time,
# stack). Weak-keyed so dead contexts clean up after themselves. Lets an
# ambient-context report say who pushed the context, and the pop wrapper
# detect a pop on a different thread than the push — which strands the
# context: the token reset cannot take effect outside the pushing thread's
# contextvars context, leaving the context current there with no tokens left.
_pushes: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def describe_push(ctx) -> str:
    """Who pushed this app context, when, and from where."""
    with _lock:
        info = _pushes.get(ctx)
    if info is None:
        return "push not recorded (predates the probe)"
    thread, t, stack = info
    return f"pushed {time.monotonic() - t:.0f}s ago on thread {thread} from:\n{stack}"


def record_context_anomaly(kind: str, ctx_id: int, depth: int, detail: str):
    """Keep the anomaly for /metrics/debug, so a capture is self-contained
    rather than depending on the warning still being in the log."""
    global _anomalies_total
    thread = threading.current_thread().name
    with _lock:
        _anomalies_total += 1
        _anomalies.append((time.monotonic(), kind, ctx_id, depth, thread, detail))


def format_context_anomalies() -> str:
    """The recorded push/pop anomalies, oldest first.

    ``ctx=0x...`` matches the scope keys in the pool tracker's scope report, so
    an anomaly here names the moment a ``teardown=NEVER`` key was orphaned.
    """
    now = time.monotonic()
    with _lock:
        items = list(_anomalies)
        total = _anomalies_total
    if not items:
        return "(none)\n"
    lines = [f"{total} anomaly(ies) since start, showing the last {len(items)}"]
    for t, kind, ctx_id, depth, thread, detail in items:
        lines.append(
            f"\n--- {now - t:.0f}s ago  {kind}  ctx=0x{ctx_id:x}  depth={depth}"
            f"  thread={thread} ---\n{detail}"
        )
    return "\n".join(lines) + "\n"


def install_ctx_probe():
    """Patch ``AppContext.push``/``pop`` to warn on unbalanced use. Idempotent."""
    if getattr(AppContext.push, "_lumen_ctx_probe", False):
        return
    orig_push = AppContext.push
    orig_pop = AppContext.pop
    orig_req_pop = RequestContext.pop

    def push(self, *args, **kwargs):
        stack = "".join(traceback.format_stack(limit=_STACK_DEPTH)[:-1])
        if self._cv_tokens:
            record_context_anomaly("double-push", id(self), len(self._cv_tokens) + 1, stack)
            logger.warning(
                "app context 0x%x pushed again (depth becomes %d); its inner pop "
                "will skip teardown and strand this request's DB session. Pushed from:\n%s",
                id(self), len(self._cv_tokens) + 1, stack,
            )
        with _lock:
            _pushes[self] = (threading.current_thread().name, time.monotonic(), stack)
        return orig_push(self, *args, **kwargs)

    def pop(self, *args, **kwargs):
        if len(self._cv_tokens) > 1:
            stack = "".join(traceback.format_stack(limit=_STACK_DEPTH)[:-1])
            record_context_anomaly("skipped-teardown-pop", id(self), len(self._cv_tokens), stack)
            logger.warning(
                "app context 0x%x popped at depth %d — teardown skipped, the DB "
                "session registered under this scope stays registered. Popped from:\n%s",
                id(self), len(self._cv_tokens), stack,
            )
        with _lock:
            info = _pushes.get(self)
        if info is not None and info[0] != threading.current_thread().name:
            # The contextvar token can only be reset in the pushing thread's
            # context; popped elsewhere, the token is consumed but the context
            # stays current over there — a poisoned thread with depth 0.
            stack = "".join(traceback.format_stack(limit=_STACK_DEPTH)[:-1])
            record_context_anomaly(
                "cross-thread-pop", id(self), len(self._cv_tokens),
                f"pushed on thread {info[0]}, popped on "
                f"{threading.current_thread().name}\npush stack:\n{info[2]}\npop stack:\n{stack}",
            )
            logger.warning(
                "app context 0x%x pushed on thread %s but popped on %s — the push "
                "thread stays poisoned with this context current",
                id(self), info[0], threading.current_thread().name,
            )
        try:
            return orig_pop(self, *args, **kwargs)
        except BaseException as exc:
            stack = "".join(traceback.format_stack(limit=_STACK_DEPTH)[:-1])
            record_context_anomaly(
                "app-ctx-pop-raised", id(self), len(self._cv_tokens),
                f"{type(exc).__name__}: {exc}\n{stack}",
            )
            logger.warning("AppContext.pop for 0x%x raised %r", id(self), exc)
            raise

    def req_pop(self, *args, **kwargs):
        # RequestContext.pop resets the request contextvar in a finally BEFORE
        # popping the app context; if anything in there raises, the app context
        # is silently never popped and its teardown never runs.
        try:
            return orig_req_pop(self, *args, **kwargs)
        except BaseException as exc:
            stack = "".join(traceback.format_stack(limit=_STACK_DEPTH)[:-1])
            record_context_anomaly(
                "request-ctx-pop-raised", id(self), len(self._cv_tokens),
                f"{type(exc).__name__}: {exc}\n{stack}",
            )
            logger.warning("RequestContext.pop for 0x%x raised %r", id(self), exc)
            raise

    push._lumen_ctx_probe = True
    pop._lumen_ctx_probe = True
    req_pop._lumen_ctx_probe = True
    AppContext.push = push
    AppContext.pop = pop
    RequestContext.pop = req_pop
