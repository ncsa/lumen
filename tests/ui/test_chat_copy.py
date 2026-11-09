"""Run the per-reply copy button on /chat in a real browser.

The page is served by the Flask test client at http://lumen.test through
page.route, like test_key_tables.py. That origin is not a secure context, so
navigator.clipboard does not exist there; an init script stubs it and records
what was written in window.__copied. /chat/stream is answered with canned SSE
bodies, so no model is needed. The page renders replies with marked and
DOMPurify, which are served from pinned copies in tests/fixtures/vendor.
"""
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from lumen.extensions import db
from lumen.models.conversation import Conversation
from lumen.models.message import Message
from tests.ui.test_accessibility import _run_all_checks
from tests.ui.test_key_tables import BOOTSTRAP_JS, BOOTSTRAP_JS_URL, ORIGIN

VENDOR = Path(__file__).parent.parent / "fixtures" / "vendor"
# Must match the scripts chat.html loads; each copy is that file, byte for byte.
VENDOR_JS = {
    "https://cdn.jsdelivr.net/npm/marked@18.1.2/lib/marked.umd.js": VENDOR / "marked-18.1.2.umd.js",
    "https://cdn.jsdelivr.net/npm/dompurify@3.4.17/dist/purify.min.js": VENDOR / "dompurify-3.4.17.purify.min.js",
    BOOTSTRAP_JS_URL: BOOTSTRAP_JS,
}
CHAT_HTML = Path(__file__).parent.parent.parent / "lumen" / "templates" / "chat.html"

REPLY = "Here you go:\n\n```python\nprint('hi')\n```\n\n- one\n- two\n"

CLIPBOARD_STUB = """
window.__copied = [];
window.__clipboardFails = false;
Object.defineProperty(navigator, "clipboard", {
  configurable: true,
  value: {
    writeText(text) {
      if (window.__clipboardFails) return Promise.reject(new Error("denied"));
      window.__copied.push(text);
      return Promise.resolve();
    },
  },
});
"""


def _sse(*events):
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events)


@pytest.fixture
def open_chat(auth_client, browser):
    """Return open(stream=None) -> page; stream is (status, content_type, body) for /chat/stream.

    The JSON bodies posted to /chat/stream are collected in open.stream_bodies.
    """
    pages = []
    stream_bodies = []

    def _open(stream=None):
        def handle(route, request):
            url = urlsplit(request.url)
            if request.url in VENDOR_JS:
                # Loaded with crossorigin + integrity, so they need CORS.
                route.fulfill(status=200, content_type="application/javascript",
                              headers={"Access-Control-Allow-Origin": "*"}, body=VENDOR_JS[request.url].read_bytes())
                return
            if f"{url.scheme}://{url.netloc}" != ORIGIN:
                route.abort()
                return
            if url.path == "/chat/stream":
                stream_bodies.append(request.post_data_json)
                status, content_type, body = stream
                route.fulfill(status=status, content_type=content_type, body=body)
                return
            path = url.path + (f"?{url.query}" if url.query else "")
            if request.method == "DELETE":
                resp = auth_client.delete(path)
            elif request.method == "POST":
                resp = auth_client.post(path, json=request.post_data_json)
            else:
                resp = auth_client.get(path)
            route.fulfill(status=resp.status_code, content_type=resp.content_type, body=resp.get_data())

        page = browser.new_page()
        pages.append(page)
        page.set_default_timeout(10_000)
        page.add_init_script(CLIPBOARD_STUB)
        page.route("**/*", handle)
        page.goto(f"{ORIGIN}/chat")
        page.wait_for_selector("#quick-chat-notice")
        return page

    _open.stream_bodies = stream_bodies
    yield _open
    for page in pages:
        page.close()


