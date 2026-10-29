"""Run the per-reply copy button on /chat in a real browser.

The page is served by the Flask test client at http://lumen.test through
page.route, like test_key_tables.py. That origin is not a secure context, so
navigator.clipboard does not exist there; an init script stubs it and records
what was written in window.__copied. /chat/stream is answered with canned SSE
bodies, so no model is needed. The page renders replies with marked and
DOMPurify, which are served from pinned copies in tests/fixtures/vendor.
"""
import json
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
    """Return open(stream=None) -> page; stream is (status, content_type, body) for /chat/stream."""
    pages = []

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
            Message(conversation_id=conv.id, role="user", content="Show me code"),
            Message(conversation_id=conv.id, role="assistant", content=REPLY, input_tokens=3, output_tokens=9),
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

    buttons = page.locator(".copy-btn")
    assert buttons.count() == 1  # only the assistant reply, not the user message
    assert buttons.get_attribute("aria-label") == "Copy reply"
    assert buttons.get_attribute("type") == "button"
    assert page.locator(".chat-bubble-assistant pre code").count() == 1  # rendered as HTML
    _run_all_checks(page.content().encode(), "/chat (stored conversation with copy button)")

    buttons.click()
    page.wait_for_function("window.__copied.length === 1")
    assert page.evaluate("window.__copied[0]") == REPLY
    assert page.inner_text("#copy-status") == "Copied to clipboard"
    assert page.locator(".copy-btn i.bi-check2").count() == 1
    page.wait_for_selector(".copy-btn i.bi-copy", state="attached", timeout=3_000)


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
    page.click(".copy-btn")
    page.wait_for_function("document.getElementById('copy-status').textContent === 'Copy failed'")
    assert page.locator(".copy-btn i.bi-exclamation-triangle").count() == 1
    assert page.evaluate("window.__copied") == []


def test_live_reply_with_done_gets_a_button(open_chat):
    body = _sse({"chunk": "**Bold** "}, {"chunk": "reply"},
                {"done": True, "reply": "**Bold** reply", "input_tokens": 1, "output_tokens": 2})
    page = open_chat(stream=(200, "text/event-stream", body))
    _send(page)

    page.click(".copy-btn")
    page.wait_for_function("window.__copied.length === 1")
    assert page.evaluate("window.__copied[0]") == "**Bold** reply"


def test_live_reply_without_done_gets_a_button(open_chat):
    body = _sse({"chunk": "partial "}, {"chunk": "`text`"})
    page = open_chat(stream=(200, "text/event-stream", body))
    _send(page)

    assert page.locator(".copy-btn").count() == 1
    page.click(".copy-btn")
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
    assert page.locator(".copy-btn").count() == 0


def test_chat_template_loads_the_pinned_renderers():
    chat = CHAT_HTML.read_text()
    for url in VENDOR_JS:
        if url != BOOTSTRAP_JS_URL:
            assert f'src="{url}"' in chat, "update VENDOR_JS and the copies in tests/fixtures/vendor"


def test_no_plain_http_clipboard_fallback():
    assert "execCommand" not in CHAT_HTML.read_text()
