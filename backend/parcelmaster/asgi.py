"""Entry point for ASGI servers: ``uvicorn parcelmaster.asgi:app``."""

from .app import create_app
from .config import Config

app = create_app(Config.from_env())
