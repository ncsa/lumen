"""Guard the LLM timing paths against a second clock.

Every span measured in ``lumen/services/llm.py``, ``lumen/blueprints/api/routes.py``,
``lumen/blueprints/chat/routes.py``, and ``lumen/services/wsgi_disconnect.py`` —
``duration``, ``t_first``, ``queue_wait``, ``send_blocked``, and the abort
accounting's ``duration`` computed from the caller's ``stream_t0`` — must come
from ``time.monotonic()``, the same clock the request-arrival instrumentation
stamps with. Mixing the two produces sums with no meaning, and a subtraction
across them is off by the Unix epoch: about 1.76e9 seconds, or 55 years, in
whichever direction the survivor sits. Nothing raises when that happens; the
rows and the histogram simply become garbage.

This is deliberately a *presence* test, not a "no ``time.time()`` inside a
subtraction" test. The span origins are plain assignments (``t0 = ...``) four
lines away from the subtractions that consume them, so a subtraction-only rule
passes green in exactly the half-converted state that causes the bug.

Stored *instants* are a different thing and are unaffected: they use
``datetime.now(timezone.utc)``, not ``time.time()``. If a genuine wall-clock
reading is ever needed in one of these files, add it to ``ALLOWED`` below with
a comment saying why — never by weakening the rule.
"""
import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

GUARDED_FILES = [
    REPO_ROOT / "lumen" / "services" / "llm.py",
    REPO_ROOT / "lumen" / "blueprints" / "api" / "routes.py",
    # Timing spans start and are consumed on these paths too:
    REPO_ROOT / "lumen" / "blueprints" / "chat" / "routes.py",
    REPO_ROOT / "lumen" / "services" / "wsgi_disconnect.py",
]

# (relative path, line number) pairs exempted from the rule. Each entry needs a
# comment explaining why a wall-clock reading is correct there.
ALLOWED: set[tuple[str, int]] = set()


def _time_module_aliases(tree):
    """Names bound to the ``time`` module — ``import time``/``import time as _time``."""
    aliases = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "time":
                    aliases.add(alias.asname or "time")
    return aliases


def _wall_clock_names(tree):
    """Names bound directly to ``time.time`` — ``from time import time as now``."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "time":
            for alias in node.names:
                if alias.name == "time":
                    names.add(alias.asname or "time")
    return names


def _offenders(source):
    """Every reference to the wall clock in ``source``, as ``(lineno, text)``."""
    tree = ast.parse(source)
    lines = source.splitlines()
    aliases = _time_module_aliases(tree)
    bare_names = _wall_clock_names(tree)

    found = []
    for node in ast.walk(tree):
        # ``time.time`` / ``_time.time`` — attribute access, so a reference that
        # is stored and called later is caught too, not only a direct call.
        hit = (
            isinstance(node, ast.Attribute)
            and node.attr == "time"
            and isinstance(node.value, ast.Name)
            and node.value.id in aliases
        )
        # ``time()`` where the name came from ``from time import time``.
        hit = hit or (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id in bare_names
        )
        if hit:
            found.append((node.lineno, lines[node.lineno - 1].strip()))
    return sorted(set(found))


def test_llm_timing_paths_use_one_clock():
    offenders = []
    for path in GUARDED_FILES:
        rel = str(path.relative_to(REPO_ROOT))
        for lineno, text in _offenders(path.read_text()):
            if (rel, lineno) in ALLOWED:
                continue
            offenders.append(f"{rel}:{lineno}: {text}")
    assert not offenders, (
        "The wall clock (time.time) must not appear in the LLM timing paths — "
        "all spans there are time.monotonic(), matching the request-arrival "
        "instrumentation. Mixing the two writes durations off by ~1.76e9 "
        "seconds and nothing raises. Found:\n" + "\n".join(offenders)
    )


def test_guard_detects_every_spelling():
    """The detector must catch the aliased, multi-line and assignment-only forms."""
    assert _offenders("import time\nt0 = time.time()\n") == [(2, "t0 = time.time()")]
    assert _offenders("import time as _time\nt0 = _time.time()\n") == [(2, "t0 = _time.time()")]
    assert _offenders("from time import time as now\nt0 = now()\n") == [(2, "t0 = now()")]
    # A bare assignment, with no subtraction in sight — the half-converted state
    # a subtraction-only rule would wave through.
    assert _offenders("import time\nclock = time.time\n")
    assert _offenders("import time as _time\nd = (\n    _time.time()\n    - t0\n)\n")
    # Monotonic is the point of the exercise, so it must not be flagged.
    assert _offenders("import time\nt0 = time.monotonic()\n") == []
