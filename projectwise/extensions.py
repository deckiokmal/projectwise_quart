# projectwise/extensions.py
from __future__ import annotations

from quart import Quart

from .config import ServiceConfigs
from .utils.logger import get_logger
from .services.mcp.client import MCPClient
from .services.memory.short_term_memory import ShortTermMemory
from .services.memory.long_term_memory import Mem0Manager


# Module-level references to the singletons
_mcp_client: MCPClient | None = None
_service_configs: ServiceConfigs | None = None


async def init_extensions(app: Quart) -> None:
    """Initialise all asynchronous extensions and attach them to the app.

    This should be called once when the application starts.  The
    resulting objects are stored on ``app.extensions`` for later use.
    """
    global mcp_client, service_configs

    logger = get_logger(__name__)

    # Load service configuration from environment (via pydantic)
    _service_configs = ServiceConfigs()
    app.extensions["service_configs"] = _service_configs
    logger.info(f"ServiceConfigs loaded: MCP URL = {_service_configs.mcp_server_url}")

    # Create and connect the MCP client.  Use the model defined in the
    # ServiceConfigs.  ``__aenter__`` returns a connected client.
    _mcp_client = await MCPClient(model=_service_configs.llm_model).__aenter__()
    app.extensions["mcp"] = _mcp_client
    logger.info(f"MCPClient connected to {_service_configs.mcp_server_url}")

    # Initialise short‑term memory using the database URI from the app config
    db_url = app.config["SQLALCHEMY_DATABASE_URI"]
    short_term_memory = ShortTermMemory(db_url=db_url, echo=False, max_history=20)
    await short_term_memory.init_models()
    app.extensions["short_term_memory"] = short_term_memory
    logger.info(f"ShortTermMemory initialised with DB: {db_url}")

    # Initialise long‑term memory (vector store)
    long_term_memory = Mem0Manager(_service_configs)
    await long_term_memory.init()
    app.extensions["long_term_memory"] = long_term_memory
    logger.info("LongTermMemory (Mem0Manager) initialised")


async def shutdown_extensions() -> None:
    """Clean up all asynchronous extensions on application shutdown."""
    global _mcp_client
    logger = get_logger(__name__)
    if _mcp_client:
        await _mcp_client.__aexit__(None, None, None)
        logger.info("MCPClient disconnected")
        _mcp_client = None
