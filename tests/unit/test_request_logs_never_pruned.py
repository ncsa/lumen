"""Guard the guarantee that ``request_logs`` is never pruned (#167).

``request_logs`` is critical data: every request Lumen has served, and the only
source the continuous aggregates can be rebuilt from. Nothing Lumen ships may
delete from it — no retention policy, no ``drop_chunks``, no ``DELETE`` or
``TRUNCATE``. ``flask enable-retention`` was removed for this reason, and
``entrypoint.sh`` runs ``flask db upgrade`` at container start, so a migration
that pruned it would do so automatically on the next deploy.

Python docstrings and comments are skipped, so code may still explain why these
are banned; SQL inside an ordinary string literal is still caught. That makes the
guard a backstop, not airtight: SQL hidden inside a docstring and executed from
there would get past it.
"""
import ast
import io
import re
import tokenize
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCANNED = ["lumen", "migrations", "scripts", "entrypoint.sh", "chart"]

# Each optional part is independent, so ONLY, TABLE, a schema and quotes compose
# in any combination: DELETE FROM ONLY "public"."request_logs" is caught too.
_TARGET = r"(ONLY\s+)?(\"?public\"?\s*\.\s*)?\"?request_logs\b"
# One retention policy is allowed: request_metrics_1m keeps 30 days (#169). Only
# the literal name is exempt; a variable or f-string target stays banned, because
# the guard cannot prove what it points at.
_BANNED = re.compile(
    r"add_retention_policy(?!\(\s*'request_metrics_1m')"
    r"|drop_chunks"
    rf"|DELETE\s+FROM\s+{_TARGET}"
    rf"|TRUNCATE\s+(TABLE\s+)?{_TARGET}",
    re.IGNORECASE,
)


def _docstring_lines(source):
    """Line numbers covered by module, class and function docstrings."""
    lines = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                lines.update(range(body[0].lineno, body[0].end_lineno + 1))
    return lines


def _python_code(source):
    """``(lineno, text)`` for each line, with comments and docstrings blanked out."""
    skip = _docstring_lines(source)
    lines = source.splitlines()
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT:
            row, col = tok.start
            lines[row - 1] = lines[row - 1][:col]
    return [(n, line) for n, line in enumerate(lines, start=1) if n not in skip]


def _files():
    for name in SCANNED:
        path = ROOT / name
        if path.is_file():
            yield path
        else:
            yield from (p for p in sorted(path.rglob("*")) if p.is_file() and "__pycache__" not in p.parts)


def test_nothing_prunes_request_logs():
    offenders = []
    for path in _files():
        try:
            source = path.read_text()
        except UnicodeDecodeError:
            continue
        lines = _python_code(source) if path.suffix == ".py" else enumerate(source.splitlines(), start=1)
        for lineno, line in lines:
            if _BANNED.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "request_logs must never be pruned (#167). Found:\n" + "\n".join(offenders)
    )


@pytest.mark.parametrize("sql", [
    "SELECT add_retention_policy('request_logs', INTERVAL '1 day')",
    "SELECT drop_chunks('request_logs', older_than => now())",
    "DELETE FROM request_logs",
    'delete from "request_logs" where id = 1',
    'DELETE FROM ONLY "public".request_logs',
    'DELETE FROM public."request_logs"',
    'DELETE FROM "public"."request_logs"',
    "TRUNCATE request_logs",
    "TRUNCATE ONLY public.request_logs",
    'TRUNCATE TABLE "public".request_logs',
    'TRUNCATE TABLE ONLY "public"."request_logs"',
    "SELECT add_retention_policy(f'{VIEW}', drop_after => INTERVAL '30 days')",
    "SELECT add_retention_policy('{VIEW}', drop_after => INTERVAL '30 days')",
    "SELECT add_retention_policy(VIEW, drop_after => INTERVAL '30 days')",
    "SELECT add_retention_policy('request_counts_hourly_by_entity', INTERVAL '30 days')",
    "SELECT add_retention_policy('request_metrics_1m_old', INTERVAL '30 days')",
])
def test_banned_pattern_catches(sql):
    assert _BANNED.search(sql)


@pytest.mark.parametrize("sql", [
    "SELECT remove_retention_policy('request_logs')",
    "DELETE FROM request_logs_archive",
    "SELECT COUNT(*) FROM request_logs",
    "SELECT add_retention_policy('request_metrics_1m', drop_after => INTERVAL '30 days')",
    "SELECT add_retention_policy( 'request_metrics_1m', INTERVAL '30 days')",
])
def test_banned_pattern_ignores(sql):
    assert not _BANNED.search(sql)