@pytest.fixture
def stored_conversation(app, test_user):
    with app.app_context():
        conv = Conversation(entity_id=test_user["id"], title="Copy me", model="test-model")
        db.session.add(conv)
        db.session.flush()
        db.session.add_all([
            Message(conversation_id=conv.id, role="user", content="Show me code",
                    created_at=datetime(2026, 1, 2, 3, 4, 5)),
            Message(conversation_id=conv.id, role="assistant", content=REPLY, input_tokens=3, output_tokens=9,
                    created_at=datetime(2026, 1, 2, 3, 4, 6)),
        ])
        db.session.commit()
        return conv.id


def _open_stored(page, cid):
    page.click(f".conv-item[data-id='{cid}']")
    page.wait_for_selector(".chat-bubble-assistant")


def _send(page, text="hello"):
    # The test user has no models; give the picker one so the form submits.
    page.evaluate("""() => {
      const opt = document.createElement("option");
      opt.value = opt.textContent = "test-model";
      document.getElementById("model-picker").appendChild(opt);
      document.getElementById("model-picker").value = "test-model";
    }""")
    page.fill("#chat-input", text)
    page.click("#send-btn")
    page.wait_for_function("!document.getElementById('send-btn').disabled")


def test_stored_reply_copies_raw_markdown(open_chat, stored_conversation):
    page = open_chat()
    _open_stored(page, stored_conversation)

    buttons = page.locator(".msg-meta .copy-btn")
    assert buttons.count() == 1  # only the assistant reply, not the user message
    assert buttons.get_attribute("aria-label") == "Copy reply"
    assert buttons.get_attribute("type") == "button"
    assert page.locator(".chat-bubble-assistant pre code").count() == 1  # rendered as HTML
    _run_all_checks(page.content().encode(), "/chat (stored conversation with copy button)")

    buttons.click()
    page.wait_for_function("window.__copied.length === 1")
    assert page.evaluate("window.__copied[0]") == REPLY
    assert page.inner_text("#copy-status") == "Copied to clipboard"
    assert page.locator(".msg-meta .copy-btn i.bi-check2").count() == 1
    page.wait_for_selector(".msg-meta .copy-btn i.bi-copy", state="attached", timeout=3_000)


def test_copy_button_is_keyboard_reachable(open_chat, stored_conversation):
    page = open_chat()
    _open_stored(page, stored_conversation)

    page.focus(".chat-bubble-assistant ~ .msg-meta .info-icon")
    page.keyboard.press("Tab")
    assert page.evaluate("document.activeElement.classList.contains('copy-btn')")
    page.keyboard.press("Enter")
    page.wait_for_function("window.__copied.length === 1")
    assert page.evaluate("window.__copied[0]") == REPLY


def test_rejected_write_says_copy_failed(open_chat, stored_conversation):
    page = open_chat()
    _open_stored(page, stored_conversation)

    page.evaluate("window.__clipboardFails = true")
    page.click(".msg-meta .copy-btn")
    page.wait_for_function("document.getElementById('copy-status').textContent === 'Copy failed'")
    assert page.locator(".msg-meta .copy-btn i.bi-exclamation-triangle").count() == 1
    assert page.evaluate("window.__copied") == []


def test_live_reply_with_done_gets_a_button(open_chat):
    body = _sse({"chunk": "**Bold** "}, {"chunk": "reply"},
                {"done": True, "reply": "**Bold** reply", "input_tokens": 1, "output_tokens": 2})
    page = open_chat(stream=(200, "text/event-stream", body))
    _send(page)

    page.click(".msg-meta .copy-btn")
    page.wait_for_function("window.__copied.length === 1")
    assert page.evaluate("window.__copied[0]") == "**Bold** reply"


def test_live_reply_without_done_gets_a_button(open_chat):
    body = _sse({"chunk": "partial "}, {"chunk": "`text`"})
    page = open_chat(stream=(200, "text/event-stream", body))
    _send(page)

    assert page.locator(".msg-meta .copy-btn").count() == 1
    page.click(".msg-meta .copy-btn")
    page.wait_for_function("window.__copied.length === 1")
    assert page.evaluate("window.__copied[0]") == "partial `text`"


