# projectwise/__init__.py
import os
from quart import Quart
from .config import get_config
from .utils.logger import get_logger
from .extensions import init_extensions
from .routes.main import main_bp
from .routes.chat import chat_bp
from .routes.mcp_control import mcp_control_bp
from .routes.chat_ws_room import chat_ws_room_bp


async def create_app(config_object=None):
    app = Quart(__name__, instance_relative_config=True)

    env = os.environ.get("APP_ENV", "default")
    if config_object:
        app.config.from_object(config_object)
    else:
        app.config.from_object(get_config(env))

    # Inisialisasi logger global
    logger = get_logger("quart_app")
    logger.info(f"Starting Quart app in {app.config['ENV']} mode")

    # Inisialisasi semua extension async (MCP, DB, dll)
    await init_extensions(app)
    logger.info("Extensions initialized successfully")

    # Register blueprints
    app.register_blueprint(main_bp)
    app.register_blueprint(chat_bp, url_prefix="/chat")
    app.register_blueprint(mcp_control_bp, url_prefix="/mcp")
    app.register_blueprint(chat_ws_room_bp)
    logger.info("Blueprints registered")

    return app
