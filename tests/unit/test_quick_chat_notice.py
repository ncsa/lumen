"""The chat page shows a dismissible notice that the built-in chat is for quick chats only.

The notice is injected into the empty chat session via the inline script in
chat.html, carries role="note" / aria-label for accessibility, links to the
connect page, and its dismissal is remembered in localStorage so it does not
reappear on every visit.
"""
import re
from pathlib import Path

LUMEN_DIR = Path(__file__).resolve().parents[2] / "lumen"
CHAT_TEMPLATE = LUMEN_DIR / "templates" / "chat.html"


def _script():
    return CHAT_TEMPLATE.read_text()


def test_notice_rendered_with_note_role():
    assert re.search(r'notice\.setAttribute\(\s*["\']role["\']\s*,\s*["\']note["\']\s*\)', _script())


def test_notice_has_accessibility_label():
    assert re.search(
        r'notice\.setAttribute\(\s*["\']aria-label["\']\s*,\s*["\']This chat is for quick interactions only["\']\s*\)',
        _script(),
    )


def test_notice_dismiss_button_is_labelled():
    assert re.search(
        r'dismiss\.setAttribute\(\s*["\']aria-label["\']\s*,\s*["\']Dismiss notice["\']\s*\)',
        _script(),
    )


def test_notice_dismissal_stored_in_localstorage():
    assert re.search(r'NOTICE_STORAGE_KEY\s*=\s*["\']lumen_quick_chat_notice_dismissed["\']', _script())
    assert re.search(r'localStorage\.setItem\(\s*NOTICE_STORAGE_KEY\s*,\s*["\']1["\']', _script())
    assert re.search(r'localStorage\.getItem\(\s*NOTICE_STORAGE_KEY\s*\)\s*===\s*["\']1["\']', _script())


def test_notice_links_to_connect_page():
    assert re.search(r"url_for\(['\"]connect\.index['\"]\)", _script())


def test_show_notice_guards_on_dismissal():
    # Every path that shows the notice (initial load, "+ New", delete) goes
    # through showQuickChatNotice(), so the dismissal check must live inside
    # it — not just in initQuickChatNotice() — or dismissing the notice would
    # not stick when starting a new conversation.
    script = _script()
    show_start = script.index("function showQuickChatNotice()")
    show_end = script.index("function hideQuickChatNotice()")
    show_body = script[show_start:show_end]
    assert "quickChatNoticeDismissed()" in show_body


def test_dismiss_moves_focus_to_chat_input():
    # After dismissal, focus must not fall to <body>; send it to the input.
    script = _script()
    dismiss_start = script.index("dismiss.addEventListener")
    dismiss_end = script.index("notice.appendChild(dismiss)")
    dismiss_body = script[dismiss_start:dismiss_end]
    assert re.search(r'getElementById\(\s*["\']chat-input["\']\s*\)\.focus\(\)', dismiss_body)