@pytest.mark.parametrize("stream, error", [
    ((200, "text/event-stream", _sse({"chunk": "half"}, {"error": "boom"})), "Error: boom"),
    ((500, "application/json", json.dumps({"error": "down"})), "Error: down"),
])
def test_error_bubbles_have_no_copy_button(open_chat, stream, error):
    page = open_chat(stream=stream)
    _send(page)

    assert page.inner_text(".chat-bubble-assistant").strip() == error
    assert page.locator(".msg-meta .copy-btn").count() == 0


def test_chat_template_loads_the_pinned_renderers():
    chat = CHAT_HTML.read_text()
    for url in VENDOR_JS:
        if url != BOOTSTRAP_JS_URL:
            assert f'src="{url}"' in chat, "update VENDOR_JS and the copies in tests/fixtures/vendor"


def test_no_plain_http_clipboard_fallback():
    assert "execCommand" not in CHAT_HTML.read_text()


# ── Copy whole conversation ─────────────────────────────────────

ISO_UTC = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"


def _copy_all(page):
    before = page.evaluate("window.__copied.length")
    page.click("#copy-chat-btn")
    page.wait_for_function(f"window.__copied.length === {before + 1}")
    return page.evaluate("window.__copied[window.__copied.length - 1]")


@pytest.fixture
def document_conversation(app, test_user):
    with app.app_context():
        conv = Conversation(entity_id=test_user["id"], title="Doc", model="test-model")
        db.session.add(conv)
        db.session.flush()
        db.session.add_all([
            Message(conversation_id=conv.id, role="user",
                    content="[Document: notes.md]\n\nSECRET FILE TEXT\n\n---\n\nSummarise it",
                    created_at=datetime(2026, 1, 2, 3, 4, 5)),
            Message(conversation_id=conv.id, role="assistant", content="A summary.", input_tokens=3, output_tokens=2,
                    created_at=datetime(2026, 1, 2, 3, 4, 6)),
        ])
        db.session.commit()
        return conv.id


def test_copy_all_button_floats_over_the_message_area(open_chat, stored_conversation):
    page = open_chat()
    btn = page.locator("#copy-chat-btn")
    assert btn.get_attribute("aria-label") == "Copy conversation as Markdown"
    assert btn.locator("i.bi-copy").count() == 1
    assert page.locator(".chat-header #copy-chat-btn").count() == 0
    assert page.locator("#chat-messages #copy-chat-btn").count() == 0  # outside the scrolling log

    _open_stored(page, stored_conversation)
    _run_all_checks(page.content().encode(), "/chat (stored conversation with copy-all button)")
    # Pinned to the top-right corner of the wrapper around the scrolling log, so
    # it stays put while the messages scroll. (Bootstrap's CSS is not served
    # here, so the scrolling itself is checked by scripts/responsive_check.py.)
    assert page.evaluate("document.getElementById('copy-chat-btn').parentElement.id") == "chat-messages-wrap"
    style = page.evaluate("""() => {
      const s = getComputedStyle(document.getElementById("copy-chat-btn"));
      return [s.position, s.top, s.right, getComputedStyle(document.getElementById("chat-messages-wrap")).position];
    }""")
    assert style == ["absolute", "8px", "20px", "relative"]
    assert btn.is_visible()


def test_copy_all_is_disabled_on_an_empty_chat(open_chat, stored_conversation):
    page = open_chat()
    assert page.locator("#copy-chat-btn").is_disabled()

    _open_stored(page, stored_conversation)
    assert page.locator("#copy-chat-btn").is_enabled()

    page.click("#new-chat-btn")
    assert page.locator("#copy-chat-btn").is_disabled()

    _open_stored(page, stored_conversation)
    assert page.locator("#copy-chat-btn").is_enabled()
    page.click(f".conv-item[data-id='{stored_conversation}'] .conv-remove-btn")
    page.wait_for_selector("#quick-chat-notice")
    assert page.locator("#copy-chat-btn").is_disabled()


