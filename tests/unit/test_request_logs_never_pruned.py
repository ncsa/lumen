"""Guard the guarantee that ``request_logs`` is never pruned (#167).

``request_logs`` is critical data: every request Lumen has served, and the only
source the continuous aggregates can be rebuilt from. Nothing Lumen ships may
delete from it — no retention policy, no ``drop_chunks``, no ``DELETE`` or
``TRUNCATE``. ``flask enable-retention`` was removed for this reason, and
``entrypoint.sh`` runs ``flask db upgrade`` at container start, so a migration
that pruned it would do so automatically on the next deploy.

Python docstrings and comments are skipped, so code may still explain why these
are banned; SQL inside an ordinary string literal is still caught.
"""
import ast
import io
import re
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCANNED = ["lumen", "migrations", "scripts", "entrypoint.sh", "chart"]

_BANNED = re.compile(
    r"add_retention_policy"
    r"|drop_chunks"
    r"|DELETE\s+FROM\s+(ONLY\s+)?\"?(public\.)?request_logs\b"
    r"|TRUNCATE\s+(TABLE\s+)?(ONLY\s+)?\"?(public\.)?request_logs\b",
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
