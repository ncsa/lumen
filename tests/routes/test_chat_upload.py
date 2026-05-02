"""Tests for /chat/upload — extension whitelist, size limits, MIME validation."""
import base64
import io


# 1x1 transparent PNG (67 bytes).
_PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


def _post_upload(client, filename, data, content_type=None):
    return client.post(
        "/chat/upload",
        data={"file": (io.BytesIO(data), filename)},
        content_type="multipart/form-data",
    )


def test_upload_requires_login(client):
    resp = client.post("/chat/upload")
    assert resp.status_code == 302  # redirected to landing


def test_upload_no_file_400(auth_client):
    resp = auth_client.post("/chat/upload", data={}, content_type="multipart/form-data")
    assert resp.status_code == 400
    assert "No file" in resp.get_json()["error"]


def test_upload_empty_filename_400(auth_client):
    resp = _post_upload(auth_client, "", b"hello")
    assert resp.status_code == 400


def test_upload_disallowed_extension_400(auth_client):
    resp = _post_upload(auth_client, "evil.exe", b"MZ\x90\x00")
    assert resp.status_code == 400
    assert "Unsupported" in resp.get_json()["error"]


def test_upload_no_extension_400(auth_client):
    resp = _post_upload(auth_client, "noext", b"hello")
    assert resp.status_code == 400


def test_upload_oversized_400(app, auth_client):
    # Default max is 10MB; fabricate 11MB of zeros under the .txt extension.
    big = b"\x00" * (11 * 1024 * 1024)
    resp = _post_upload(auth_client, "big.txt", big)
    assert resp.status_code == 400
    assert "exceeds" in resp.get_json()["error"]


def test_upload_text_file_returns_doc(auth_client):
    resp = _post_upload(auth_client, "notes.txt", b"hello world")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["type"] == "doc"
    assert body["filename"] == "notes.txt"
    assert body["text"] == "hello world"


def test_upload_text_file_truncated_at_max_chars(app, auth_client):
    # Override max_text_chars via YAML_DATA so we don't have to allocate 100k bytes.
    app.config["YAML_DATA"] = {
        **app.config.get("YAML_DATA", {}),
        "chat": {"upload": {"max_text_chars": 10}},
    }
    try:
        resp = _post_upload(auth_client, "long.txt", b"abcdefghijklmnopqrstuvwxyz")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["text"].startswith("abcdefghij")
        assert "Document truncated" in body["text"]
    finally:
        app.config["YAML_DATA"].get("chat", {}).pop("upload", None)


def test_upload_png_image_returns_data_url(auth_client):
    resp = _post_upload(auth_client, "pixel.png", _PNG_1x1)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["type"] == "image"
    assert body["filename"] == "pixel.png"
    assert body["data_url"].startswith("data:image/png;base64,")


def test_upload_image_with_wrong_extension(auth_client):
    """A PNG body uploaded as .jpg is still detected as image/png and returned as image."""
    resp = _post_upload(auth_client, "fake.jpg", _PNG_1x1)
    # filetype.guess returns image/png; the route only checks startswith("image/"),
    # so this is accepted as an image. Documents the current behavior.
    assert resp.status_code == 200
    assert resp.get_json()["type"] == "image"


def test_upload_binary_with_text_extension_400(auth_client):
    """Binary content (PDF magic bytes) uploaded as .txt fails the MIME-vs-extension check."""
    pdf_magic = b"%PDF-1.4\n" + b"\x00" * 200
    resp = _post_upload(auth_client, "fake.txt", pdf_magic)
    assert resp.status_code == 400
    assert "does not match" in resp.get_json()["error"]


def test_upload_corrupt_pdf_returns_400(auth_client):
    """Bytes that magic-detect as PDF but pypdf can't parse → 400."""
    # %PDF magic header but no valid structure after.
    bogus = b"%PDF-1.4\n" + b"\x00" * 200
    resp = _post_upload(auth_client, "bad.pdf", bogus)
    assert resp.status_code == 400
    assert "PDF" in resp.get_json()["error"]


def test_upload_custom_allowed_extensions(app, auth_client):
    """upload.allowed_extensions in YAML_DATA narrows the whitelist."""
    app.config["YAML_DATA"] = {
        **app.config.get("YAML_DATA", {}),
        "chat": {"upload": {"allowed_extensions": ["txt"]}},
    }
    try:
        # png is no longer allowed
        resp = _post_upload(auth_client, "pixel.png", _PNG_1x1)
        assert resp.status_code == 400
        # txt still allowed
        resp = _post_upload(auth_client, "ok.txt", b"hi")
        assert resp.status_code == 200
    finally:
        app.config["YAML_DATA"].get("chat", {}).pop("upload", None)
