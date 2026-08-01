from dotenv import load_dotenv
load_dotenv()

from a2wsgi import WSGIMiddleware
from lumen import create_app
from lumen.services.db_pool import resolve_wsgi_workers

flask_app = create_app()

app = WSGIMiddleware(
    flask_app,
    workers=resolve_wsgi_workers(flask_app.config.get("SQLALCHEMY_ENGINE_OPTIONS", {})),
)
