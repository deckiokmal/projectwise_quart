# projectwise/__init__.py
from __future__ import annotations

import os
from quart import Quart
from quart_schema import QuartSchema

from .config import get_config
from .utils.logger import get_logger
from .extensions import init_extensions, shutdown_extensions
from .routes.main import main_bp
from .routes.chat import chat_bp
# from .routes.api import api_bp
from .routes.mcp_control import mcp_control_bp
from .routes.ingestion import ingestion_bp
from .routes.ws_chat import ws_chat_bp


async def create_app(config_object: object | None = None) -> Quart:
    """Application factory for the ProjectWise Quart app.

    Args:
        config_object: Optional explicit configuration class.  If not
            provided, the value of the ``APP_ENV`` environment variable
            is used to determine which configuration class to load via
            :func:`get_config`.  See :mod:`projectwise.config` for details.

    Returns:
        A fully configured :class:`quart.Quart` application instance.
    """

    app = Quart(__name__, instance_relative_config=True)
    
    QuartSchema(app)

    env = os.environ.get("APP_ENV", "default")
    if config_object:
        app.config.from_object(config_object)
    else:
        app.config.from_object(get_config(env))

    # Inisialisasi logger global
    logger = get_logger("quart.app")
    logger.info(f"Starting ProjectWise Client app in {app.config['ENV']} mode")

    # Inisialisasi semua extension async (MCP, DB, dll)
    await init_extensions(app)
    logger.info("Extensions initialized successfully")

    @app.after_serving
    async def _cleanup():
        await shutdown_extensions(app)  # <— pastikan MCP & resource lain rapi
        logger.info("Extensions shutdown successfully")

    # Register blueprints
    app.register_blueprint(main_bp)
    app.register_blueprint(chat_bp, url_prefix="/chat")
    app.register_blueprint(ws_chat_bp)
    # app.register_blueprint(api_bp, url_prefix="/api")
    app.register_blueprint(mcp_control_bp, url_prefix="/mcp")
    app.register_blueprint(ingestion_bp)
    logger.info("Blueprints registered")

    return app
