"""The chat page shows a dismissible notice that the built-in chat is for quick chats only.

The notice is injected into the empty chat session via the inline script in
chat.html, carries role="note" / aria-label for accessibility, links to the
connect page, and its dismissal is remembered in localStorage so it does not
reappear on every visit.
"""
from pathlib import Path

LUMEN_DIR = Path(__file__).resolve().parents[2] / "lumen"
CHAT_TEMPLATE = LUMEN_DIR / "templates" / "chat.html"


def _script():
    return CHAT_TEMPLATE.read_text()


def test_notice_rendered_with_note_role():
    assert _script().count('notice.setAttribute("role", "note")') == 1


def test_notice_has_accessibility_label():
    assert 'aria-label", "This chat is for quick interactions only"' in _script()


def test_notice_dismiss_button_is_labelled():
    assert 'aria-label", "Dismiss notice"' in _script()


def test_notice_dismissal_stored_in_localstorage():
    assert 'const NOTICE_STORAGE_KEY = "lumen_quick_chat_notice_dismissed"' in _script()
    assert 'localStorage.setItem(NOTICE_STORAGE_KEY, "1"' in _script()
    assert 'localStorage.getItem(NOTICE_STORAGE_KEY) === "1"' in _script()


def test_notice_links_to_connect_page():
    assert "url_for('connect.index')" in _script()
