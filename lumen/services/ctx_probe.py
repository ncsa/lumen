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
from collections import deque

from flask.ctx import AppContext

logger = logging.getLogger(__name__)

# Deep enough to reach past Flask's own plumbing to whoever triggered the push.
_STACK_DEPTH = 20
# Anomalies are rare (a handful per hour at the observed leak rate) but each
# carries a stack, so the ring stays small.
_ANOMALY_RING = 50

_lock = threading.Lock()
_anomalies: deque = deque(maxlen=_ANOMALY_RING)
_anomalies_total = 0


def record_context_anomaly(kind: str, ctx_id: int, depth: int, detail: str):
    """Keep the anomaly for /metrics/debug, so a capture is self-contained
    rather than depending on the warning still being in the log."""
    global _anomalies_total
    with _lock:
        _anomalies_total += 1
        _anomalies.append((time.monotonic(), kind, ctx_id, depth, detail))


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
    for t, kind, ctx_id, depth, detail in items:
        lines.append(f"\n--- {now - t:.0f}s ago  {kind}  ctx=0x{ctx_id:x}  depth={depth} ---\n{detail}")
    return "\n".join(lines) + "\n"


def install_ctx_probe():
    """Patch ``AppContext.push``/``pop`` to warn on unbalanced use. Idempotent."""
    if getattr(AppContext.push, "_lumen_ctx_probe", False):
        return
    orig_push = AppContext.push
    orig_pop = AppContext.pop

    def push(self, *args, **kwargs):
        if self._cv_tokens:
            stack = "".join(traceback.format_stack(limit=_STACK_DEPTH)[:-1])
            record_context_anomaly("double-push", id(self), len(self._cv_tokens) + 1, stack)
            logger.warning(
                "app context 0x%x pushed again (depth becomes %d); its inner pop "
                "will skip teardown and strand this request's DB session. Pushed from:\n%s",
                id(self), len(self._cv_tokens) + 1, stack,
            )
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
        return orig_pop(self, *args, **kwargs)

    push._lumen_ctx_probe = True
    pop._lumen_ctx_probe = True
    AppContext.push = push
    AppContext.pop = pop
