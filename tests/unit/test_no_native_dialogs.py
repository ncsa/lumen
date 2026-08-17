"""Guard against reintroducing native browser dialogs.

alert()/confirm()/prompt() are unstyled, block the event loop, and cannot be
themed or made accessible. All user-facing dialogs must be styled Bootstrap
modals instead — use the shared appAlert()/appConfirm()/appPrompt() helpers in
static/js/app.js (backed by the #app-dialog modal in base.html), or a bespoke
modal like the user-search or ack-consent ones. See CLAUDE.md.
"""
import re
from pathlib import Path

LUMEN_DIR = Path(__file__).resolve().parents[2] / "lumen"

# A bare call like `alert(` / `window.confirm(` — but not appAlert(, a word
# such as sendAlert(, or a mention inside a comment.
_USAGE = re.compile(r"(?:^|[^\w.$])(?:window\s*\.\s*)?(alert|confirm|prompt)\s*\(")
_COMMENT = re.compile(r"^\s*(//|\*|/\*|<!--)")


def test_native_dialogs_are_not_used():
    offenders = []
    files = sorted(LUMEN_DIR.glob("templates/**/*.html")) + sorted(LUMEN_DIR.glob("static/js/*.js"))
    for path in files:
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if _COMMENT.match(line):
                continue
            if _USAGE.search(line):
                offenders.append(f"{path.relative_to(LUMEN_DIR.parent)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "Native browser dialogs (alert/confirm/prompt) are banned — use the "
        "styled appAlert/appConfirm/appPrompt modals from app.js (see CLAUDE.md). Found:\n"
        + "\n".join(offenders)
    )
