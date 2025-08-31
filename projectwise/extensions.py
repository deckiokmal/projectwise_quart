# projectwise/extensions.py
from __future__ import annotations

import asyncio
from quart import Quart

from .config import ServiceConfigs
from .utils.logger import get_logger
from .services.mcp.client import MCPClient
from .services.memory.short_term_memory import ShortTermMemory
from .services.memory.long_term_memory import Mem0Manager
from projectwise.models.models import ModelDB


# Module-level references to the singletons
_mcp_client: MCPClient | None = None
_service_configs: ServiceConfigs | None = None


async def init_extensions(app: Quart) -> None:
    """Initialise all asynchronous extensions and attach them to the app.

    This should be called once when the application starts.  The
    resulting objects are stored on ``app.extensions`` for later use.
    """

    logger = get_logger(__name__)

    # Load service configuration from environment (via pydantic)
    service_configs = ServiceConfigs()
    
    # Check LLM API KEY
    if not service_configs.llm_api_key:
        raise RuntimeError("LLM_API_KEY kosong. Set di .env sebelum menjalankan aplikasi.")
    
    app.extensions["service_configs"] = service_configs
    logger.info(f"ServiceConfigs loaded: MCP URL = {service_configs.mcp_server_url}")

    # Siapkan state MCP (tidak connect di startup)
    app.extensions["mcp"] = None
    app.extensions["mcp_lock"] = asyncio.Lock()
    app.extensions["mcp_status"] = {
        "connected": False,
        "connecting": False,
        "error": None
    }
    logger.info("MCP state initialised")

    # Initialise Models Database
    db_url = app.config["SQLALCHEMY_DATABASE_URI"]
    models_db = ModelDB(db_url)
    await models_db.init_models()
    app.extensions["db"] = models_db
    
    # Initialise short‑term memory using the database URI from the app config
    short_term_memory = ShortTermMemory(db_url=db_url, echo=False, max_history=20)
    await short_term_memory.init_models()
    app.extensions["short_term_memory"] = short_term_memory

    # Initialise long‑term memory (vector store)
    long_term_memory = Mem0Manager(service_configs)
    try:
        await long_term_memory.init()
    except Exception as e:
        logger.warning("LongTermMemory init error (will run degraded): %s", e)
        
    app.extensions["long_term_memory"] = long_term_memory
    logger.info(
        "LongTermMemory (Mem0Manager) initialised (ready=%s, degraded=%s)",
        long_term_memory.ready, long_term_memory.degraded
    )


async def shutdown_extensions(app: Quart) -> None:
    """Clean up all asynchronous extensions on application shutdown."""
    client = app.extensions["mcp"]
    logger = get_logger(__name__)
    if client:
        await client.__aexit__(None, None, None)
        logger.info("MCPClient disconnected")
        client = None