def test_copy_all_writes_the_conversation_as_markdown(open_chat, stored_conversation):
    page = open_chat()
    _open_stored(page, stored_conversation)

    assert _copy_all(page) == (
        "**User** (2026-01-02T03:04:05Z):\n\nShow me code"
        "\n\n---\n\n"
        f"**Assistant** (test-model, 2026-01-02T03:04:06Z):\n\n{REPLY}"
    )
    assert page.inner_text("#copy-status") == "Copied to clipboard"


def test_copy_all_leaves_out_attached_document_text(open_chat, document_conversation):
    page = open_chat()
    _open_stored(page, document_conversation)

    text = _copy_all(page)
    assert text.startswith("**User** (2026-01-02T03:04:05Z):\n\n[Document: notes.md]\n\nSummarise it\n\n---\n\n")
    assert "SECRET FILE TEXT" not in text


def test_copy_all_includes_a_live_exchange(open_chat, stored_conversation):
    body = _sse({"chunk": "Live "}, {"chunk": "answer"},
                {"done": True, "reply": "Live answer", "thinking": "hmm", "input_tokens": 1, "output_tokens": 2})
    page = open_chat(stream=(200, "text/event-stream", body))
    _open_stored(page, stored_conversation)
    _send(page, "and now?")

    entries = _copy_all(page).split("\n\n---\n\n")
    assert len(entries) == 4
    assert re.fullmatch(rf"\*\*User\*\* \({ISO_UTC}\):\n\nand now\?", entries[2])
    assert re.fullmatch(rf"\*\*Assistant\*\* \(test-model, {ISO_UTC}\):\n\nLive answer", entries[3])
    assert "hmm" not in entries[3]  # thinking text is left out

    messages = open_chat.stream_bodies[-1]["messages"]
    assert [sorted(m) for m in messages] == [["content", "role"]] * 3


def test_copy_all_includes_a_reply_without_done(open_chat):
    page = open_chat(stream=(200, "text/event-stream", _sse({"chunk": "partial"})))
    assert page.locator("#copy-chat-btn").is_disabled()
    _send(page)

    entries = _copy_all(page).split("\n\n---\n\n")
    assert re.fullmatch(rf"\*\*Assistant\*\* \(test-model, {ISO_UTC}\):\n\npartial", entries[1])


def test_copy_all_leaves_out_error_text(open_chat):
    page = open_chat(stream=(500, "application/json", json.dumps({"error": "down"})))
    _send(page, "hi")

    text = _copy_all(page)
    assert re.fullmatch(rf"\*\*User\*\* \({ISO_UTC}\):\n\nhi", text)


def test_copy_all_exports_image_messages_as_text_plus_marker(open_chat):
    page = open_chat()
    page.evaluate("""() => {
      const opt = document.createElement("option");
      opt.value = opt.textContent = "test-model";
      document.getElementById("model-picker").appendChild(opt);
      document.getElementById("model-picker").value = "test-model";
    }""")
    page.route("**/chat/stream", lambda route: route.fulfill(
        status=200, content_type="text/event-stream", body=_sse({"done": True, "reply": "A cat."})))
    page.route("**/chat/upload", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"type": "image", "filename": "cat.png", "data_url": "data:image/png;base64,iVBORw0KGgo="})))
    page.set_input_files("#file-input", files=[{"name": "cat.png", "mimeType": "image/png", "buffer": b"png"}])
    page.wait_for_selector("#attachment-row:not(.d-none)")
    page.fill("#chat-input", "What is this?")
    page.click("#send-btn")
    page.wait_for_function("!document.getElementById('send-btn').disabled")

    entries = _copy_all(page).split("\n\n---\n\n")
    assert re.fullmatch(rf"\*\*User\*\* \({ISO_UTC}\):\n\n\[Image\]\n\nWhat is this\?", entries[0])
    assert "base64" not in entries[0]
