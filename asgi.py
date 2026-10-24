import os

from dotenv import load_dotenv

load_dotenv()

from lumen import create_app
from lumen.services.db_pool import resolve_wsgi_workers
from lumen.services.wsgi_disconnect import DisconnectAwareWSGIMiddleware

flask_app = create_app()

wsgi_workers = resolve_wsgi_workers(flask_app.config.get("SQLALCHEMY_ENGINE_OPTIONS", {}))
flask_app.logger.info(
    "WSGI thread pool: workers=%d (LUMEN_WSGI_WORKERS=%s)",
    wsgi_workers, os.environ.get("LUMEN_WSGI_WORKERS") or "unset",
)

app = DisconnectAwareWSGIMiddleware(flask_app, workers=wsgi_workers)
