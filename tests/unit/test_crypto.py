from lumen.services.crypto import hash_api_key


def test_hash_consistent(app):
    with app.app_context():
        h1 = hash_api_key("my-key")
        h2 = hash_api_key("my-key")
        assert h1 == h2


def test_hash_different_keys(app):
    with app.app_context():
        h1 = hash_api_key("key-one")
        h2 = hash_api_key("key-two")
        assert h1 != h2


def test_hash_is_hex_string(app):
    with app.app_context():
        result = hash_api_key("test")
        assert isinstance(result, str)
        int(result, 16)  # valid hex


def test_hash_length(app):
    with app.app_context():
        result = hash_api_key("test")
        assert len(result) == 64  # sha256 = 32 bytes = 64 hex chars
