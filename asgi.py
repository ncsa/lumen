from dotenv import load_dotenv
load_dotenv()

from a2wsgi import WSGIMiddleware
from lumen import create_app

app = WSGIMiddleware(create_app())
