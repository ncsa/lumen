"""Guard against reintroducing flask.stream_with_context.

stream_with_context re-pushes the request's app context onto whichever WSGI
worker thread iterates the response body. A client disconnect abandons the
generator, the push is never undone, and the thread is permanently poisoned:
every later request on it reuses the stuck context, whose session teardown
never runs — one leaked idle-in-transaction connection per poisoned thread.

Streaming generators must run context-free instead: capture the app object and
all needed scalars in the view, then push a short-lived ``with app.app_context():``
around each DB phase, never spanning a ``yield``. See CLAUDE.md ("DB connection
hygiene") and the streaming paths in chat/routes.py, api/routes.py and llm.py.
"""
import re
from pathlib import Path

LUMEN_DIR = Path(__file__).resolve().parents[2] / "lumen"

# Matches an import or a call, but not the word inside a comment/docstring
# explaining why it is banned.
_USAGE = re.compile(r"^\s*[^#]*\bstream_with_context\s*[(\n,)]|import\b.*\bstream_with_context\b")


def test_stream_with_context_is_not_used():
    offenders = []
    for path in sorted(LUMEN_DIR.rglob("*.py")):
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            stripped = line.split("#", 1)[0]
            if "stream_with_context" in stripped and _USAGE.search(stripped):
                offenders.append(f"{path.relative_to(LUMEN_DIR.parent)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "stream_with_context is banned (poisons the worker thread on client "
        "disconnect and leaks a DB connection; see CLAUDE.md). Found:\n"
        + "\n".join(offenders)
    )
