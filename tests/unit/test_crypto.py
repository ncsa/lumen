from sqlalchemy import text

from lumen.services.crypto import _ENC_PREFIX, decrypt_secret, encrypt_secret, hash_api_key


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


def test_encrypt_decrypt_roundtrip(app):
    with app.app_context():
        ct = encrypt_secret("sk-upstream-secret")
        assert ct.startswith(_ENC_PREFIX)
        assert "sk-upstream-secret" not in ct
        assert decrypt_secret(ct) == "sk-upstream-secret"


def test_decrypt_passes_through_legacy_plaintext(app):
    with app.app_context():
        # Rows written before encryption at rest have no prefix and must survive.
        assert decrypt_secret("sk-legacy-plaintext") == "sk-legacy-plaintext"


def test_endpoint_api_key_encrypted_at_rest(app, test_model):
    """The ORM reads/writes plaintext but the DB row only holds ciphertext."""
    with app.app_context():
        from lumen.extensions import db
        from lumen.models.model_endpoint import ModelEndpoint

        ep = ModelEndpoint(model_config_id=test_model["id"], url="http://up.example/v1",
                           api_key="sk-at-rest-test", healthy=True)
        db.session.add(ep)
        db.session.commit()
        ep_id = ep.id
        db.session.expire_all()

        raw = db.session.execute(
            text("SELECT api_key FROM model_endpoints WHERE id = :id"), {"id": ep_id}
        ).scalar_one()
        assert raw.startswith(_ENC_PREFIX)
        assert "sk-at-rest-test" not in raw
        assert db.session.get(ModelEndpoint, ep_id).api_key == "sk-at-rest-test"
