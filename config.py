import os


class Config:
    # SECRET_KEY is set at runtime from LUMEN_SECRET_KEY env var or config.yaml.
    # Do not set SECRET_KEY here — it will be overwritten and the env var silently ignored.
    SECRET_KEY = ""

    _db_url = os.environ.get("DATABASE_URL", "sqlite:///lumen_dev.db")
    SQLALCHEMY_DATABASE_URI = _db_url.replace("postgres://", "postgresql://", 1)
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    CONFIG_YAML = os.environ.get("CONFIG_YAML", "./config.yaml")

    OAUTH2_CLIENT_ID = os.environ.get("OAUTH2_CLIENT_ID", "")
    OAUTH2_CLIENT_SECRET = os.environ.get("OAUTH2_CLIENT_SECRET", "")
    OAUTH2_SERVER_METADATA_URL = os.environ.get("OAUTH2_SERVER_METADATA_URL", "")
    OAUTH2_REDIRECT_URI = os.environ.get("OAUTH2_REDIRECT_URI", "http://localhost:5001/callback")
    OAUTH2_SCOPES = os.environ.get("OAUTH2_SCOPES", "openid email profile")
    OAUTH2_PARAMS = {}
