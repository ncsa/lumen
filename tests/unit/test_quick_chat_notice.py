"""The chat page shows a notice that the built-in chat is for quick chats only.

The notice is injected into the empty chat session via the inline script in
chat.html, carries role="note" / aria-label for accessibility, and links to the
connect page. It has no close button: it simply disappears once the first chat
message is sent (hideQuickChatNotice()), so there is nothing to remember in
localStorage.
"""

import re
from pathlib import Path

LUMEN_DIR = Path(__file__).resolve().parents[2] / "lumen"
CHAT_TEMPLATE = LUMEN_DIR / "templates" / "chat.html"


def _script():
    return CHAT_TEMPLATE.read_text()


def _notice_build():
    # The body of showQuickChatNotice(), which renders the notice.
    script = _script()
    start = script.index("function showQuickChatNotice()")
    end = script.index("function hideQuickChatNotice()")
    return script[start:end]


def test_notice_rendered_with_note_role():
    assert re.search(r'notice\.setAttribute\(\s*["\']role["\']\s*,\s*["\']note["\']\s*\)', _notice_build())


def test_notice_has_accessibility_label():
    assert re.search(
        r'notice\.setAttribute\(\s*["\']aria-label["\']\s*,\s*["\']This chat is for quick interactions only["\']\s*\)',
        _notice_build(),
    )


def test_notice_has_no_close_button():
    # The notice disappears on its own once the first chat is sent, so it must
    # not render a close/dismiss button.
    build = _notice_build()
    assert "btn-close" not in build
    assert "Dismiss notice" not in build
    assert "localStorage" not in build


def test_notice_disappears_on_first_message():
    # hideQuickChatNotice() removes #quick-chat-notice from the DOM and must be
    # invoked on the send path, so the notice clears the moment the first chat
    # starts.
    assert "function hideQuickChatNotice()" in _script()

    script = _script()
    send_start = script.index('input.value = "";')
    send_end = script.index("chatHistory.push", send_start)
    send_body = script[send_start:send_end]
    assert "hideQuickChatNotice()" in send_body


def test_notice_links_to_connect_page():
    # The "connect a dedicated client" link is wired to the connect page.
    assert "link.href = CONNECT_URL" in _notice_build()
    assert re.search(r"url_for\(['\"]connect\.index['\"]\)", _script())
